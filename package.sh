#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

WITH_USER_DATA=0
if [[ "${1:-}" == "--with-user-data" ]]; then
  WITH_USER_DATA=1
fi

if command -v cc >/dev/null 2>&1; then
  make -C "$ROOT" all
fi

VERSION="$(python3 - <<'PY'
from pathlib import Path
import re
text = Path("src/scrsaver/__init__.py").read_text(encoding="utf-8")
print(re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1))
PY
)"

NAME="scrsaver-${VERSION}-linux"
DIST="$ROOT/dist"
STAGE="$DIST/$NAME"
ARCHIVE="$DIST/${NAME}.tar.gz"

rm -rf "$STAGE"
mkdir -p "$STAGE/src" "$STAGE/data" "$STAGE/native"

cp -a "$ROOT/src/scrsaver" "$STAGE/src/"
find "$STAGE/src" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/src" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

cp "$ROOT/scrsaver" "$ROOT/install.sh" "$ROOT/README.md" "$ROOT/LICENSE" "$ROOT/CHANGELOG.md" "$STAGE/"
cp "$ROOT/data/scrsaver.svg" "$ROOT/data/scrsaver.service" "$STAGE/data/"
cat > "$STAGE/data/scrsaver.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=SCR Saver
Comment=Play Windows .scr screensavers when idle
Exec=scrsaver
Icon=scrsaver
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

if [ -x "$ROOT/native/unshield" ]; then
  cp "$ROOT/native/unshield" "$STAGE/native/unshield"
  chmod +x "$STAGE/native/unshield"
fi
if [ -f "$ROOT/native/libscrsaver_pace.so" ]; then
  cp "$ROOT/native/libscrsaver_pace.so" "$STAGE/native/libscrsaver_pace.so"
fi
if [ -f "$ROOT/native/scrsaver_pace.c" ]; then
  cp "$ROOT/native/scrsaver_pace.c" "$STAGE/native/scrsaver_pace.c"
fi
if [ -f "$ROOT/Makefile" ]; then
  cp "$ROOT/Makefile" "$STAGE/Makefile"
fi
chmod +x "$STAGE/scrsaver" "$STAGE/install.sh"

if [ "$WITH_USER_DATA" -eq 1 ]; then
  if [ -d "${HOME}/.local/share/scrsaver/library" ]; then
    mkdir -p "$STAGE/user-data/library"
    cp -a "${HOME}/.local/share/scrsaver/library/." "$STAGE/user-data/library/"
  fi
  if [ -f "${HOME}/.config/scrsaver/config.json" ]; then
    mkdir -p "$STAGE/user-data"
    python3 - "$STAGE/user-data/config.json" <<'PY'
import json
import sys
from pathlib import Path
src = Path.home() / ".config/scrsaver/config.json"
dst = Path(sys.argv[1])
cfg = json.loads(src.read_text(encoding="utf-8"))
cfg["autostart"] = False
cfg["enabled"] = False
dst.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
PY
  fi
fi

cat > "$STAGE/INSTALL.txt" <<'EOF'
SCR Saver (alpha)

  tar -xzf scrsaver-*-linux.tar.gz
  cd scrsaver-*-linux
  ./install.sh

Needs Python 3, PyQt6, python-evdev, Wine, wmctrl, xdotool, and membership
in the input group. See README.md for distro packages.

A new Wine prefix is created on first play. Do not copy a Wine prefix from
another machine.

native/unshield is an optional x86_64 helper for InstallShield installers.
On another CPU architecture, install the distro unshield package instead.

If this archive includes user-data/, you can copy imported savers with:

  mkdir -p ~/.local/share/scrsaver ~/.config/scrsaver
  cp -a user-data/library ~/.local/share/scrsaver/
  cp user-data/config.json ~/.config/scrsaver/
EOF

tar -C "$DIST" -czf "$ARCHIVE" "$NAME"
rm -rf "$STAGE"

echo "Wrote $ARCHIVE"
ls -lh "$ARCHIVE"
if [ "$WITH_USER_DATA" -eq 1 ]; then
  echo "This archive includes personal library/config from this machine."
else
  echo "This is a clean source archive (no personal library or config)."
fi
