sudo apt update
sudo apt install curl software-properties-common apt-transport-https ca-certificates gnupg2 -y
sudo apt install jq -y
sudo apt install firefox -y

arch=$(uname -m)
echo "detected architecture: $arch"

latest_tag=$(curl -sSL "https://api.github.com/repos/mozilla/geckodriver/tags" | jq -r '.[0].name')
echo "detected latest tag for geckodriver: $latest_tag"

if [ "$arch" == "x86_64" ]; then
    geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux64.tar.gz"
elif [ "$arch" == "aarch64" ]; then
    geckodriver_url="https://github.com/mozilla/geckodriver/releases/download/$latest_tag/geckodriver-$latest_tag-linux-aarch64.tar.gz"

else
    echo "Unsupported architecture: $arch"
    exit 1
fi
echo "downloading $geckodriver_url"

# Download and install GeckoDriver
temp_dir=$(mktemp -d)
cd "$temp_dir" || exit

curl -sSL "$geckodriver_url" -o geckodriver.tar.gz

# Extract GeckoDriver archive
sudo mkdir -p /usr/local/bin/
sudo tar -xzvf geckodriver.tar.gz -C /usr/local/bin/

# Set permissions
sudo chmod +x /usr/local/bin/geckodriver

# Cleanup
rm -rf "$temp_dir"

# Verify installation
geckodriver --version
