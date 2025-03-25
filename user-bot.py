import os
import logging
from flask import Flask, request, jsonify
from telethon import TelegramClient

# Flask app setup
app = Flask(__name__)

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("user-bot")

# Retrieve Telegram API credentials from environment variables
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE_NUMBER = os.environ["PHONE_NUMBER"]

# Initialize Telethon client
client = TelegramClient("userbot.session", API_ID, API_HASH)

@app.route("/forward", methods=["POST"])
def forward_message():
    """Endpoint to forward messages."""
    try:
        data = request.json
        chat_id = int(data.get("chat_id", 0))
        message_id = int(data.get("message_id", 0))
        
        if not (chat_id and message_id):
            raise ValueError("Missing or invalid chat_id/message_id")

        logger.info(f"Forwarding message: chat_id={chat_id}, message_id={message_id}")

        # Forward the message to yourself (or another chat)
        client.loop.run_until_complete(client.forward_messages("me", message_id, chat_id))

        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"Error forwarding message: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    # Start the Telethon client before running the Flask app
    with client:
        logger.info("User bot is running.")
        app.run(host="0.0.0.0", port=5001)
