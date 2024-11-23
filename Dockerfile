FROM python:slim

# Install necessary dependencies
RUN apt-get update && apt-get upgrade -y
RUN apt-get install -y --no-install-recommends \
    curl \
    software-properties-common \
    apt-transport-https \
    ca-certificates \
    gnupg2 \
    jq


# Add Mozilla APT repository for Firefox
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg | tee /etc/apt/keyrings/packages.mozilla.org.asc > /dev/null && \
    echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" | tee /etc/apt/sources.list.d/mozilla.list > /dev/null && \
    echo 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000' | tee /etc/apt/preferences.d/mozilla > /dev/null && \
    apt-get update && \
    apt-get install -y firefox && \
    rm -rf /var/lib/apt/lists/*

# Detect the architecture and install GeckoDriver
RUN arch=$(uname -m) && \
    echo "detected architecture: $arch" && \
    latest_tag=$(curl -sSL "https://api.github.com/repos/mozilla/geckodriver/tags" | jq -r '.[0].name') && \
    echo "detected latest tag for geckodriver: $latest_tag" && \
    if [ "$arch" = "x86_64" ]; then \
        geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux64.tar.gz"; \
    elif [ "$arch" = "aarch64" ]; then \
        geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux-aarch64.tar.gz"; \
    else \
        echo "Unsupported architecture: $arch" && exit 1; \
    fi && \
    echo "downloading $geckodriver_url" && \
    temp_dir=$(mktemp -d) && cd "$temp_dir" && \
    curl -sSL "$geckodriver_url" -o geckodriver.tar.gz && \
    tar -xzvf geckodriver.tar.gz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/geckodriver && \
    rm -rf "$temp_dir"



# Cleanup
RUN rm -rf /var/lib/apt/lists/*

# Copy other files and set working directory
COPY requirements.txt /app/
WORKDIR /app
RUN pip install -r requirements.txt
COPY . .

# Define the command to run your application
CMD ["python3", "bot.py"]
