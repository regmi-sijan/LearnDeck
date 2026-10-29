#!/usr/bin/env bash
# One-step start for macOS / Linux: creates a virtual environment, installs dependencies, runs LearnDeck.
#   ./start.sh            (first run takes a minute to install)
#   ./start.sh --port 9000 --no-browser
set -e
cd "$(dirname "$0")"

# Find a Python that is 3.9 or newer (macOS's built-in python3 can be old).
PY=""
for cand in ${PYTHON:-} python3.13 python3.12 python3.11 python3.10 python3 python3.9; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "LearnDeck needs Python 3.9 or newer."
  echo "Install it from https://www.python.org/downloads/ or with Homebrew:  brew install python@3.12"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating a virtual environment with $($PY --version)…"
  "$PY" -m venv .venv
fi

# Install (or update) dependencies only when requirements.txt changed.
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing dependencies…"
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.installed
fi

[ -f .env ] || cp .env.example .env

exec .venv/bin/python run.py "$@"
