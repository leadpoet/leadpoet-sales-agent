#!/usr/bin/env bash
# Verify this bundle, then optionally call the official miner submit helper.
set -euo pipefail
BUNDLE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd -- "$BUNDLE/.." && pwd)"
DEFAULT_REPO="$PROJECT/data/validator"
[[ -d "$DEFAULT_REPO" ]] || DEFAULT_REPO="$PROJECT/leadpoet"
REPO="${LEADPOET_REPO:-$DEFAULT_REPO}"
PY="${ARENA_PYTHON:-$PROJECT/leadpoet/.venv/bin/python}"
API="${ARENA_API:-https://gateway.subnet71.com}"
case "${1:-}" in
  ""|--send) ;;
  *) echo "Usage: $0 [--send]" >&2; exit 2 ;;
esac
test -x "$PY" || { echo "Set ARENA_PYTHON to the LeadPoet Python interpreter." >&2; exit 2; }
test -f "$REPO/scripts/lab_arena_miner.py" || { echo "Set LEADPOET_REPO to the current official checkout." >&2; exit 2; }
"$PY" "$PROJECT/tools/verify_arena.py" --repo "$REPO" --api "$API"
if [[ "${1:-}" != "--send" ]]; then
  echo "Preflight complete; no submission requested."
  exit 0
fi
cd -- "$REPO"
exec "$PY" scripts/lab_arena_miner.py submit-source \
  --source "$BUNDLE" --api-base-url "$API" \
  --wallet-name "${ARENA_WALLET:-miner}" \
  --hotkey-name "${ARENA_HOTKEY:-default}"
