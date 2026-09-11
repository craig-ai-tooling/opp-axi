#!/usr/bin/env bash
# Install the latest opp-axi release.
#
# The artifact is a zipapp, not a static binary: it needs python3 >= 3.10 on the
# target machine.
set -euo pipefail

REPO="craig-ai-tooling/opp-axi"
BIN="${BIN:-$HOME/.local/bin/opp-axi}"
URL="https://github.com/${REPO}/releases/latest/download/opp-axi.pyz"

if ! command -v python3 >/dev/null 2>&1; then
  echo "need python3 >= 3.10 on PATH — the artifact is a zipapp, not a static binary" >&2
  exit 1
fi

mkdir -p "$(dirname "$BIN")"
curl -fsSL "$URL" -o "$BIN"
chmod +x "$BIN"
"$BIN" --help >/dev/null

echo "installed $BIN"
echo
echo "next: $BIN doctor    # says exactly what still needs configuring"
