#!/usr/bin/env bash
# Live per-region dashboard. Updates in place once per second.
# Usage: ./scripts/watch-regions.sh [--interval SECS]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

exec .venv/bin/python -m rideco.sim.watch_regions "$@"
