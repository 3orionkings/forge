#!/bin/bash
# Portable Task 12 wrapper; all parsing and state logic is bounded Python stdlib.
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 2
cd "$SCRIPT_DIR" || exit 2
exec python3 -m forge_lib.liveness "$@"
