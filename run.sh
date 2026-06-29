#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ] || [ -x "$HOME/.cargo/bin/uv" ]; then
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "curl or wget is required to install uv automatically."
  fi
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating local app environment..."
  if command -v uv >/dev/null 2>&1; then
    if ! uv venv --managed-python --python 3.14.6 .venv; then
      uv venv --managed-python --python 3.14 .venv
    fi
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m venv .venv
  elif command -v python >/dev/null 2>&1; then
    python -m venv .venv
  else
    echo "Python is required to run PyManager."
    exit 1
  fi
fi

echo "Installing/updating requirements..."
if command -v uv >/dev/null 2>&1; then
  uv pip install --python ".venv/bin/python" -r requirements.txt
else
  ".venv/bin/python" -m pip install -r requirements.txt
fi

echo
if [ -n "${PYMANAGER_PORT:-}" ]; then
  echo " PyManager starting at http://localhost:${PYMANAGER_PORT}"
else
  echo " PyManager starting. The app will print its local URL."
fi
echo " Press Ctrl+C to stop."
echo
".venv/bin/python" app.py
