#!/bin/bash
# Provider-neutral liveness contract.  Runnable by macOS Bash 3.

set -u
set -o pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P) || exit 1
LIVE="$ROOT/scripts/forge-liveness.sh"
FIXTURE="$ROOT/tests/fixtures/liveness_fixture.py"
CASES=0
FAILURES=0
TMPDIRS=""
PIDS=""

cleanup() {
  for pid in $PIDS; do kill "$pid" >/dev/null 2>&1 || true; wait "$pid" >/dev/null 2>&1 || true; done
  old_ifs=$IFS; IFS=':'
  for directory in $TMPDIRS; do test -n "$directory" && test -d "$directory" && rm -rf -- "$directory"; done
  IFS=$old_ifs
}
trap cleanup EXIT HUP INT TERM

fresh() {
  DIR=$(mktemp -d "${TMPDIR:-/tmp}/forge-liveness-test.XXXXXX") || exit 1
  TMPDIRS="${TMPDIRS:+$TMPDIRS:}$DIR"
  STATE="$DIR/state.sqlite3"
}
spawn_live() { sleep 30 & PID=$!; PIDS="${PIDS:+$PIDS }$PID"; }
spawn_busy() { python3 -c 'while True: pass' & BUSY_PID=$!; PIDS="${PIDS:+$PIDS }$BUSY_PID"; }
make_fixture() {
  mode=$1
  shift
  META=$(python3 "$FIXTURE" --state "$STATE" --mode "$mode" "$@") || return 1
  JOB=$(printf '%s' "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
  ATTEMPT=$(printf '%s' "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin)["attempt_id"])')
  REVISION=$(printf '%s' "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
}
ok() { CASES=$((CASES + 1)); echo "pass: $1"; }
bad() { CASES=$((CASES + 1)); FAILURES=$((FAILURES + 1)); echo "FAIL: $1"; echo "  ${2:-}"; }
expect() {
  name=$1 want_rc=$2 want_status=$3
  shift 3
  out=$("$@" 2>&1); rc=$?
  if test "$rc" -eq "$want_rc" && printf '%s' "$out" | python3 -c \
    'import json,sys; d=json.load(sys.stdin); assert d["status"]==sys.argv[1]' "$want_status" >/dev/null 2>&1; then
    ok "$name"
  else
    bad "$name" "want rc=$want_rc status=$want_status; got rc=$rc out=$out"
  fi
}
check() { "$LIVE" --state "$STATE" --job "$JOB" --attempt "$ATTEMPT" --revision "$REVISION"; }

fresh
expect "missing state is unavailable" 2 UNAVAILABLE "$LIVE" --state "$STATE" --job x --attempt y --revision 1

fresh
printf 'not sqlite' >"$STATE"
expect "invalid state fails closed" 2 INVALID "$LIVE" --state "$STATE" --job x --attempt y --revision 1

fresh
spawn_live
make_fixture alive --pid "$PID"
expect "exact live identity is ALIVE" 0 ALIVE check

fresh
make_fixture stopped
expect "dead identity is STOPPED" 3 STOPPED check

fresh
spawn_live
make_fixture alive --pid "$PID"
python3 - "$STATE" "$ATTEMPT" <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
 db.execute("update attempts set provider_identity=? where id=?",("9"*64,sys.argv[2]))
PY
expect "PID reuse identity mismatch is STOPPED" 3 STOPPED check

fresh
spawn_live
make_fixture repeated --pid "$PID"
expect "repeated stderr outranks live CPU" 3 STALLED check

fresh
spawn_live
spawn_busy
make_fixture chatter --pid "$PID" --worker-pid "$BUSY_PID"
expect "exact busy worker CPU keeps an idle provider ALIVE" 0 ALIVE check

fresh
spawn_live
make_fixture objective --pid "$PID"
expect "current-attempt objective event is ALIVE" 0 ALIVE check

fresh
spawn_live
make_fixture stale_objective --pid "$PID"
expect "stale objective event cannot bypass STALL" 3 STALLED check

fresh
spawn_live
make_fixture repeated_then_objective --pid "$PID"
expect "newer objective progress clears older repeated stderr" 0 ALIVE check

fresh
spawn_live
make_fixture chatter --pid "$PID"
expect "provider chatter is not objective progress" 3 STALLED check

fresh
spawn_live
make_fixture pause_provider --pid "$PID"
expect "provider access pause exits four" 4 PAUSED_PROVIDER_ACCESS check

fresh
spawn_live
make_fixture pause_policy --pid "$PID"
expect "policy pause exits five" 5 PAUSED_POLICY check

fresh
spawn_live
make_fixture bad_pause_provider --pid "$PID"
expect "substituted provider-pause receipt is invalid" 2 INVALID check

fresh
make_fixture done
expect "verified terminal receipt is DONE" 0 DONE check

fresh
make_fixture done_pause
expect "terminal receipt outranks stale pause evidence" 0 DONE check

fresh
make_fixture bad_receipt
expect "substituted terminal receipt is INVALID" 2 INVALID check

fresh
spawn_live
make_fixture alive --pid "$PID"
expect "stale revision is invalid" 2 INVALID "$LIVE" --state "$STATE" --job "$JOB" --attempt "$ATTEMPT" --revision 99
expect "stale attempt is invalid" 2 INVALID "$LIVE" --state "$STATE" --job "$JOB" --attempt wrong --revision "$REVISION"
expect "unknown argument is invalid" 2 INVALID "$LIVE" --state "$STATE" --job "$JOB" --attempt "$ATTEMPT" --revision "$REVISION" --signal TERM

fresh
spawn_live
make_fixture alive --pid "$PID"
ln -s "$DIR" "$DIR/alias"
expect "state path with a symlink ancestor is invalid" 2 INVALID "$LIVE" --state "$DIR/alias/state.sqlite3" --job "$JOB" --attempt "$ATTEMPT" --revision "$REVISION"

echo
echo "cases=$CASES failures=$FAILURES"
test "$FAILURES" -eq 0
