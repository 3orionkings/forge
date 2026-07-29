#!/bin/zsh

# Self-tests for recovery-watchdog.sh.
#
# Prove the instrument REFUSES, prove it still admits a healthy baseline, and prove
# it FAILS CLOSED when its own storage or inputs are broken. 2026-07-28 shipped two
# instruments that failed open — a "reading" label that meant "command not
# classified", and a stage classifier that called healthy lanes hung.
#
# Cases marked [R-n] were added from the 2026-07-29 adversarial review.

set -u
set -o pipefail

readonly TEST_DIR="${0:A:h}"
readonly WD="${TEST_DIR}/recovery-watchdog.sh"

typeset -i CASES=0
typeset -i FAILURES=0
typeset -a TMPDIRS=()

cleanup() { local d; for d in "${TMPDIRS[@]}"; do [[ -n "${d}" && -d "${d}" ]] && rm -rf "${d}"; done }
trap cleanup EXIT

fresh_state() {
  FORGE_RECOVERY_STATE="$(mktemp -d)"; TMPDIRS+=("${FORGE_RECOVERY_STATE}")
  export FORGE_RECOVERY_STATE
  export FORGE_FAIL_N=2 FORGE_HANDOFF_MAX=2 FORGE_STALL_T=1200 FORGE_ENV_MAX=3600 FORGE_ALIVE_TTL=600
  export FORGE_RUN_CEILING=95
  unset FORGE_NOW
}

# Setup commands must not silently no-op (review finding P1-8).
must() { "$@" >/dev/null || { print -r -- "SETUP FAILED: $*"; exit 1; } }

ok() { (( CASES++ )); print -r -- "pass: $1" }
bad() { (( CASES++ )); (( FAILURES++ )); print -r -- "FAIL: $1"; [[ -n "${2:-}" ]] && print -r -- "  $2" }

expect() {  # <name> <want-rc> <want-substring>  (runs `check`)
  local name="$1" want_rc="$2" want_sub="$3" out rc
  out="$("${WD}" check --run r --unit u 2>&1)"; rc=$?
  if [[ "${rc}" == "${want_rc}" && "${out}" == *"${want_sub}"* ]]; then ok "${name}"
  else bad "${name}" "want rc=${want_rc} sub='${want_sub}' | got rc=${rc} out='${out}'"; fi
}

expect_err() {  # <name> <command...>  — must exit non-zero
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then bad "${name}" "command unexpectedly succeeded: $*"; else ok "${name}"; fi
}

expect_sub() {  # <name> <want-substring>  — runs `check`, ignores rc
  local name="$1" want="$2" out
  out="$("${WD}" check --run r --unit u 2>&1)"
  if [[ "${out}" == *"${want}"* ]]; then ok "${name}"
  else bad "${name}" "want sub='${want}' | got '${out}'"; fi
}

expect_nosub() {  # <name> <forbidden-substring>
  local name="$1" nope="$2" out
  out="$("${WD}" check --run r --unit u 2>&1)"
  if [[ "${out}" != *"${nope}"* ]]; then ok "${name}"
  else bad "${name}" "forbidden sub='${nope}' present in '${out}'"; fi
}

# ------------------------------------------------------------------ baseline
fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol --objective "units landed"
expect "baseline healthy unit is OK" 0 "OK:"

# ------------------------------------------------------------------ REPEAT
fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" attempt --run r --unit u --result fail --detail "guard refused"
expect "one failure does not trip" 0 "OK:"
must "${WD}" attempt --run r --unit u --result fail --detail "same failure again"
expect "FAIL_N consecutive failures trip REPEAT -> handoff" 10 "TRIP: REPEAT"
[[ "$("${WD}" check --run r --unit u 2>&1)" == *"to=sol"* ]] \
  && ok "REPEAT names the peer as taker" || bad "REPEAT names the peer as taker"

fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" attempt --run r --unit u --result pass
must "${WD}" attempt --run r --unit u --result fail
expect "a pass resets the failure streak" 0 "OK:"

# [R-5] Two red checks around REAL progress is not a stuck unit.
fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" progress --run r --unit u --evidence "migration renumbered; 3 of 9 conflicts resolved"
must "${WD}" attempt --run r --unit u --result fail
expect "[R-5] objective progress between failures resets REPEAT" 0 "OK:"

fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" handoff --run r --unit u
expect "handoff resets the streak for the new owner" 0 "OK:"
[[ "$("${WD}" check --run r --unit u 2>&1)" == *"owner=sol"* ]] \
  && ok "handoff transfers ownership to the peer" || bad "handoff transfers ownership to the peer"

# ------------------------------------------------------------------ PINGPONG
# [R-5] Only tenures that were really worked count.
fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" handoff --run r --unit u
must "${WD}" handoff --run r --unit u
expect "[R-5] bookkeeping handoffs with no attempts do NOT fake PINGPONG" 0 "OK:"

fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" handoff --run r --unit u
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" handoff --run r --unit u
must "${WD}" attempt --run r --unit u --result fail
expect "both owners genuinely worked and failed -> ESCALATE" 20 "TRIP: PINGPONG"
[[ "$("${WD}" check --run r --unit u 2>&1)" == *"ESCALATE"* ]] \
  && ok "PINGPONG demands an owner ask" || bad "PINGPONG demands an owner ask"

# ------------------------------------------------------------------ STALL
fresh_state
export FORGE_NOW=1000000
must "${WD}" init --run r --unit u --owner claude --peer sol
export FORGE_NOW=1001199
expect "just under STALL_T does not trip" 0 "OK:"
export FORGE_NOW=1001200
expect "STALL_T with no objective progress trips -> handoff" 10 "TRIP: STALL"

fresh_state
export FORGE_NOW=1000000
must "${WD}" init --run r --unit u --owner claude --peer sol
export FORGE_NOW=1001100
must "${WD}" progress --run r --unit u --evidence "unit R1 landed at abc1234"
export FORGE_NOW=1001200
expect "objective progress resets the stall clock" 0 "OK:"

# The 2026-07-29 failure exactly: busy, honest, blocked — and delivering nothing.
fresh_state
export FORGE_NOW=1000000
must "${WD}" init --run r --unit u --owner claude --peer sol
export FORGE_NOW=1000600
must "${WD}" block --run r --unit u --detail "guard refused; wrote docs instead"
export FORGE_NOW=1001200
expect "busy-but-no-progress still trips STALL" 10 "TRIP: STALL"

# [R-5] A long CPU-active gate must NOT be killed for lack of "units landed".
fresh_state
export FORGE_NOW=1000000
must "${WD}" init --run r --unit u --owner sol --peer claude
export FORGE_NOW=1001000
must "${WD}" alive --run r --unit u --evidence "sol-liveness STATUS=ALIVE cpu=180% log+4KB"
export FORGE_NOW=1001300
expect "[R-5] fresh liveness receipt defers STALL for a long gate" 0 "stall_deferred"
export FORGE_NOW=1001700   # liveness receipt now older than ALIVE_TTL
expect "[R-5] a stale liveness receipt stops deferring" 10 "TRIP: STALL"

# ------------------------------------------------------------------ BLOCKED
fresh_state
export FORGE_NOW=1000000 FORGE_STALL_T=999999
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" block --run r --unit u --detail "load 253/10 cores"
export FORGE_NOW=1003599
expect "just under ENV_MAX does not trip BLOCKED" 0 "OK:"
export FORGE_NOW=1003600
expect "ENV_MAX of refusal trips BLOCKED -> handoff with block declared" 10 "TRIP: BLOCKED"
[[ "$("${WD}" check --run r --unit u 2>&1)" == *"declare_block=true"* ]] \
  && ok "BLOCKED tells the taker the block is environmental" || bad "BLOCKED tells the taker the block is environmental"

fresh_state
export FORGE_NOW=1000000 FORGE_STALL_T=999999
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" block --run r --unit u
export FORGE_NOW=1000100
must "${WD}" attempt --run r --unit u --result fail
export FORGE_NOW=1003600
expect "a broken block streak does not trip BLOCKED" 0 "OK:"

# ------------------------------------------------------------------ terminal
# [R-6] `grep -q` under pipefail could return 141 and drop through.
fresh_state
export FORGE_NOW=1000000
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" attempt --run r --unit u --result fail
must "${WD}" done --run r --unit u
expect "[R-6] a done unit never trips (no SIGPIPE fallthrough)" 0 "OK: unit=u done"
expect_err "[R-6] post-terminal events are refused" "${WD}" attempt --run r --unit u --result fail

# ------------------------------------------------------------------ fail-closed
# [R-1] The headline review finding: broken storage must not read as healthy.
fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
chmod 000 "${FORGE_RECOVERY_STATE}/r/ledger.tsv"
if [[ "$(id -u)" == "0" ]]; then
  ok "[R-1] (skipped as root: chmod cannot deny the owner)"
else
  typeset out rc; out="$("${WD}" check --run r --unit u 2>&1)"; rc=$?
  (( rc != 0 )) && ok "[R-1] an unreadable ledger fails closed (rc=${rc})" \
                || bad "[R-1] an unreadable ledger fails closed" "got rc=0 out='${out}'"
fi
chmod 644 "${FORGE_RECOVERY_STATE}/r/ledger.tsv"

fresh_state
typeset out rc; out="$("${WD}" check --run r --unit ghost 2>&1)"; rc=$?
(( rc == 30 )) && [[ "${out}" == *"UNKNOWN"* ]] \
  && ok "[R-1] an unregistered unit is UNKNOWN, never OK" \
  || bad "[R-1] an unregistered unit is UNKNOWN, never OK" "rc=${rc} out='${out}'"

expect_err "[R-1] events before init are refused" "${WD}" attempt --run r --unit u2 --result fail

fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
expect_err "[R-6] duplicate init is refused" "${WD}" init --run r --unit u --owner claude --peer sol
expect_err "[R-1] self-handoff (owner==peer) is refused" "${WD}" init --run r --unit u3 --owner sol --peer sol

# ------------------------------------------------------------------ input hardening
fresh_state
expect_err "[R-6] a dangling value flag errors instead of looping" "${WD}" check --run
expect_err "[R-6] a run id with a path component is refused" "${WD}" init --run ../escape --unit u --owner c --peer s
expect_err "[R-6] a non-numeric clock is refused" env FORGE_NOW=1+2 "${WD}" check --run r --unit u
expect_err "[R-6] epoch zero is refused" env FORGE_NOW=0 "${WD}" check --run r --unit u
expect_err "[R-6] a non-numeric threshold is refused" env FORGE_STALL_T=abc "${WD}" check --run r --unit u
must "${WD}" init --run r --unit u --owner claude --peer sol
expect_err "[R-1] progress without evidence is refused" "${WD}" progress --run r --unit u

# ------------------------------------------------------------------ ledger integrity
fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
must "${WD}" attempt --run r --unit u --result fail --detail "$(printf 'tab\there\nand newline')"
typeset -i LINES FIELDS
LINES=$(wc -l < "${FORGE_RECOVERY_STATE}/r/ledger.tsv" | tr -d ' ')
FIELDS=$(awk -F'\t' 'NF!=7{bad=1} END{print bad+0}' "${FORGE_RECOVERY_STATE}/r/ledger.tsv")
(( LINES == 2 && FIELDS == 0 )) \
  && ok "[R-8] tabs/newlines cannot add rows OR fields to the ledger" \
  || bad "[R-8] ledger integrity" "lines=${LINES} malformed=${FIELDS}"

# [R-6] Concurrent appends must all survive (mkdir lock).
fresh_state
must "${WD}" init --run r --unit u --owner claude --peer sol
for i in 1 2 3 4 5 6 7 8; do "${WD}" attempt --run r --unit u --result fail --detail "c${i}" >/dev/null & done
wait
typeset -i ROWS_N
ROWS_N=$(awk -F'\t' '$3=="attempt"' "${FORGE_RECOVERY_STATE}/r/ledger.tsv" | wc -l | tr -d ' ')
(( ROWS_N == 8 )) && ok "[R-6] 8 concurrent appends all land" \
                  || bad "[R-6] concurrent appends" "expected 8 attempt rows, got ${ROWS_N}"

# ------------------------------------------------------------------ status JSON
fresh_state
must "${WD}" init --run r --unit u1 --owner claude --peer sol
must "${WD}" init --run r --unit u2 --owner sol --peer claude
must "${WD}" attempt --run r --unit u2 --result fail --detail 'quote " and \ backslash'
typeset STATUS_OUT; STATUS_OUT="$("${WD}" status --run r 2>&1)"
if [[ "${STATUS_OUT}" == *'"u1"'* && "${STATUS_OUT}" == *'"u2"'* ]]; then
  if command -v python3 >/dev/null 2>&1; then
    if print -r -- "${STATUS_OUT}" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert len(d["units"])==2' 2>/dev/null; then
      ok "[R-8] status emits PARSEABLE JSON (validated, not substring-matched)"
    else
      bad "[R-8] status JSON does not parse" "${STATUS_OUT}"
    fi
  else
    ok "[R-8] status emits JSON for every unit (python3 absent; substring only)"
  fi
else
  bad "[R-8] status JSON" "${STATUS_OUT}"
fi

print -r -- ""
print -r -- "cases=${CASES} failures=${FAILURES}"
(( FAILURES == 0 )) || exit 1
