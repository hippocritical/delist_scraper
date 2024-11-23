#!/usr/bin/env bash
#encoding=utf8

# Update the repository and install necessary packages
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y python3-venv curl software-properties-common apt-transport-https ca-certificates gnupg2 jq

# Create a virtual environment and activate it
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip and install Python dependencies
python3 -m pip install --upgrade pip
REQUIREMENTS=requirements.txt
python3 -m pip install --upgrade -r ${REQUIREMENTS}

echo "Virtual environment setup complete. Activate it using 'source .venv/bin/activate'!"

