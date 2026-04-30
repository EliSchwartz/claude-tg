#!/usr/bin/env bash
# Wrapper: run the protocol validation probe and print a report.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m claude_tg.probe "$@"
