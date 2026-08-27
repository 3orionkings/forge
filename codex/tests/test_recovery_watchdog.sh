#!/bin/bash
# Shipping contract: every recovery-watchdog entrypoint refuses without effects.

set -u
set -o pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P) || exit 1
WD="$ROOT/scripts/recovery-watchdog.sh"
PY_ENTRY="$ROOT/scripts/forge_lib/recovery.py"
REAL_PYTHON=$(command -v python3) || {
  echo "test_recovery_watchdog: python3 is required" >&2
  exit 1
}
DISABLED="Automated Forge recovery is disabled in this build"
CASES=0
FAILURES=0
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/forge-recovery-disabled.XXXXXX") || exit 1

cleanup() {
  test ! -d "$TEMP_ROOT" || rm -rf -- "$TEMP_ROOT"
}
trap cleanup EXIT HUP INT TERM

ok() {
  CASES=$((CASES + 1))
  echo "pass: $1"
}

bad() {
  CASES=$((CASES + 1))
  FAILURES=$((FAILURES + 1))
  echo "FAIL: $1"
  test -z "${2:-}" || echo "  $2"
}

fake_bin="$TEMP_ROOT/bin"
mkdir "$fake_bin"
python_marker="$TEMP_ROOT/python-invoked"
python_fake="$fake_bin/python3"
printf '%s\n' '#!/bin/sh' \
  'printf invoked >"$FORGE_RECOVERY_PYTHON_MARKER"' \
  'exit 97' >"$python_fake"
chmod 700 "$python_fake"

state_root="$TEMP_ROOT/state-must-not-exist"
stdout_path="$TEMP_ROOT/stdout"
stderr_path="$TEMP_ROOT/stderr"
PATH="$fake_bin:/usr/bin:/bin" FORGE_RECOVERY_PYTHON_MARKER="$python_marker" \
  /bin/bash "$WD" init --state "$state_root" --run r --unit u \
  --owner claude --peer sol --integration-authority codex \
  >"$stdout_path" 2>"$stderr_path"
rc=$?
if test "$rc" -eq 1 \
  && test ! -s "$stdout_path" \
  && grep -F "$DISABLED" "$stderr_path" >/dev/null 2>&1 \
  && test ! -e "$python_marker" \
  && test ! -e "$state_root"; then
  ok "shell entrypoint refuses before Python or state"
else
  bad "shell entrypoint refuses before Python or state" \
    "rc=$rc stdout=$(cat "$stdout_path") stderr=$(cat "$stderr_path")"
fi

python_state="$TEMP_ROOT/python-state-must-not-exist"
"$REAL_PYTHON" "$PY_ENTRY" status --state "$python_state" --run r \
  >"$stdout_path" 2>"$stderr_path"
rc=$?
if test "$rc" -eq 1 \
  && test ! -s "$stdout_path" \
  && grep -F "$DISABLED" "$stderr_path" >/dev/null 2>&1 \
  && test ! -e "$python_state"; then
  ok "Python entrypoint refuses before state"
else
  bad "Python entrypoint refuses before state" \
    "rc=$rc stdout=$(cat "$stdout_path") stderr=$(cat "$stderr_path")"
fi

if ROOT="$ROOT" "$REAL_PYTHON" - <<'PY'
import os
import sys

sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
from forge_lib import recovery

def forbidden(*_args, **_kwargs):
    raise AssertionError("disabled recovery reached parser or state")

recovery._parser = forbidden
recovery.Store = forbidden
try:
    recovery.run(["init", "--state", "/must/not/exist"])
except recovery.WatchdogError as error:
    assert "Automated Forge recovery is disabled in this build" in str(error)
else:
    raise AssertionError("disabled recovery returned successfully")
PY
then
  ok "programmatic run refuses before parser or Store"
else
  bad "programmatic run refuses before parser or Store"
fi

if ! grep -F "exec python3" "$WD" >/dev/null 2>&1 \
  && grep -F "$DISABLED" "$WD" >/dev/null 2>&1; then
  ok "wrapper contains only the fail-closed shipping route"
else
  bad "wrapper contains only the fail-closed shipping route"
fi

if test "$FAILURES" -ne 0; then
  echo "FAIL: $FAILURES of $CASES recovery-disabled cases"
  exit 1
fi

echo "PASS: $CASES recovery-disabled cases"
