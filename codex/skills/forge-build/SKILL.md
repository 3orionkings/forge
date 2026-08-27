---
name: forge-build
description: Use when an approved frozen work order defines one bounded implementation unit.
---

# Forge Build

Forge Build is standalone Act 3 for one coherent unit. Codex remains driver,
verifier, integrator, and sole commit/merge/push authority.

Read the shared [engine](../forge/ENGINE.md), [routing
policy](../forge/ROUTING-POLICY.md), [Acts 3–4](../forge/references/acts-3-4.md),
[admission](../forge/references/admission.md), [bridge
contract](../forge/references/bridge-tools.md), [liveness](../forge/references/liveness.md),
[usage limits](../forge/references/usage-limits.md), and the selected
[Sprint](../forge/profiles/sprint.md) or [Assured](../forge/profiles/assured.md)
profile. Owner and repository rules bind.

## Freeze one feasible unit

Require a genuine Gate-1 `APPROVED`. Resolve profile from owner instruction,
`.ai/EXECUTION_PROFILE.md`, then Assured; apply binding routing or the Fable 5 at max
external/Sol 5.6 at ultra native defaults.

Perform the feasibility and capability check across repository, tools, permissions,
sandbox/network, route, budget, worktree, files, and proof. A missing material
capability stops before product work; never weaken proof or authority.

Freeze an identified work order with:

- plan/Gate-1 identity, profile/source, objective, acceptance, repository, isolated
  worktree, immutable baseline and fingerprints;
- exact allowed files, exclusions, route/model/effort, predecessor receipts,
  structured proof argv, evidence, scope, and weight;
- tool/security/network/Git limits, owner-supplied budgets, work-log and report paths,
  and positive `MAX_FIX_ROUNDS` (default two);
- the report contract and Codex verification/integration duties.

The report contract binds the unit ID, work-order SHA-256, admitted job and attempt
IDs, requested and observed route, status (`DONE`, `BLOCKED`, or `FAILED`),
implementation summary, exact changed files plus confirmation that no other path
changed, proof argv, exit code, summary, and artifact hashes, work-log and report
artifact path, SHA-256, and receipt ID, deviations, risks, follow-ups and unresolved
questions, concrete terminal-receipt identity, and no commit, merge, or push
confirmation. Any frozen-field change needs a new identity and admission.

Treat an executor report the same way: a provider report is pre-terminal. Label it explicitly and never
ask it to predict a terminal receipt. After the terminal artifact exists, Codex must
produce and validate a driver-enriched final execution receipt containing every
bound field above and concrete identities; placeholder terminal text is invalid.

## Admit, execute, and observe

For Claude work:

1. Run provider-free `forge_preflight`; require ready state and
   `provider_called: false`.
2. Call `forge_start` once with role=`build`, exact route/budget, stable idempotency
   key, full prompt, and product admission. It is the atomic admission and reservation.
3. Require bound `ADMIT`; persist the returned job ID, attempt/session evidence, and
   revision. MCP success without that guard receipt is not admission.
4. Use `forge_status`, bounded `forge_wait`, and terminal `forge_result`; measure
   objective/artifact progress, not narration.

Never invoke Claude directly, shell-quote a prompt, or guess a session. On `REJECT`,
launch nothing.

For native work, construct the private exact guard request and use
`scripts/forge-native-lane.py` `reserve` before native delegation. Persist its receipt,
`heartbeat` while active, and `release` only after quiescence. Both routes use the
same global scheduler; the bridge never manufactures a Codex lane.

The executor changes only admitted files, runs proof, reports, and stops on baseline
or scope drift. It never edits driver controls or stages, commits, merges, pushes,
publishes, rebases, checkpoints, or runs destructive Git.

## Apply exactly one cadence

Validate receipt, baseline, paths, route, report, proof artifact, and executor Git
boundary first.

| Profile | Per-unit cadence |
|---|---|
| Sprint | Passing smallest relevant proof; no per-unit cross-model review and no Codex proof rerun; immediate serial integration; one comprehensive sprint-end gate later. |
| Assured | A genuine other-family review receipt; Codex inspects the complete diff; Codex reruns the proof; serial integration and integrated gate. |

Red proof alone never promotes Sprint; fix and rerun the same targeted proof unless a
named wider risk and proper authority change the profile.

## Repair or stop

Every terminal repair attempt consumes one round. Claude repairs use `forge_resume`
with one exact fix prompt on the same explicit job, same recorded session, and exact
stored revision; include a fresh idempotency key and typed product `refreeze` with
exact dirty-file hashes, require fresh `ADMIT`, and repeat verification.

For a native repair, quiesce the failed native agent and every descendant, then use
Git status and `git ls-files` to require that the full source Git dirty set exactly
equals the admitted file set and every dirty path is already tracked. Also run
`git diff --cached --quiet --exit-code` and reject any index delta. Enforce both gates
before patch capture or lease release. Any staged, untracked/new, extra, or missing
dirt fails closed: create no patch or successor, preserve the original worktree,
active lease, owner receipt, and content, and return a manual owner-handoff decision;
automated recovery is disabled in this release. The former
`new-unit/manual typed-recovery decision` is not an executable option.

When that gate passes, run `git diff --binary --full-index` over only the admitted
files. Create every patch, control, and report artifact beneath a canonical owner-only
`0700` parent. Publish with one descriptor using
`O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC` at `0600`, then `fchmod` and `fsync`. Pin and
recheck regular, single-link, owner, mode, device, and inode identity. Hash and consume
each artifact from one held descriptor/read sequence; symlinks, pre-existing links,
or pathname swaps fail closed. Store the patch bytes as an owner-private patch
artifact this way and verify its exact declared path set and SHA-256. Use the current owner receipt for authenticated
`release` of the current lease.

Freeze a fresh successor work order and attempt with the same full file contract,
proof, constraints, and weight plus an incremented fix-round count. Create a fresh
isolated clean worktree at the frozen baseline. Safely publish a hashed successor
control containing the predecessor lease and receipt hash; patch hash, size, and path
set; fix round and `MAX_FIX_ROUNDS`; and baseline and exact work-order, proof, and
report-contract hashes. Bind its control path and hash into the fresh private guard
request's canonical instruction. Separately publish an append-only native progress
work-log, distinct from the successor control, whose hash-chained events record the
actual gate, patch, predecessor release, clean worktree, control, admission, apply,
proof, report, quiescence, and successor-release evidence. Bind that progress artifact
through `--work-log`, then run actual
`scripts/forge-native-lane.py reserve` for a normal feature lane and require a new
`ADMIT` and owner-private receipt.

Only after `ADMIT`, parse the stored admission argv and guard-request hash. Re-read and
verify the control and patch through their held pins so the receipt plus stored
admission can reconstruct the successor after restart. Feed the already verified bytes
to `git apply --binary -` through stdin; never reopen an artifact pathname. Apply
without staging or committing, verify the exact declared dirty set and content hashes,
then dispatch the successor. Require its passing exact proof, complete report contract,
and quiesced executor and descendants before authenticated release. After release,
safely publish a separate owner-private terminal-release artifact binding lease ID,
attempt ID, status, revision, owner-receipt hash, release time, and evidence ID. The
driver-enriched final receipt must reference that release artifact's own path and
SHA-256, not the admission owner receipt. Any patch, file,
proof, constraint, weight, or other contract change is a new unit with full admission.
Both paths remain bounded by `MAX_FIX_ROUNDS`. Never hide failure in an unlinked job.

At fix-round exhaustion, preserve evidence and ask for manual handoff, re-planning,
or cancellation of a non-recovery job. A provider or policy pause, ambiguous effect, bad receipt, drift, or
unapproved route is not success. No model or provider substitution occurs without
owner approval.

Codex alone integrates and may commit, merge, or push after the profile and actual
owner/repository gates pass. No invented owner commit-approval gate exists. Record
unrun provider or deployment checks as unrun.
