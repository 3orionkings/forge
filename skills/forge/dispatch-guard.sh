#!/bin/zsh

# Forge dispatch admission guard.
#
# Why lane-weighted machine admission exists:
# On 2026-07-28, 23 concurrent lanes on a 10-core Mac drove load to 507 and
# exhausted swap to 0.93 GB free. Reading, patching, typechecking, linting,
# testing, and builds all consume CPU and memory; build phases do not overlap
# freely. At the same time, this machine's desktop baseline alone can sit near
# load 34 on 10 cores with zero lanes (Chrome ~2.9 GB, Spotlight mds_stores
# ~2.3 GB, Dropbox, and Claude.app ~1 GB). One absolute "quiet machine"
# threshold would refuse every lane forever. That is not admission discipline;
# it is a broken instrument. These thresholds measure headroom for this lane:
# HEAVY defaults to load <= 1x cores and swap headroom >= 4 GB. When macOS load
# exceeds that ceiling, HEAVY may still enter only if CPU idle and memory-free
# headroom are both >=50%; this distinguishes I/O-inflated load from real compute
# or memory pressure. LIGHT defaults to load <= 4x cores and swap headroom >=1 GB.
# Every probe and threshold has an environment override for deterministic tests.
#
# "Swap headroom" means room for swap to GROW, not free space inside swap. See
# probe_free_swap_gb for why the obvious macOS reading is the wrong instrument.

set -u
set -o pipefail

typeset -r GUARD_SELF="${0:A}"
typeset -r FORGE_DIR="${GUARD_SELF:h}"
typeset -r FORGE_SKILL="${FORGE_DIR}/SKILL.md"
typeset -r FORGE_ENGINE="${FORGE_DIR}/ENGINE.md"
typeset -r ROUTING_POLICY="${FORGE_DIR}/ROUTING-POLICY.md"
typeset -r GUARD_TESTS="${FORGE_DIR}/dispatch-guard.test.sh"
typeset -r SOL_LIVENESS="${FORGE_DIR}/sol-liveness.sh"
typeset -r RECOVERY_DIR="${FORGE_DIR:h}/forge-recovery"
typeset -r WATCHDOG="${RECOVERY_DIR}/recovery-watchdog.sh"
typeset -r WATCHDOG_TESTS="${RECOVERY_DIR}/recovery-watchdog.test.sh"

usage() {
  print -r -- 'Usage: dispatch-guard.sh [declaration flags]'
  print -r -- ''
  print -r -- 'Required flags:'
  print -r -- '  --verdict APPROVED|REVISE      Required, and must be APPROVED, for PRODUCT'
  print -r -- '                                 lanes (feature/integration/recovery).'
  print -r -- '                                 Unconstrained (and optional) for lanes that'
  print -r -- '                                 write no product code: control-plane, review.'
  print -r -- '  --active-lanes N                Active lanes before this admission'
  print -r -- '  --heavy-leases N               Active HEAVY/gate leases before admission'
  print -r -- '  --weight LIGHT|HEAVY'
  print -r -- '  --lane-type feature|integration|recovery|control-plane|review'
  print -r -- '  --mode normal|recovery'
  print -r -- '  --worktree ABSOLUTE_PATH'
  print -r -- '  --clean-tree true|false         Product lanes are checked against Git'
  print -r -- '  --builder ID                    Any model may build any lane; identity is'
  print -r -- '                                 still required for receipts and role checks'
  print -r -- '  --executor-policy POLICY        any (default), codex-only, or claude-only;'
  print -r -- '                                 must equal the project overlay declaration.'
  print -r -- '                                 Applies only'
  print -r -- '                                 to product executors, never review lanes.'
  print -r -- '  --proof-scope targeted|broad    targeted = an explicitly narrow named'
  print -r -- '                                 test/file/feature proof: LIGHT, may run in'
  print -r -- '                                 any admitted lane. broad = full suite, gate,'
  print -r -- '                                 typecheck, production build, coverage or db'
  print -r -- '                                 rebuild: HEAVY, one at a time. DEFAULTS broad'
  print -r -- '  --execution-profile sprint|assured   Cadence profile; DEFAULTS TO assured'
  print -r -- '                                 when omitted (fail safe, never fail cheap)'
  print -r -- '  --admission-action launch|expand     launch = a new lane (default);'
  print -r -- '                                 expand = guarded re-admission of an ALREADY'
  print -r -- '                                 active writer with a widened file set'
  print -r -- '  --owns-heavy-lease true|false Expansion only: whether this lane owns the'
  print -r -- '                                 one active HEAVY lease (default false)'
  print -r -- '  --base-sha FULL_IMMUTABLE_SHA   Exactly 40 or 64 hexadecimal characters'
  print -r -- '  --plan-file ABSOLUTE_PATH       Product lanes: governing approved plan'
  print -r -- '  --act2-review-log ABSOLUTE_PATH Product lanes: immutable Act-2 log'
  print -r -- '  --execution-contract ABS_PATH   Product lanes: finalized unit contract'
  print -r -- '  --project-overlay ABS_PATH      Product lanes: required; contains exactly'
  print -r -- '                                 one executor_policy=<policy> declaration'
  print -r -- '  --freeze-manifest ABSOLUTE_PATH Product lanes: separate immutable SHA-256'
  print -r -- '                                 manifest for the approved control plane'
  print -r -- '  --freeze-manifest-sha256 SHA256 Product lanes: current manifest hash'
  print -r -- '  --oracle-exact true|false       Product lanes: whether the oracle requires'
  print -r -- '                                 byte-exact output from the executor tool'
  print -r -- '  --capability-receipt PATH       Required when --oracle-exact true'
  print -r -- '  --capability-receipt-sha256 SHA Required when --oracle-exact true'
  print -r -- '  --backlog-id ID                 Repeat for every governing backlog item'
  print -r -- '  --file REPO_RELATIVE_FILE       Repeat for the exact declared file set'
  print -r -- '  --proof-command COMMAND'
  print -r -- '  --work-log ABSOLUTE_PATH'
  print -r -- '  --report ABSOLUTE_PATH'
  print -r -- '  --predecessor none|UNIT_ID'
  print -r -- '  --instruction TEXT              Build instruction inspected for commit/merge'
  print -r -- '  --unmerged-branches N'
  print -r -- ''
  print -r -- 'Current-state declaration flags (repeatable where noted):'
  print -r -- '  --active-writer-worktree PATH   Repeat for each active writer'
  print -r -- '  --parallel-file FILE            Repeat for the union of active lane file sets'
  print -r -- '  --current-file FILE             Expansion only: repeat for every path in this'
  print -r -- '                                 lane current declared-set snapshot; the new'
  print -r -- '                                 --file set must be a superset'
  print -r -- ''
  print -r -- 'Required only when --predecessor is not none:'
  print -r -- '  --predecessor-builder ID'
  print -r -- '  --report-receipt KEY=VALUE;...'
  print -r -- '  --review-receipt KEY=VALUE;...'
  print -r -- ''
  print -r -- 'Report receipt keys: predecessor, consumed_by, report, receipt_id'
  print -r -- 'Review receipt keys: predecessor, builder, reviewer, thread, verdict, receipt_id'
  print -r -- ''
  print -r -- 'Machine metric overrides:'
  print -r -- '  FORGE_GUARD_LOAD_1'
  print -r -- '  FORGE_GUARD_FREE_SWAP_GB   Swap HEADROOM in GB (room to grow),'
  print -r -- '                             not free space inside existing swap'
  print -r -- '  FORGE_GUARD_CORE_COUNT'
  print -r -- '  FORGE_GUARD_CPU_IDLE_PCT'
  print -r -- '  FORGE_GUARD_MEMORY_FREE_PCT'
  print -r -- 'Threshold overrides (these are MINIMUMS a machine must clear;'
  print -r -- 'raising them makes the guard stricter, not more permissive):'
  print -r -- '  FORGE_GUARD_HEAVY_MAX_LOAD_PER_CORE (default 1)'
  print -r -- '  FORGE_GUARD_HEAVY_MIN_SWAP_GB       (default 4)'
  print -r -- '  FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT    (default 50)'
  print -r -- '  FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT (default 50)'
  print -r -- '  FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT    (default 50)'
  print -r -- '  FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT (default 50)'
  print -r -- '  FORGE_GUARD_LIGHT_MAX_LOAD_PER_CORE (default 4)'
  print -r -- '  FORGE_GUARD_LIGHT_MIN_SWAP_GB       (default 1)'
}

reject() {
  local code="$1"
  shift
  print -u2 -r -- "REJECT: ${code}: $*"
  exit 1
}

require_value() {
  local option="$1"
  local remaining="$2"
  (( remaining >= 2 )) || reject "ARGUMENT_VALUE" "${option} requires a value"
}

is_nonnegative_integer() {
  print -r -- "$1" | grep -Eq '^[0-9]+$'
}

is_positive_number() {
  print -r -- "$1" | grep -Eq '^([0-9]+([.][0-9]*)?|[.][0-9]+)$' &&
    awk -v value="$1" 'BEGIN { exit !(value > 0) }'
}

is_nonnegative_number() {
  print -r -- "$1" | grep -Eq '^([0-9]+([.][0-9]*)?|[.][0-9]+)$'
}

is_percentage() {
  is_nonnegative_number "$1" &&
    awk -v value="$1" 'BEGIN { exit !(value <= 100) }'
}

float_gt() {
  awk -v left="$1" -v right="$2" 'BEGIN { exit !(left > right) }'
}

float_lt() {
  awk -v left="$1" -v right="$2" 'BEGIN { exit !(left < right) }'
}

multiply() {
  awk -v left="$1" -v right="$2" 'BEGIN { printf "%.6f", left * right }'
}

normalize_worktree() {
  local value="$1"
  while [[ "${value}" != "/" && "${value}" == */ ]]; do
    value="${value%/}"
  done
  print -r -- "${value}"
}

sha256_file() {
  local file_path="$1"
  local output
  if command -v sha256sum >/dev/null 2>&1; then
    output="$(sha256sum -- "${file_path}" 2>/dev/null)" || return 1
    print -r -- "${output%%[[:space:]]*}"
    return 0
  fi
  if command -v shasum >/dev/null 2>&1; then
    output="$(shasum -a 256 -- "${file_path}" 2>/dev/null)" || return 1
    print -r -- "${output%%[[:space:]]*}"
    return 0
  fi
  return 1
}

verify_sha256_manifest() {
  local manifest="$1"
  local manifest_dir="${manifest:h}"
  local manifest_name="${manifest:t}"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "${manifest_dir}" && sha256sum -c -- "${manifest_name}" >/dev/null 2>&1)
    return $?
  fi
  if command -v shasum >/dev/null 2>&1; then
    (cd "${manifest_dir}" && shasum -a 256 -c -- "${manifest_name}" >/dev/null 2>&1)
    return $?
  fi
  return 1
}

manifest_hash_for_path() {
  local manifest="$1"
  local wanted="$2"
  awk -v wanted="${wanted}" '
    {
      hash = substr($0, 1, 64)
      separator = substr($0, 65, 2)
      path = substr($0, 67)
      if (length(hash) == 64 && hash ~ /^[0-9A-Fa-f]+$/ &&
          (separator == "  " || separator == " *") && path == wanted) {
        value = hash
        count += 1
      }
    }
    END {
      if (count == 1) print value
      else exit 1
    }
  ' "${manifest}"
}

manifest_path_count() {
  local manifest="$1"
  local wanted="$2"
  awk -v wanted="${wanted}" '
    {
      hash = substr($0, 1, 64)
      separator = substr($0, 65, 2)
      path = substr($0, 67)
      if (length(hash) == 64 && hash ~ /^[0-9A-Fa-f]+$/ &&
          (separator == "  " || separator == " *") && path == wanted) count += 1
    }
    END { print count + 0 }
  ' "${manifest}"
}

line_value() {
  local file="$1"
  local wanted="$2"
  awk -v wanted="${wanted}" '
    index($0, "=") > 0 {
      key = substr($0, 1, index($0, "=") - 1)
      if (key == wanted) {
        print substr($0, index($0, "=") + 1)
        count += 1
      }
    }
    END { if (count != 1) exit 1 }
  ' "${file}"
}

file_path_is_exact() {
  local value="$1"
  [[ -n "${value}" ]] || return 1
  [[ "${value}" != /* ]] || return 1
  [[ "${value}" != ./* ]] || return 1
  [[ "${value}" != ../* && "${value}" != ".." ]] || return 1
  [[ "${value}" != */../* && "${value}" != */.. ]] || return 1
  [[ "${value}" != */./* && "${value}" != */. ]] || return 1
  [[ "${value}" != *//* && "${value}" != */ ]] || return 1
  case "${value}" in
    *'*'*|*'?'*) return 1 ;;
  esac
  # Brackets are legal literal path characters (Next.js dynamic segments like
  # src/app/matters/[id]/... — including files the lane will CREATE, so no
  # existence check). This is safe because the guard never shell-globs declared
  # files: they are compared as opaque exact strings for dedup, overlap and
  # HANDOFF checks. Only * and ? are rejected above.
  return 0
}

model_family() {
  local value="${(L)1}"
  case "${value}" in
    *claude*|*opus*|*fable*) print -r -- "claude" ;;
    *codex*|*sol*|*terra*|gpt-5.*) print -r -- "codex" ;;
    *) print -r -- "unknown" ;;
  esac
}

# is_codex_sol_builder() was deleted on 2026-07-29 together with the builder vendor
# lock it served (see the --builder section below). It allow-listed exactly one
# vendor's names, so keeping it would have left a loaded gun in the file for the next
# repair session to re-wire. model_family() below is the survivor: it does not gate
# WHO may build, only whether a predecessor's builder and reviewer were genuinely
# different families, which is a real cross-model-review property.

receipt_value() {
  local receipt="$1"
  local wanted="$2"
  local pair
  local -a pairs
  pairs=("${(@s:;:)receipt}")
  for pair in "${pairs[@]}"; do
    if [[ "${pair%%=*}" == "${wanted}" ]]; then
      print -r -- "${pair#*=}"
      return 0
    fi
  done
  return 1
}

probe_core_count() {
  if [[ -n "${FORGE_GUARD_CORE_COUNT:-}" ]]; then
    print -r -- "${FORGE_GUARD_CORE_COUNT}"
    return 0
  fi
  if command -v sysctl >/dev/null 2>&1; then
    sysctl -n hw.ncpu 2>/dev/null && return 0
  fi
  if command -v getconf >/dev/null 2>&1; then
    getconf _NPROCESSORS_ONLN 2>/dev/null && return 0
  fi
  return 1
}

probe_load_1() {
  local raw
  if [[ -n "${FORGE_GUARD_LOAD_1:-}" ]]; then
    print -r -- "${FORGE_GUARD_LOAD_1}"
    return 0
  fi
  if command -v sysctl >/dev/null 2>&1; then
    raw="$(sysctl -n vm.loadavg 2>/dev/null)" || raw=""
    if [[ -n "${raw}" ]]; then
      print -r -- "${raw}" | awk '{ gsub(/[{}]/, ""); print $1 }'
      return 0
    fi
  fi
  if [[ -r /proc/loadavg ]]; then
    awk '{ print $1 }' /proc/loadavg
    return 0
  fi
  return 1
}

# Swap headroom probe.
#
# This used to read `sysctl vm.swapusage` free, which is NOT headroom on macOS.
# macOS has no fixed swap area: the kernel allocates and reclaims 1 GB swapfiles
# in /System/Volumes/VM on demand, so "free" reports only the slack inside the
# files that happen to exist this second. It sits near zero on a perfectly
# healthy machine — that is the normal state immediately before the kernel
# allocates one more file. Gating on it is a Linux-shaped instrument on a
# dynamic-swap OS. On 2026-07-29 it refused lanes on a 64 GB machine at normal
# memory pressure with 814 GB of free disk, reporting 1.24 GB "free swap"; the
# owner correctly read that as an artificial constraint.
#
# The true ceiling on macOS swap is free space on the volume holding the
# swapfiles, so that is what this probes. Linux, where swap really is a fixed
# area with a hard ceiling, still reads SwapFree.
probe_free_swap_gb() {
  local avail_kb
  local vm_dir
  if [[ -n "${FORGE_GUARD_FREE_SWAP_GB:-}" ]]; then
    print -r -- "${FORGE_GUARD_FREE_SWAP_GB}"
    return 0
  fi
  if [[ -r /proc/meminfo ]]; then
    awk '/^SwapFree:/ { printf "%.6f", $2 / 1048576; found = 1 } END { exit !found }' \
      /proc/meminfo
    return $?
  fi
  if command -v df >/dev/null 2>&1; then
    vm_dir="/System/Volumes/VM"
    [[ -d "${vm_dir}" ]] || vm_dir="/"
    avail_kb="$(df -Pk "${vm_dir}" 2>/dev/null | awk 'NR == 2 { print $4 }')"
    if [[ -n "${avail_kb}" ]] && print -r -- "${avail_kb}" | grep -Eq '^[0-9]+$'; then
      awk -v kb="${avail_kb}" 'BEGIN { printf "%.6f", kb / 1048576 }'
      return 0
    fi
  fi
  return 1
}

# Load average alone is not CPU pressure on macOS: runnable tasks and processes
# blocked in uninterruptible I/O both count. Spotlight indexing can therefore put
# load above core count while more than half the CPU and most memory remain idle.
# These probes are used only for the HEAVY high-load escape below. If either cannot
# be measured, the escape fails closed and the original load ceiling still binds.
probe_cpu_idle_pct() {
  local value
  if [[ -n "${FORGE_GUARD_CPU_IDLE_PCT:-}" ]]; then
    print -r -- "${FORGE_GUARD_CPU_IDLE_PCT}"
    return 0
  fi
  if command -v top >/dev/null 2>&1; then
    value="$(top -l 1 -n 0 2>/dev/null | awk '
      /CPU usage:/ {
        for (i=1; i<=NF; i++) if ($i == "idle") {
          v=$(i-1); gsub(/%/, "", v); print v; exit
        }
      }')"
    [[ -n "${value}" ]] && { print -r -- "${value}"; return 0; }
    value="$(top -bn1 2>/dev/null | awk '
      /Cpu\(s\):/ {
        for (i=1; i<=NF; i++) if ($i ~ /id,?$/) {
          v=$(i-1); gsub(/,/, "", v); print v; exit
        }
      }')"
    [[ -n "${value}" ]] && { print -r -- "${value}"; return 0; }
  fi
  return 1
}

probe_memory_free_pct() {
  local value
  if [[ -n "${FORGE_GUARD_MEMORY_FREE_PCT:-}" ]]; then
    print -r -- "${FORGE_GUARD_MEMORY_FREE_PCT}"
    return 0
  fi
  if command -v memory_pressure >/dev/null 2>&1; then
    value="$(memory_pressure -Q 2>/dev/null | awk -F': ' '
      /System-wide memory free percentage/ {
        gsub(/%/, "", $2); print $2; exit
      }')"
    [[ -n "${value}" ]] && { print -r -- "${value}"; return 0; }
  fi
  if [[ -r /proc/meminfo ]]; then
    awk '
      /^MemTotal:/ { total=$2 }
      /^MemAvailable:/ { available=$2 }
      END {
        if (total > 0 && available >= 0) printf "%.6f", available * 100 / total
        else exit 1
      }' /proc/meminfo
    return $?
  fi
  return 1
}

typeset VERDICT=""
typeset ACTIVE_LANES=""
typeset HEAVY_LEASES=""
typeset WEIGHT=""
typeset LANE_TYPE=""
typeset MODE=""
typeset WORKTREE=""
typeset CLEAN_TREE=""
typeset BUILDER=""
typeset EXECUTOR_POLICY="any"
typeset BASE_SHA=""
typeset PLAN_FILE=""
typeset ACT2_REVIEW_LOG=""
typeset EXECUTION_CONTRACT=""
typeset PROJECT_OVERLAY=""
typeset FREEZE_MANIFEST=""
typeset FREEZE_MANIFEST_SHA256=""
typeset ORACLE_EXACT=""
typeset CAPABILITY_RECEIPT=""
typeset CAPABILITY_RECEIPT_SHA256=""
typeset PROOF_COMMAND=""
typeset WORK_LOG=""
typeset REPORT_PATH=""
typeset PREDECESSOR=""
typeset PREDECESSOR_BUILDER=""
typeset PROOF_SCOPE="broad"   # omitted => broad => HEAVY: never silently parallelize
typeset EXECUTION_PROFILE="assured"   # omitted => assured: never silently cheapen cadence
typeset ADMISSION_ACTION="launch"
typeset OWNS_HEAVY_LEASE="false"
typeset REPORT_RECEIPT=""
typeset REVIEW_RECEIPT=""
typeset INSTRUCTION=""
typeset UNMERGED_BRANCHES=""
typeset -a BACKLOG_IDS
typeset -a DECLARED_FILES
typeset -a ACTIVE_WRITER_WORKTREES
typeset -a PARALLEL_FILES
typeset -a CURRENT_FILES
BACKLOG_IDS=()
DECLARED_FILES=()
ACTIVE_WRITER_WORKTREES=()
PARALLEL_FILES=()
CURRENT_FILES=()

while (( $# > 0 )); do
  case "$1" in
    --verdict)
      require_value "$1" "$#"
      VERDICT="$2"
      shift 2
      ;;
    --active-lanes)
      require_value "$1" "$#"
      ACTIVE_LANES="$2"
      shift 2
      ;;
    --heavy-leases)
      require_value "$1" "$#"
      HEAVY_LEASES="$2"
      shift 2
      ;;
    --weight)
      require_value "$1" "$#"
      WEIGHT="${(U)2}"
      shift 2
      ;;
    --lane-type)
      require_value "$1" "$#"
      LANE_TYPE="${(L)2}"
      shift 2
      ;;
    --mode)
      require_value "$1" "$#"
      MODE="${(L)2}"
      shift 2
      ;;
    --worktree)
      require_value "$1" "$#"
      WORKTREE="$2"
      shift 2
      ;;
    --clean-tree)
      require_value "$1" "$#"
      CLEAN_TREE="${(L)2}"
      shift 2
      ;;
    --builder)
      require_value "$1" "$#"
      BUILDER="$2"
      shift 2
      ;;
    --executor-policy)
      require_value "$1" "$#"
      EXECUTOR_POLICY="${(L)2}"
      shift 2
      ;;
    --base-sha)
      require_value "$1" "$#"
      BASE_SHA="$2"
      shift 2
      ;;
    --plan-file)
      require_value "$1" "$#"
      PLAN_FILE="$2"
      shift 2
      ;;
    --act2-review-log)
      require_value "$1" "$#"
      ACT2_REVIEW_LOG="$2"
      shift 2
      ;;
    --execution-contract)
      require_value "$1" "$#"
      EXECUTION_CONTRACT="$2"
      shift 2
      ;;
    --project-overlay)
      require_value "$1" "$#"
      PROJECT_OVERLAY="$2"
      shift 2
      ;;
    --freeze-manifest)
      require_value "$1" "$#"
      FREEZE_MANIFEST="$2"
      shift 2
      ;;
    --freeze-manifest-sha256)
      require_value "$1" "$#"
      FREEZE_MANIFEST_SHA256="$2"
      shift 2
      ;;
    --oracle-exact)
      require_value "$1" "$#"
      ORACLE_EXACT="${(L)2}"
      shift 2
      ;;
    --capability-receipt)
      require_value "$1" "$#"
      CAPABILITY_RECEIPT="$2"
      shift 2
      ;;
    --capability-receipt-sha256)
      require_value "$1" "$#"
      CAPABILITY_RECEIPT_SHA256="$2"
      shift 2
      ;;
    --backlog-id)
      require_value "$1" "$#"
      BACKLOG_IDS+=("$2")
      shift 2
      ;;
    --file)
      require_value "$1" "$#"
      DECLARED_FILES+=("$2")
      shift 2
      ;;
    --proof-command)
      require_value "$1" "$#"
      PROOF_COMMAND="$2"
      shift 2
      ;;
    --work-log)
      require_value "$1" "$#"
      WORK_LOG="$2"
      shift 2
      ;;
    --report)
      require_value "$1" "$#"
      REPORT_PATH="$2"
      shift 2
      ;;
    --predecessor)
      require_value "$1" "$#"
      PREDECESSOR="$2"
      shift 2
      ;;
    --predecessor-builder)
      require_value "$1" "$#"
      PREDECESSOR_BUILDER="$2"
      shift 2
      ;;
    --proof-scope)
      (( $# >= 2 )) || reject "FIELD_PROOF_SCOPE" "--proof-scope requires a value"
      PROOF_SCOPE="$2"
      shift 2
      continue
      ;;
    --execution-profile)
      (( $# >= 2 )) || reject "FIELD_EXECUTION_PROFILE" "--execution-profile requires a value"
      EXECUTION_PROFILE="$2"
      shift 2
      continue
      ;;
    --admission-action)
      (( $# >= 2 )) || reject "FIELD_ADMISSION_ACTION" "--admission-action requires a value"
      ADMISSION_ACTION="$2"
      shift 2
      continue
      ;;
    --owns-heavy-lease)
      require_value "$1" "$#"
      OWNS_HEAVY_LEASE="${(L)2}"
      shift 2
      ;;
    --report-receipt)
      require_value "$1" "$#"
      REPORT_RECEIPT="$2"
      shift 2
      ;;
    --review-receipt)
      require_value "$1" "$#"
      REVIEW_RECEIPT="$2"
      shift 2
      ;;
    --instruction)
      require_value "$1" "$#"
      INSTRUCTION="$2"
      shift 2
      ;;
    --unmerged-branches)
      require_value "$1" "$#"
      UNMERGED_BRANCHES="$2"
      shift 2
      ;;
    --active-writer-worktree)
      require_value "$1" "$#"
      ACTIVE_WRITER_WORKTREES+=("$2")
      shift 2
      ;;
    --parallel-file)
      require_value "$1" "$#"
      PARALLEL_FILES+=("$2")
      shift 2
      ;;
    --current-file)
      require_value "$1" "$#"
      CURRENT_FILES+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      reject "ARGUMENT_UNKNOWN" "unknown flag: $1"
      ;;
  esac
done

is_nonnegative_integer "${ACTIVE_LANES}" ||
  reject "FIELD_ACTIVE_LANES" "--active-lanes must be a non-negative integer"
[[ "${EXECUTION_PROFILE}" == "sprint" || "${EXECUTION_PROFILE}" == "assured" ]] ||
  reject "FIELD_EXECUTION_PROFILE" "--execution-profile must be sprint or assured; got ${EXECUTION_PROFILE}"
[[ "${ADMISSION_ACTION}" == "launch" || "${ADMISSION_ACTION}" == "expand" ]] ||
  reject "FIELD_ADMISSION_ACTION" "--admission-action must be launch or expand; got ${ADMISSION_ACTION}"
[[ "${OWNS_HEAVY_LEASE}" == "true" || "${OWNS_HEAVY_LEASE}" == "false" ]] ||
  reject "FIELD_HEAVY_LEASE_OWNERSHIP" "--owns-heavy-lease must be true or false"
if [[ "${ADMISSION_ACTION}" == "launch" && "${OWNS_HEAVY_LEASE}" == "true" ]]; then
  reject "HEAVY_LEASE_OWNERSHIP" "a new launch cannot already own a HEAVY lease"
fi

# A guarded re-admission widens an existing lane's declared file set. It is NOT a new
# lane, so it does not consume lane-cap headroom. Not called atomic: the scheduler
# state is caller-supplied, so this is a guarded check against the declared snapshot,
# not a lock over a shared registry.
if [[ "${ADMISSION_ACTION}" == "launch" ]]; then
  (( ACTIVE_LANES < 3 )) ||
    reject "LANE_CAP" "maximum three active lanes total; ${ACTIVE_LANES} already active"
fi

is_nonnegative_integer "${HEAVY_LEASES}" ||
  reject "FIELD_HEAVY_LEASES" "--heavy-leases must be a non-negative integer"
(( HEAVY_LEASES <= 1 )) ||
  reject "HEAVY_LEASE_STATE" "at most one HEAVY/gate lease may exist; declaration has ${HEAVY_LEASES}"

[[ "${WEIGHT}" == "LIGHT" || "${WEIGHT}" == "HEAVY" ]] ||
  reject "FIELD_WEIGHT" "--weight must be LIGHT or HEAVY"
if [[ "${OWNS_HEAVY_LEASE}" == "true" ]]; then
  [[ "${ADMISSION_ACTION}" == "expand" && "${WEIGHT}" == "HEAVY" && "${HEAVY_LEASES}" == "1" ]] ||
    reject "HEAVY_LEASE_OWNERSHIP" "only an expanding HEAVY lane may own the one declared HEAVY lease"
elif [[ "${WEIGHT}" == "HEAVY" ]] && (( HEAVY_LEASES >= 1 )); then
  reject "HEAVY_LEASE" "a HEAVY/gate lease is already held by another lane"
fi

case "${LANE_TYPE}" in
  feature|integration|recovery|control-plane|review) ;;
  *) reject "FIELD_LANE_TYPE" "--lane-type is required and must be feature, integration, recovery, control-plane, or review" ;;
esac

# Verdict gate — PRODUCT lanes only. This check MUST stay downstream of lane-type
# validation, because the lane type is what decides whether the verdict binds at all.
#
# Why it moved here: until 2026-07-29 this ran first, before the guard had even looked
# at --lane-type, so EVERY lane needed a literal APPROVED verdict. That made the
# control plane inescapable, and it is the headline defect behind the 2026-07-28/29
# run that burned ~88% of an allowance for zero product. Recovery exists precisely
# because a plan that DID exist went wrong — so at the moment repair is needed the
# governing verdict is REVISE, or absent because the round died mid-flight. The guard
# then refused the control-plane lane that would fix the machine AND refused the
# review lane that would produce a new verdict. The only legal escape from REVISE was
# a verdict the system had made itself unable to obtain.
#
# The rule the incident actually vindicates is narrower: don't build PRODUCT against
# an unapproved plan. Lanes that write no product code — control-plane, review — are
# admissible whatever the governing verdict says, including REVISE or absent. They
# remain subject to the shared lane cap, heavy lease, writer/file isolation,
# receipts, HANDOFF ownership, and machine headroom. The Git/freeze checks below
# are product-only so repair and review cannot deadlock on the state they must fix.
case "${LANE_TYPE}" in
  feature|integration|recovery)
    [[ -n "${VERDICT}" ]] ||
      reject "VERDICT_MISSING" "governing plan verdict is required for product lane type ${LANE_TYPE}"
    [[ "${VERDICT}" == "APPROVED" ]] ||
      reject "VERDICT_NOT_APPROVED" "governing plan verdict must be APPROVED for product lane type ${LANE_TYPE}; got ${VERDICT}"
    ;;
  control-plane|review)
    : # Writes no product code, so the plan verdict does not gate it. See above.
    ;;
esac

case "${MODE}" in
  normal|recovery) ;;
  *) reject "FIELD_MODE" "--mode is required and must be normal or recovery" ;;
esac
if [[ "${MODE}" == "recovery" && "${LANE_TYPE}" != "recovery" ]]; then
  reject "RECOVERY_MODE" "recovery mode is valid only for an explicit recovery lane"
fi
if [[ "${LANE_TYPE}" == "recovery" && "${MODE}" != "recovery" ]]; then
  reject "RECOVERY_MODE" "a recovery lane must explicitly select recovery mode"
fi

[[ -n "${WORKTREE}" && "${WORKTREE}" == /* ]] ||
  reject "FIELD_WORKTREE" "--worktree must be an absolute path"
WORKTREE="$(normalize_worktree "${WORKTREE}")"
typeset writer_is_active="false"
for active_writer in "${ACTIVE_WRITER_WORKTREES[@]}"; do
  if [[ "$(normalize_worktree "${active_writer}")" == "${WORKTREE}" ]]; then
    writer_is_active="true"
  fi
done
if [[ "${ADMISSION_ACTION}" == "launch" ]]; then
  # One writer per worktree. Unchanged.
  [[ "${writer_is_active}" == "true" ]] &&
    reject "DUPLICATE_WRITER" "worktree already has an active writer: ${WORKTREE}"
else
  # Guarded re-admission only widens a lane that is ALREADY writing. Expanding a lane
  # that is not active would create an unaccounted writer outside the lane cap.
  [[ "${writer_is_active}" == "true" ]] ||
    reject "EXPAND_NOT_ACTIVE" "--admission-action expand requires this worktree to be an active writer: ${WORKTREE}"
fi

[[ "${CLEAN_TREE}" == "true" || "${CLEAN_TREE}" == "false" ]] ||
  reject "FIELD_CLEAN_TREE" "--clean-tree must be true or false"
# A LAUNCH always requires a clean tree. A guarded EXPANSION is by definition mid-flight:
# the lane is already writing, so its tree is expected to be dirty. Refusing that would
# make expansion unusable exactly when it is needed.
if [[ "${CLEAN_TREE}" == "false" && "${MODE}" != "recovery" && "${ADMISSION_ACTION}" != "expand" ]]; then
  reject "DIRTY_TREE" "worktree is dirty and explicit recovery mode is not active"
fi

# Builder identity is required, while global Forge stays vendor-neutral. A prior
# repair wrongly generalized one project's Codex-executes rule into universal
# policy. Project routing belongs in the explicit executor-policy overlay below.
# A project's standing Codex-executes amendment remains binding through codex-only
# until the owner rescinds it. Identity remains mandatory for role and review receipts.
[[ -n "${BUILDER}" ]] ||
  reject "FIELD_BUILDER" "--builder is required"

# Global Forge remains vendor-neutral. A project may narrow only its PRODUCT
# executor route with an explicit overlay. This preserves portability while making
# owner amendments mechanically enforceable. Review/control-plane lanes stay open
# to the opposite family so cross-model review and repair cannot deadlock.
case "${EXECUTOR_POLICY}" in
  any|codex-only|claude-only) ;;
  *) reject "FIELD_EXECUTOR_POLICY" "--executor-policy must be any, codex-only, or claude-only" ;;
esac
case "${LANE_TYPE}" in
  feature|integration|recovery)
    if [[ "${EXECUTOR_POLICY}" == "codex-only" && "$(model_family "${BUILDER}")" != "codex" ]]; then
      reject "BUILDER_POLICY" "project overlay requires a Codex-family product executor; got ${BUILDER}"
    fi
    if [[ "${EXECUTOR_POLICY}" == "claude-only" && "$(model_family "${BUILDER}")" != "claude" ]]; then
      reject "BUILDER_POLICY" "project overlay requires a Claude-family product executor; got ${BUILDER}"
    fi
    ;;
esac

print -r -- "${BASE_SHA}" |
  grep -Eq '^([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})$' ||
  reject "FIELD_BASE_SHA" "--base-sha must be a full immutable 40- or 64-character hexadecimal SHA"

# Product admission is bound to a real repository state and a separately hashed
# control-plane snapshot. Control-plane and review lanes are deliberately exempt:
# they must remain able to repair or review a missing/broken freeze without first
# pretending that the broken freeze is valid.
typeset PRODUCT_LANE="false"
case "${LANE_TYPE}" in
  feature|integration|recovery) PRODUCT_LANE="true" ;;
esac

if [[ "${PRODUCT_LANE}" == "true" ]]; then
  [[ -n "${PLAN_FILE}" && "${PLAN_FILE}" == /* ]] ||
    reject "PLAN_FILE_MISSING" "product lanes require --plan-file as an absolute path"
  [[ -f "${PLAN_FILE}" && -s "${PLAN_FILE}" ]] ||
    reject "FIELD_PLAN_FILE" "governing plan is missing, empty, or not a regular file: ${PLAN_FILE}"
  [[ -n "${ACT2_REVIEW_LOG}" && "${ACT2_REVIEW_LOG}" == /* ]] ||
    reject "ACT2_REVIEW_LOG_MISSING" "product lanes require --act2-review-log as an absolute path"
  [[ -f "${ACT2_REVIEW_LOG}" && -s "${ACT2_REVIEW_LOG}" ]] ||
    reject "FIELD_ACT2_REVIEW_LOG" "Act-2 review log is missing, empty, or not a regular file: ${ACT2_REVIEW_LOG}"
  [[ -n "${EXECUTION_CONTRACT}" && "${EXECUTION_CONTRACT}" == /* ]] ||
    reject "EXECUTION_CONTRACT_MISSING" "product lanes require --execution-contract as an absolute path"
  [[ -f "${EXECUTION_CONTRACT}" && -s "${EXECUTION_CONTRACT}" ]] ||
    reject "FIELD_EXECUTION_CONTRACT" "execution contract is missing, empty, or not a regular file: ${EXECUTION_CONTRACT}"
  typeset ACTUAL_CONTRACT_SHA
  ACTUAL_CONTRACT_SHA="$(sha256_file "${EXECUTION_CONTRACT}")" ||
    reject "HASH_TOOL_UNAVAILABLE" "unable to compute SHA-256 for execution contract"

  # Exact-oracle feasibility is a safety declaration, not assured cadence. Sprint may
  # skip the control freeze, but it may never silently omit whether byte-exact output is
  # required or bypass a directly verified capability receipt.
  [[ "${ORACLE_EXACT}" == "true" || "${ORACLE_EXACT}" == "false" ]] ||
    reject "FIELD_ORACLE_EXACT" "product lanes require explicit --oracle-exact true or false"

  # Freeze manifests and control hashes are `assured` capability-freeze machinery.
  # Under `sprint` they are admission overhead with no cadence purpose: sprint runs no
  # unit review and no proof rerun, so there is nothing for a control freeze to bind.
  if [[ "${EXECUTION_PROFILE}" == "assured" ]]; then
    [[ -n "${FREEZE_MANIFEST}" && "${FREEZE_MANIFEST}" == /* ]] ||
      reject "FREEZE_MANIFEST_MISSING" "assured product lanes require --freeze-manifest as an absolute path"
    [[ -f "${FREEZE_MANIFEST}" && -s "${FREEZE_MANIFEST}" ]] ||
      reject "FIELD_FREEZE_MANIFEST" "freeze manifest is missing, empty, or not a regular file: ${FREEZE_MANIFEST}"
    print -r -- "${FREEZE_MANIFEST_SHA256}" | grep -Eq '^[0-9A-Fa-f]{64}$' ||
      reject "FIELD_FREEZE_MANIFEST_SHA256" "--freeze-manifest-sha256 must be a full SHA-256"

  fi

  [[ -n "${PROJECT_OVERLAY}" && "${PROJECT_OVERLAY}" == /* ]] ||
    reject "PROJECT_OVERLAY_MISSING" "product lanes require --project-overlay as an absolute path"
  [[ -f "${PROJECT_OVERLAY}" && -s "${PROJECT_OVERLAY}" ]] ||
    reject "FIELD_PROJECT_OVERLAY" "project overlay is missing, empty, or not a regular file: ${PROJECT_OVERLAY}"
  typeset OVERLAY_POLICY
  OVERLAY_POLICY="$(line_value "${PROJECT_OVERLAY}" executor_policy)" ||
    reject "PROJECT_OVERLAY_FORMAT" "project overlay requires exactly one executor_policy=any|codex-only|claude-only declaration"
  case "${OVERLAY_POLICY}" in
    any|codex-only|claude-only) ;;
    *) reject "PROJECT_OVERLAY_FORMAT" "unsupported executor_policy in project overlay: ${OVERLAY_POLICY}" ;;
  esac
  [[ "${OVERLAY_POLICY}" == "${EXECUTOR_POLICY}" ]] ||
    reject "OVERLAY_POLICY_MISMATCH" "project overlay requires ${OVERLAY_POLICY}, but caller declared ${EXECUTOR_POLICY}"

  # Sprint: a capability receipt is required ONLY when the caller explicitly declares
  # oracle-exact true, and it is verified directly — no full control freeze is forced.
  if [[ "${ORACLE_EXACT}" == "true" ]]; then
    [[ -n "${CAPABILITY_RECEIPT}" && "${CAPABILITY_RECEIPT}" == /* ]] ||
      reject "CAPABILITY_RECEIPT_MISSING" "oracle-exact product lanes require --capability-receipt as an absolute path"
    [[ -f "${CAPABILITY_RECEIPT}" && -s "${CAPABILITY_RECEIPT}" ]] ||
      reject "FIELD_CAPABILITY_RECEIPT" "capability receipt is missing, empty, or not a regular file: ${CAPABILITY_RECEIPT}"
    print -r -- "${CAPABILITY_RECEIPT_SHA256}" | grep -Eq '^[0-9A-Fa-f]{64}$' ||
      reject "FIELD_CAPABILITY_RECEIPT_SHA256" "--capability-receipt-sha256 must be a full SHA-256"
  fi

  [[ -d "${WORKTREE}" ]] ||
    reject "WORKTREE_MISSING" "product worktree does not exist: ${WORKTREE}"
  command -v git >/dev/null 2>&1 ||
    reject "WORKTREE_GIT_UNAVAILABLE" "git is required to validate a product worktree"
  [[ "$(git -C "${WORKTREE}" rev-parse --is-inside-work-tree 2>/dev/null)" == "true" ]] ||
    reject "WORKTREE_NOT_GIT" "product worktree is not a Git worktree: ${WORKTREE}"

  typeset WORKTREE_PHYSICAL GIT_TOP GIT_TOP_PHYSICAL
  WORKTREE_PHYSICAL="$(cd "${WORKTREE}" 2>/dev/null && pwd -P)" ||
    reject "WORKTREE_UNRESOLVED" "cannot resolve product worktree: ${WORKTREE}"
  GIT_TOP="$(git -C "${WORKTREE}" rev-parse --show-toplevel 2>/dev/null)" ||
    reject "WORKTREE_NOT_GIT" "cannot resolve Git worktree root: ${WORKTREE}"
  GIT_TOP_PHYSICAL="$(cd "${GIT_TOP}" 2>/dev/null && pwd -P)" ||
    reject "WORKTREE_UNRESOLVED" "cannot resolve Git top level: ${GIT_TOP}"
  [[ "${WORKTREE_PHYSICAL}" == "${GIT_TOP_PHYSICAL}" ]] ||
    reject "WORKTREE_NOT_ROOT" "--worktree must name the Git top level exactly; got ${WORKTREE}"

  typeset RESOLVED_BASE HEAD_SHA OBSERVED_STATUS
  RESOLVED_BASE="$(git -C "${WORKTREE}" rev-parse --verify "${BASE_SHA}^{commit}" 2>/dev/null)" ||
    reject "BASE_SHA_UNRESOLVED" "declared base does not resolve to a commit in ${WORKTREE}: ${BASE_SHA}"
  [[ "${(L)RESOLVED_BASE}" == "${(L)BASE_SHA}" ]] ||
    reject "BASE_SHA_UNRESOLVED" "declared base resolves to ${RESOLVED_BASE}, not exactly ${BASE_SHA}"
  HEAD_SHA="$(git -C "${WORKTREE}" rev-parse HEAD 2>/dev/null)" ||
    reject "BASE_HEAD_MISMATCH" "worktree has no resolvable HEAD"
  [[ "${(L)HEAD_SHA}" == "${(L)BASE_SHA}" ]] ||
    reject "BASE_HEAD_MISMATCH" "worktree HEAD ${HEAD_SHA} does not equal declared base ${BASE_SHA}"

  OBSERVED_STATUS="$(git -C "${WORKTREE}" status --porcelain=v1 --untracked-files=all --ignore-submodules=none 2>/dev/null)" ||
    reject "WORKTREE_STATUS" "unable to inspect product worktree state"
  if [[ "${CLEAN_TREE}" == "true" && -n "${OBSERVED_STATUS}" ]]; then
    reject "WORKTREE_DIRTY" "worktree was declared clean but Git reports changes"
  fi
  if [[ "${CLEAN_TREE}" == "false" && -z "${OBSERVED_STATUS}" ]]; then
    reject "CLEAN_TREE_MISMATCH" "worktree was declared dirty but Git reports a clean tree"
  fi

  # Capability-freeze verification is `assured`-only machinery (see the manifest
  # requirement above). Under `sprint` there is no control freeze to verify.
  if [[ "${EXECUTION_PROFILE}" == "assured" ]]; then
  typeset ACTUAL_FREEZE_SHA
  ACTUAL_FREEZE_SHA="$(sha256_file "${FREEZE_MANIFEST}")" ||
    reject "HASH_TOOL_UNAVAILABLE" "unable to compute SHA-256 for freeze manifest"
  [[ "${(L)ACTUAL_FREEZE_SHA}" == "${(L)FREEZE_MANIFEST_SHA256}" ]] ||
    reject "FREEZE_MANIFEST_HASH_MISMATCH" "freeze manifest is ${ACTUAL_FREEZE_SHA}, expected ${FREEZE_MANIFEST_SHA256}"
  (( $(manifest_path_count "${FREEZE_MANIFEST}" "${FREEZE_MANIFEST}") == 0 )) ||
    reject "FREEZE_MANIFEST_SELF_REFERENCE" "freeze manifest may not hash itself"
  verify_sha256_manifest "${FREEZE_MANIFEST}" ||
    reject "FREEZE_CONTENT_MISMATCH" "one or more frozen control-plane artifacts changed or the manifest is malformed"

  typeset CONTRACT_MANIFEST_SHA
  CONTRACT_MANIFEST_SHA="$(manifest_hash_for_path "${FREEZE_MANIFEST}" "${EXECUTION_CONTRACT}")" ||
    reject "FREEZE_CONTRACT_MISSING" "freeze manifest must contain the execution contract exactly once"
  [[ "${(L)CONTRACT_MANIFEST_SHA}" == "${(L)ACTUAL_CONTRACT_SHA}" ]] ||
    reject "FREEZE_CONTRACT_MISMATCH" "execution contract hash does not match its freeze-manifest entry"

  manifest_hash_for_path "${FREEZE_MANIFEST}" "${PROJECT_OVERLAY}" >/dev/null ||
    reject "FREEZE_OVERLAY_MISSING" "freeze manifest must contain the active project overlay exactly once"

  typeset -a REQUIRED_CONTROL_FILES
  REQUIRED_CONTROL_FILES=(
    "${PLAN_FILE}"
    "${ACT2_REVIEW_LOG}"
    "${FORGE_SKILL}"
    "${FORGE_ENGINE}"
    "${ROUTING_POLICY}"
    "${GUARD_SELF}"
    "${GUARD_TESTS}"
    "${SOL_LIVENESS}"
    "${WATCHDOG}"
    "${WATCHDOG_TESTS}"
  )
  for required_control in "${REQUIRED_CONTROL_FILES[@]}"; do
    [[ -f "${required_control}" && -s "${required_control}" ]] ||
      reject "REQUIRED_CONTROL_MISSING" "required Forge control is missing or empty: ${required_control}"
    manifest_hash_for_path "${FREEZE_MANIFEST}" "${required_control}" >/dev/null ||
      reject "FREEZE_REQUIRED_CONTROL_MISSING" "freeze manifest must contain required control exactly once: ${required_control}"
  done
  fi   # end assured-only capability-freeze verification

  if [[ "${ORACLE_EXACT}" == "true" ]]; then
    typeset ACTUAL_CAPABILITY_SHA CAPABILITY_MANIFEST_SHA
    ACTUAL_CAPABILITY_SHA="$(sha256_file "${CAPABILITY_RECEIPT}")" ||
      reject "HASH_TOOL_UNAVAILABLE" "unable to compute SHA-256 for capability receipt"
    [[ "${(L)ACTUAL_CAPABILITY_SHA}" == "${(L)CAPABILITY_RECEIPT_SHA256}" ]] ||
      reject "CAPABILITY_RECEIPT_HASH_MISMATCH" "capability receipt is ${ACTUAL_CAPABILITY_SHA}, expected ${CAPABILITY_RECEIPT_SHA256}"
    # Sprint verifies the receipt DIRECTLY (above): its content must hash to the
    # declared SHA. Binding it into a control freeze is assured-only.
    if [[ "${EXECUTION_PROFILE}" == "assured" ]]; then
      CAPABILITY_MANIFEST_SHA="$(manifest_hash_for_path "${FREEZE_MANIFEST}" "${CAPABILITY_RECEIPT}")" ||
        reject "FREEZE_CAPABILITY_MISSING" "freeze manifest must contain the capability receipt exactly once"
      [[ "${(L)CAPABILITY_MANIFEST_SHA}" == "${(L)ACTUAL_CAPABILITY_SHA}" ]] ||
        reject "FREEZE_CAPABILITY_MISMATCH" "capability receipt hash does not match its freeze-manifest entry"
    fi

    typeset CAP_VERSION CAP_ID CAP_CONTRACT_SHA CAP_MECHANISM CAP_TARGET
    typeset CAP_EXIT CAP_ORACLE_SHA CAP_OBSERVED_SHA CAP_TARGET_SHA CAP_TARGET_PHYSICAL
    CAP_VERSION="$(line_value "${CAPABILITY_RECEIPT}" version)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one version field"
    CAP_ID="$(line_value "${CAPABILITY_RECEIPT}" receipt_id)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one receipt_id field"
    CAP_CONTRACT_SHA="$(line_value "${CAPABILITY_RECEIPT}" contract_sha256)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one contract_sha256 field"
    CAP_MECHANISM="$(line_value "${CAPABILITY_RECEIPT}" mechanism)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one mechanism field"
    CAP_TARGET="$(line_value "${CAPABILITY_RECEIPT}" disposable_target)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one disposable_target field"
    CAP_EXIT="$(line_value "${CAPABILITY_RECEIPT}" exit)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one exit field"
    CAP_ORACLE_SHA="$(line_value "${CAPABILITY_RECEIPT}" oracle_sha256)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one oracle_sha256 field"
    CAP_OBSERVED_SHA="$(line_value "${CAPABILITY_RECEIPT}" observed_sha256)" ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt requires one observed_sha256 field"
    [[ "${CAP_VERSION}" == "1" && -n "${CAP_ID}" && -n "${CAP_MECHANISM}" && "${CAP_EXIT}" == "0" ]] ||
      reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt must be version 1 with nonempty ID/mechanism and exit=0"
    for capability_hash in "${CAP_CONTRACT_SHA}" "${CAP_ORACLE_SHA}" "${CAP_OBSERVED_SHA}"; do
      print -r -- "${capability_hash}" | grep -Eq '^[0-9A-Fa-f]{64}$' ||
        reject "CAPABILITY_RECEIPT_FORMAT" "capability receipt hashes must be full SHA-256 values"
    done
    [[ "${(L)CAP_CONTRACT_SHA}" == "${(L)ACTUAL_CONTRACT_SHA}" ]] ||
      reject "CAPABILITY_CONTRACT_MISMATCH" "capability probe was not run against this execution contract"
    [[ "${(L)CAP_ORACLE_SHA}" == "${(L)CAP_OBSERVED_SHA}" ]] ||
      reject "CAPABILITY_MISMATCH" "capability probe did not reproduce the exact oracle bytes"
    [[ "${CAP_TARGET}" == /* && -f "${CAP_TARGET}" ]] ||
      reject "CAPABILITY_TARGET_MISSING" "disposable capability target must be an existing absolute regular file"
    CAP_TARGET_PHYSICAL="$(cd "${CAP_TARGET:h}" 2>/dev/null && print -r -- "$(pwd -P)/${CAP_TARGET:t}")" ||
      reject "CAPABILITY_TARGET_MISSING" "cannot resolve disposable capability target"
    [[ "${CAP_TARGET_PHYSICAL}" != "${WORKTREE_PHYSICAL}" && "${CAP_TARGET_PHYSICAL}" != "${WORKTREE_PHYSICAL}"/* ]] ||
      reject "CAPABILITY_TARGET_NOT_DISPOSABLE" "capability target must be outside the product worktree"
    CAP_TARGET_SHA="$(sha256_file "${CAP_TARGET}")" ||
      reject "HASH_TOOL_UNAVAILABLE" "unable to hash disposable capability target"
    [[ "${(L)CAP_TARGET_SHA}" == "${(L)CAP_OBSERVED_SHA}" ]] ||
      reject "CAPABILITY_TARGET_MISMATCH" "disposable capability target does not match the observed capability hash"
  fi
fi

(( ${#BACKLOG_IDS[@]} > 0 )) ||
  reject "FIELD_BACKLOG_IDS" "at least one --backlog-id is required"
for backlog_id in "${BACKLOG_IDS[@]}"; do
  [[ -n "${backlog_id}" ]] ||
    reject "FIELD_BACKLOG_IDS" "backlog item IDs may not be empty"
done

(( ${#DECLARED_FILES[@]} > 0 )) ||
  reject "FIELD_FILE_SET" "at least one exact --file is required"
typeset -A declared_seen
typeset -A parallel_seen
for parallel_file in "${PARALLEL_FILES[@]}"; do
  file_path_is_exact "${parallel_file}" ||
    reject "FILE_SET_NOT_EXACT" "parallel file is not a canonical repo-relative file path: ${parallel_file}"
  parallel_seen[${parallel_file}]=1
done
for declared_file in "${DECLARED_FILES[@]}"; do
  file_path_is_exact "${declared_file}" ||
    reject "FILE_SET_NOT_EXACT" "declared file is not a canonical repo-relative file path: ${declared_file}"
  [[ -z "${declared_seen[${declared_file}]-}" ]] ||
    reject "FILE_SET_DUPLICATE" "declared file appears more than once: ${declared_file}"
  declared_seen[${declared_file}]=1
  if [[ -n "${parallel_seen[${declared_file}]-}" ]]; then
    reject "FILE_OVERLAP" "declared file overlaps an active parallel unit: ${declared_file}"
  fi
  if [[ "${LANE_TYPE}" != "integration" ]]; then
    case "${declared_file}" in
      .ai/HANDOFF.md|*/.ai/HANDOFF.md)
        reject "HANDOFF_FORBIDDEN" "unit lanes may not edit .ai/HANDOFF.md; the integration owner writes it once in the gated uncommitted candidate"
        ;;
    esac
  fi
done

# Expansion replaces no declaration: the caller supplies the lane's complete current
# set and the complete widened set. Requiring the former makes the superset check
# explicit and prevents a re-admission from silently dropping paths already owned by
# the lane. Launches have no prior set and may not smuggle one in.
if [[ "${ADMISSION_ACTION}" == "launch" ]]; then
  (( ${#CURRENT_FILES[@]} == 0 )) ||
    reject "CURRENT_SET_ON_LAUNCH" "--current-file is valid only with --admission-action expand"
else
  (( ${#CURRENT_FILES[@]} > 0 )) ||
    reject "EXPAND_CURRENT_SET_MISSING" "expansion requires the current declared-set snapshot via --current-file"
  typeset -A current_seen
  for current_file in "${CURRENT_FILES[@]}"; do
    file_path_is_exact "${current_file}" ||
      reject "FILE_SET_NOT_EXACT" "current-set file is not a canonical repo-relative path: ${current_file}"
    [[ -z "${current_seen[${current_file}]-}" ]] ||
      reject "CURRENT_SET_DUPLICATE" "current-set file appears more than once: ${current_file}"
    current_seen[${current_file}]=1
    [[ -n "${declared_seen[${current_file}]-}" ]] ||
      reject "EXPAND_NOT_SUPERSET" "expanded file set omits currently declared path: ${current_file}"
  done

  # A mid-flight product tree may be dirty, but every changed path must already be in
  # the full widened declaration. This catches callers that ask permission only after
  # writing an undeclared path, as well as accidental dirty side effects.
  if [[ "${PRODUCT_LANE}" == "true" ]]; then
    typeset dirty_path
    while IFS= read -r dirty_path; do
      [[ -n "${dirty_path}" ]] || continue
      [[ -n "${declared_seen[${dirty_path}]-}" ]] ||
        reject "DIRTY_PATH_UNDECLARED" "dirty expansion path is outside the expanded declared set: ${dirty_path}"
    done < <(
      git -C "${WORKTREE}" -c core.quotepath=false diff --name-only --no-renames HEAD --
      git -C "${WORKTREE}" -c core.quotepath=false ls-files --others --exclude-standard
    )
  fi
fi

[[ -n "${PROOF_COMMAND}" ]] ||
  reject "FIELD_PROOF_COMMAND" "--proof-command is required"
[[ -n "${WORK_LOG}" && "${WORK_LOG}" == /* ]] ||
  reject "FIELD_WORK_LOG" "--work-log must be an absolute path"
[[ -n "${REPORT_PATH}" && "${REPORT_PATH}" == /* ]] ||
  reject "FIELD_REPORT" "--report must be an absolute path"
[[ -n "${INSTRUCTION}" ]] ||
  reject "FIELD_INSTRUCTION" "--instruction is required"

[[ "${PROOF_SCOPE}" == "targeted" || "${PROOF_SCOPE}" == "broad" ]] ||
  reject "FIELD_PROOF_SCOPE" "--proof-scope must be targeted or broad; got ${PROOF_SCOPE}"

# Weight is decided by what the PROOF COMMAND actually runs — never by narrative words
# in --instruction. Before 2026-08-03 the word "build" anywhere in a human-readable
# instruction forced HEAVY, which serialized every ordinary feature lane behind the one
# gate lease and defeated the three-lane cap for no safety gain.
#
# BROAD (always HEAVY): a whole-suite or whole-project command — an unfiltered test run,
# any gate, typecheck, lint over the project, a production build/compile, a coverage
# campaign, or a database rebuild.
# TARGETED (LIGHT-eligible): every test/build segment is narrowed to a named test, file,
# project or filter. Segments are evaluated independently: a path used only by `tee`, or
# one targeted command beside a full suite, cannot launder the broad segment.
typeset -i PROOF_LOOKS_BROAD=0
if print -r -- "${PROOF_COMMAND}" | grep -Eiq \
  '(^|[^[:alnum:]_-])(gate|typecheck|tsc|coverage|db:(reset|rebuild|migrate:fresh)|migrate:fresh|prisma +migrate +reset|drizzle-kit +push)([^[:alnum:]_-]|$)'; then
  PROOF_LOOKS_BROAD=1
fi

typeset proof_segments_text
proof_segments_text="$(print -r -- "${PROOF_COMMAND}" | sed -E 's/&&/;/g; s/\|\|/;/g; s/\|/;/g')"
proof_segments_text="${proof_segments_text//$'\n'/;}"
typeset -a proof_segments
proof_segments=("${(@s:;:)proof_segments_text}")
typeset -r TEST_RUNNER_RE='(^|[^[:alnum:]_-])(test|tests|vitest|jest|pytest|go +test|cargo +test|eslint|lint)([^[:alnum:]_-]|$)'
typeset -r TARGET_SELECTOR_RE='((-t|-k)(=[^[:space:]]+|[[:space:]]+[^[:space:]]+)|--(testNamePattern|filter|project|grep)(=[^[:space:]]+|[[:space:]]+[^[:space:]]+))'
typeset -r TARGET_TEST_FILE_RE='(^|[[:space:]])[^[:space:]|;&<>]*[.](test|spec)[.](js|jsx|ts|tsx|mjs|cjs|py|go|rs|rb|java|kt|php|swift)([[:space:]]|$)'
typeset -r TARGET_SOURCE_PATH_RE='(^|[[:space:]])[^[:space:]|;&<>]+/[^[:space:]|;&<>]+[.](js|jsx|ts|tsx|mjs|cjs|py|go|rs|rb|java|kt|php|swift)([[:space:]]|$)'
typeset -r BUILD_SELECTOR_RE='--(filter|project|scope)(=[^[:space:]]+|[[:space:]]+[^[:space:]]+)'
for proof_segment in "${proof_segments[@]}"; do
  [[ -n "${proof_segment//[[:space:]]/}" ]] || continue

  if print -r -- "${proof_segment}" | grep -Eiq \
    '(^|[^[:alnum:]_-])(build|compile)([^[:alnum:]_-]|$)' &&
    ! print -r -- "${proof_segment}" | grep -Eiq -- "${BUILD_SELECTOR_RE}"; then
    PROOF_LOOKS_BROAD=1
  fi

  if print -r -- "${proof_segment}" | grep -Eiq -- "${TEST_RUNNER_RE}"; then
    typeset -i segment_is_targeted=0
    if print -r -- "${proof_segment}" | grep -Eiq -- "${TARGET_SELECTOR_RE}" ||
      print -r -- "${proof_segment}" | grep -Eiq -- "${TARGET_TEST_FILE_RE}" ||
      print -r -- "${proof_segment}" | grep -Eiq -- "${TARGET_SOURCE_PATH_RE}"; then
      segment_is_targeted=1
    fi
    # `go test ./...` is a repository-wide suite, not a targeted path.
    if print -r -- "${proof_segment}" | grep -Eiq \
      '(^|[[:space:]])go[[:space:]]+test([[:space:]]+[^[:space:]]+)*[[:space:]]+\./\.\.\.([[:space:]]|$)'; then
      segment_is_targeted=0
    fi
    (( segment_is_targeted )) || PROOF_LOOKS_BROAD=1
  fi
done

if [[ "${PROOF_SCOPE}" == "targeted" ]] && (( PROOF_LOOKS_BROAD )); then
  reject "PROOF_SCOPE_UNDERDECLARED" "proof command runs a full suite/gate/typecheck/build/coverage/db-rebuild; it is broad and must be declared --proof-scope broad with --weight HEAVY"
fi
if [[ "${PROOF_SCOPE}" == "broad" && "${WEIGHT}" == "LIGHT" ]]; then
  reject "WEIGHT_UNDERDECLARED" "a broad proof (full suite, gate, typecheck, build, coverage, db rebuild) requires HEAVY weight"
fi

# The executor never commits — now enforced VENDOR-NEUTRALLY.
#
# This is a git-safety property about the ROLE, not about the vendor: whoever executes
# a unit, the Claude-side orchestrator reviews and commits per the ACTIVE PROFILE
# (assured: complete-diff review + its own proof rerun; sprint: changed paths match
# the admitted set, no overlap/safety breach, executor proof exited 0), then commits.
# The previous version got that wrong twice.
#
# First, the whole block was wrapped in a model-family test for "codex", so the rule
# evaporated the moment a Claude-side model was the executor — a Claude executor could
# be handed "and then commit it" with no objection at all. The binding is on the
# executing role, so the scan now runs for every declared builder.
#
# Second, the verb list was porous. An adversarial probe got the instruction
# "checkpoint, cherry-pick, and push" ADMITTED: every one of those either writes
# history or publishes it, which is precisely what the rule exists to prevent, but
# none of the three appeared in a list that knew only commit and merge. Naming a git
# verb the scan has not memorised should not be a bypass, so the list below covers
# commit, merge, push, cherry-pick, rebase and checkpoint with their inflections.
#
# The scan covers every executor-controlled input: the short instruction, the actual
# execution contract sent to the executor, and the proof command it must run. A benign
# summary must not launder a contract or proof that mutates history. Safe negatives are
# stripped first so the correct prohibition remains admissible.
typeset -r COMMIT_VERBS='(commit|commits|committed|committing|merge|merges|merged|merging|push|pushes|pushed|pushing|cherry-pick|cherry-picks|cherry-picked|cherry-picking|cherrypick|cherrypicks|cherrypicked|cherrypicking|rebase|rebases|rebased|rebasing|checkpoint|checkpoints|checkpointed|checkpointing)'
typeset -r COMMIT_NEGATIONS="(do not|don't|never|must not|shall not|should not|cannot|can't|without)"
typeset -r GIT_GLOBAL_OPTIONS='([[:space:]]+(-[cC][[:space:]]+[^[:space:]]+|--(git-dir|work-tree)(=[^[:space:]]+|[[:space:]]+[^[:space:]]+)))*'
typeset -r GIT_AUTHORITY_COMMANDS='(reset|clean|restore|checkout|switch|update-ref|symbolic-ref|send-pack|receive-pack|pull|stash|replace|notes|tag|branch[[:space:]]+(-d|--delete))'
typeset authority_text="${INSTRUCTION}"$'\n'"${PROOF_COMMAND}"
if [[ -n "${EXECUTION_CONTRACT}" && -f "${EXECUTION_CONTRACT}" ]]; then
  authority_text+=$'\n'"$(<"${EXECUTION_CONTRACT}")"
fi
typeset authority_lower="${(L)authority_text}"
typeset authority_without_safe_negatives
authority_without_safe_negatives="$(
  print -r -- "${authority_lower}" |
    sed -E \
      -e "s/${COMMIT_NEGATIONS}[[:space:]]+${COMMIT_VERBS}(,?[[:space:]]+(or|and|nor)?[[:space:]]*${COMMIT_VERBS})*//g" \
      -e "s/${COMMIT_NEGATIONS}[[:space:]]+(run[[:space:]]+)?git${GIT_GLOBAL_OPTIONS}[[:space:]]+${GIT_AUTHORITY_COMMANDS}//g"
)"
if print -r -- "${authority_without_safe_negatives}" |
  grep -Eq "(^|[^[:alnum:]_])${COMMIT_VERBS}([^[:alnum:]_]|\$)"; then
  reject "EXECUTOR_COMMIT_INSTRUCTION" "executor inputs may not direct commit/merge/push/cherry-pick/rebase/checkpoint work; the orchestrator owns those actions"
fi
if print -r -- "${authority_without_safe_negatives}" |
  grep -Eiq "(^|[^[:alnum:]_])git${GIT_GLOBAL_OPTIONS}[[:space:]]+${GIT_AUTHORITY_COMMANDS}([^[:alnum:]_-]|\$)"; then
  reject "EXECUTOR_GIT_AUTHORITY" "executor inputs may not run destructive or history-publishing Git commands; owner/orchestrator authority is required"
fi

# CP-2 WIP self-measurement: the guard measures unmerged product branches
# itself from the worktree's repository rather than trusting a caller-typed
# count (a hard-coded count admitted a lane against stale WIP on 2026-08-01).
# Scope rule matches the tracker: refs/heads matching ^(opus|sol|fable)/ with
# at least one commit not reachable from the WIP base. The base is
# FORGE_GUARD_WIP_BASE when set, otherwise the repository's own default branch:
# origin/HEAD, then a local main or master. A base that does not resolve still
# refuses the lane. FORGE_GUARD_WIP_MEASURED overrides only when the worktree
# has no resolvable repository (declared in the test suite).
default_wip_base() {
  local repo="$1" candidate
  candidate="$(git -C "${repo}" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
  if [[ -n "${candidate}" ]]; then
    print -r -- "${candidate}"
    return 0
  fi
  for candidate in main master; do
    if git -C "${repo}" rev-parse --verify --quiet "refs/heads/${candidate}^{commit}" >/dev/null 2>&1; then
      print -r -- "${candidate}"
      return 0
    fi
  done
  print -r -- "origin/HEAD"
}

measure_unmerged_branches() {
  local repo="$1" base="$2" count=0 line branch_sha ahead
  if [[ -n "${FORGE_GUARD_WIP_MEASURED:-}" ]]; then
    print -r -- "${FORGE_GUARD_WIP_MEASURED}"
    return 0
  fi
  git -C "${repo}" rev-parse --verify --quiet "${base}^{commit}" >/dev/null 2>&1 || return 1
  while IFS='|' read -r line branch_sha; do
    ahead="$(git -C "${repo}" rev-list --count "${base}..${branch_sha}" 2>/dev/null)" || continue
    [[ "${ahead}" == 0 ]] || (( count += 1 ))
  done < <(git -C "${repo}" for-each-ref --format='%(refname:short)|%(objectname)' \
      'refs/heads/opus/*' 'refs/heads/sol/*' 'refs/heads/fable/*' 2>/dev/null)
  print -r -- "${count}"
}

typeset WIP_BASE="${FORGE_GUARD_WIP_BASE:-$(default_wip_base "${WORKTREE}")}"
typeset MEASURED_WIP=""
MEASURED_WIP="$(measure_unmerged_branches "${WORKTREE}" "${WIP_BASE}")" ||
  reject "WIP_UNMEASURABLE" "cannot measure WIP: base ${WIP_BASE} is not resolvable from ${WORKTREE}; a lane may not launch on an unmeasured repository"
if [[ -n "${UNMERGED_BRANCHES}" ]]; then
  is_nonnegative_integer "${UNMERGED_BRANCHES}" ||
    reject "FIELD_UNMERGED_BRANCHES" "--unmerged-branches must be a non-negative integer"
  if [[ "${UNMERGED_BRANCHES}" != "${MEASURED_WIP}" ]]; then
    reject "DECLARED_WIP_MISMATCH" "declared --unmerged-branches ${UNMERGED_BRANCHES} disagrees with the measured count ${MEASURED_WIP} (base ${WIP_BASE}); the guard admits only measured WIP"
  fi
fi
UNMERGED_BRANCHES="${MEASURED_WIP}"
if [[ "${ADMISSION_ACTION}" == "launch" && "${LANE_TYPE}" == "feature" ]] && (( UNMERGED_BRANCHES > 10 )); then
  reject "WIP_CAP" "${UNMERGED_BRANCHES} measured unmerged branches exceed the cap of ten; new product FEATURE lanes are blocked"
fi

[[ -n "${PREDECESSOR}" ]] ||
  reject "FIELD_PREDECESSOR" "--predecessor is required; use none for an initial unit"
if [[ "${(L)PREDECESSOR}" != "none" ]]; then
  [[ -n "${PREDECESSOR_BUILDER}" ]] ||
    reject "RECEIPT_PREDECESSOR_BUILDER" "successor requires --predecessor-builder"
  [[ -n "${REPORT_RECEIPT}" ]] ||
    reject "RECEIPT_REPORT_MISSING" "successor requires a report-consumption receipt"
  # Cross-model unit review is `assured` cadence. Under `sprint` the report-consumption
  # receipt is still required (the orchestrator must have READ the predecessor report),
  # but no unit review and no orchestrator proof rerun are performed, so demanding a
  # review receipt would make sprint unexecutable.
  if [[ "${EXECUTION_PROFILE}" == "assured" ]]; then
    [[ -n "${REVIEW_RECEIPT}" ]] ||
      reject "RECEIPT_REVIEW_MISSING" "successor requires a cross-model review receipt under the assured profile"
  fi

  typeset report_predecessor
  typeset report_consumed_by
  typeset report_consumed_path
  typeset report_receipt_id
  report_predecessor="$(receipt_value "${REPORT_RECEIPT}" predecessor)" ||
    reject "RECEIPT_REPORT_FORMAT" "report receipt is missing predecessor"
  report_consumed_by="$(receipt_value "${REPORT_RECEIPT}" consumed_by)" ||
    reject "RECEIPT_REPORT_FORMAT" "report receipt is missing consumed_by"
  report_consumed_path="$(receipt_value "${REPORT_RECEIPT}" report)" ||
    reject "RECEIPT_REPORT_FORMAT" "report receipt is missing report"
  report_receipt_id="$(receipt_value "${REPORT_RECEIPT}" receipt_id)" ||
    reject "RECEIPT_REPORT_FORMAT" "report receipt is missing receipt_id"
  [[ "${report_predecessor}" == "${PREDECESSOR}" ]] ||
    reject "RECEIPT_REPORT_PREDECESSOR" "report receipt predecessor does not match ${PREDECESSOR}"
  [[ -n "${report_consumed_path}" && -n "${report_receipt_id}" ]] ||
    reject "RECEIPT_REPORT_FORMAT" "report receipt values may not be empty"
  [[ "${report_consumed_path}" == /* && -f "${report_consumed_path}" && -s "${report_consumed_path}" ]] ||
    reject "RECEIPT_REPORT_FILE" "report receipt must name an existing nonempty absolute regular file: ${report_consumed_path}"
  # ROUTING-POLICY.md §2: the Claude-side default is Fable 5 and the owner may
  # select either Claude model as orchestrator. The consumed_by check is a
  # FAMILY check (Claude side), not a model pin — a model pin here contradicted
  # the routing policy, which wins.
  case "${(L)report_consumed_by}" in
    *opus*|*fable*|*claude*) ;;
    *) reject "RECEIPT_REPORT_ORCHESTRATOR" "report must be consumed and recorded by the Claude-side orchestrator" ;;
  esac

  typeset review_predecessor
  typeset review_builder
  typeset review_reviewer
  typeset review_thread
  typeset review_verdict
  typeset review_receipt_id
  # Under sprint there is no unit review, so there may legitimately be no receipt to
  # parse. A receipt that IS supplied is validated at either profile — a supplied
  # receipt must never be a fake one.
  if [[ -n "${REVIEW_RECEIPT}" ]]; then
  review_predecessor="$(receipt_value "${REVIEW_RECEIPT}" predecessor)" ||
    reject "RECEIPT_REVIEW_FORMAT" "review receipt is missing predecessor"
  review_builder="$(receipt_value "${REVIEW_RECEIPT}" builder)" ||
    reject "RECEIPT_REVIEW_FORMAT" "review receipt is missing builder"
  review_reviewer="$(receipt_value "${REVIEW_RECEIPT}" reviewer)" ||
    reject "RECEIPT_REVIEW_FORMAT" "review receipt is missing reviewer"
  review_thread="$(receipt_value "${REVIEW_RECEIPT}" thread)" ||
    reject "RECEIPT_REVIEW_FORMAT" "review receipt is missing thread"
  review_verdict="$(receipt_value "${REVIEW_RECEIPT}" verdict)" ||
    reject "RECEIPT_REVIEW_FORMAT" "review receipt is missing verdict"
  review_receipt_id="$(receipt_value "${REVIEW_RECEIPT}" receipt_id)" ||
    reject "RECEIPT_REVIEW_FORMAT" "review receipt is missing receipt_id"
  [[ "${review_predecessor}" == "${PREDECESSOR}" ]] ||
    reject "RECEIPT_REVIEW_PREDECESSOR" "review receipt predecessor does not match ${PREDECESSOR}"
  [[ "${(L)review_builder}" == "${(L)PREDECESSOR_BUILDER}" ]] ||
    reject "RECEIPT_REVIEW_BUILDER" "review receipt builder does not match --predecessor-builder"
  [[ -n "${review_reviewer}" && -n "${review_thread}" && -n "${review_receipt_id}" ]] ||
    reject "RECEIPT_REVIEW_FORMAT" "review receipt values may not be empty"
  [[ "$(model_family "${review_builder}")" != "unknown" &&
    "$(model_family "${review_reviewer}")" != "unknown" ]] ||
    reject "RECEIPT_REVIEW_IDENTITY" "review receipt must identify recognized builder and reviewer model families"
  [[ "$(model_family "${review_builder}")" != "$(model_family "${review_reviewer}")" ]] ||
    reject "RECEIPT_REVIEW_NOT_CROSS_MODEL" "same-model review is not cross-model review"
  [[ "${review_verdict}" == "APPROVED" ]] ||
    reject "RECEIPT_REVIEW_VERDICT" "predecessor cross-model review must return APPROVED; got ${review_verdict}"
  fi
fi

typeset CORE_COUNT
typeset LOAD_1
typeset FREE_SWAP_GB
typeset HEAVY_LOAD_FACTOR="${FORGE_GUARD_HEAVY_MAX_LOAD_PER_CORE:-1}"
typeset HEAVY_MIN_SWAP_GB="${FORGE_GUARD_HEAVY_MIN_SWAP_GB:-4}"
typeset HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT="${FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT:-50}"
typeset HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT="${FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT:-50}"
typeset LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT="${FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT:-50}"
typeset LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT="${FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT:-50}"
typeset LIGHT_LOAD_FACTOR="${FORGE_GUARD_LIGHT_MAX_LOAD_PER_CORE:-4}"
typeset LIGHT_MIN_SWAP_GB="${FORGE_GUARD_LIGHT_MIN_SWAP_GB:-1}"

CORE_COUNT="$(probe_core_count)" ||
  reject "MACHINE_PROBE_CORES" "unable to determine core count; set FORGE_GUARD_CORE_COUNT"
LOAD_1="$(probe_load_1)" ||
  reject "MACHINE_PROBE_LOAD" "unable to determine one-minute load; set FORGE_GUARD_LOAD_1"
FREE_SWAP_GB="$(probe_free_swap_gb)" ||
  reject "MACHINE_PROBE_SWAP" "unable to determine free swap; set FORGE_GUARD_FREE_SWAP_GB"

is_positive_number "${CORE_COUNT}" ||
  reject "MACHINE_VALUE_CORES" "core count must be a positive number; got ${CORE_COUNT}"
is_nonnegative_number "${LOAD_1}" ||
  reject "MACHINE_VALUE_LOAD" "one-minute load must be a non-negative number; got ${LOAD_1}"
is_nonnegative_number "${FREE_SWAP_GB}" ||
  reject "MACHINE_VALUE_SWAP" "free swap must be a non-negative number; got ${FREE_SWAP_GB}"
is_positive_number "${HEAVY_LOAD_FACTOR}" ||
  reject "MACHINE_THRESHOLD_HEAVY_LOAD" "HEAVY load-per-core threshold must be positive"
is_nonnegative_number "${HEAVY_MIN_SWAP_GB}" ||
  reject "MACHINE_THRESHOLD_HEAVY_SWAP" "HEAVY swap threshold must be non-negative"
is_percentage "${HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT}" ||
  reject "MACHINE_THRESHOLD_HEAVY_CPU_IDLE" "HEAVY high-load minimum CPU idle threshold must be between 0 and 100"
is_percentage "${HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT}" ||
  reject "MACHINE_THRESHOLD_HEAVY_MEMORY" "HEAVY high-load minimum memory-free threshold must be between 0 and 100"
is_percentage "${LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT}" ||
  reject "MACHINE_THRESHOLD_LIGHT_CPU_IDLE" "LIGHT high-load minimum CPU idle threshold must be between 0 and 100"
is_percentage "${LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT}" ||
  reject "MACHINE_THRESHOLD_LIGHT_MEMORY" "LIGHT high-load minimum memory-free threshold must be between 0 and 100"
is_positive_number "${LIGHT_LOAD_FACTOR}" ||
  reject "MACHINE_THRESHOLD_LIGHT_LOAD" "LIGHT load-per-core threshold must be positive"
is_nonnegative_number "${LIGHT_MIN_SWAP_GB}" ||
  reject "MACHINE_THRESHOLD_LIGHT_SWAP" "LIGHT swap threshold must be non-negative"

typeset MAX_LOAD
typeset MIN_SWAP
typeset HEADROOM_NOTE=""
if [[ "${WEIGHT}" == "HEAVY" ]]; then
  MAX_LOAD="$(multiply "${CORE_COUNT}" "${HEAVY_LOAD_FACTOR}")"
  MIN_SWAP="${HEAVY_MIN_SWAP_GB}"
  if float_gt "${LOAD_1}" "${MAX_LOAD}"; then
    typeset CPU_IDLE_PCT MEMORY_FREE_PCT
    CPU_IDLE_PCT="$(probe_cpu_idle_pct)" ||
      reject "MACHINE_PROBE_CPU_IDLE" "HEAVY load ${LOAD_1} exceeds ${MAX_LOAD} and CPU idle could not be measured; set FORGE_GUARD_CPU_IDLE_PCT"
    MEMORY_FREE_PCT="$(probe_memory_free_pct)" ||
      reject "MACHINE_PROBE_MEMORY" "HEAVY load ${LOAD_1} exceeds ${MAX_LOAD} and memory headroom could not be measured; set FORGE_GUARD_MEMORY_FREE_PCT"
    is_percentage "${CPU_IDLE_PCT}" ||
      reject "MACHINE_VALUE_CPU_IDLE" "CPU idle must be between 0 and 100; got ${CPU_IDLE_PCT}"
    is_percentage "${MEMORY_FREE_PCT}" ||
      reject "MACHINE_VALUE_MEMORY" "memory free percentage must be between 0 and 100; got ${MEMORY_FREE_PCT}"
    float_lt "${MEMORY_FREE_PCT}" "${HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT}" &&
      reject "MACHINE_MEMORY_HEAVY" "HEAVY lane load ${LOAD_1} exceeds ${MAX_LOAD} and memory free ${MEMORY_FREE_PCT}% is below ${HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT}%"
    float_lt "${CPU_IDLE_PCT}" "${HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT}" &&
      reject "MACHINE_LOAD_HEAVY" "HEAVY lane load ${LOAD_1} exceeds ${MAX_LOAD} and CPU idle ${CPU_IDLE_PCT}% is below ${HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT}%"
    HEADROOM_NOTE=" high_load_headroom=cpu_idle:${CPU_IDLE_PCT}%/${HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT}%,memory_free:${MEMORY_FREE_PCT}%/${HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT}%"
  fi
  float_lt "${FREE_SWAP_GB}" "${MIN_SWAP}" &&
    reject "MACHINE_SWAP_HEAVY" "HEAVY lane free swap ${FREE_SWAP_GB} GB is below ${MIN_SWAP} GB"
else
  MAX_LOAD="$(multiply "${CORE_COUNT}" "${LIGHT_LOAD_FACTOR}")"
  MIN_SWAP="${LIGHT_MIN_SWAP_GB}"
  # A LIGHT lane on a machine whose load is inflated by PROCESS COUNT rather than
  # compute may still enter, on the same two-condition test HEAVY already uses:
  # CPU idle AND memory-free headroom both at or above their thresholds. Measured
  # 2026-08-06: 391 Chrome renderers held load at 564 on 10 cores while total Chrome
  # CPU was 20.8%, CPU idle 63.3% and memory free 70%. A bare load ceiling refuses
  # every lane forever on such a desktop; that is a broken instrument, not admission
  # discipline. Both conditions must pass — real compute or memory pressure still
  # rejects, and the ordinary load threshold is unchanged.
  if float_gt "${LOAD_1}" "${MAX_LOAD}"; then
    typeset LIGHT_CPU_IDLE_PCT LIGHT_MEMORY_FREE_PCT
    LIGHT_CPU_IDLE_PCT="$(probe_cpu_idle_pct)" ||
      reject "MACHINE_PROBE_CPU_IDLE" "LIGHT load ${LOAD_1} exceeds ${MAX_LOAD} and CPU idle could not be measured; set FORGE_GUARD_CPU_IDLE_PCT"
    LIGHT_MEMORY_FREE_PCT="$(probe_memory_free_pct)" ||
      reject "MACHINE_PROBE_MEMORY" "LIGHT load ${LOAD_1} exceeds ${MAX_LOAD} and memory headroom could not be measured; set FORGE_GUARD_MEMORY_FREE_PCT"
    is_percentage "${LIGHT_CPU_IDLE_PCT}" ||
      reject "MACHINE_VALUE_CPU_IDLE" "CPU idle must be between 0 and 100; got ${LIGHT_CPU_IDLE_PCT}"
    is_percentage "${LIGHT_MEMORY_FREE_PCT}" ||
      reject "MACHINE_VALUE_MEMORY" "memory free percentage must be between 0 and 100; got ${LIGHT_MEMORY_FREE_PCT}"
    float_lt "${LIGHT_MEMORY_FREE_PCT}" "${LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT}" &&
      reject "MACHINE_MEMORY_LIGHT" "LIGHT lane load ${LOAD_1} exceeds ${MAX_LOAD} and memory free ${LIGHT_MEMORY_FREE_PCT}% is below ${LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT}%"
    float_lt "${LIGHT_CPU_IDLE_PCT}" "${LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT}" &&
      reject "MACHINE_LOAD_LIGHT" "LIGHT lane load ${LOAD_1} exceeds ${MAX_LOAD} and CPU idle ${LIGHT_CPU_IDLE_PCT}% is below ${LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT}%"
    HEADROOM_NOTE=" high_load_headroom=cpu_idle:${LIGHT_CPU_IDLE_PCT}%/${LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT}%,memory_free:${LIGHT_MEMORY_FREE_PCT}%/${LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT}%"
  fi
  float_lt "${FREE_SWAP_GB}" "${MIN_SWAP}" &&
    reject "MACHINE_SWAP_LIGHT" "LIGHT lane free swap ${FREE_SWAP_GB} GB is below ${MIN_SWAP} GB"
fi

print -r -- "ADMIT: action=${ADMISSION_ACTION} profile=${EXECUTION_PROFILE} proof_scope=${PROOF_SCOPE} lane=${LANE_TYPE} weight=${WEIGHT} executor_policy=${EXECUTOR_POLICY} active_before=${ACTIVE_LANES} heavy_leases_before=${HEAVY_LEASES} load=${LOAD_1}/${MAX_LOAD} swap_gb=${FREE_SWAP_GB}/${MIN_SWAP}${HEADROOM_NOTE}"
exit 0
