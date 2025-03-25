FROM python:3.9-slim

# Set work directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the bot script
COPY download_bot.py .

# Run the bot script
CMD ["python", "download_bot.py"]
