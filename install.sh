#!/bin/bash
# Nakheel date grader - one-time setup on Raspberry Pi OS (Bookworm) for Pi 3B+
set -e
sudo apt update
sudo apt install -y python3-picamera2 python3-gpiozero python3-pil python3-venv espeak-ng alsa-utils fonts-dejavu-core i2c-tools
sudo raspi-config nonint do_i2c 0            # turn on I2C for the OLED
sudo raspi-config nonint do_audio 1 2>/dev/null || true   # audio out -> 3.5 mm headphone jack
# send the Pi's audio out of GPIO18 (header pin 12) to the PAM8403 - see the Module 2 wiring diagram
CFG=/boot/firmware/config.txt; [ -f $CFG ] || CFG=/boot/config.txt
grep -q "audremap" $CFG || echo "dtoverlay=audremap,pins=18_19" | sudo tee -a $CFG
python3 -m venv --system-site-packages ~/nakheel-venv
~/nakheel-venv/bin/pip install --upgrade luma.oled google-genai
# start automatically at boot
sudo tee /etc/systemd/system/nakheel.service >/dev/null <<UNIT
[Unit]
Description=Nakheel date grader
After=network-online.target sound.target
Wants=network-online.target
[Service]
User=$USER
WorkingDirectory=$PWD
Environment=GEMINI_API_KEY=PASTE_YOUR_KEY_HERE
Environment=NAKHEEL_LANG=en
ExecStart=$HOME/nakheel-venv/bin/python $PWD/nakheel_grader.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
echo
echo "Now: sudo nano /etc/systemd/system/nakheel.service  -> paste your Gemini API key"
echo "Then: sudo systemctl daemon-reload && sudo systemctl enable --now nakheel"
echo "Check the OLED is found with: i2cdetect -y 1   (you should see 3c)"
