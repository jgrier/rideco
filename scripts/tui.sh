#!/usr/bin/env bash
# Launch the RideCo TUI.
# Usage: ./scripts/tui.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

exec .venv/bin/python -m rideco.tui.app "$@"
