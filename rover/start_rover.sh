#!/bin/bash
sudo pigpiod
sleep 1
cd ~/ort-rover/rover
source venv/bin/activate
python rover.py
