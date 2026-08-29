#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="$(id -un)"

echo "========================================"
echo " Woody Monitor installer"
echo "========================================"
echo "User:      $APP_USER"
echo "Directory: $APP_DIR"
echo

cd "$APP_DIR"

# ------------------------------------------------------------
# System dependencies
# ------------------------------------------------------------

echo "Installing required system packages..."

sudo apt-get update
sudo apt-get install -y     python3     python3-venv     python3-pip

# Give Woody Monitor access to USB/serial devices
if getent group dialout >/dev/null 2>&1; then
    sudo usermod -aG dialout "$APP_USER"
fi

# ------------------------------------------------------------
# Runtime directories
# ------------------------------------------------------------

mkdir -p data logs

# ------------------------------------------------------------
# Environment file
# ------------------------------------------------------------

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example"

    # Try to detect a stable USB serial device automatically
    if [ -d /dev/serial/by-id ]; then
        mapfile -t SERIAL_DEVICES < <(find /dev/serial/by-id -maxdepth 1 -type l -print | sort)

        if [ "${#SERIAL_DEVICES[@]}" -eq 1 ]; then
            sed -i "s|^WOODY_SERIAL_DEVICE=.*|WOODY_SERIAL_DEVICE=${SERIAL_DEVICES[0]}|" .env
            echo "Detected serial device: ${SERIAL_DEVICES[0]}"
        elif [ "${#SERIAL_DEVICES[@]}" -gt 1 ]; then
            echo "Multiple serial devices found."
            echo "Please select the correct device in $APP_DIR/.env"
            printf "  %s\\n" "${SERIAL_DEVICES[@]}"
        else
            echo "No USB serial device detected."
            echo "Configure WOODY_SERIAL_DEVICE in $APP_DIR/.env"
        fi
    else
        echo "No /dev/serial/by-id directory found."
        echo "Configure WOODY_SERIAL_DEVICE in $APP_DIR/.env"
    fi
else
    echo ".env already exists - keeping current configuration"
fi

# ------------------------------------------------------------
# Python virtual environment
# ------------------------------------------------------------

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# ------------------------------------------------------------
# systemd service
# ------------------------------------------------------------

SERVICE_FILE="/etc/systemd/system/woody-monitor.service"

sudo tee "$SERVICE_FILE" >/dev/null <<SERVICE
[Unit]
Description=Woody Monitor - Scotte/PellMon pellet burner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/woody_monitor.py
Restart=always
RestartSec=5
WatchdogSec=30

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable woody-monitor

echo
echo "========================================"
echo " Installation complete"
echo "========================================"
echo
echo "Edit configuration:"
echo "  nano $APP_DIR/.env"
echo
echo "Find serial device:"
echo "  ls -l /dev/serial/by-id/"
echo
echo "Then start Woody Monitor:"
echo "  sudo systemctl start woody-monitor"
echo
echo "Status:"
echo "  systemctl status woody-monitor --no-pager"
echo
