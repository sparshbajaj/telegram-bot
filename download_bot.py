import os
import re
import base64
import asyncio
import logging
import aiohttp
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.error import TelegramError
from dotenv import load_dotenv
import tempfile
from datetime import datetime

# Load environment variables
load_dotenv()

# Configure logging with rotation
from logging.handlers import RotatingFileHandler
# Set the root logger level to DEBUG to capture all messages.
# The handlers will filter them based on their own level.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG, 
    handlers=[
        # This handler will write INFO and above to the file.
        RotatingFileHandler("bot.log", maxBytes=10*1024*1024, backupCount=5),
        # This handler will print INFO and above to the console.
        logging.StreamHandler()
    ]
)
# To see DEBUG messages, you might need to adjust the handler levels, e.g., logging.StreamHandler().setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

# Configuration with validation
BOT_TOKEN = os.getenv('BOT_TOKEN')
ARIA2_RPC_URL = os.getenv('ARIA2_RPC_URL', 'http://localhost:6800/jsonrpc')
ARIA2_RPC_SECRET = os.getenv('ARIA2_RPC_SECRET')
MAX_CONCURRENT_DOWNLOADS = int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '5'))
UPDATE_INTERVAL = int(os.getenv('UPDATE_INTERVAL', '10'))

# A common browser user agent to avoid being blocked
BROWSER_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'

# Global state with better structure
class DownloadTracker:
    def __init__(self):
        self.active_downloads: Dict[str, Dict] = {}
        self.failed_downloads: Dict[str, Dict] = {}
        self.user_downloads: Dict[int, List[str]] = {}
    
    def add_download(self, gid: str, user_id: int, name: str, chat_id: int):
        self.active_downloads[gid] = {
            'user_id': user_id,
            'name': name,
            'chat_id': chat_id,
            'start_time': datetime.now()
        }
        if user_id not in self.user_downloads:
            self.user_downloads[user_id] = []
        self.user_downloads[user_id].append(gid)
    
    def remove_download(self, gid: str):
        if gid in self.active_downloads:
            user_id = self.active_downloads[gid]['user_id']
            if user_id in self.user_downloads and gid in self.user_downloads[user_id]:
                self.user_downloads[user_id].remove(gid)
            del self.active_downloads[gid]
    
    def get_user_downloads(self, user_id: int) -> List[str]:
        return self.user_downloads.get(user_id, [])

tracker = DownloadTracker()

async def aria2_request(method: str, params: List = None) -> Optional[dict]:
    """Make async Aria2 RPC request with better error handling"""
    params = params or []
    request_data = {
        "jsonrpc": "2.0",
        "id": f"telegram-bot-{datetime.now().timestamp()}",
        "method": method,
        "params": [f"token:{ARIA2_RPC_SECRET}"] + params if ARIA2_RPC_SECRET else params
    }
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(ARIA2_RPC_URL, json=request_data) as response:
                response.raise_for_status()
                result = await response.json()
                
                if 'error' in result:
                    logger.error(f"Aria2 RPC error for method {method}: {result['error']}")
                    raise Exception(f"Aria2 error: {result['error']['message']}")
                
                return result.get('result')
    except Exception as e:
        logger.error(f"Aria2 request failed for method {method}: {str(e)}")
        raise

def format_size(bytes_size: int) -> str:
    """Format bytes to human readable size"""
    bytes_size = int(bytes_size)
    if bytes_size == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"

def format_speed(bytes_per_sec: int) -> str:
    """Format download speed"""
    return f"{format_size(int(bytes_per_sec))}/s"

def estimate_time(completed: int, total: int, speed: int) -> str:
    """Estimate remaining time"""
    if speed == 0 or total <= completed:
        return "N/A"
    
    remaining_bytes = total - completed
    remaining_seconds = remaining_bytes / speed
    
    if remaining_seconds < 60:
        return f"{int(remaining_seconds)}s"
    elif remaining_seconds < 3600:
        return f"{int(remaining_seconds // 60)}m {int(remaining_seconds % 60)}s"
    else:
        hours = int(remaining_seconds // 3600)
        minutes = int((remaining_seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with inline keyboard"""
    keyboard = [
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("⏸ Pause All", callback_data="pause"),
         InlineKeyboardButton("▶️ Resume All", callback_data="resume")],
        [InlineKeyboardButton("🔄 Retry Failed", callback_data="retry"),
         InlineKeyboardButton("⏹ Cancel All", callback_data="cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    start_text = (
        "🤖 <b>Aria2 Download Bot</b>\n\n"
        "📥 Send me:\n"
        "• HTTP/HTTPS/FTP URLs\n"
        "• Magnet links\n"
        "• .torrent files\n\n"
        "<b>Commands:</b>\n"
        "<code>/start</code> - Show this menu\n"
        "<code>/status</code> - Show download status\n"
        "<code>/my_downloads</code> - Show your downloads\n"
        "<code>/stats</code> - Show bot statistics\n"
        "<code>/help</code> - Show detailed help"
    )
    
    await update.message.reply_text(
        start_text,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all incoming messages"""
    if update.message.document:
        await handle_file(update, context)
    elif update.message.text and not update.message.text.startswith('/'):
        await handle_text(update, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process text messages with better URL validation"""
    text = update.message.text.strip()
    user_id = update.message.from_user.id
    
    # Check concurrent download limit
    if len(tracker.get_user_downloads(user_id)) >= MAX_CONCURRENT_DOWNLOADS:
        await update.message.reply_text(
            f"❌ Maximum concurrent downloads ({MAX_CONCURRENT_DOWNLOADS}) reached. "
            "Please wait for some downloads to complete."
        )
        return
    
    if re.match(r'^magnet:\?xt=urn:btih:[a-fA-F0-9]{40}', text, re.I):
        await start_download(update, context, 'magnet', text)
    elif re.match(r'^(https?|ftp)://[^\s/$.?#].[^\s]*$', text, re.I):
        await start_download(update, context, 'url', text)
    else:
        await update.message.reply_text(
            "❌ Invalid input. Please send:\n"
            "• Valid HTTP/HTTPS/FTP URL\n"
            "• Valid magnet link\n"
            "• .torrent file"
        )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle torrent files with improved error handling"""
    user_id = update.message.from_user.id
    document = update.message.document
    
    if not document.file_name.lower().endswith('.torrent'):
        await update.message.reply_text("❌ Only .torrent files are supported")
        return
    
    # Check concurrent download limit
    if len(tracker.get_user_downloads(user_id)) >= MAX_CONCURRENT_DOWNLOADS:
        await update.message.reply_text(
            f"❌ Maximum concurrent downloads ({MAX_CONCURRENT_DOWNLOADS}) reached."
        )
        return
    
    try:
        file = await context.bot.get_file(document.file_id)
        
        with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as tmp_file:
            await file.download_to_drive(tmp_file.name)
            
            with open(tmp_file.name, 'rb') as f:
                torrent_content = base64.b64encode(f.read()).decode('utf-8')
            
            os.unlink(tmp_file.name)
        
        gid = await aria2_request("aria2.addTorrent", [torrent_content, [], {}])
        
        if gid:
            logger.info(f"Started torrent download for '{document.file_name}' with GID: {gid}")
            tracker.add_download(gid, user_id, document.file_name, update.message.chat_id)
            asyncio.create_task(track_download(context, gid, document.file_name, update.message.chat_id))
            await update.message.reply_text(f"⏬ Torrent download started: {document.file_name}")
        else:
            await update.message.reply_text("❌ Failed to start torrent download")
            
    except Exception as e:
        logger.error(f"Torrent handling error: {str(e)}", exc_info=True)
        await update.message.reply_text(f"❌ Torrent error: {str(e)}")

async def start_download(update: Update, context: ContextTypes.DEFAULT_TYPE, dtype: str, content: str):
    """Start new download with smarter filename detection and better options."""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    try:
        if dtype == 'magnet':
            gid = await aria2_request("aria2.addUri", [[content]])
            name = "Magnet Link" # Name will be fetched later by tracker
        else:  # URL
            # --- NEW: Filename detection and improved options ---
            name = Path(urlparse(content).path).name or "download" # Fallback name
            try:
                # Make a HEAD request to get headers without downloading the body
                async with aiohttp.ClientSession() as session:
                    async with session.head(content, allow_redirects=True, timeout=15) as response:
                        if 'Content-Disposition' in response.headers:
                            cd = response.headers['Content-Disposition']
                            match = re.search(r'filename="?([^"]+)"?', cd)
                            if match:
                                name = match.group(1)
                                logger.info(f"Found filename in Content-Disposition: {name}")
            except Exception as e:
                logger.warning(f"Could not fetch headers to determine filename for {content}: {e}")

            # Set options for Aria2, including the user agent and filename
            dl_options = {
                'out': name,
                'user-agent': BROWSER_USER_AGENT
            }
            gid = await aria2_request("aria2.addUri", [[content], dl_options])
            # --- END NEW ---

        if gid:
            logger.info(f"Started download for '{name}' ({dtype}) with GID: {gid}")
            tracker.add_download(gid, user_id, name, chat_id)
            asyncio.create_task(track_download(context, gid, name, chat_id))
            await update.message.reply_text(f"⏬ Download started: {name}")
        else:
            await update.message.reply_text("❌ Failed to start download")
            
    except Exception as e:
        logger.error(f"Download start error for content '{content}': {str(e)}", exc_info=True)
        await update.message.reply_text(f"❌ Download error: {str(e)}")

async def track_download(context: ContextTypes.DEFAULT_TYPE, gid: str, initial_name: str, chat_id: int):
    """Monitor download progress with enhanced tracking."""
    last_progress = -1
    last_message_text = ""
    name = initial_name
    
    try:
        msg = await context.bot.send_message(
            chat_id, 
            f"📦 <b>{name}</b>\nStatus: Preparing...",
            parse_mode='HTML'
        )
        
        while gid in tracker.active_downloads:
            await asyncio.sleep(UPDATE_INTERVAL)
            
            try:
                status = await aria2_request("aria2.tellStatus", [gid])
                if not status:
                    logger.warning(f"Could not get status for GID {gid}. Assuming it's removed.")
                    break
                
                logger.debug(f"Status for GID {gid}: {status}")

                # Update name if it's a torrent and we now have the real name
                if 'bittorrent' in status and 'info' in status['bittorrent']:
                    name = status['bittorrent']['info'].get('name', name)
                    tracker.active_downloads[gid]['name'] = name

                completed = int(status.get('completedLength', 0))
                total = int(status.get('totalLength', 1))
                speed = int(status.get('downloadSpeed', 0))
                progress = (completed / total) * 100 if total > 0 else 0
                
                progress_bar = "█" * int(progress // 10) + "░" * (10 - int(progress // 10))
                
                message_text = (
                    f"📦 <b>{name}</b>\n"
                    f"Progress: {progress:.1f}%\n"
                    f"[{progress_bar}]\n"
                    f"Status: {status.get('status', 'N/A')}\n"
                    f"Size: {format_size(completed)} / {format_size(total)}\n"
                    f"Speed: {format_speed(speed)}\n"
                    f"ETA: {estimate_time(completed, total, speed)}"
                )
                
                # Only edit the message if the content has actually changed
                if message_text != last_message_text:
                    try:
                        await msg.edit_text(message_text, parse_mode='HTML')
                        last_message_text = message_text
                    except TelegramError as e:
                        if "message is not modified" not in str(e).lower():
                            logger.warning(f"Message edit error for GID {gid}: {e}")
                
                if status.get('status') in ['complete', 'error', 'removed']:
                    final_text = ""
                    if status['status'] == 'complete':
                        logger.info(f"Download completed for GID {gid}: {name}")
                        final_text = (
                            f"✅ <b>Completed: {name}</b>\n"
                            f"Size: {format_size(total)}\n"
                        )
                    else:
                        error_msg = status.get('errorMessage', 'Unknown error')
                        logger.error(f"Download failed for GID {gid} ({name}): {error_msg}")
                        tracker.failed_downloads[gid] = tracker.active_downloads.get(gid, {}).copy()
                        if gid in tracker.failed_downloads:
                            tracker.failed_downloads[gid]['error'] = error_msg
                        
                        final_text = (
                            f"❌ <b>Failed: {name}</b>\n"
                            f"Error: {error_msg}"
                        )
                    
                    await msg.edit_text(final_text, parse_mode='HTML')
                    break # Exit the tracking loop
                    
            except Exception as e:
                logger.error(f"Inner tracking loop error for GID {gid}: {str(e)}", exc_info=True)
                await asyncio.sleep(UPDATE_INTERVAL)
                
    except Exception as e:
        logger.error(f"Outer tracking loop error for GID {gid}: {str(e)}", exc_info=True)
        try:
            await context.bot.send_message(chat_id, f"❌ Tracking failed for {name}")
        except: pass
    finally:
        # Always remove from active downloads when tracking stops for any reason
        tracker.remove_download(gid)


# ... (The rest of the functions from `button_callback` to `main` are mostly okay,
# but I'll include the corrected versions for completeness and consistency with HTML parsing)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "status":
        await show_status_callback(query, context)
    elif query.data == "pause":
        await pause_downloads_callback(query, context)
    elif query.data == "resume":
        await resume_downloads_callback(query, context)
    elif query.data == "retry":
        await retry_failed_callback(query, context)
    elif query.data == "cancel":
        await cancel_downloads_callback(query, context)

async def show_status_callback(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """Show status via callback by editing the message."""
    try:
        active = await aria2_request("aria2.tellActive")
        if not active:
            await query.edit_message_text("ℹ️ No active downloads")
            return
            
        status_lines = ["📊 <b>Current Downloads:</b>\n"]
        for idx, download in enumerate(active[:10], 1):
            name = "Unknown"
            if 'bittorrent' in download and 'info' in download['bittorrent']:
                name = download['bittorrent']['info'].get('name', 'N/A')
            elif download.get('files'):
                # Using the name from the file path set by 'out' option
                name = Path(download['files'][0]['path']).name
            
            completed = int(download.get('completedLength', 0))
            total = int(download.get('totalLength', 1))
            progress = (completed / total) * 100 if total > 0 else 0
            speed = int(download.get('downloadSpeed', 0))
            
            status_lines.append(
                f"{idx}. <b>{name[:30]}{'...' if len(name) > 30 else ''}</b>\n"
                f"   Progress: {progress:.1f}% | Speed: {format_speed(speed)}\n"
            )
            
        await query.edit_message_text("\n".join(status_lines), parse_mode='HTML')
    except Exception as e:
        logger.error("Error in show_status_callback: %s", e, exc_info=True)
        await query.edit_message_text(f"❌ Status error: {str(e)}")

async def pause_downloads_callback(query, context):
    try:
        await aria2_request("aria2.pauseAll")
        await query.edit_message_text("⏸ All downloads paused")
    except Exception as e:
        await query.edit_message_text(f"❌ Pause error: {str(e)}")

async def resume_downloads_callback(query, context):
    try:
        await aria2_request("aria2.unpauseAll")
        await query.edit_message_text("▶️ Downloads resumed")
    except Exception as e:
        await query.edit_message_text(f"❌ Resume error: {str(e)}")

async def retry_failed_callback(query, context):
    await query.edit_message_text("ℹ️ Retry functionality is complex and not yet implemented. Please resend the link/file.")

async def cancel_downloads_callback(query, context):
    try:
        active = await aria2_request("aria2.tellActive")
        for download in active:
            await aria2_request("aria2.remove", [download['gid']])
        
        await aria2_request("aria2.purgeDownloadResult")
        
        tracker.active_downloads.clear()
        tracker.failed_downloads.clear()
        tracker.user_downloads.clear()
        
        await query.edit_message_text("⏹ All active downloads canceled and cleared.")
    except Exception as e:
        logger.error("Error in cancel_downloads_callback: %s", e, exc_info=True)
        await query.edit_message_text(f"❌ Cancel error: {str(e)}")

async def my_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_gids = tracker.get_user_downloads(user_id)
    
    if not user_gids:
        await update.message.reply_text("ℹ️ You have no active downloads.")
        return
    
    status_lines = [f"📊 <b>Your Active Downloads ({len(user_gids)}):</b>\n"]
    for gid in user_gids:
        if gid in tracker.active_downloads:
            download_info = tracker.active_downloads[gid]
            status_lines.append(f"• <code>{download_info['name']}</code>")
    
    await update.message.reply_text("\n".join(status_lines), parse_mode='HTML')

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        global_stats = await aria2_request("aria2.getGlobalStat")
        
        stats_text = (
            f"📈 <b>Bot Statistics</b>\n\n"
            f"Active: {global_stats.get('numActive', 0)}\n"
            f"Waiting: {global_stats.get('numWaiting', 0)}\n"
            f"Stopped: {global_stats.get('numStopped', 0)}\n"
            f"DL Speed: {format_speed(global_stats.get('downloadSpeed', 0))}\n"
            f"UL Speed: {format_speed(global_stats.get('uploadSpeed', 0))}\n\n"
            f"Failed (session): {len(tracker.failed_downloads)}\n"
            f"Tracked (bot): {len(tracker.active_downloads)}"
        )
        await update.message.reply_text(stats_text, parse_mode='HTML')
    except Exception as e:
        logger.error("Error in show_stats: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Stats error: {str(e)}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 <b>Aria2 Download Bot Help</b>\n\n"
        "<b>Supported Formats:</b>\n"
        "• HTTP/HTTPS/FTP URLs\n"
        "• Magnet links\n"
        "• .torrent files\n\n"
        "<b>Commands:</b>\n"
        "<code>/start</code> - Show main menu\n"
        "<code>/status</code> - Show download status\n"
        "<code>/my_downloads</code> - Show your downloads\n"
        "<code>/stats</code> - Show bot statistics\n"
        "<code>/help</code> - Show this help\n\n"
        "<b>Features:</b>\n"
        "• Smart filename detection\n"
        "• Real-time progress updates\n"
        "• User-specific download tracking\n"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        active = await aria2_request("aria2.tellActive")
        if not active:
            await update.message.reply_text("ℹ️ No active downloads")
            return
        # ... (logic is same as show_status_callback, just with reply_text)
        status_lines = ["📊 <b>Current Downloads:</b>\n"]
        for idx, download in enumerate(active[:10], 1):
            name = Path(download['files'][0]['path']).name if download.get('files') else "Unknown"
            completed = int(download.get('completedLength', 0))
            total = int(download.get('totalLength', 1))
            progress = (completed / total) * 100 if total > 0 else 0
            speed = int(download.get('downloadSpeed', 0))
            
            status_lines.append(
                f"{idx}. <b>{name[:30]}{'...' if len(name) > 30 else ''}</b>\n"
                f"   Progress: {progress:.1f}% | Speed: {format_speed(speed)}\n"
            )
        await update.message.reply_text("\n".join(status_lines), parse_mode='HTML')
    except Exception as e:
        logger.error("Error in status_command: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Status error: {str(e)}")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not found in environment variables")
    
    logger.info("Starting Telegram Aria2 Bot...")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    handlers = [
        CommandHandler("start", start),
        CommandHandler("status", status_command),
        CommandHandler("my_downloads", my_downloads),
        CommandHandler("stats", show_stats),
        CommandHandler("help", help_command),
        CallbackQueryHandler(button_callback),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
        MessageHandler(filters.Document.ALL, handle_file)
    ]
    
    for handler in handlers:
        application.add_handler(handler)
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        logger.critical(f"Bot crashed with a critical error: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
