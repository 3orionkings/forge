#!/bin/zsh

set -u
set -o pipefail

readonly TEST_DIR="${0:A:h}"
readonly GUARD="${TEST_DIR}/dispatch-guard.sh"
readonly FORGE_SKILL="${TEST_DIR}/SKILL.md"
readonly FORGE_ENGINE="${TEST_DIR}/ENGINE.md"
readonly ROUTING_POLICY="${TEST_DIR}/ROUTING-POLICY.md"
readonly GUARD_TESTS="${TEST_DIR}/dispatch-guard.test.sh"
readonly SOL_LIVENESS="${TEST_DIR}/sol-liveness.sh"
readonly RECOVERY_DIR="${TEST_DIR:h}/forge-recovery"
readonly WATCHDOG="${RECOVERY_DIR}/recovery-watchdog.sh"
readonly WATCHDOG_TESTS="${RECOVERY_DIR}/recovery-watchdog.test.sh"

readonly TEST_TMP="$(mktemp -d /tmp/forge-dispatch-guard-test.XXXXXX)" || exit 1
readonly TEST_REPO="${TEST_TMP}/repo"
readonly TEST_FREEZE_TARGET="${TEST_TMP}/control-plane.txt"
readonly TEST_PLAN="${TEST_TMP}/plan.md"
readonly TEST_ACT2_REVIEW_LOG="${TEST_TMP}/act2-review-log.md"
readonly TEST_EXECUTION_CONTRACT="${TEST_TMP}/execution-contract.md"
readonly TEST_COMMIT_CONTRACT="${TEST_TMP}/execution-contract-commit.md"
readonly TEST_PROJECT_OVERLAY="${TEST_TMP}/project-overlay.md"
readonly TEST_ANY_OVERLAY="${TEST_TMP}/any-project-overlay.md"
readonly TEST_PREDECESSOR_REPORT="${TEST_TMP}/unit-0-report.md"
readonly TEST_EMPTY_PREDECESSOR_REPORT="${TEST_TMP}/unit-0-report-empty.md"
readonly TEST_FREEZE_MANIFEST="${TEST_TMP}/control-plane.sha256"
readonly TEST_INCOMPLETE_MANIFEST="${TEST_TMP}/incomplete-control-plane.sha256"
readonly TEST_CAPABILITY_RECEIPT="${TEST_TMP}/oracle-capability.txt"
readonly TEST_CAPABILITY_ARTIFACT="${TEST_TMP}/oracle-artifact.bin"
trap 'rm -rf -- "${TEST_TMP}"' EXIT HUP INT TERM

mkdir -p "${TEST_REPO}/scripts" || exit 1
git -C "${TEST_REPO}" init -q || exit 1
git -C "${TEST_REPO}" config user.name 'Forge Guard Test' || exit 1
git -C "${TEST_REPO}" config user.email 'forge-guard@example.invalid' || exit 1
print -r -- 'fixture' > "${TEST_REPO}/scripts/cp1.txt"
git -C "${TEST_REPO}" add scripts/cp1.txt || exit 1
git -C "${TEST_REPO}" commit -qm 'fixture parent' || exit 1
readonly TEST_PARENT_SHA="$(git -C "${TEST_REPO}" rev-parse HEAD)" || exit 1
git -C "${TEST_REPO}" commit --allow-empty -qm 'fixture base' || exit 1
readonly TEST_BASE_SHA="$(git -C "${TEST_REPO}" rev-parse HEAD)" || exit 1

print -r -- 'approved control plane' > "${TEST_FREEZE_TARGET}"
print -r -- 'approved product plan' > "${TEST_PLAN}"
print -r -- 'VERDICT: APPROVED' > "${TEST_ACT2_REVIEW_LOG}"
print -r -- 'goal, files, constraints, and proof' > "${TEST_EXECUTION_CONTRACT}"
print -r -- 'Implement the unit, then git commit -am done and git push.' > "${TEST_COMMIT_CONTRACT}"
print -r -- 'executor_policy=codex-only' > "${TEST_PROJECT_OVERLAY}"
print -r -- 'executor_policy=any' > "${TEST_ANY_OVERLAY}"
print -r -- 'executor report: proof passed' > "${TEST_PREDECESSOR_REPORT}"
: > "${TEST_EMPTY_PREDECESSOR_REPORT}"
print -n -- 'exact bytes' > "${TEST_CAPABILITY_ARTIFACT}"
readonly TEST_EXECUTION_CONTRACT_SHA="$(sha256sum "${TEST_EXECUTION_CONTRACT}" | awk '{ print $1 }')" || exit 1
readonly TEST_CAPABILITY_ARTIFACT_SHA="$(sha256sum "${TEST_CAPABILITY_ARTIFACT}" | awk '{ print $1 }')" || exit 1
{
  print -r -- 'version=1'
  print -r -- 'receipt_id=test-oracle-capability'
  print -r -- "contract_sha256=${TEST_EXECUTION_CONTRACT_SHA}"
  print -r -- 'mechanism=disposable apply_patch probe'
  print -r -- "disposable_target=${TEST_CAPABILITY_ARTIFACT}"
  print -r -- 'exit=0'
  print -r -- "oracle_sha256=${TEST_CAPABILITY_ARTIFACT_SHA}"
  print -r -- "observed_sha256=${TEST_CAPABILITY_ARTIFACT_SHA}"
} > "${TEST_CAPABILITY_RECEIPT}"
readonly TEST_CAPABILITY_RECEIPT_SHA="$(sha256sum "${TEST_CAPABILITY_RECEIPT}" | awk '{ print $1 }')" || exit 1
{
  sha256sum "${TEST_FREEZE_TARGET}"
  sha256sum "${TEST_PLAN}"
  sha256sum "${TEST_ACT2_REVIEW_LOG}"
  sha256sum "${TEST_EXECUTION_CONTRACT}"
  sha256sum "${TEST_PROJECT_OVERLAY}"
  sha256sum "${TEST_ANY_OVERLAY}"
  sha256sum "${TEST_CAPABILITY_RECEIPT}"
  sha256sum "${FORGE_SKILL}"
  sha256sum "${FORGE_ENGINE}"
  sha256sum "${ROUTING_POLICY}"
  sha256sum "${GUARD}"
  sha256sum "${GUARD_TESTS}"
  sha256sum "${SOL_LIVENESS}"
  sha256sum "${WATCHDOG}"
  sha256sum "${WATCHDOG_TESTS}"
} > "${TEST_FREEZE_MANIFEST}" || exit 1
readonly TEST_FREEZE_MANIFEST_SHA="$(sha256sum "${TEST_FREEZE_MANIFEST}" | awk '{ print $1 }')" || exit 1
{
  sha256sum "${TEST_FREEZE_TARGET}"
  sha256sum "${TEST_PLAN}"
  sha256sum "${TEST_ACT2_REVIEW_LOG}"
  sha256sum "${TEST_EXECUTION_CONTRACT}"
  sha256sum "${TEST_PROJECT_OVERLAY}"
  sha256sum "${TEST_CAPABILITY_RECEIPT}"
} > "${TEST_INCOMPLETE_MANIFEST}" || exit 1
readonly TEST_INCOMPLETE_MANIFEST_SHA="$(sha256sum "${TEST_INCOMPLETE_MANIFEST}" | awk '{ print $1 }')" || exit 1

typeset -i CASES=0
typeset -i FAILURES=0

typeset CASE_LOAD_1="1"
typeset CASE_FREE_SWAP_GB="16"
typeset CASE_CORE_COUNT="10"
typeset CASE_HEAVY_LOAD_FACTOR="1"
typeset CASE_HEAVY_MIN_SWAP_GB="4"
typeset CASE_HIGH_LOAD_MIN_CPU_IDLE_PCT="50"
typeset CASE_HIGH_LOAD_MIN_MEMORY_FREE_PCT="50"
typeset CASE_CPU_IDLE_PCT="20"
typeset CASE_MEMORY_FREE_PCT="86"
typeset CASE_LIGHT_LOAD_FACTOR="4"
typeset CASE_LIGHT_MIN_SWAP_GB="1"

typeset -a COMMON_ARGS
COMMON_ARGS=(
  --active-lanes 0
  --heavy-leases 0
  --weight LIGHT
  --lane-type feature
  --mode normal
  --worktree "${TEST_REPO}"
  --clean-tree true
  --builder codex
  --executor-policy codex-only
  --base-sha "${TEST_BASE_SHA}"
  --plan-file "${TEST_PLAN}"
  --act2-review-log "${TEST_ACT2_REVIEW_LOG}"
  --execution-contract "${TEST_EXECUTION_CONTRACT}"
  --project-overlay "${TEST_PROJECT_OVERLAY}"
  --freeze-manifest "${TEST_FREEZE_MANIFEST}"
  --freeze-manifest-sha256 "${TEST_FREEZE_MANIFEST_SHA}"
  --oracle-exact false
  --backlog-id CP-1
  --file scripts/cp1.txt
  --proof-scope targeted
  --proof-command 'zsh -n scripts/cp1.txt'
  --work-log "${TEST_TMP}/forge-cp1-unit-work.log"
  --report "${TEST_TMP}/forge-cp1-unit-report.md"
  --predecessor none
  --instruction 'Codex implements only the declared files.'
  --unmerged-branches 0
)

typeset -a BASE_ARGS
BASE_ARGS=(--verdict APPROVED "${COMMON_ARGS[@]}")

invoke_guard() {
  # CP-2: cases declare their WIP via --unmerged-branches; the harness mirrors
  # that declaration into the measured channel so the guard's declared-vs-
  # measured cross-check holds for every legacy case. Dedicated CP-2 cases
  # override CASE_WIP_MEASURED to exercise mismatch and self-measurement.
  local -a args=("$@")
  local wip_measured="${CASE_WIP_MEASURED:-}"
  if [[ -z "${wip_measured}" ]]; then
    local i
    for (( i = 1; i <= ${#args[@]}; i++ )); do
      if [[ "${args[i]}" == "--unmerged-branches" ]]; then
        wip_measured="${args[i+1]}"
      fi
    done
  fi
  FORGE_GUARD_WIP_MEASURED="${wip_measured:-0}" \
  FORGE_GUARD_LOAD_1="${CASE_LOAD_1}" \
  FORGE_GUARD_FREE_SWAP_GB="${CASE_FREE_SWAP_GB}" \
  FORGE_GUARD_CORE_COUNT="${CASE_CORE_COUNT}" \
  FORGE_GUARD_HEAVY_MAX_LOAD_PER_CORE="${CASE_HEAVY_LOAD_FACTOR}" \
  FORGE_GUARD_HEAVY_MIN_SWAP_GB="${CASE_HEAVY_MIN_SWAP_GB}" \
  FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT="${CASE_HIGH_LOAD_MIN_CPU_IDLE_PCT}" \
  FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT="${CASE_HIGH_LOAD_MIN_MEMORY_FREE_PCT}" \
  FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_CPU_IDLE_PCT="${CASE_HIGH_LOAD_MIN_CPU_IDLE_PCT}" \
  FORGE_GUARD_LIGHT_HIGH_LOAD_MIN_MEMORY_FREE_PCT="${CASE_HIGH_LOAD_MIN_MEMORY_FREE_PCT}" \
  FORGE_GUARD_CPU_IDLE_PCT="${CASE_CPU_IDLE_PCT}" \
  FORGE_GUARD_MEMORY_FREE_PCT="${CASE_MEMORY_FREE_PCT}" \
  FORGE_GUARD_LIGHT_MAX_LOAD_PER_CORE="${CASE_LIGHT_LOAD_FACTOR}" \
  FORGE_GUARD_LIGHT_MIN_SWAP_GB="${CASE_LIGHT_MIN_SWAP_GB}" \
    "${GUARD}" "$@"
}

assert_rejected() {
  local name="$1"
  local expected="$2"
  shift 2

  local output
  local rc
  output="$(invoke_guard "$@" 2>&1)"
  rc=$?
  (( CASES += 1 ))

  if (( rc != 0 )) && [[ "${output}" == *"${expected}"* ]]; then
    print -r -- "PASS: ${name}"
  else
    print -r -- "FAIL: ${name}"
    print -r -- "  expected: non-zero with ${expected}"
    print -r -- "  actual exit: ${rc}"
    print -r -- "  actual output: ${output}"
    (( FAILURES += 1 ))
  fi
}

assert_admitted() {
  local name="$1"
  shift

  local output
  local rc
  output="$(invoke_guard "$@" 2>&1)"
  rc=$?
  (( CASES += 1 ))

  if (( rc == 0 )) && [[ "${output}" == ADMIT:* ]]; then
    print -r -- "PASS: ${name}"
  else
    print -r -- "FAIL: ${name}"
    print -r -- "  expected: zero with ADMIT:"
    print -r -- "  actual exit: ${rc}"
    print -r -- "  actual output: ${output}"
    (( FAILURES += 1 ))
  fi
}

if [[ ! -x "${GUARD}" ]]; then
  print -r -- "FAIL: dispatch guard exists and is executable"
  exit 1
fi

# Verdict gate — binds PRODUCT lanes (feature/integration/recovery) and nothing else.
# The admitted counterparts for control-plane and review lanes are below; the pair
# together is the proof that the 2026-07-28/29 catch-22 is gone: the guard now refuses
# to build product against an unapproved plan while still permitting the control-plane
# repair and the review lane that produce the next verdict.
assert_rejected \
  "a feature lane whose governing verdict is REVISE" \
  "VERDICT_NOT_APPROVED" \
  "${BASE_ARGS[@]}" --verdict REVISE

assert_rejected \
  "an integration lane whose governing verdict is REVISE" \
  "VERDICT_NOT_APPROVED" \
  "${BASE_ARGS[@]}" --lane-type integration --verdict REVISE

assert_rejected \
  "a recovery lane whose governing verdict is REVISE" \
  "VERDICT_NOT_APPROVED" \
  "${BASE_ARGS[@]}" --lane-type recovery --mode recovery --verdict REVISE

assert_rejected \
  "a feature lane whose governing verdict is missing" \
  "VERDICT_MISSING" \
  "${COMMON_ARGS[@]}"

assert_rejected \
  "a fourth concurrent lane" \
  "LANE_CAP" \
  "${BASE_ARGS[@]}" --active-lanes 3

# The verdict exemption is scoped to the verdict and to nothing else. A control-plane
# lane is exempt from APPROVED; it is NOT exempt from the lane cap, the heavy lease,
# writer/file isolation, the clean-tree rule, receipts, HANDOFF ownership or machine
# headroom. These two prove the exemption did not become a general bypass.
assert_rejected \
  "a fourth concurrent lane even when it is control-plane" \
  "LANE_CAP" \
  "${BASE_ARGS[@]}" --lane-type control-plane --verdict REVISE --active-lanes 3

assert_rejected \
  "a control-plane lane declaring .ai/HANDOFF.md" \
  "HANDOFF_FORBIDDEN" \
  "${BASE_ARGS[@]}" --lane-type control-plane --verdict REVISE --file .ai/HANDOFF.md

assert_rejected \
  "a second heavy or gate lease" \
  "HEAVY_LEASE" \
  "${BASE_ARGS[@]}" --weight HEAVY --heavy-leases 1 --proof-scope broad \
  --proof-command 'pnpm test'

assert_rejected \
  "a duplicate writer for one worktree" \
  "DUPLICATE_WRITER" \
  "${BASE_ARGS[@]}" --active-writer-worktree "${TEST_REPO}"

assert_rejected \
  "parallel units with overlapping declared files" \
  "FILE_OVERLAP" \
  "${BASE_ARGS[@]}" --parallel-file scripts/cp1.txt

assert_rejected \
  "a dirty worktree outside recovery mode" \
  "DIRTY_TREE" \
  "${BASE_ARGS[@]}" --clean-tree false

# CHANGED 2026-07-29: the global case that asserted PRODUCT_BUILDER for
# `--builder opus` was deleted. It wrongly generalized one project's routing rule
# into a universal Forge vendor lock. The global default is neutral; the explicit
# project overlay cases below prove a project's Codex-executes amendment separately.

# The executor never commits, whoever the executor is. Three cases: the original Codex
# builder, a Claude builder (the family test that used to skip this scan entirely), and
# the verbatim adversarial probe "checkpoint, cherry-pick, and push" that the narrow
# commit/merge word list admitted.
assert_rejected \
  "an instruction telling a Codex builder to commit or merge" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" \
  --instruction 'Codex must commit and merge the completed unit.'

assert_rejected \
  "an instruction telling a Claude builder to commit or merge" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" --executor-policy any --project-overlay "${TEST_ANY_OVERLAY}" \
  --builder claude-fable-5 \
  --instruction 'Fable must commit and merge the completed unit.'

assert_rejected \
  "an instruction ordering checkpoint, cherry-pick and push" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" \
  --instruction 'Checkpoint, cherry-pick, and push the completed unit.'

assert_rejected \
  "an instruction ordering a rebase onto the base SHA" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" --builder sol \
  --instruction 'Sol should rebase the unit onto the declared base SHA.'

# A negation earlier in the instruction must not launder a positive order later in it.
# The safe-negative strip is a narrow allowance for the correct contract sentence, not
# a prefix that buys immunity for the rest of the paragraph.
assert_rejected \
  "a forbidding sentence followed by a separate commit order" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" \
  --instruction 'Never touch the proof. Commit when green.'

assert_rejected \
  "a forbidding sentence followed by a separate push order" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" \
  --instruction 'Do not merge. Then push the branch.'

# A review lane writes no product code, but it is still an executor: it may not be
# ordered to publish history either.
assert_rejected \
  "a review lane instructed to push its result" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" --lane-type review --verdict REVISE \
  --instruction 'Review the diff, then push the result.'

assert_rejected \
  "the actual execution contract may not order commit or push" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --execution-contract "${TEST_COMMIT_CONTRACT}"

assert_rejected \
  "the proof command may not commit or publish history" \
  "EXECUTOR_COMMIT_INSTRUCTION" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --weight HEAVY --proof-scope broad \
  --proof-command 'pnpm test && git commit -am done && git push'

assert_rejected \
  "executor instruction may not discard work with git reset --hard" \
  "EXECUTOR_GIT_AUTHORITY" \
  "${BASE_ARGS[@]}" --instruction 'Run git reset --hard HEAD~1 before finishing.'

assert_rejected \
  "executor instruction may not discard untracked work with git clean" \
  "EXECUTOR_GIT_AUTHORITY" \
  "${BASE_ARGS[@]}" --instruction 'Run git clean -fd before finishing.'

assert_rejected \
  "executor instruction may not advance a branch with git update-ref" \
  "EXECUTOR_GIT_AUTHORITY" \
  "${BASE_ARGS[@]}" --instruction 'Run git update-ref refs/heads/main HEAD.'

assert_rejected \
  "executor instruction may not publish with git send-pack" \
  "EXECUTOR_GIT_AUTHORITY" \
  "${BASE_ARGS[@]}" --instruction 'Run git send-pack origin HEAD:refs/heads/main.'

assert_rejected \
  "executor instruction may not delete a branch" \
  "EXECUTOR_GIT_AUTHORITY" \
  "${BASE_ARGS[@]}" --instruction 'Run git branch -D old-work.'

assert_admitted \
  "negative git-safety instructions remain admissible" \
  "${BASE_ARGS[@]}" \
  --instruction 'Do not commit or push. Do not run git reset --hard.'

CASE_LOAD_1="11"
CASE_FREE_SWAP_GB="8"
assert_rejected \
  "a heavy lane above the core-count load ceiling" \
  "MACHINE_LOAD_HEAVY" \
  "${BASE_ARGS[@]}" --weight HEAVY --proof-scope broad --proof-command 'pnpm test'

# macOS load includes I/O-bound tasks. A high load average with most CPU and memory
# still idle is headroom, not overload. The escape hatch requires BOTH signals;
# either one below threshold must still fail closed.
CASE_LOAD_1="25"
CASE_CPU_IDLE_PCT="60"
CASE_MEMORY_FREE_PCT="86"
assert_admitted \
  "an I/O-inflated heavy load with CPU and memory headroom" \
  "${BASE_ARGS[@]}" --weight HEAVY --proof-scope broad --proof-command 'pnpm test'

CASE_CPU_IDLE_PCT="49.99"
assert_rejected \
  "a high-load heavy lane without enough idle CPU" \
  "MACHINE_LOAD_HEAVY" \
  "${BASE_ARGS[@]}" --weight HEAVY --proof-scope broad --proof-command 'pnpm test'

CASE_CPU_IDLE_PCT="60"
CASE_MEMORY_FREE_PCT="49.99"
assert_rejected \
  "a high-load heavy lane without enough free memory" \
  "MACHINE_MEMORY_HEAVY" \
  "${BASE_ARGS[@]}" --weight HEAVY --proof-scope broad --proof-command 'pnpm test'

# The same instrument problem reaches LIGHT lanes. Measured 2026-08-06: 391 Chrome
# renderers held load at 564 on 10 cores while total Chrome CPU was 20.8%, CPU idle
# 63.3% and memory free 70%. A bare load ceiling refuses every LIGHT lane forever on
# such a desktop. The escape is the SAME two-condition test HEAVY uses, and either
# condition below threshold must still fail closed.
CASE_LOAD_1="60"
CASE_CPU_IDLE_PCT="63"
CASE_MEMORY_FREE_PCT="70"
assert_admitted \
  "an I/O-inflated light load with CPU and memory headroom" \
  "${BASE_ARGS[@]}" --weight LIGHT --proof-scope targeted --proof-command 'pnpm vitest run one.test.ts'

CASE_CPU_IDLE_PCT="49.99"
CASE_MEMORY_FREE_PCT="70"
assert_rejected \
  "a high-load light lane without enough idle CPU" \
  "MACHINE_LOAD_LIGHT" \
  "${BASE_ARGS[@]}" --weight LIGHT --proof-scope targeted --proof-command 'pnpm vitest run one.test.ts'

CASE_CPU_IDLE_PCT="63"
CASE_MEMORY_FREE_PCT="49.99"
assert_rejected \
  "a high-load light lane without enough free memory" \
  "MACHINE_MEMORY_LIGHT" \
  "${BASE_ARGS[@]}" --weight LIGHT --proof-scope targeted --proof-command 'pnpm vitest run one.test.ts'

CASE_LOAD_1="1"
CASE_CPU_IDLE_PCT="20"
CASE_MEMORY_FREE_PCT="86"


CASE_LOAD_1="5"
CASE_CPU_IDLE_PCT="20"
CASE_MEMORY_FREE_PCT="86"
CASE_FREE_SWAP_GB="3.99"
assert_rejected \
  "a heavy lane below four GB free swap" \
  "MACHINE_SWAP_HEAVY" \
  "${BASE_ARGS[@]}" --weight HEAVY --proof-scope broad --proof-command 'pnpm test'

CASE_LOAD_1="41"
CASE_FREE_SWAP_GB="8"
assert_rejected \
  "a light lane above four times core-count load" \
  "MACHINE_LOAD_LIGHT" \
  "${BASE_ARGS[@]}"

CASE_LOAD_1="1"
CASE_FREE_SWAP_GB="0.99"
assert_rejected \
  "a light lane below one GB free swap" \
  "MACHINE_SWAP_LIGHT" \
  "${BASE_ARGS[@]}"

CASE_LOAD_1="1"
CASE_FREE_SWAP_GB="16"
assert_rejected \
  "a missing immutable base SHA" \
  "FIELD_BASE_SHA" \
  "${BASE_ARGS[@]}" --base-sha ''

assert_rejected \
  "a product lane without a finalized execution contract" \
  "EXECUTION_CONTRACT_MISSING" \
  "${BASE_ARGS[@]}" --execution-contract ''

assert_rejected \
  "a product lane without its governing plan file" \
  "PLAN_FILE_MISSING" \
  "${BASE_ARGS[@]}" --plan-file ''

assert_rejected \
  "a product lane without a control-plane freeze manifest" \
  "FREEZE_MANIFEST_MISSING" \
  "${BASE_ARGS[@]}" --freeze-manifest '' --freeze-manifest-sha256 ''

assert_rejected \
  "an oracle-exact product lane without a capability receipt" \
  "CAPABILITY_RECEIPT_MISSING" \
  "${BASE_ARGS[@]}" --oracle-exact true

assert_rejected \
  "a project-specific executor policy without its overlay" \
  "PROJECT_OVERLAY_MISSING" \
  "${BASE_ARGS[@]}" --executor-policy codex-only --project-overlay ''

assert_rejected \
  "a caller that passes a codex-only overlay but declares policy any" \
  "OVERLAY_POLICY_MISMATCH" \
  "${BASE_ARGS[@]}" --executor-policy any --builder opus \
  --instruction 'Opus implements only the declared files.'

assert_rejected \
  "a product freeze that omits canonical Forge controls" \
  "FREEZE_REQUIRED_CONTROL_MISSING" \
  "${BASE_ARGS[@]}" --freeze-manifest "${TEST_INCOMPLETE_MANIFEST}" \
  --freeze-manifest-sha256 "${TEST_INCOMPLETE_MANIFEST_SHA}"

assert_rejected \
  "a product lane whose declared base does not resolve in its worktree" \
  "BASE_SHA_UNRESOLVED" \
  "${BASE_ARGS[@]}" --base-sha bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

assert_rejected \
  "a product lane whose declared base is not worktree HEAD" \
  "BASE_HEAD_MISMATCH" \
  "${BASE_ARGS[@]}" --base-sha "${TEST_PARENT_SHA}"

assert_rejected \
  "a product lane whose worktree is not a Git repository" \
  "WORKTREE_NOT_GIT" \
  "${BASE_ARGS[@]}" --worktree "${TEST_TMP}"

assert_rejected \
  "a product lane whose worktree is only a repository subdirectory" \
  "WORKTREE_NOT_ROOT" \
  "${BASE_ARGS[@]}" --worktree "${TEST_REPO}/scripts"

assert_rejected \
  "a product lane with a stale freeze-manifest hash" \
  "FREEZE_MANIFEST_HASH_MISMATCH" \
  "${BASE_ARGS[@]}" \
  --freeze-manifest-sha256 ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff

print -r -- 'mutated control plane' > "${TEST_FREEZE_TARGET}"
assert_rejected \
  "a product lane whose freeze manifest no longer matches its files" \
  "FREEZE_CONTENT_MISMATCH" \
  "${BASE_ARGS[@]}"
print -r -- 'approved control plane' > "${TEST_FREEZE_TARGET}"

typeset TEST_SELF_MANIFEST="${TEST_TMP}/self-referential.sha256"
typeset TEST_SELF_MANIFEST_SHA
{
  sha256sum "${TEST_FREEZE_TARGET}"
  sha256sum "${TEST_EXECUTION_CONTRACT}"
  sha256sum "${TEST_PROJECT_OVERLAY}"
  print -r -- "0000000000000000000000000000000000000000000000000000000000000000  ${TEST_SELF_MANIFEST}"
} > "${TEST_SELF_MANIFEST}"
TEST_SELF_MANIFEST_SHA="$(sha256sum "${TEST_SELF_MANIFEST}" | awk '{ print $1 }')"
assert_rejected \
  "a freeze manifest that lists itself" \
  "FREEZE_MANIFEST_SELF_REFERENCE" \
  "${BASE_ARGS[@]}" --freeze-manifest "${TEST_SELF_MANIFEST}" \
  --freeze-manifest-sha256 "${TEST_SELF_MANIFEST_SHA}"

typeset TEST_NO_CONTRACT_MANIFEST="${TEST_TMP}/no-contract.sha256"
typeset TEST_NO_CONTRACT_MANIFEST_SHA
{
  sha256sum "${TEST_FREEZE_TARGET}"
  sha256sum "${TEST_PROJECT_OVERLAY}"
} > "${TEST_NO_CONTRACT_MANIFEST}"
TEST_NO_CONTRACT_MANIFEST_SHA="$(sha256sum "${TEST_NO_CONTRACT_MANIFEST}" | awk '{ print $1 }')"
assert_rejected \
  "a freeze manifest that omits the execution contract" \
  "FREEZE_CONTRACT_MISSING" \
  "${BASE_ARGS[@]}" --freeze-manifest "${TEST_NO_CONTRACT_MANIFEST}" \
  --freeze-manifest-sha256 "${TEST_NO_CONTRACT_MANIFEST_SHA}"

assert_rejected \
  "an oracle-exact product lane with a stale capability receipt hash" \
  "CAPABILITY_RECEIPT_HASH_MISMATCH" \
  "${BASE_ARGS[@]}" --oracle-exact true \
  --capability-receipt "${TEST_CAPABILITY_RECEIPT}" \
  --capability-receipt-sha256 ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff

assert_admitted \
  "an oracle-exact product lane with a frozen capability receipt" \
  "${BASE_ARGS[@]}" --oracle-exact true \
  --capability-receipt "${TEST_CAPABILITY_RECEIPT}" \
  --capability-receipt-sha256 "${TEST_CAPABILITY_RECEIPT_SHA}"

typeset TEST_BAD_CAPABILITY_RECEIPT="${TEST_TMP}/bad-oracle-capability.txt"
typeset TEST_BAD_CAPABILITY_RECEIPT_SHA
typeset TEST_BAD_CAPABILITY_MANIFEST="${TEST_TMP}/bad-oracle-capability.sha256"
typeset TEST_BAD_CAPABILITY_MANIFEST_SHA
{
  print -r -- 'version=1'
  print -r -- 'receipt_id=test-bad-oracle-capability'
  print -r -- "contract_sha256=${TEST_EXECUTION_CONTRACT_SHA}"
  print -r -- 'mechanism=disposable apply_patch probe'
  print -r -- "disposable_target=${TEST_CAPABILITY_ARTIFACT}"
  print -r -- 'exit=0'
  print -r -- "oracle_sha256=${TEST_CAPABILITY_ARTIFACT_SHA}"
  print -r -- 'observed_sha256=0000000000000000000000000000000000000000000000000000000000000000'
} > "${TEST_BAD_CAPABILITY_RECEIPT}"
TEST_BAD_CAPABILITY_RECEIPT_SHA="$(sha256sum "${TEST_BAD_CAPABILITY_RECEIPT}" | awk '{ print $1 }')"
{
  sha256sum "${TEST_FREEZE_TARGET}"
  sha256sum "${TEST_PLAN}"
  sha256sum "${TEST_ACT2_REVIEW_LOG}"
  sha256sum "${TEST_EXECUTION_CONTRACT}"
  sha256sum "${TEST_PROJECT_OVERLAY}"
  sha256sum "${TEST_BAD_CAPABILITY_RECEIPT}"
  sha256sum "${FORGE_SKILL}"
  sha256sum "${FORGE_ENGINE}"
  sha256sum "${ROUTING_POLICY}"
  sha256sum "${GUARD}"
  sha256sum "${GUARD_TESTS}"
  sha256sum "${SOL_LIVENESS}"
  sha256sum "${WATCHDOG}"
  sha256sum "${WATCHDOG_TESTS}"
} > "${TEST_BAD_CAPABILITY_MANIFEST}"
TEST_BAD_CAPABILITY_MANIFEST_SHA="$(sha256sum "${TEST_BAD_CAPABILITY_MANIFEST}" | awk '{ print $1 }')"
assert_rejected \
  "an oracle capability receipt whose observed bytes differ from the oracle" \
  "CAPABILITY_MISMATCH" \
  "${BASE_ARGS[@]}" --oracle-exact true \
  --capability-receipt "${TEST_BAD_CAPABILITY_RECEIPT}" \
  --capability-receipt-sha256 "${TEST_BAD_CAPABILITY_RECEIPT_SHA}" \
  --freeze-manifest "${TEST_BAD_CAPABILITY_MANIFEST}" \
  --freeze-manifest-sha256 "${TEST_BAD_CAPABILITY_MANIFEST_SHA}"

print -r -- 'dirty' > "${TEST_REPO}/untracked.txt"
assert_rejected \
  "a product lane whose worktree is actually dirty despite a clean declaration" \
  "WORKTREE_DIRTY" \
  "${BASE_ARGS[@]}"
rm -f -- "${TEST_REPO}/untracked.txt"

assert_rejected \
  "a heavy proof underdeclared as a light lane" \
  "WEIGHT_UNDERDECLARED" \
  "${BASE_ARGS[@]}" --proof-scope broad --proof-command 'pnpm gate'

assert_rejected \
  "a new feature lane above the unmerged-branch cap" \
  "WIP_CAP" \
  "${BASE_ARGS[@]}" --unmerged-branches 11

assert_rejected \
  "a successor without a report-consumption receipt" \
  "RECEIPT_REPORT_MISSING" \
  "${BASE_ARGS[@]}" --predecessor UNIT-0 --predecessor-builder codex

assert_rejected \
  "a successor without a cross-model review receipt" \
  "RECEIPT_REVIEW_MISSING" \
  "${BASE_ARGS[@]}" --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0"

assert_rejected \
  "a same-model receipt labeled cross-model review" \
  "RECEIPT_REVIEW_NOT_CROSS_MODEL" \
  "${BASE_ARGS[@]}" --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0" \
  --review-receipt \
  'predecessor=UNIT-0;builder=codex;reviewer=codex-sol;thread=thread-0;verdict=APPROVED;receipt_id=xr-0'

assert_rejected \
  "a successor whose cross-model review returned REVISE" \
  "RECEIPT_REVIEW_VERDICT" \
  "${BASE_ARGS[@]}" --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0" \
  --review-receipt \
  'predecessor=UNIT-0;builder=codex;reviewer=claude-opus;thread=thread-0;verdict=REVISE;receipt_id=xr-0'

assert_rejected \
  "a unit lane declaring .ai/HANDOFF.md" \
  "HANDOFF_FORBIDDEN" \
  "${BASE_ARGS[@]}" --file .ai/HANDOFF.md

assert_rejected \
  "recovery mode used by a non-recovery lane" \
  "RECOVERY_MODE" \
  "${BASE_ARGS[@]}" --mode recovery --clean-tree false

assert_admitted \
  "a normal light product lane with headroom" \
  "${BASE_ARGS[@]}"

# The escape from REVISE. A run whose plan verdict is REVISE — or whose review round
# died before returning one — must still be able to repair its control plane and run
# the review that produces the next verdict. These four cases are the direct inverse of
# the product-lane verdict rejections above.
assert_admitted \
  "a control-plane lane under a REVISE verdict" \
  "${BASE_ARGS[@]}" --lane-type control-plane --verdict REVISE

assert_admitted \
  "a control-plane repair lane without product freeze fields" \
  "${BASE_ARGS[@]}" --lane-type control-plane --verdict REVISE \
  --execution-contract '' --freeze-manifest '' --freeze-manifest-sha256 '' \
  --oracle-exact ''

assert_admitted \
  "a review lane under a REVISE verdict" \
  "${BASE_ARGS[@]}" --lane-type review --verdict REVISE

assert_admitted \
  "a control-plane lane with no verdict at all" \
  "${COMMON_ARGS[@]}" --lane-type control-plane

assert_admitted \
  "a review lane with no verdict at all" \
  "${COMMON_ARGS[@]}" --lane-type review

# Vendor lock gone: this is the exact declaration that used to be rejected as
# PRODUCT_BUILDER. Any model may be declared builder for any lane; routing is argued
# per unit in the plan, not hard-coded in the guard.
assert_admitted \
  "a Claude-side builder on a product feature lane" \
  "${BASE_ARGS[@]}" --executor-policy any --project-overlay "${TEST_ANY_OVERLAY}" \
  --builder claude-fable-5 \
  --instruction 'Fable implements only the declared files.'

assert_admitted \
  "an Opus builder on a product feature lane" \
  "${BASE_ARGS[@]}" --executor-policy any --project-overlay "${TEST_ANY_OVERLAY}" \
  --builder opus \
  --instruction 'Opus implements only the declared files.'

# Project overlays can narrow the global vendor-neutral default. A codex-only owner
# amendment requires Codex to execute product units while Opus reviews/orchestrates.
assert_rejected \
  "a Claude product builder under a codex-only project overlay" \
  "BUILDER_POLICY" \
  "${BASE_ARGS[@]}" --executor-policy codex-only --builder opus \
  --instruction 'Opus implements only the declared files.'

assert_admitted \
  "a Codex product builder under a codex-only project overlay" \
  "${BASE_ARGS[@]}" --executor-policy codex-only --builder codex

assert_admitted \
  "an Opus reviewer under a codex-only product-executor overlay" \
  "${BASE_ARGS[@]}" --executor-policy codex-only --lane-type review \
  --verdict REVISE --builder opus --instruction 'Opus reviews only; it writes no product code.'

# Widening the commit scan must not make the CORRECT contract sentence unspeakable.
# The safe-negative strip has to survive a full negated verb list, or every honest
# dispatch instruction would be rejected by the very words that forbid the action.
assert_admitted \
  "an instruction forbidding commit, push and merge" \
  "${BASE_ARGS[@]}" \
  --instruction 'Sol implements only the declared files and must not commit, push, or merge; the orchestrator does that after its own proof rerun.'

CASE_LOAD_1="5"
CASE_FREE_SWAP_GB="8"
assert_admitted \
  "one heavy product lane with the sole lease and headroom" \
  "${BASE_ARGS[@]}" --weight HEAVY --proof-scope broad --proof-command 'pnpm test'

CASE_LOAD_1="1"
CASE_FREE_SWAP_GB="16"
assert_rejected \
  "a recovery lane whose dirty declaration is stale" \
  "CLEAN_TREE_MISMATCH" \
  "${BASE_ARGS[@]}" --lane-type recovery --mode recovery --clean-tree false

print -r -- 'preserved recovery change' > "${TEST_REPO}/recovery-untracked.txt"
assert_admitted \
  "a dirty preserved tree in explicit recovery mode" \
  "${BASE_ARGS[@]}" --lane-type recovery --mode recovery --clean-tree false
rm -f -- "${TEST_REPO}/recovery-untracked.txt"

assert_admitted \
  "an integration lane despite the feature WIP cap" \
  "${BASE_ARGS[@]}" --lane-type integration --unmerged-branches 26

assert_admitted \
  "a successor with both valid receipts" \
  "${BASE_ARGS[@]}" --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0" \
  --review-receipt \
  'predecessor=UNIT-0;builder=codex;reviewer=claude-opus;thread=thread-0;verdict=APPROVED;receipt_id=xr-0'

CASE_LOAD_1="15"
CASE_FREE_SWAP_GB="3"
CASE_HEAVY_LOAD_FACTOR="2"
CASE_HEAVY_MIN_SWAP_GB="2"
assert_admitted \
  "environment threshold overrides are honored" \
  "${BASE_ARGS[@]}" --weight HEAVY --proof-scope broad --proof-command 'pnpm test'
CASE_LOAD_1="2"
CASE_FREE_SWAP_GB="64"
CASE_HEAVY_LOAD_FACTOR="1"
CASE_HEAVY_MIN_SWAP_GB="4"

# CP-2: the guard measures WIP itself and refuses caller-typed counts that
# disagree with the measurement.
CASE_WIP_MEASURED="7"
assert_rejected \
  "a declared WIP count that disagrees with the measured count" \
  "DECLARED_WIP_MISMATCH" \
  "${BASE_ARGS[@]}" --unmerged-branches 13

# CP-2: measured WIP governs the cap even when the caller declares nothing.
CASE_WIP_MEASURED="11"
typeset -a NO_WIP_ARGS
NO_WIP_ARGS=()
for (( _i = 1; _i <= ${#BASE_ARGS[@]}; _i++ )); do
  if [[ "${BASE_ARGS[_i]}" == "--unmerged-branches" ]]; then
    (( _i += 1 ))
    continue
  fi
  NO_WIP_ARGS+=("${BASE_ARGS[_i]}")
done
assert_rejected \
  "a feature lane above the cap with no declared count at all" \
  "WIP_CAP" \
  "${NO_WIP_ARGS[@]}"

CASE_WIP_MEASURED="7"
assert_admitted \
  "a feature lane under the measured cap with no declared count" \
  "${NO_WIP_ARGS[@]}"
CASE_WIP_MEASURED=""

# ============================================================================
# Execution-profile awareness (2026-08-03). sprint performs NO unit review and NO
# orchestrator proof rerun, so demanding a review receipt would make sprint
# unexecutable. Omitting the flag must NOT cheapen cadence.
# ============================================================================

assert_admitted \
  "sprint successor needs only a report receipt (no review receipt)" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0"

assert_rejected \
  "successor rejects a report receipt whose file is missing" \
  "RECEIPT_REPORT_FILE" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_TMP}/missing-report.md;receipt_id=rr-0"

assert_rejected \
  "successor rejects an empty report file" \
  "RECEIPT_REPORT_FILE" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_EMPTY_PREDECESSOR_REPORT};receipt_id=rr-0"

assert_rejected \
  "successor report receipt requires an absolute path" \
  "RECEIPT_REPORT_FILE" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  'predecessor=UNIT-0;consumed_by=claude-opus;report=relative-report.md;receipt_id=rr-0'

assert_rejected \
  "assured successor still requires a cross-model review receipt" \
  "RECEIPT_REVIEW_MISSING" \
  "${BASE_ARGS[@]}" --execution-profile assured \
  --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0"

assert_rejected \
  "an omitted profile defaults to assured and still demands the review receipt" \
  "RECEIPT_REVIEW_MISSING" \
  "${BASE_ARGS[@]}" \
  --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0"

assert_rejected \
  "sprint still requires the report-consumption receipt" \
  "RECEIPT_REPORT_MISSING" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --predecessor UNIT-0 --predecessor-builder codex

assert_rejected \
  "a supplied review receipt is validated even under sprint" \
  "RECEIPT_REVIEW_NOT_CROSS_MODEL" \
  "${BASE_ARGS[@]}" --execution-profile sprint \
  --predecessor UNIT-0 --predecessor-builder codex \
  --report-receipt \
  "predecessor=UNIT-0;consumed_by=claude-opus;report=${TEST_PREDECESSOR_REPORT};receipt_id=rr-0" \
  --review-receipt \
  'predecessor=UNIT-0;builder=codex;reviewer=codex;thread=t-1;verdict=APPROVED;receipt_id=vr-0'

assert_rejected \
  "an unknown execution profile is refused" \
  "FIELD_EXECUTION_PROFILE" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --execution-profile fast

# ============================================================================
# Guarded same-lane file-set expansion. NOT called atomic: scheduler state is
# caller-supplied, so this is a guarded check against the declared snapshot.
# ============================================================================

assert_admitted \
  "safe expansion of an already-active writer is admitted" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --file scripts/cp1-extra.txt \
  --parallel-file scripts/other-lane.txt

assert_rejected \
  "expansion overlapping another active lane is rejected" \
  "FILE_OVERLAP" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --file scripts/contested.txt \
  --parallel-file scripts/contested.txt

assert_rejected \
  "expansion requires the current declared-set snapshot" \
  "EXPAND_CURRENT_SET_MISSING" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand \
  --active-writer-worktree "${TEST_REPO}" \
  --file scripts/cp1-extra.txt

assert_rejected \
  "expanded declaration must be a superset of the current set" \
  "EXPAND_NOT_SUPERSET" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/not-in-expanded-set.txt \
  --file scripts/cp1-extra.txt

assert_rejected \
  "expansion of a worktree that is not an active writer is rejected" \
  "EXPAND_NOT_ACTIVE" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand

assert_admitted \
  "expansion does not consume lane-cap headroom at three active lanes" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --active-lanes 3 \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --file scripts/cp1-extra.txt

assert_rejected \
  "a launch may not claim an existing current file set" \
  "CURRENT_SET_ON_LAUNCH" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --current-file scripts/cp1.txt

assert_admitted \
  "a HEAVY expansion may retain its own sole heavy lease" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --active-lanes 1 \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --owns-heavy-lease true --heavy-leases 1 \
  --weight HEAVY --proof-scope broad --proof-command 'pnpm test' \
  --file scripts/cp1-extra.txt

assert_rejected \
  "a HEAVY expansion may not consume another lane's lease" \
  "HEAVY_LEASE" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --active-lanes 2 \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --owns-heavy-lease false --heavy-leases 1 \
  --weight HEAVY --proof-scope broad --proof-command 'pnpm test' \
  --file scripts/cp1-extra.txt

assert_rejected \
  "a lane cannot claim a heavy lease absent from the snapshot" \
  "HEAVY_LEASE_OWNERSHIP" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --active-lanes 1 \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --owns-heavy-lease true --heavy-leases 0 \
  --weight HEAVY --proof-scope broad --proof-command 'pnpm test' \
  --file scripts/cp1-extra.txt

CASE_WIP_MEASURED="11"
assert_admitted \
  "WIP cap does not strand an already-running feature expansion" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --active-lanes 1 \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --unmerged-branches 11 \
  --file scripts/cp1-extra.txt
CASE_WIP_MEASURED=""

assert_rejected \
  "a launch at three active lanes is still capped" \
  "LANE_CAP" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --active-lanes 3

assert_rejected \
  "launch still rejects a duplicate writer" \
  "DUPLICATE_WRITER" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --active-writer-worktree "${TEST_REPO}"

assert_rejected \
  "an unknown admission action is refused" \
  "FIELD_ADMISSION_ACTION" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --admission-action widen

# ============================================================================
# Guarded expansion of a mid-flight lane (2026-08-03). An expanding lane is by
# definition already writing, so its tree is expected to be dirty. A LAUNCH is not.
# ============================================================================

print -r -- 'in progress' > "${TEST_REPO}/expand-wip.txt"
assert_rejected \
  "dirty expansion rejects a changed path outside the expanded declaration" \
  "DIRTY_PATH_UNDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --clean-tree false \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --file scripts/cp1-extra.txt

assert_admitted \
  "expansion of an active writer with its expected dirty tree is admitted" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --clean-tree false \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --file scripts/cp1-extra.txt \
  --file expand-wip.txt

assert_rejected \
  "a NEW launch with a dirty tree is still rejected" \
  "DIRTY_TREE" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --clean-tree false

assert_rejected \
  "a dirty expansion still cannot overlap another lane" \
  "FILE_OVERLAP" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --admission-action expand --clean-tree false \
  --active-writer-worktree "${TEST_REPO}" \
  --current-file scripts/cp1.txt \
  --file scripts/contested.txt --parallel-file scripts/contested.txt \
  --file expand-wip.txt
rm -f -- "${TEST_REPO}/expand-wip.txt"

# ============================================================================
# Sprint admission overhead: freeze manifests / control hashes are assured-only.
# ============================================================================

assert_admitted \
  "sprint does not require a freeze manifest or its hash" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --execution-profile sprint \
  --freeze-manifest "" --freeze-manifest-sha256 "" --oracle-exact false

assert_rejected \
  "sprint still requires an explicit oracle-exact declaration" \
  "FIELD_ORACLE_EXACT" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --execution-profile sprint \
  --freeze-manifest "" --freeze-manifest-sha256 "" --oracle-exact ""

assert_rejected \
  "sprint rejects an invalid oracle-exact declaration" \
  "FIELD_ORACLE_EXACT" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --execution-profile sprint \
  --freeze-manifest "" --freeze-manifest-sha256 "" --oracle-exact maybe

assert_rejected \
  "assured still requires the freeze manifest" \
  "FREEZE_MANIFEST_MISSING" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --execution-profile assured \
  --freeze-manifest ""

assert_rejected \
  "sprint with oracle-exact true still requires a verified capability receipt" \
  "CAPABILITY_RECEIPT_MISSING" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --execution-profile sprint \
  --freeze-manifest "" --freeze-manifest-sha256 "" --oracle-exact true

assert_admitted \
  "sprint admits a valid directly verified oracle capability receipt" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --execution-profile sprint \
  --freeze-manifest "" --freeze-manifest-sha256 "" --oracle-exact true \
  --capability-receipt "${TEST_CAPABILITY_RECEIPT}" \
  --capability-receipt-sha256 "${TEST_CAPABILITY_RECEIPT_SHA}"

# ============================================================================
# Proof scope: weight comes from what the PROOF COMMAND runs, never from
# narrative words in --instruction. Before this, the word "build" in a human
# instruction forced HEAVY and serialized every ordinary feature lane.
# ============================================================================

assert_admitted \
  "targeted lane 1 of 3 is LIGHT and admissible" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --active-lanes 0 \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm vitest run src/lib/foo.test.ts'

assert_admitted \
  "targeted lane 2 of 3 is admissible alongside the others" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --active-lanes 1 \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm vitest run -t "renders the panel"'

assert_admitted \
  "targeted lane 3 of 3 is admissible; targeted work is not serialized" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --active-lanes 2 \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm vitest run src/components/panel.test.tsx'

assert_admitted \
  "an instruction containing the word build stays LIGHT when the proof is targeted" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm vitest run src/lib/foo.test.ts' \
  --instruction 'Codex implements the declared files so the feature can build and test cleanly. Do not commit.'

assert_rejected \
  "an unfiltered full test run declared targeted is rejected" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT --proof-command 'pnpm test'

assert_rejected \
  "a full gate declared targeted is rejected" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT --proof-command 'pnpm gate'

assert_rejected \
  "a typecheck declared targeted is rejected" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT --proof-command 'pnpm typecheck'

assert_rejected \
  "a production build declared targeted is rejected" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT --proof-command 'pnpm build'

assert_rejected \
  "a database rebuild declared targeted is rejected" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT --proof-command 'pnpm db:reset'

assert_rejected \
  "a full test piped to a log path is still broad" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm test | tee /tmp/proof.log'

assert_rejected \
  "go test all packages is broad despite its path-like token" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT --proof-command 'go test ./...'

assert_rejected \
  "one targeted test cannot launder a broad command in the same proof" \
  "PROOF_SCOPE_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm test && pnpm vitest run src/lib/foo.test.ts'

assert_admitted \
  "a targeted test may pipe its output to a log" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm vitest run src/lib/foo.test.ts | tee /tmp/proof.log'

assert_admitted \
  "a compound proof remains targeted when every test segment is targeted" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope targeted --weight LIGHT \
  --proof-command 'pnpm vitest run src/lib/foo.test.ts && pnpm vitest run -t renders'

assert_rejected \
  "a broad proof still requires HEAVY weight" \
  "WEIGHT_UNDERDECLARED" \
  "${COMMON_ARGS[@]}" --verdict APPROVED \
  --proof-scope broad --weight LIGHT --proof-command 'pnpm test'

typeset -a NOSCOPE_ARGS
NOSCOPE_ARGS=()
typeset -i _skip=0
for _a in "${COMMON_ARGS[@]}"; do
  if (( _skip )); then _skip=0; continue; fi
  if [[ "${_a}" == "--proof-scope" ]]; then _skip=1; continue; fi
  NOSCOPE_ARGS+=("${_a}")
done
assert_rejected \
  "an omitted proof scope defaults to broad and refuses a LIGHT lane" \
  "WEIGHT_UNDERDECLARED" \
  "${NOSCOPE_ARGS[@]}" --verdict APPROVED --weight LIGHT \
  --proof-command 'pnpm vitest run src/lib/foo.test.ts'

assert_rejected \
  "an unknown proof scope is refused" \
  "FIELD_PROOF_SCOPE" \
  "${COMMON_ARGS[@]}" --verdict APPROVED --proof-scope narrow

if (( FAILURES == 0 )); then
  print -r -- "PASS: ${CASES} dispatch-guard cases"
  exit 0
fi

print -r -- "FAIL: ${FAILURES} of ${CASES} dispatch-guard cases"
exit 1
