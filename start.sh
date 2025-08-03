#!/bin/bash

echo "Installing requirements..."
pip install -r requirements.txt

echo "Starting bot..."
python bot.py
