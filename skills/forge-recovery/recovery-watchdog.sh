#!/bin/zsh

# Forge recovery watchdog — detects a STUCK ORCHESTRATION and hands the unit to
# the peer for that role.
#
# Why this exists, and why it is not sol-liveness.sh:
# sol-liveness.sh answers "is this Codex PROCESS still working?" by the work-log
# clock and CPU. It is the right instrument for a hung build. It cannot see the
# 2026-07-28/29 failure, because in that failure nothing was hung. Claude/Opus was
# alive, honest, and busy for hours — reading lane reports, writing contracts,
# repairing the tracker — while the mission metric (units landed) stayed at ZERO.
# An activity-based instrument reports "healthy" through that entire failure. The
# owner detected it by manually asking for status, then manually routed the work to
# Sol, which then made progress. This tool automates exactly that judgement.
#
# It measures OUTCOMES, not processes:
#   REPEAT   — FAIL_N consecutive failed attempts under one owner, with NO objective
#              progress between them (progress resets the streak; two red checks
#              around a real improvement is not a stuck unit).
#   STALL    — no objective progress for STALL_T. DEFERRED while a process-liveness
#              `alive` receipt is fresh, so a legitimately long CPU-active gate is
#              not killed — that mistake is what duplicated a healthy lane on 28 Jul.
#   BLOCKED  — a precondition (guard, auth, dependency) has refused for ENV_MAX.
#   PINGPONG — every eligible owner has had a REAL tenure (>=1 attempt). Escalate.
#   BUDGET   — the RUN's recorded spend has reached RUN_CEILING. Escalate to the owner.
#
# ROLES ARE FIRST-CLASS (2026-07-29 review finding: "only the stuck ROLE transfers"
# was unenforceable prose). The ledger used to store nothing but owner/peer, so a
# handoff swapped the WHOLE unit — every role at once — while the skill claimed only
# one role moved. A rule an instrument cannot express is a rule that will be broken
# quietly. Each unit now carries three role slots (executor · decider · reviewer);
# `handoff --role R` moves exactly R to the other side and leaves the rest alone.
# The slots are VENDOR-NEUTRAL: either side may hold any role (ROUTING-POLICY §1).
# The one role rule that survives is not about vendor identity — whoever executes a
# unit does not commit it; the orchestrating decider does (ROUTING-POLICY §3).
#
# RUN-LEVEL BUDGET BREAKER (2026-07-29 review finding: per-unit caps do not bound a
# RUN). FAIL_N/HANDOFF_MAX bound one unit's attempts; nothing bounded the sequence.
# On 2026-07-28 twenty-three individually-plausible units burned ~88% of a fresh
# allowance and delivered zero product — no single unit was out of policy, the SUM
# was. `budget` records spend against RUN_CEILING and `check` refuses to green-light
# any further work once the ceiling is reached. It ESCALATES rather than degrading:
# ROUTING-POLICY §4 requires stopping and asking the owner at <=5% remaining,
# proposing one tier down, never silently spending the rest.
#
# FAIL-CLOSED, not fail-open (2026-07-29 review finding P0-1): an unwritable ledger,
# an unknown unit, a malformed flag, or a bad clock EXITS NON-ZERO. A watchdog that
# reports OK when its own storage is broken is worse than no watchdog.
#
# Self-attestation is bounded, not eliminated: `progress` and `alive` REQUIRE
# --evidence, which is recorded in the ledger for audit. A file cannot stop a model
# lying to it; it can make the lie inspectable by the peer and the owner.
#
# Every threshold and the clock have an environment override so the instrument is
# deterministic under test.

set -u
set -o pipefail

readonly SELF="${0:A}"

# Exit codes: 0 OK | 10 handoff | 20 escalate | 30 unknown/never-registered | 1 error
readonly RC_OK=0 RC_HANDOFF=10 RC_ESCALATE=20 RC_UNKNOWN=30

die() { print -r -- "recovery-watchdog: $1" >&2; exit 1; }

# Regex, not glob: `<->` and `#` need extended_glob, which is NOT set here — a glob
# predicate silently mismatched every input when first written.
is_uint() { [[ "$1" =~ '^[0-9]+$' ]] }

require_uint() {
  is_uint "$2" || die "$1 must be a non-negative integer, got: $2"
}

STATE_ROOT="${FORGE_RECOVERY_STATE:-${HOME}/.forge-recovery}"
FAIL_N="${FORGE_FAIL_N:-2}";           require_uint FORGE_FAIL_N "${FAIL_N}"
HANDOFF_MAX="${FORGE_HANDOFF_MAX:-2}"; require_uint FORGE_HANDOFF_MAX "${HANDOFF_MAX}"
STALL_T="${FORGE_STALL_T:-1200}";      require_uint FORGE_STALL_T "${STALL_T}"
ENV_MAX="${FORGE_ENV_MAX:-3600}";      require_uint FORGE_ENV_MAX "${ENV_MAX}"
ALIVE_TTL="${FORGE_ALIVE_TTL:-600}";   require_uint FORGE_ALIVE_TTL "${ALIVE_TTL}"
# Percent of one side's allowance this RUN may consume before it must stop and ask.
# 95 == "<=5% remaining", the ROUTING-POLICY §4 threshold. 0 disables the breaker.
RUN_CEILING="${FORGE_RUN_CEILING:-95}"; require_uint FORGE_RUN_CEILING "${RUN_CEILING}"

# Run-level rows live under a reserved unit id. `valid_id` forbids a leading
# underscore, so no real unit can ever collide with (or forge) a budget row.
readonly RUN_UNIT='__run__'

# The three role slots. Vendor-neutral by construction: this list names ROLES, and
# nowhere in this file is a role bound to a model id (ROUTING-POLICY §1).
valid_role() { [[ "$1" == "executor" || "$1" == "decider" || "$1" == "reviewer" ]] }

now_epoch() {
  local n
  if [[ -n "${FORGE_NOW:-}" ]]; then n="${FORGE_NOW}"; else n="$(date +%s)"; fi
  is_uint "${n}" || die "clock is not a positive integer epoch: ${n}"
  (( n > 0 )) || die "epoch must be > 0 (epoch 0 disables age checks)"
  print -r -- "${n}"
}

usage() {
  print -r -- 'Usage: recovery-watchdog.sh <command> [flags]'
  print -r -- ''
  print -r -- 'Commands:'
  print -r -- '  init     --run R --unit U --owner M --peer M2 [--role ROLE] [--objective TEXT]'
  print -r -- '  attempt  --run R --unit U --result pass|fail [--detail TEXT]'
  print -r -- '  progress --run R --unit U --evidence TEXT   Objective metric MOVED'
  print -r -- '  alive    --run R --unit U --evidence TEXT   sol-liveness said ALIVE'
  print -r -- '  block    --run R --unit U [--detail TEXT]   Precondition refused'
  print -r -- '  handoff  --run R --unit U [--role ROLE] [--to M] [--detail TEXT]'
  print -r -- '  budget   --run R --spend N [--side S] [--detail TEXT]   Run-level spend'
  print -r -- '  done     --run R --unit U [--detail TEXT]'
  print -r -- '  check    --run R --unit U'
  print -r -- '  status   --run R'
  print -r -- ''
  print -r -- 'ROLE: executor | decider | reviewer. Either side may hold any role.'
  print -r -- '  init  --role R gives R to --owner and the other two roles to --peer.'
  print -r -- '  handoff --role R moves ONLY R to the other side (default: the init role).'
  print -r -- ''
  print -r -- "check exit: ${RC_OK} OK | ${RC_HANDOFF} handoff | ${RC_ESCALATE} escalate | ${RC_UNKNOWN} unknown unit"
  print -r -- ''
  print -r -- 'Env: FORGE_RECOVERY_STATE FORGE_FAIL_N FORGE_HANDOFF_MAX FORGE_STALL_T'
  print -r -- '     FORGE_ENV_MAX FORGE_ALIVE_TTL FORGE_RUN_CEILING FORGE_NOW'
}

# ---------------------------------------------------------------- flag parsing
typeset CMD="${1:-}"
[[ -z "${CMD}" || "${CMD}" == "-h" || "${CMD}" == "--help" ]] && { usage; exit 0; }
shift

typeset RUN="" UNIT="" OWNER="" PEER="" RESULT="" DETAIL="" OBJECTIVE="" TO="" EVIDENCE=""
typeset ROLE="" SIDE="" SPEND=""
# A dangling value flag (`--run` with nothing after it) previously never shifted and
# spun forever. Every value flag now proves it has a value first.
need_val() { (( $# >= 2 )) || die "flag $1 requires a value"; }
while (( $# )); do
  case "$1" in
    --run)       need_val "$@"; RUN="$2";       shift 2 ;;
    --unit)      need_val "$@"; UNIT="$2";      shift 2 ;;
    --owner)     need_val "$@"; OWNER="$2";     shift 2 ;;
    --peer)      need_val "$@"; PEER="$2";      shift 2 ;;
    --to)        need_val "$@"; TO="$2";        shift 2 ;;
    --result)    need_val "$@"; RESULT="$2";    shift 2 ;;
    --detail)    need_val "$@"; DETAIL="$2";    shift 2 ;;
    --evidence)  need_val "$@"; EVIDENCE="$2";  shift 2 ;;
    --objective) need_val "$@"; OBJECTIVE="$2"; shift 2 ;;
    --role)      need_val "$@"; ROLE="$2";      shift 2 ;;
    --side)      need_val "$@"; SIDE="$2";      shift 2 ;;
    --spend)     need_val "$@"; SPEND="$2";     shift 2 ;;
    *) die "unknown flag: $1" ;;
  esac
done

# Identifiers become path and TSV components; constrain them rather than sanitize.
valid_id() { [[ "$1" =~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' ]] }
[[ -z "${RUN}" ]] && die "--run is required"
valid_id "${RUN}"  || die "--run must match [A-Za-z0-9][A-Za-z0-9._-]* (no slashes): ${RUN}"
[[ -n "${UNIT}"  ]] && { valid_id "${UNIT}"  || die "--unit must match [A-Za-z0-9][A-Za-z0-9._-]*: ${UNIT}" }
[[ -n "${OWNER}" ]] && { valid_id "${OWNER}" || die "--owner must match [A-Za-z0-9][A-Za-z0-9._-]*: ${OWNER}" }
[[ -n "${PEER}"  ]] && { valid_id "${PEER}"  || die "--peer must match [A-Za-z0-9][A-Za-z0-9._-]*: ${PEER}" }
[[ -n "${TO}"    ]] && { valid_id "${TO}"    || die "--to must match [A-Za-z0-9][A-Za-z0-9._-]*: ${TO}" }
[[ -n "${SIDE}"  ]] && { valid_id "${SIDE}"  || die "--side must match [A-Za-z0-9][A-Za-z0-9._-]*: ${SIDE}" }

# Fail LOUD on a flag the command will not act on. A silently-ignored --role would
# read as "only the reviewer moved" while the whole unit changed hands — precisely
# the unenforceable-prose failure this flag exists to end.
if [[ -n "${ROLE}" ]]; then
  valid_role "${ROLE}" || die "--role must be executor|decider|reviewer, got: ${ROLE}"
  [[ "${CMD}" == "init" || "${CMD}" == "handoff" ]] || die "--role applies only to init and handoff"
fi
if [[ -n "${SPEND}" || -n "${SIDE}" ]]; then
  [[ "${CMD}" == "budget" ]] || die "--spend/--side apply only to budget"
fi

typeset RUN_DIR="${STATE_ROOT}/${RUN}"
typeset LEDGER="${RUN_DIR}/ledger.tsv"
typeset LOCK="${RUN_DIR}/.lock"
mkdir -p "${RUN_DIR}" || die "cannot create state dir ${RUN_DIR}"

# Concurrency: mkdir is atomic on POSIX filesystems. Two lanes appending or two
# first-callers truncating the ledger previously raced (review finding P1-6).
lock_acquire() {
  local i=0
  while ! mkdir "${LOCK}" 2>/dev/null; do
    (( ++i > 100 )) && die "lock timeout on ${LOCK}"
    sleep 0.05
  done
}
lock_release() { rmdir "${LOCK}" 2>/dev/null || true }

# Tabs/newlines in free text would corrupt the TSV.
# printf '%s', never `print` — `print` appends a newline that `tr` then turns into a
# TRAILING SPACE, which silently broke every `$5=="fail"` comparison.
sanitize() { printf '%s' "${1}" | tr '\t\n' '  ' }

# Fail CLOSED on a write failure; an unwritable ledger must never read as healthy.
#
# Row shape (7 tab-separated fields, every event, no exceptions):
#   1 epoch · 2 unit · 3 event · 4 owner/holder/side · 5 result|objective|spend ·
#   6 detail|peer|prior-holder · 7 ROLE
# Field 7 is a dedicated column rather than a token smuggled into free text: role is
# the thing handoff must transfer precisely, and a value parsed out of a sanitized
# free-text blob is a value that can be spoofed by a --detail string.
append_locked() {
  local ev="$1" own="$2" res="$3" det="$4" extra="${5:-}" role="${6:-}"
  lock_acquire
  { [[ -e "${LEDGER}" ]] || : > "${LEDGER}" } || { lock_release; die "cannot create ${LEDGER}" }
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(now_epoch)" "${UNIT}" "${ev}" "${own}" "$(sanitize "${res}")" \
    "$(sanitize "${det}${extra}")" "${role}" >> "${LEDGER}" \
    || { lock_release; die "ledger append failed: ${LEDGER}" }
  lock_release
}

unit_rows() { [[ -e "${LEDGER}" ]] && awk -F'\t' -v u="${UNIT}" '$2==u' "${LEDGER}" || true }

# The unit's PRIMARY role — the one named at init, the role the unit's `attempt`
# rows are made under. Legacy ledgers written before roles existed have an empty
# field 7; they read as `executor`, which is what a bare owner/peer swap meant.
primary_role() {
  unit_rows | awk -F'\t' '$3=="init"{r=$7} END{print (r==""?"executor":r)}'
}

# owner/peer of the unit == holder/counterpart of the PRIMARY role. A handoff of a
# NON-primary role deliberately does not move these: that is the whole point of
# "only the stuck role transfers", and it is why REPEAT/PINGPONG/STALL below also
# ignore non-primary handoffs — otherwise a bookkeeping reviewer swap would reset
# the executor's failure streak and the stall clock for free.
current_roles() {
  unit_rows | awk -F'\t' '
    $3=="init"    { o=$4; p=$6; pr=($7==""?"executor":$7); next }
    $3=="handoff" { r=($7==""?pr:$7); if (r==pr) { o=$4; p=$6 } next }
    END { print o"\t"p }'
}

# The two sides in play, as declared at init. Handoff flips a role between them.
init_sides() { unit_rows | awk -F'\t' '$3=="init"{print $4"\t"$6; exit}' }

# Full role map: executor<TAB>decider<TAB>reviewer.
# init gives the named role to --owner and the other two to --peer, because that is
# the cross-model shape forge actually runs (one side builds, the other side decides
# and reviews). Nothing here consults a model id — either side may hold any role.
role_map() {
  unit_rows | awk -F'\t' '
    $3=="init" {
      A=$4; B=$6; pr=($7==""?"executor":$7)
      h["executor"]=B; h["decider"]=B; h["reviewer"]=B; h[pr]=A; next }
    $3=="handoff" { r=($7==""?pr:$7); h[r]=$4; next }
    END { printf "%s\t%s\t%s\n", h["executor"], h["decider"], h["reviewer"] }'
}

roles_str() {
  role_map | awk -F'\t' '{printf "executor:%s,decider:%s,reviewer:%s\n", $1, $2, $3}'
}

role_holder() {
  role_map | awk -F'\t' -v r="$1" \
    '{ print (r=="executor") ? $1 : ((r=="decider") ? $2 : $3) }'
}
last_event() { unit_rows | awk -F'\t' 'END{print $3}' }
has_init()   { unit_rows | awk -F'\t' '$3=="init"{f=1} END{exit f?0:1}' }

require_registered() {
  [[ -n "$(unit_rows)" ]] || die "unit ${UNIT} has no ledger entries; run init first"
  has_init || die "unit ${UNIT} has events but no init; refusing mixed state"
}
refuse_after_done() {
  [[ "$(last_event)" == "done" ]] && die "unit ${UNIT} is done; refusing post-terminal event"
}

json_escape() { print -r -- "${1}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' }

# ---------------------------------------------------------------- commands
case "${CMD}" in
  init)
    [[ -z "${UNIT}" || -z "${OWNER}" || -z "${PEER}" ]] && die "init needs --unit --owner --peer"
    [[ "${OWNER}" == "${PEER}" ]] && die "--owner and --peer must differ (no self-handoff)"
    [[ -n "$(unit_rows)" ]] && die "unit ${UNIT} already registered; refusing duplicate init"
    # No allow-list of model ids anywhere in this branch: ROUTING-POLICY §1 makes
    # routing an argued proposal, so any side may be registered in any role.
    typeset init_role="${ROLE:-executor}"
    lock_acquire
    { [[ -e "${LEDGER}" ]] || : > "${LEDGER}" } || { lock_release; die "cannot create ${LEDGER}" }
    printf '%s\t%s\tinit\t%s\t%s\t%s\t%s\n' \
      "$(now_epoch)" "${UNIT}" "${OWNER}" "$(sanitize "${OBJECTIVE}")" "${PEER}" "${init_role}" \
      >> "${LEDGER}" \
      || { lock_release; die "ledger append failed: ${LEDGER}" }
    lock_release
    print -r -- "INIT: unit=${UNIT} owner=${OWNER} peer=${PEER} role=${init_role} roles=$(roles_str)"
    ;;

  attempt)
    [[ -z "${UNIT}" ]] && die "attempt needs --unit"
    [[ "${RESULT}" != "pass" && "${RESULT}" != "fail" ]] && die "--result must be pass|fail"
    require_registered; refuse_after_done
    typeset owner_now; owner_now="$(current_roles)"; owner_now="${owner_now%%$'\t'*}"
    append_locked "attempt" "${owner_now}" "${RESULT}" "${DETAIL}"
    print -r -- "ATTEMPT: unit=${UNIT} owner=${owner_now} result=${RESULT}"
    ;;

  progress|alive)
    [[ -z "${UNIT}" ]] && die "${CMD} needs --unit"
    # Evidence is mandatory: an empty progress ping resetting the stall clock
    # forever is the exact "busy but zero landed" failure this tool exists to catch.
    [[ -z "${EVIDENCE}" ]] && die "${CMD} requires --evidence (what objectively moved / liveness output)"
    require_registered; refuse_after_done
    typeset owner_now; owner_now="$(current_roles)"; owner_now="${owner_now%%$'\t'*}"
    append_locked "${CMD}" "${owner_now}" "" "${EVIDENCE}"
    print -r -- "${CMD:u}: unit=${UNIT} owner=${owner_now}"
    ;;

  block|done)
    [[ -z "${UNIT}" ]] && die "${CMD} needs --unit"
    require_registered; refuse_after_done
    typeset owner_now; owner_now="$(current_roles)"; owner_now="${owner_now%%$'\t'*}"
    append_locked "${CMD}" "${owner_now}" "" "${DETAIL}"
    print -r -- "${CMD:u}: unit=${UNIT} owner=${owner_now}"
    ;;

  handoff)
    [[ -z "${UNIT}" ]] && die "handoff needs --unit"
    require_registered; refuse_after_done
    # ONE role moves. Default is the unit's primary role, which reproduces the old
    # whole-unit swap exactly; naming another role transfers only that slot and
    # leaves the rest of the map untouched.
    typeset move_role sides side_a side_b holder_now new_holder
    move_role="${ROLE:-$(primary_role)}"
    sides="$(init_sides)"
    side_a="${sides%%$'\t'*}"; side_b="${sides##*$'\t'}"
    [[ -z "${side_b}" ]] && die "no peer recorded; run init first"
    holder_now="$(role_holder "${move_role}")"
    [[ -z "${holder_now}" ]] && die "no holder recorded for role ${move_role}"
    if [[ -n "${TO}" ]]; then
      new_holder="${TO}"
    elif [[ "${holder_now}" == "${side_a}" ]]; then
      new_holder="${side_b}"
    else
      new_holder="${side_a}"
    fi
    [[ "${new_holder}" == "${holder_now}" ]] \
      && die "handoff target already holds role ${move_role}: ${holder_now}"
    lock_acquire
    printf '%s\t%s\thandoff\t%s\t%s\t%s\t%s\n' \
      "$(now_epoch)" "${UNIT}" "${new_holder}" "$(sanitize "${DETAIL}")" "${holder_now}" \
      "${move_role}" >> "${LEDGER}" \
      || { lock_release; die "ledger append failed: ${LEDGER}" }
    lock_release
    typeset post_exec post_dec note=""
    post_exec="$(role_holder executor)"; post_dec="$(role_holder decider)"
    # Vendor-neutral, and the only role rule left standing: the executor never
    # commits, the decider does. If the move collapsed both onto one side there is
    # nobody left to hold that separation — say so, in the handoff line the taker
    # reads, rather than letting it pass silently.
    [[ -n "${post_exec}" && "${post_exec}" == "${post_dec}" ]] \
      && note=" note=executor_equals_decider:re-seat_the_decider"
    print -r -- "HANDOFF: unit=${UNIT} role=${move_role} from=${holder_now} to=${new_holder} roles=$(roles_str)${note}"
    ;;

  budget)
    # Run-level, not unit-level: the 2026-07-28 overspend was a SUM of in-policy
    # units, so the ceiling has to live above the units that individually obeyed it.
    [[ -n "${UNIT}" ]] && die "budget is run-level; --unit is not accepted"
    [[ -z "${SPEND}" ]] && die "budget needs --spend N (percent of one side's allowance consumed)"
    require_uint --spend "${SPEND}"
    typeset spend_side="${SIDE:-run}"
    UNIT="${RUN_UNIT}"
    append_locked "budget" "${spend_side}" "${SPEND}" "${DETAIL}"
    typeset bsum
    bsum="$(awk -F'\t' -v s="${spend_side}" -v ru="${RUN_UNIT}" \
      '$2==ru && $3=="budget" && $4==s {n+=$5} END{print n+0}' "${LEDGER}")"
    print -r -- "BUDGET: run=${RUN} side=${spend_side} spend=+${SPEND} total=${bsum}/${RUN_CEILING}"
    ;;

  check)
    [[ -z "${UNIT}" ]] && die "check needs --unit"
    typeset NOW; NOW="$(now_epoch)"

    # BUDGET first — before the terminal check, deliberately. `check` is what
    # green-lights the NEXT move, and a finished unit answering "OK" is exactly how
    # a run marches from one in-policy unit to the next until ~88% of an allowance
    # is gone. Once the ceiling is reached every check says stop, whatever the unit's
    # own state. It escalates rather than degrading: ROUTING-POLICY §4 requires
    # asking the owner and proposing one tier down, never quietly spending the rest.
    typeset BUDGET_LINE BUDGET_SIDE
    typeset -i BUDGET_SPENT
    BUDGET_LINE="$([[ -e "${LEDGER}" ]] && awk -F'\t' -v ru="${RUN_UNIT}" '
      $2==ru && $3=="budget" { s[$4]+=$5 }
      END { m=0; w=""; for(k in s){ if(s[k]>m){ m=s[k]; w=k } } print m"\t"w }' \
      "${LEDGER}" 2>/dev/null || true)"
    [[ -z "${BUDGET_LINE}" ]] && BUDGET_LINE=$'0\t'
    BUDGET_SPENT="${BUDGET_LINE%%$'\t'*}"; BUDGET_SIDE="${BUDGET_LINE##*$'\t'}"
    if (( RUN_CEILING > 0 && BUDGET_SPENT >= RUN_CEILING )); then
      print -r -- "TRIP: BUDGET run=${RUN} side=${BUDGET_SIDE:-run} spent=${BUDGET_SPENT}/${RUN_CEILING} action=ESCALATE owner_ask_required propose_one_tier_down"
      exit ${RC_ESCALATE}
    fi

    typeset ROWS; ROWS="$(unit_rows)"
    # Unknown unit is UNKNOWN, never OK. A never-registered unit reading as healthy
    # is how a broken ledger looked green (review finding P0-1).
    if [[ -z "${ROWS}" ]]; then
      print -r -- "UNKNOWN: unit=${UNIT} never registered (or ledger unreadable)"
      exit ${RC_UNKNOWN}
    fi

    typeset roles owner_now peer_now PRIMARY ROLES_STR
    roles="$(current_roles)"
    owner_now="${roles%%$'\t'*}"; peer_now="${roles##*$'\t'}"
    # Name the role in every trip line: the taker has to know WHICH slot moves, or
    # "only the stuck role transfers" degrades back into a whole-unit swap.
    PRIMARY="$(primary_role)"; ROLES_STR="$(roles_str)"
    # The one role rule that is NOT about vendor identity survives the policy change:
    # whoever executes does not commit; the decider does (ROUTING-POLICY §3). A
    # handoff can legitimately land both roles on one side, and then that rule has
    # nobody to bind. Surface it — advisory, not a trip, because the correct fix is
    # to re-seat the decider, not to abort a unit that is otherwise healthy.
    typeset CONFLICT="" exec_h dec_h
    exec_h="$(role_holder executor)"; dec_h="$(role_holder decider)"
    [[ -n "${exec_h}" && "${exec_h}" == "${dec_h}" ]] \
      && CONFLICT=" role_conflict=executor_equals_decider"

    # Terminal state. Computed with awk's exit status — the old `| grep -q .` under
    # pipefail could return 141 (SIGPIPE) and silently fall through.
    if print -r -- "${ROWS}" | awk -F'\t' '$3=="done"{f=1} END{exit f?0:1}'; then
      print -r -- "OK: unit=${UNIT} done"; exit ${RC_OK}
    fi

    # PINGPONG — count only tenures that were REALLY worked (>=1 attempt), so two
    # bookkeeping handoffs with no work cannot fake "both models tried". Only
    # PRIMARY-role handoffs open a new tenure: swapping the reviewer does not give
    # the executor a fresh turn.
    typeset -i TENURES
    TENURES=$(print -r -- "${ROWS}" | awk -F'\t' '
      $3=="init"{ pr=($7==""?"executor":$7); if(worked){n++} worked=0; next }
      $3=="handoff"{ r=($7==""?pr:$7); if(r==pr){ if(worked){n++} worked=0 } next }
      $3=="attempt"{ worked=1 }
      END{ if(worked){n++} print n+0 }')
    if (( TENURES > HANDOFF_MAX )); then
      print -r -- "TRIP: PINGPONG unit=${UNIT} worked_tenures=${TENURES}/${HANDOFF_MAX} action=ESCALATE owner_ask_required"
      exit ${RC_ESCALATE}
    fi

    # REPEAT — consecutive failures under the current owner. init, a PRIMARY-role
    # handoff, a pass AND genuine progress all reset it: two red checks around a
    # real improvement is progress, not a stuck unit (review finding P1-5).
    # A non-primary handoff must NOT reset it — otherwise `handoff --role reviewer`
    # buys the same failing executor two more attempts for free.
    typeset -i FAILS
    FAILS=$(print -r -- "${ROWS}" | awk -F'\t' '
      $3=="init"{ pr=($7==""?"executor":$7); n=0; next }
      $3=="handoff"{ r=($7==""?pr:$7); if(r==pr) n=0; next }
      $3=="progress"||($3=="attempt"&&$5=="pass"){n=0; next}
      $3=="attempt"&&$5=="fail"{n++}
      END{print n+0}')
    if (( FAILS >= FAIL_N )); then
      print -r -- "TRIP: REPEAT unit=${UNIT} owner=${owner_now} consecutive_fails=${FAILS}/${FAIL_N} action=HANDOFF role=${PRIMARY} to=${peer_now}"
      exit ${RC_HANDOFF}
    fi

    # BLOCKED — an unbroken precondition-refusal streak older than ENV_MAX.
    typeset -i BLOCK_START
    BLOCK_START=$(print -r -- "${ROWS}" | awk -F'\t' '
      $3=="block"{ if(s==0) s=$1; next } { s=0 } END{print s+0}')
    if (( BLOCK_START > 0 && NOW - BLOCK_START >= ENV_MAX )); then
      print -r -- "TRIP: BLOCKED unit=${UNIT} owner=${owner_now} blocked_seconds=$(( NOW - BLOCK_START ))/${ENV_MAX} action=HANDOFF role=${PRIMARY} to=${peer_now} declare_block=true"
      exit ${RC_HANDOFF}
    fi

    # STALL — deferred while a fresh process-liveness receipt exists, so a long
    # CPU-active gate is not mistaken for a stalled orchestrator. That confusion is
    # what killed a healthy lane on 28 Jul.
    typeset -i LAST_ALIVE
    LAST_ALIVE=$(print -r -- "${ROWS}" | awk -F'\t' '$3=="alive"{t=$1} END{print t+0}')
    # Same role scoping as REPEAT: a reviewer swap is bookkeeping, not movement of
    # the objective metric, so it must not buy another STALL_T of silence.
    typeset -i LAST_MOVE
    LAST_MOVE=$(print -r -- "${ROWS}" | awk -F'\t' '
      $3=="init"{ pr=($7==""?"executor":$7); t=$1; next }
      $3=="handoff"{ r=($7==""?pr:$7); if(r==pr) t=$1; next }
      $3=="progress"||($3=="attempt"&&$5=="pass"){t=$1}
      END{print t+0}')
    if (( LAST_ALIVE > 0 && NOW - LAST_ALIVE < ALIVE_TTL )); then
      print -r -- "OK: unit=${UNIT} owner=${owner_now} roles=${ROLES_STR}${CONFLICT} stall_deferred_liveness_age=$(( NOW - LAST_ALIVE ))s/${ALIVE_TTL}s"
      exit ${RC_OK}
    fi
    if (( LAST_MOVE > 0 && NOW - LAST_MOVE >= STALL_T )); then
      print -r -- "TRIP: STALL unit=${UNIT} owner=${owner_now} no_objective_progress_seconds=$(( NOW - LAST_MOVE ))/${STALL_T} action=HANDOFF role=${PRIMARY} to=${peer_now}"
      exit ${RC_HANDOFF}
    fi

    print -r -- "OK: unit=${UNIT} owner=${owner_now} roles=${ROLES_STR}${CONFLICT} fails=${FAILS}/${FAIL_N} worked_tenures=${TENURES}/${HANDOFF_MAX} since_progress=$(( NOW - LAST_MOVE ))s/${STALL_T}s"
    exit ${RC_OK}
    ;;

  status)
    # Machine-readable progress the PEER or the OWNER reads without asking the stuck
    # model how it is doing — the automated form of the manual "status?" that caught
    # the 2026-07-29 stall.
    typeset NOW; NOW="$(now_epoch)"
    [[ -e "${LEDGER}" ]] || die "no ledger for run ${RUN}"
    # Reserved run-level rows are not units and must not be fed back into `check`
    # (their id is deliberately unrepresentable by valid_id, so check would die).
    typeset UNITS; UNITS=$(awk -F'\t' 'NF && $2 !~ /^__/ {print $2}' "${LEDGER}" | sort -u)
    typeset BLINE BSIDE; typeset -i BSPENT
    BLINE="$(awk -F'\t' -v ru="${RUN_UNIT}" '
      $2==ru && $3=="budget" { s[$4]+=$5 }
      END { m=0; w=""; for(k in s){ if(s[k]>m){ m=s[k]; w=k } } print m"\t"w }' "${LEDGER}")"
    BSPENT="${BLINE%%$'\t'*}"; BSIDE="${BLINE##*$'\t'}"
    print -r -- "{"
    print -r -- "  \"run\": \"$(json_escape "${RUN}")\","
    print -r -- "  \"generated_epoch\": ${NOW},"
    print -r -- "  \"thresholds\": {\"fail_n\": ${FAIL_N}, \"handoff_max\": ${HANDOFF_MAX}, \"stall_t\": ${STALL_T}, \"env_max\": ${ENV_MAX}, \"alive_ttl\": ${ALIVE_TTL}, \"run_ceiling\": ${RUN_CEILING}},"
    print -r -- "  \"budget\": {\"ceiling\": ${RUN_CEILING}, \"worst_side\": \"$(json_escape "${BSIDE:-run}")\", \"spent\": ${BSPENT}},"
    print -r -- "  \"units\": ["
    typeset first=1 u verdict uroles; typeset -i rc att
    for u in ${(f)UNITS}; do
      [[ -z "${u}" ]] && continue
      verdict="$("${SELF}" check --run "${RUN}" --unit "${u}" 2>&1)"; rc=$?
      att=$(awk -F'\t' -v x="${u}" '$2==x && $3=="attempt"' "${LEDGER}" | wc -l | tr -d ' ')
      uroles="$( UNIT="${u}"; roles_str )"   # subshell: zsh keeps prefix assignments
      (( first )) || print -r -- "    ,"
      first=0
      print -r -- "    {\"unit\": \"$(json_escape "${u}")\", \"roles\": \"$(json_escape "${uroles}")\", \"attempts\": ${att}, \"exit\": ${rc}, \"verdict\": \"$(json_escape "${verdict}")\"}"
    done
    print -r -- "  ]"
    print -r -- "}"
    ;;

  *) die "unknown command: ${CMD}" ;;
esac
