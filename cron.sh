#!/usr/bin/env bash
# Запуск cleanup.py для всіх серверів
# Використання: cron.sh [gather|check]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"
CLEANUP="$SCRIPT_DIR/cleanup.py"
CMD="${1:-}"

if [[ -z "$CMD" ]]; then
    echo "Використання: $0 [gather|check]" >&2
    exit 1
fi

LOG_DIR="/var/log/orthanc-cleanup"
mkdir -p "$LOG_DIR"

for env_file in "$SCRIPT_DIR"/server*.env; do
    server=$(basename "$env_file" .env)
    log="$LOG_DIR/${server}_${CMD}.log"
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') === $server $CMD ===" >> "$log"
    "$PYTHON" "$CLEANUP" "$CMD" --env "$env_file" >> "$log" 2>&1 || true
done
