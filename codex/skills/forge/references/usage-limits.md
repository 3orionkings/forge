# Usage limits and provider boundaries

Forge uses **owner-supplied** per-job bounds. Every live external start supplies an
explicit wall-time bound, token bound, turn bound, and USD bound; do not rely on
runtime defaults. **No default live provider calls** are authorized by loading or
validating this skill.

The typed budget shape is defined in [bridge-tools.md](bridge-tools.md). Budget is a
ceiling, not a target and not permission to spend without the task's authority.

## Before a live start

Record:

- exact unit and frozen scope;
- provider/model/effort route;
- `max_seconds`, `max_tokens`, `max_turns`, and decimal `max_cost_usd`;
- owner source for each bound;
- what artifacts will remain if the job stops;
- whether any external network access is separately authorized.

If the owner did not supply bounds, ask before starting. Offline validation, static
checks, local preflight, and fake-provider tests remain provider-free.

## Provider and policy states

The bridge classifies provider/auth/billing/credit/model/permission/sandbox/transport
outcomes. It stops at provider or policy states and preserves the terminal receipt,
events, stderr, report, worker log, job, attempt/session, revision, baseline, and
worktree.

`PAUSED_PROVIDER_ACCESS` and `PAUSED_POLICY` are preserved pauses. They are not
success, proof failure, permission to retry, route change, handoff readiness, or
evidence that a model does not exist. Inspect status/result, record the classification,
and ask for the missing owner/provider/policy decision.

## Five-percent boundary and cutoff

When reported remaining allowance is **at or below 5%**, or the side is cut off:

1. **stop** before starting another unit or turn on that side;
2. **preserve** every job, receipt, plan, log, and worktree artifact;
3. state which unit is in flight, which route stopped, and what remains usable;
4. **ask the owner** what route or timing to use and wait for the answer.

On the Claude side, propose **Fable 5 to Opus** as the one-tier-down option. It is a
proposal only. On the Codex side, ask without inventing a default substitute. Forge
may **never silently substitute** a model, effort, provider, executor, proof, or scope.

Do not infer a percentage the bridge/provider did not report. An observed cutoff or
owner-supplied figure is honest evidence; a guess is not.

## Model override and resume

A resume model override is still a substitution and needs owner approval. Approval
must name the new model and the job it replaces. Resume then uses the same explicit
job and exact revision through the typed bridge, which revalidates baseline and
admission. Never start a fresh unlinked job to disguise a failed resume or select an
implicit/latest session.

No automatic Fable-to-Opus change occurs. No fallback flag, fallback model, provider
swap, or reduced proof is supported by policy.

## Budget exhaustion

At a job's wall, token, turn, or cost ceiling:

- allow the bridge to stop according to its bounded contract;
- retrieve the receipt and account for observed usage provenance;
- do not replay a post-effect turn automatically;
- preserve partial work without calling it complete;
- ask whether to resume, re-route, reduce future scope, wait, or cancel.

Provider-authoritative, estimated, and unavailable usage are different evidence.
Label which one was returned. Never convert unavailable accounting into a fabricated
number.

## Transient transport

Follow the bridge's state and receipt. Forge permits **exactly one retry** only when
the failed transport ended **before the first token** and **before any tool effect**.
That retry uses the **same idempotency identity**, job, budget, model, baseline, and
admission. It is recovery of one pre-effect request, not a second turn.

**Never replay after any token or tool effect.** A timeout or ambiguous receipt is not
proof of no effect; preserve it and ask. Any post-effect continuation, new model, new
provider, expanded scope, or second transport failure requires an owner decision and,
when applicable, a freshly frozen/admitted attempt.

## Profile isolation under limits

Limits do not change profile semantics:

- Sprint still has no per-unit cross-model review or Codex proof rerun. A stopped
  executor proof is incomplete, not a reason to invent Assured ceremony.
- Assured still requires genuine cross-model review, Codex complete-diff review and
  proof rerun, and the integrated gate. A pause does not waive them.
- Gate 2 remains only Assured plus complex. A model change for approved complex
  routing returns to the owner decision; Sprint logs an owner-approved change without
  inventing Gate 2.

## Cancellation

If the owner chooses cancellation, request it only through the typed cancellation
tool in [bridge-tools.md](bridge-tools.md). Do not signal or kill from a liveness or
budget script. `INTERRUPTING` is not terminal; wait for the validated cancellation
receipt and descendant quiescence.
