#!/bin/bash
echo "Installing PyTorch manually..."
pip install torch==1.13.1+cpu -f https://download.pytorch.org/whl/cpu/torch_stable.html

echo "Starting bot..."
python bot.py

