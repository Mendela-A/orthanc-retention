#!/usr/bin/env bash
# Запуск cleanup.py для всіх серверів
# Використання: cron.sh [gather|check]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"
CLEANUP="$SCRIPT_DIR/cleanup.py"
CMD="${1:-}"
LOG_DIR="/var/log/orthanc-cleanup"

if [[ -z "$CMD" ]]; then
    echo "Використання: $0 [gather|check]" >&2
    exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "[ERROR] Python не знайдено: $PYTHON" >&2
    echo "        Створіть venv: python -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [[ ! -d "$LOG_DIR" ]]; then
    echo "[ERROR] Директорія логів відсутня: $LOG_DIR" >&2
    echo "        Створіть: sudo mkdir -p $LOG_DIR && sudo chown \$USER $LOG_DIR" >&2
    exit 1
fi

# Перевірка наявності server*.env файлів
shopt -s nullglob
env_files=("$SCRIPT_DIR"/server.*.env)
shopt -u nullglob

if [[ ${#env_files[@]} -eq 0 ]]; then
    echo "[ERROR] Не знайдено жодного server.*.env у $SCRIPT_DIR" >&2
    exit 1
fi

for env_file in "${env_files[@]}"; do
    server=$(basename "$env_file" .env)
    log="$LOG_DIR/${server}_${CMD}.log"
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') === $server $CMD ===" >> "$log"
    "$PYTHON" "$CLEANUP" "$CMD" --env "$env_file" >> "$log" 2>&1 || \
        echo "[ERROR] $server $CMD завершився з помилкою (код $?)" >> "$log"
done
