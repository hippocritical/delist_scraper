FROM python:3.9-slim

# Install necessary dependencies
RUN apt-get update && apt-get install -y \
    curl software-properties-common apt-transport-https \
    ca-certificates gnupg2 jq firefox-esr \
    && rm -rf /var/lib/apt/lists/*

# Install GeckoDriver
RUN arch=$(uname -m) && \
    latest_tag=$(curl -sSL "https://api.github.com/repos/mozilla/geckodriver/tags" | jq -r '.[0].name') && \
    if [ "$arch" = "x86_64" ]; then \
        geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux64.tar.gz"; \
    elif [ "$arch" = "aarch64" ]; then \
        geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux-aarch64.tar.gz"; \
    else \
        echo "Unsupported architecture: $arch" && exit 1; \
    fi && \
    curl -sSL "$geckodriver_url" -o geckodriver.tar.gz && \
    tar -xzvf geckodriver.tar.gz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/geckodriver && \
    rm -rf geckodriver.tar.gz

# Set up virtual environment
WORKDIR /app
RUN python3 -m venv .venv && \
    . .venv/bin/activate && \
    pip install --upgrade pip && \
    if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
