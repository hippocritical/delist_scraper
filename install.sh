#!/usr/bin/env bash
#encoding=utf8

git pull
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip

REQUIREMENTS=requirements.txt
python3 -m pip install --upgrade -r ${REQUIREMENTS}
echo "Don't forget to activate your virtual environment with 'source .venv/bin/activate'!"