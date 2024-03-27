FROM python:slim

# Install necessary dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    software-properties-common \
    apt-transport-https \
    ca-certificates \
    gnupg2

# Add the Chromium repository key
RUN curl -fSsL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor | tee /usr/share/keyrings/chromium-browser.gpg > /dev/null

# Add the Chromium repository
RUN echo "deb [arch=amd64 signed-by=/usr/share/keyrings/chromium-browser.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | tee /etc/apt/sources.list.d/chromium.list

# Install Chromium
RUN apt-get update && \
    apt-get install -y --no-install-recommends chromium

# Cleanup
RUN rm -rf /var/lib/apt/lists/*

# Copy other files and set working directory
COPY requirements.txt /app/
WORKDIR /app
RUN pip install -r requirements.txt
COPY . .

# Define the command to run your application
CMD ["python3", "bot.py"]

