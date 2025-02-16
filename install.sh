#!/usr/bin/env bash
#encoding=utf8

# Check for the --nosudo flag
if [[ "$1" == "--nosudo" ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

# Update the repository and install necessary packages
$SUDO apt-get update && $SUDO apt-get upgrade -y
$SUDO apt-get install -y python3-venv curl software-properties-common apt-transport-https ca-certificates gnupg2 jq

# Create a virtual environment and activate it
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip and install Python dependencies
python3 -m pip install --upgrade pip
REQUIREMENTS=requirements.txt
python3 -m pip install --upgrade -r ${REQUIREMENTS}

echo "Virtual environment setup complete. Activate it using 'source .venv/bin/activate'!"

