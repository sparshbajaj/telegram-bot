import os
import re
import base64
import asyncio
import logging
import requests
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ARIA2_RPC_URL = os.getenv('ARIA2_RPC_URL')
ARIA2_RPC_SECRET = os.getenv('ARIA2_RPC_SECRET')

# Global state
active_downloads = {}
failed_downloads = {}
progress_messages = {}

def aria2_request(method: str, params: list = None):
    """Make Aria2 RPC request"""
    params = params or []
    try:
        response = requests.post(
            ARIA2_RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": "telegram-bot",
                "method": method,
                "params": [f"token:{ARIA2_RPC_SECRET}"] + params
            },
            timeout=10
        )
        response.raise_for_status()
        return response.json().get('result', {})
    except Exception as e:
        logger.error(f"Aria2 error: {str(e)}")
        raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    await update.message.reply_text(
        "📥 Send me download links or torrent files\n\n"
        "Commands:\n"
        "/pause - Pause all downloads\n"
        "/resume - Resume paused downloads\n"
        "/retry - Retry failed downloads\n"
        "/status - Show current progress\n"
        "/cancel - Cancel all downloads"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all incoming messages"""
    if update.message.document:
        await handle_file(update, context)
    else:
        await handle_text(update, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process text messages"""
    text = update.message.text.strip()
    user_id = update.message.from_user.id
    
    if text.startswith('/'):
        return  # Let command handlers process
    
    if re.match(r'^magnet:\?', text, re.I):
        await start_download(update, context, 'magnet', text)
    elif re.match(r'^(https?|ftp)://', text, re.I):
        await start_download(update, context, 'url', text)
    else:
        await update.message.reply_text("❌ Invalid input")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle torrent files"""
    user_id = update.message.from_user.id
    document = update.message.document
    
    if not document.file_name.lower().endswith('.torrent'):
        await update.message.reply_text("❌ Only .torrent files supported")
        return
    
    try:
        file = await context.bot.get_file(document.file_id)
        torrent_path = await file.download_to_drive()
        
        with open(torrent_path, 'rb') as f:
            torrent_content = base64.b64encode(f.read()).decode('utf-8')
        
        gid = aria2_request("aria2.addTorrent", [torrent_content, [], {}])
        asyncio.create_task(track_download(update, context, gid, document.file_name))
        await update.message.reply_text("⏬ Torrent download started")
    except Exception as e:
        await update.message.reply_text(f"❌ Torrent error: {str(e)}")

async def start_download(update: Update, context: ContextTypes.DEFAULT_TYPE, dtype: str, content: str):
    """Start new download"""
    try:
        gid = aria2_request("aria2.addUri", [[content]])
        asyncio.create_task(track_download(update, context, gid, content))
        await update.message.reply_text("⏬ Download started")
    except Exception as e:
        await update.message.reply_text(f"❌ Download error: {str(e)}")

async def track_download(update: Update, context: ContextTypes.DEFAULT_TYPE, gid: str, name: str):
    """Monitor download progress with updates"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    
    try:
        # Send initial progress message
        msg = await context.bot.send_message(chat_id, f"📦 {name}\nProgress: 0%")
        
        while True:
            await asyncio.sleep(10)
            status = aria2_request("aria2.tellStatus", [gid])
            
            # Calculate progress
            completed = int(status.get('completedLength', 0))
            total = int(status.get('totalLength', 1))
            progress = (completed / total) * 100 if total > 0 else 0
            
            # Update message
            await msg.edit_text(f"📦 {name}\nProgress: {progress:.1f}%")
            
            if status.get('status') in ['complete', 'error', 'removed']:
                final_status = "✅ Completed" if status['status'] == 'complete' else "❌ Failed"
                await msg.edit_text(f"{final_status}: {name}")
                if status['status'] == 'error':
                    failed_downloads[gid] = name
                break
                
    except Exception as e:
        logger.error(f"Tracking error: {str(e)}")
        await context.bot.send_message(chat_id, f"❌ Tracking failed for {name}")

async def pause_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause all active downloads"""
    try:
        aria2_request("aria2.pauseAll")
        await update.message.reply_text("⏸ All downloads paused")
    except Exception as e:
        await update.message.reply_text(f"❌ Pause error: {str(e)}")

async def resume_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume paused downloads"""
    try:
        aria2_request("aria2.unpauseAll")
        await update.message.reply_text("▶️ Downloads resumed")
    except Exception as e:
        await update.message.reply_text(f"❌ Resume error: {str(e)}")

async def retry_failed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retry failed downloads"""
    try:
        if not failed_downloads:
            await update.message.reply_text("❌ No failed downloads")
            return
            
        for gid, name in list(failed_downloads.items()):
            try:
                aria2_request("aria2.retryDownload", [gid])
                await update.message.reply_text(f"🔄 Retrying: {name}")
                del failed_downloads[gid]
            except Exception as e:
                logger.error(f"Retry failed for {gid}: {str(e)}")
                await update.message.reply_text(f"❌ Failed to retry: {name}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Retry error: {str(e)}")

async def cancel_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel all downloads"""
    try:
        aria2_request("aria2.purgeDownloadResult")
        active = aria2_request("aria2.tellActive")
        for download in active:
            aria2_request("aria2.remove", [download['gid']])
        
        await update.message.reply_text("⏹ All downloads canceled")
        failed_downloads.clear()
    except Exception as e:
        await update.message.reply_text(f"❌ Cancel error: {str(e)}")

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current download status"""
    try:
        active = aria2_request("aria2.tellActive")
        if not active:
            await update.message.reply_text("ℹ️ No active downloads")
            return
            
        status_msg = ["Current downloads:"]
        for idx, download in enumerate(active, 1):
            name = download.get('bittorrent', {}).get('info', {}).get('name', download['gid'])
            completed = int(download.get('completedLength', 0))
            total = int(download.get('totalLength', 1))
            progress = (completed / total) * 100 if total > 0 else 0
            status_msg.append(f"{idx}. {name} - {progress:.1f}%")
            
        await update.message.reply_text("\n".join(status_msg))
    except Exception as e:
        await update.message.reply_text(f"❌ Status error: {str(e)}")

def main():
    """Start the bot"""
    # Verify configuration
    if not all([BOT_TOKEN, ARIA2_RPC_URL, ARIA2_RPC_SECRET]):
        raise RuntimeError("Missing environment variables in .env file")

    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    handlers = [
        CommandHandler("start", start),
        CommandHandler("pause", pause_downloads),
        CommandHandler("resume", resume_downloads),
        CommandHandler("retry", retry_failed),
        CommandHandler("status", show_status),
        CommandHandler("cancel", cancel_downloads),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
        MessageHandler(filters.Document.ALL, handle_file)
    ]
    
    for handler in handlers:
        application.add_handler(handler)
    
    # Start polling
    application.run_polling()

if __name__ == "__main__":
    main()