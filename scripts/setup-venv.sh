#!/usr/bin/env bash
# Reproducible backend Python env.
#
# Why uv and not Homebrew python + pip: on macOS 26, Homebrew's python@3.12
# ships a broken pyexpat (links a newer expat symbol than the system
# libexpat.1.dylib exports). That breaks plistlib (so platform.mac_ver()
# returns ''), which in turn crashes pip's vendored truststore. uv ships its
# own self-contained CPython, sidestepping all of it.
set -euo pipefail
cd "$(dirname "$0")/../backend"

if ! command -v uv >/dev/null 2>&1; then
  echo "installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

echo
echo "done. start the backend with:  backend/.venv/bin/python backend/server.py"
