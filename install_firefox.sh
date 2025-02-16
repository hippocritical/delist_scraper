#!/usr/bin/env bash

# Check for the --nosudo flag
if [[ "$1" == "--nosudo" ]]; then
    SUDO=""
else
    SUDO="sudo"
fi


# Update and install necessary dependencies
$SUDO apt-get update
$SUDO apt-get install -y curl software-properties-common apt-transport-https ca-certificates gnupg2 jq git

# Add Mozilla APT repository for Firefox and install Firefox
$SUDO mkdir -p /etc/apt/keyrings
curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg | $SUDO tee /etc/apt/keyrings/packages.mozilla.org.asc > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" | $SUDO tee /etc/apt/sources.list.d/mozilla.list > /dev/null
echo -e 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000' | $SUDO tee /etc/apt/preferences.d/mozilla > /dev/null
$SUDO apt-get update
$SUDO apt-get install -y firefox

# Detect the architecture and install GeckoDriver
arch=$(uname -m)
echo "Detected architecture: $arch"

# Fetch the latest tag for GeckoDriver
latest_tag=$(curl -sSL "https://api.github.com/repos/mozilla/geckodriver/tags" | jq -r '.[0].name')
echo "Detected latest tag for GeckoDriver: $latest_tag"

# Determine the correct URL for GeckoDriver based on architecture
if [ "$arch" == "x86_64" ]; then
    geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux64.tar.gz"
elif [ "$arch" == "aarch64" ]; then
    geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux-aarch64.tar.gz"
else
    echo "Unsupported architecture: $arch"
    exit 1
fi

echo "Downloading GeckoDriver from $geckodriver_url"

# Download and install GeckoDriver
temp_dir=$(mktemp -d)
cd "$temp_dir" || exit
curl -sSL "$geckodriver_url" -o geckodriver.tar.gz

# Extract GeckoDriver archive and move to /usr/local/bin
$SUDO mkdir -p /usr/local/bin/
$SUDO tar -xzvf geckodriver.tar.gz -C /usr/local/bin/
$SUDO chmod +x /usr/local/bin/geckodriver

# Cleanup temporary files
rm -rf "$temp_dir"

# Verify GeckoDriver installation
geckodriver --version

