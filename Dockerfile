FROM python:3.11-slim

# Install FFmpeg and essential system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy single-file bot
COPY bot.py .

# Health check port exposed
EXPOSE 3000

# Run bot
CMD ["python", "bot.py"]
