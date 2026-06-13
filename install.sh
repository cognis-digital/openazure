#!/usr/bin/env bash
# openazure installer (macOS / Linux).
# Tries pipx, then uv, then pip --user, installing from git+https.
set -euo pipefail

REPO="git+https://github.com/cognis-digital/openazure.git"

echo "Installing openazure from ${REPO} ..."

if command -v pipx >/dev/null 2>&1; then
    echo "-> using pipx"
    pipx install "${REPO}"
elif command -v uv >/dev/null 2>&1; then
    echo "-> using uv tool install"
    uv tool install "${REPO}"
elif command -v python3 >/dev/null 2>&1; then
    echo "-> using pip (python3 -m pip --user)"
    python3 -m pip install --user "${REPO}"
elif command -v python >/dev/null 2>&1; then
    echo "-> using pip (python -m pip --user)"
    python -m pip install --user "${REPO}"
else
    echo "ERROR: need one of pipx, uv, or python3/python on PATH." >&2
    exit 1
fi

echo
echo "Done. Try:  openazure serve --in-memory"
