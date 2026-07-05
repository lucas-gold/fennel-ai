#!/usr/bin/env bash
# Create the backend Python environment.
# uv rather than Homebrew python: Homebrew's python@3.12 on macOS 26 has a
# broken pyexpat, which takes plistlib and pip's truststore down with it.
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
