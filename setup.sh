#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
SKIP_CONFIGURE="${1:-}"

"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  printf 'ffmpeg and ffprobe are required for complete video support.\n' >&2
fi

if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  chmod 600 config.json 2>/dev/null || true
fi

if [[ "$SKIP_CONFIGURE" != "--skip-configure" ]]; then
  .venv/bin/python setup_wizard.py
fi

printf '\nSetup complete.\n'
printf 'Validate: .venv/bin/python scripts/doctor.py\n'
printf 'Start:    ./start.sh\n'
