#!/usr/bin/env bash
# Install sysdash as a systemd user service that starts on login. No root needed.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${SYSDASH_PORT:-8765}"
UNIT="$HOME/.config/systemd/user/sysdash.service"

command -v python3 >/dev/null || { echo "sysdash needs python3"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "sysdash needs Python 3.10 or newer"; exit 1; }
command -v systemctl >/dev/null || { echo "sysdash needs systemd (run it by hand with: python3 $DIR/sysdash.py)"; exit 1; }

if [[ -e $UNIT ]] && ! grep -q "$DIR/sysdash.py" "$UNIT"; then
  echo "A different sysdash service already exists at $UNIT."
  echo "Remove it first (./uninstall.sh from that copy) or edit it by hand."
  exit 1
fi

mkdir -p "$(dirname "$UNIT")"
sed -e "s|@DIR@|$DIR|g" -e "s|@PORT@|$PORT|g" "$DIR/contrib/sysdash.service.in" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now sysdash.service

echo "sysdash is running: http://localhost:$PORT"
echo "Logs: journalctl --user -u sysdash -f"
