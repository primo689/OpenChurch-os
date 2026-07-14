#!/bin/bash
# Nightly backup for One Church. Usage: ./backup.sh [/path/to/usb-drive]
DIR="$(cd "$(dirname "$0")" && pwd)"
STAMP=$(date +%Y%m%d-%H%M%S)
DEST="$DIR/backups/church-$STAMP.db"
sqlite3 "$DIR/data/church.db" ".backup '$DEST'"
# Keep the newest 30 local backups
ls -1t "$DIR"/backups/church-*.db 2>/dev/null | tail -n +31 | xargs -r rm
# Optionally copy to a second location (USB drive, network share)
if [ -n "$1" ] && [ -d "$1" ]; then
  cp "$DEST" "$1/"
  ls -1t "$1"/church-*.db 2>/dev/null | tail -n +31 | xargs -r rm
fi
