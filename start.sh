#!/bin/bash

echo "Installing requirements..."
pip install --prefer-binary -r requirements.txt

echo "Starting bot..."
python bot.py

