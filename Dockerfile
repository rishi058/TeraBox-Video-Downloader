# Use the official Python slim image as the base
FROM python:3.10-slim

# Set environment variables to avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install prerequisites, Xvfb (Virtual Display), and ffmpeg
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    xvfb \
    unzip \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome (Required for DrissionPage)
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
# Note: Ensure you have a requirements.txt with your python packages!
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all the application files into the container
COPY . .

# Start Xvfb in the background, set DISPLAY, and run the app
CMD sh -c "Xvfb :99 -screen 0 1280x1024x24 -ac & export DISPLAY=:99 && python main.py"
