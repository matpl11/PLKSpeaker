#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
cd ..
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install edge-tts

python3 tts.py
