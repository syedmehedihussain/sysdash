#!/usr/bin/env bash
# Stop and remove the sysdash user service. Your data stays in ~/.local/state/sysdash unless you pass --purge.
set -euo pipefail

systemctl --user disable --now sysdash.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/sysdash.service"
systemctl --user daemon-reload

if [[ ${1:-} == "--purge" ]]; then
  rm -rf "${SYSDASH_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/sysdash}"
  echo "sysdash removed, data deleted."
else
  echo "sysdash removed. History is still in ${SYSDASH_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/sysdash} (use --purge to delete it)."
fi
