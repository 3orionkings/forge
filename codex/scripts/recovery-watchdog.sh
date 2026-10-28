#!/bin/bash
# Fail-closed compatibility entry point for the paused Forge recovery subsystem.
# The hardened implementation is preserved on its recorded WIP branch; shipped
# bytes must not start Python, open recovery state, or perform a handoff.

set -u

echo "recovery-watchdog: Automated Forge recovery is disabled in this build; existing work and state were left unchanged. Use a manual handoff." >&2
exit 1
