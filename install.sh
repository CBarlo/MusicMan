#!/bin/bash
# ============================================================
#  MUSIC MAN CONSOLE — install.sh
#  Broken Arrow Expedition
# ============================================================
#  Run once on a fresh Raspberry Pi OS Lite (64-bit).
#  Requires internet access for apt and pip.
#
#  Usage:
#    chmod +x install.sh
#    sudo ./install.sh
#
#  After running:
#    http://musicman.local       Operator console
#    http://musicman.local/admin Admin panel
#    http://musicman.local/display  Display output
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
AMBER='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${AMBER}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
header()  {
    echo -e "\n${AMBER}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${AMBER}  $1${NC}"
    echo -e "${AMBER}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

if [ "$EUID" -ne 0 ]; then
    error "Please run with sudo: sudo ./install.sh"
fi

INSTALL_DIR=/home/pi/musicman
SERVICE_USER=pi
REPO_URL=https://github.com/CBarlo/MusicMan.git
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

clear
echo ""
echo -e "${AMBER}╔════════════════════════════════════════════╗${NC}"
echo -e "${AMBER}║     MUSIC MAN CONSOLE — INSTALLER         ║${NC}"
echo -e "${AMBER}║     Broken Arrow Expedition                ║${NC}"
echo -e "${AMBER}╚════════════════════════════════════════════╝${NC}"
echo ""
info "Starting installation (~15 minutes on a fresh Pi)."
echo ""


# ════════════════════════════════════════════
header "STEP 1 — System update"
# ════════════════════════════════════════════
apt-get update -qq
apt-get upgrade -y -qq
success "System updated"


# ════════════════════════════════════════════
header "STEP 2 — System packages"
# ════════════════════════════════════════════
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    ffmpeg \
    libhidapi-hidraw0 \
    libhidapi-libusb0 \
    libudev-dev \
    libusb-1.0-0-dev \
    libsdl2-mixer-2.0-0 \
    libsdl2-2.0-0 \
    libasound2-dev \
    libportaudio2 \
    chromium-browser \
    avahi-daemon \
    dnsmasq \
    hostapd \
    samba \
    curl \
    wget \
    unzip \
    at \
    fonts-open-sans \
    fonts-dejavu-core
success "System packages installed"


# ════════════════════════════════════════════
header "STEP 3 — Copy application files"
# ════════════════════════════════════════════
if [ -f "$SCRIPT_DIR/musicman.py" ]; then
    info "Installer found next to script — copying files from local source..."
    mkdir -p $INSTALL_DIR
    rsync -a --exclude='venv' --exclude='logs' --exclude='assets' \
          --exclude='config.yaml' --exclude='install.yaml' --exclude='.git' \
          "$SCRIPT_DIR/" "$INSTALL_DIR/"
    success "Files copied from local source"
else
    info "Cloning from GitHub: $REPO_URL"
    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Repo already exists — pulling latest..."
        sudo -u $SERVICE_USER git -C "$INSTALL_DIR" pull --quiet
    else
        sudo -u $SERVICE_USER git clone --quiet "$REPO_URL" "$INSTALL_DIR"
    fi
    success "Repo ready at $INSTALL_DIR"
fi
chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR


# ════════════════════════════════════════════
header "STEP 4 — Directory structure"
# ════════════════════════════════════════════
mkdir -p \
    $INSTALL_DIR/assets/circles \
    $INSTALL_DIR/assets/roles \
    $INSTALL_DIR/assets/sfx \
    $INSTALL_DIR/assets/music \
    $INSTALL_DIR/assets/display \
    $INSTALL_DIR/assets/macros \
    $INSTALL_DIR/assets/game_entries \
    $INSTALL_DIR/assets/fonts \
    $INSTALL_DIR/logs
chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR
success "Directory structure created"


# ════════════════════════════════════════════
header "STEP 5 — Python virtual environment"
# ════════════════════════════════════════════
if [ ! -d "$INSTALL_DIR/venv" ]; then
    sudo -u $SERVICE_USER python3 -m venv $INSTALL_DIR/venv
    success "Virtual environment created"
else
    info "Virtual environment already exists — skipping creation"
fi

info "Installing Python packages from requirements.txt..."
sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/pip install --quiet --upgrade pip
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/pip install --quiet -r "$INSTALL_DIR/requirements.txt"
    success "Python packages installed"
else
    warn "requirements.txt not found — installing base packages manually"
    sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/pip install --quiet \
        "Flask>=3.1" "flask-sock>=0.7" "gunicorn>=26.0" "pygame>=2.6" \
        "mutagen>=1.47" "PyYAML>=6.0" "requests>=2.34" "bleak>=0.22" \
        "numpy>=2.0" "streamdeck>=0.9.8" "Pillow>=11.0" "websocket-client>=1.9"
    success "Base packages installed"
fi


# ════════════════════════════════════════════
header "STEP 6 — Config files"
# ════════════════════════════════════════════
if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
    if [ -f "$INSTALL_DIR/config.starter.yaml" ]; then
        cp "$INSTALL_DIR/config.starter.yaml" "$INSTALL_DIR/config.yaml"
        chown $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR/config.yaml"
        success "config.yaml created from starter template"
    else
        warn "No config.starter.yaml found — you must create config.yaml manually"
    fi
else
    info "config.yaml already exists — leaving untouched"
fi

if [ ! -f "$INSTALL_DIR/install.yaml" ]; then
    cat > $INSTALL_DIR/install.yaml << 'INSTALLYAML'
# MusicMan installation settings — edit to match your Pi
# These override the system: block in config.yaml

system:
  hostname: musicman
  port: 80
  audio_device: default
  debug: false
  display_enabled: false
  log_level: info
  admin_password: changeme
INSTALLYAML
    chown $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR/install.yaml"
    warn "install.yaml created with defaults — CHANGE admin_password before use!"
else
    info "install.yaml already exists — leaving untouched"
fi


# ════════════════════════════════════════════
header "STEP 7 — Sudoers (no-password service control)"
# ════════════════════════════════════════════
cat > /etc/sudoers.d/musicman-system << 'SUDOERS'
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart musicman.service
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart musicman
pi ALL=(ALL) NOPASSWD: /sbin/shutdown
pi ALL=(ALL) NOPASSWD: /usr/sbin/shutdown
pi ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/dnsmasq.d/musicman-nodes.conf
pi ALL=(ALL) NOPASSWD: /bin/systemctl reload dnsmasq
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart bluetooth
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart bluetooth.service
pi ALL=(ALL) NOPASSWD: /home/pi/musicman/scripts/backup_sd_image.sh *
SUDOERS
chmod 440 /etc/sudoers.d/musicman-system
success "Sudoers configured"


# ════════════════════════════════════════════
header "STEP 8 — Stream Deck USB permissions"
# ════════════════════════════════════════════
cat > /etc/udev/rules.d/70-streamdeck.rules << 'UDEVEOF'
# Elgato Stream Deck MK.2
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0080", MODE="0666", GROUP="plugdev"
# Elgato Stream Deck Original
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0060", MODE="0666", GROUP="plugdev"
# Elgato Stream Deck XL
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="006c", MODE="0666", GROUP="plugdev"
# Elgato Stream Deck Mini
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", ATTRS{idProduct}=="0063", MODE="0666", GROUP="plugdev"
UDEVEOF

cat > /etc/udev/rules.d/71-streamdeck-reconnect.rules << 'RECONNEOF'
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", \
    RUN+="/bin/systemctl restart musicman.service"
RECONNEOF

udevadm control --reload-rules
udevadm trigger
usermod -a -G plugdev $SERVICE_USER
success "Stream Deck USB permissions configured"


# ════════════════════════════════════════════
header "STEP 9 — Systemd services"
# ════════════════════════════════════════════
if [ -f "$INSTALL_DIR/musicman.service" ]; then
    cp "$INSTALL_DIR/musicman.service" /etc/systemd/system/musicman.service
    success "musicman.service installed from repo"
else
    warn "musicman.service not found in repo — writing default"
    cat > /etc/systemd/system/musicman.service << SVCEOF
[Unit]
Description=Music Man Console
After=network.target sound.target avahi-daemon.service
Wants=avahi-daemon.service

[Service]
Type=simple
User=pi
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/gunicorn musicman:app \\
    --bind 0.0.0.0:80 \\
    --workers 1 \\
    --threads 32 \\
    --worker-class gthread \\
    --timeout 30 \\
    --keep-alive 5 \\
    --log-level info \\
    --access-logfile $INSTALL_DIR/logs/access.log \\
    --error-logfile $INSTALL_DIR/logs/error.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF
fi

if [ -f "$INSTALL_DIR/streamdeck.service" ]; then
    cp "$INSTALL_DIR/streamdeck.service" /etc/systemd/system/streamdeck.service
    success "streamdeck.service installed from repo"
fi

systemctl daemon-reload
systemctl enable musicman.service
success "musicman.service enabled"

if [ -f /etc/systemd/system/streamdeck.service ]; then
    systemctl enable streamdeck.service
    success "streamdeck.service enabled"
fi


# ════════════════════════════════════════════
header "STEP 10 — Network file sharing (Samba)"
# ════════════════════════════════════════════
SMB_CONF=/etc/samba/smb.conf
if ! grep -q '^\[musicman\]' "$SMB_CONF" 2>/dev/null; then
    cat >> "$SMB_CONF" << 'SMBEOF'

[musicman]
path = /home/pi/musicman
browseable = yes
writable = yes
valid users = pi
create mask = 0664
directory mask = 0775
SMBEOF
fi

# Samba password prompt — interactively set here rather than hardcoded
echo ""
warn "Set the Samba password for user 'pi' (used to access the musicman share):"
smbpasswd -a pi || warn "Samba password not set — run 'sudo smbpasswd -a pi' later"

systemctl enable smbd --quiet
systemctl restart smbd
success "Samba share 'musicman' configured (smb://musicman.local)"


# ════════════════════════════════════════════
header "STEP 11 — Start services"
# ════════════════════════════════════════════
info "Starting musicman service..."
systemctl start musicman.service
sleep 4

if systemctl is-active --quiet musicman.service; then
    success "Music Man service is running"
else
    warn "Service may not have started — check logs with:"
    warn "  sudo journalctl -u musicman.service -n 50"
fi


# ════════════════════════════════════════════
header "NETWORK SETUP (optional)"
# ════════════════════════════════════════════
echo ""
warn "hostapd / dnsmasq AP setup is NOT done automatically."
warn "To set up the Pi as a WiFi access point:"
warn "  1. Edit /etc/hostapd/hostapd.conf (ssid, wpa_passphrase, interface)"
warn "  2. Edit /etc/dnsmasq.conf (dhcp-range, domain)"
warn "  3. Run: sudo systemctl enable --now hostapd dnsmasq"
warn "See the WLED setup guide in the repo for the full config."


# ════════════════════════════════════════════
echo ""
echo -e "${AMBER}╔════════════════════════════════════════════╗${NC}"
echo -e "${AMBER}║     INSTALLATION COMPLETE                  ║${NC}"
echo -e "${AMBER}╚════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}✓${NC}  Music Man Console is running"
echo -e "${GREEN}✓${NC}  Auto-starts on every boot"
echo -e "${GREEN}✓${NC}  Stream Deck auto-reconnects on plug"
echo -e "${GREEN}✓${NC}  Samba share: smb://musicman.local  (user: pi)"
echo ""
echo -e "    Operator UI:   ${BLUE}http://musicman.local${NC}"
echo -e "    Admin panel:   ${BLUE}http://musicman.local/admin${NC}"
echo -e "    Display:       ${BLUE}http://musicman.local/display${NC}"
echo ""
echo -e "    Logs:    $INSTALL_DIR/logs/"
echo -e "    Config:  $INSTALL_DIR/config.yaml  (edit for your expedition)"
echo -e "    Assets:  $INSTALL_DIR/assets/"
echo ""
echo -e "${AMBER}Next steps:${NC}"
echo -e "  1. Change admin_password in $INSTALL_DIR/install.yaml"
echo -e "  2. Edit config.yaml with your circles, roles, and WLED device IPs"
echo -e "  3. Open ${BLUE}http://musicman.local/admin${NC} to configure the system"
echo -e "  4. Plug in the Stream Deck"
echo -e "  5. Open ${BLUE}http://musicman.local${NC} on the iPad"
echo ""
