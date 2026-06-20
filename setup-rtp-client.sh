#!/bin/bash
# Setup script for remote-thermo-printer client on tprinter1

set -e

echo "=== Remote Thermo Printer Client Setup ==="
echo ""

# Check if source is in place
if [ ! -d /opt/remote-thermo-printer/src ]; then
    echo "ERROR: Source code not found at /opt/remote-thermo-printer/src"
    exit 1
fi

echo "✓ Source code found"

# Verify dependencies
python3 -c "from pydantic_settings import BaseSettings" && echo "✓ pydantic-settings installed" || (echo "✗ Missing pydantic-settings"; exit 1)
python3 -c "import websockets" && echo "✓ websockets installed" || (echo "✗ Missing websockets"; exit 1)
python3 -c "import PIL" && echo "✓ pillow installed" || (echo "✗ Missing pillow"; exit 1)

echo ""

# Create/update systemd service
echo "Setting up systemd service..."
sudo tee /etc/systemd/system/remote-thermo-printer-client.service > /dev/null <<'EOF'
[Unit]
Description=Remote Thermo Printer Client
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/remote-thermo-printer/src
Environment="PYTHONPATH=/opt/remote-thermo-printer/src"
Environment="RTP_CLIENT_CONFIG=/opt/remote-thermo-printer/src/configs/tprinter1/raspberrypi.conf"
ExecStart=/usr/bin/python3 -m client
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "✓ Service file created"

# Enable and start the service
echo "Enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable remote-thermo-printer-client.service
echo "✓ Service enabled for autostart"

# Verify wireguard is enabled
sudo systemctl is-enabled wg-quick@wg0.service >/dev/null && echo "✓ WireGuard enabled for autostart" || echo "⚠ WireGuard not enabled"

# Start the service if not already running
if sudo systemctl is-active --quiet remote-thermo-printer-client.service; then
    echo "✓ Service already running"
else
    echo "Starting service..."
    sudo systemctl start remote-thermo-printer-client.service
    sleep 2
    if sudo systemctl is-active --quiet remote-thermo-printer-client.service; then
        echo "✓ Service started successfully"
    else
        echo "⚠ Service failed to start - check logs with: sudo journalctl -u remote-thermo-printer-client -n 50"
    fi
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Service status:"
sudo systemctl status remote-thermo-printer-client.service --no-pager | head -8
echo ""
echo "WireGuard status:"
sudo systemctl status wg-quick@wg0.service --no-pager | head -8
echo ""
echo "View logs:"
echo "  sudo journalctl -u remote-thermo-printer-client -f"
echo "  sudo journalctl -u wg-quick@wg0 -f"
