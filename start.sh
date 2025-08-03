#!/bin/bash
echo "Installing torch..."
pip install torch==2.0.1+cpu -f https://download.pytorch.org/whl/cpu/torch_stable.html

echo "Starting bot..."
python bot.py
