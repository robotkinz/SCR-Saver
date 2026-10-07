#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"

chmod +x "$ROOT/scrsaver"
if [ -f "$ROOT/native/unshield" ]; then
  chmod +x "$ROOT/native/unshield" || true
fi

missing=()
command -v python3 >/dev/null || missing+=("python3")
command -v wine >/dev/null || missing+=("wine")
command -v wmctrl >/dev/null || missing+=("wmctrl")
command -v xdotool >/dev/null || missing+=("xdotool")
python3 -c "from PyQt6.QtWidgets import QApplication" 2>/dev/null || missing+=("python-pyqt6")
python3 -c "from evdev import InputDevice" 2>/dev/null || missing+=("python-evdev")
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Missing dependencies: ${missing[*]}"
  echo "Arch/Garuda: sudo pacman -S --needed python python-pyqt6 python-evdev wine-staging wmctrl xdotool 7zip"
  echo "Debian/Ubuntu: sudo apt install python3 python3-pyqt6 python3-evdev wine wmctrl xdotool p7zip-full"
  echo "Continue anyway? The app will not run until these are installed."
fi

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

ln -sfn "$ROOT/scrsaver" "$BIN_DIR/scrsaver"
cp "$ROOT/data/scrsaver.svg" "$ICON_DIR/scrsaver.svg"

cat > "$APP_DIR/scrsaver.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SCR Saver
Comment=Play Windows .scr screensavers when idle
Exec=$ROOT/scrsaver
Icon=$ROOT/data/scrsaver.svg
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

PYTHONPATH="$ROOT/src" python3 - <<'PY'
from scrsaver.autostart import set_autostart
from scrsaver.config import load_config, save_config

set_autostart(True)
cfg = load_config()
cfg.autostart = True
save_config(cfg)
print("Enabled systemd --user scrsaver.service")
PY

echo "Installed launcher to $BIN_DIR/scrsaver"
echo "Desktop entry: $APP_DIR/scrsaver.desktop"
echo "User service:  systemctl --user status scrsaver.service"
echo
echo "Start it with:  scrsaver"
echo "Or from the application menu as “SCR Saver”."
echo
if ! id -nG | grep -qw input; then
  echo "Note: this user is not in the 'input' group. Idle detection needs that group."
  echo "  sudo usermod -aG input $USER"
  echo "Then log out and back in."
fi
