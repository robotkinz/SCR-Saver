#!/usr/bin/env bash
# Copy the live tree into ~/SCR Saver Project for GitHub Desktop.
# Does not commit or push.
set -euo pipefail

LIVE="$(cd "$(dirname "$0")" && pwd)"
DEST="${SCRSAVER_GITHUB_DIR:-$HOME/SCR Saver Project}"

if [[ ! -d "$DEST" ]]; then
  echo "GitHub folder does not exist: $DEST" >&2
  exit 1
fi

python3 "$LIVE/write-social.py"

rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  "$LIVE/src/scrsaver/" "$DEST/src/scrsaver/"

rsync -a --delete "$LIVE/data/" "$DEST/data/"

rsync -a --delete \
  --exclude '*.so.dbg' \
  "$LIVE/native/" "$DEST/native/"

copy_file() {
  local name="$1"
  if [[ -e "$LIVE/$name" ]]; then
    cp -a "$LIVE/$name" "$DEST/$name"
  fi
}

copy_file scrsaver
copy_file install.sh
copy_file package.sh
copy_file README.md
copy_file LICENSE
copy_file CHANGELOG.md
copy_file SOCIAL.md
copy_file PUBLISH.md
copy_file Makefile
copy_file .gitignore
copy_file write-social.py
copy_file sync-github-folder.sh

chmod +x "$DEST/scrsaver" "$DEST/install.sh" "$DEST/package.sh" \
  "$DEST/write-social.py" "$DEST/sync-github-folder.sh"
if [[ -f "$DEST/native/unshield" ]]; then
  chmod +x "$DEST/native/unshield" || true
fi
if [[ -f "$DEST/native/afterdark_cfg.exe" ]]; then
  chmod +x "$DEST/native/afterdark_cfg.exe" || true
fi
if [[ -f "$DEST/native/build-afterdark-cfg.sh" ]]; then
  chmod +x "$DEST/native/build-afterdark-cfg.sh" || true
fi

echo
echo "Synced live tree -> $DEST"
if [[ -d "$DEST/.git" ]]; then
  echo
  git -C "$DEST" status --short
  echo
  echo "Next: copy SOCIAL.md into your post, then commit and push in GitHub Desktop."
fi
