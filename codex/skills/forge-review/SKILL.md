---
name: forge-review
description: Use when an existing implementation plan needs independent adversarial challenge before product work begins.
---

# Forge Review

Forge Review is standalone Act 2: Codex submits an existing plan to a bounded,
read-only external challenge. Codex is the plan arbiter; Claude advises. No product
code or execution occurs.

Read the shared [Act 2 authority](../forge/references/acts-1-2.md), [typed bridge
contract](../forge/references/bridge-tools.md), [review
admission](../forge/references/admission.md), and [usage
limits](../forge/references/usage-limits.md). Owner instructions and repository policy
bind.

## Freeze the review

Load or, from a clear owner proposal, draft `PLAN.md`. Initialize append-only
`PLAN-REVIEW-LOG.md`. These are the only
control artifacts Codex may edit in this skill. Record their hashes, the repository
identity and baseline, the active profile, owner budgets, and a finite `MAX_ROUNDS`
before external work. Use the owner's round bound or five by default.

## Run one external review thread

1. Call `forge_preflight` for the exact repository. Require ready state,
   `provider_called: false`, healthy containment, and the binding route—Fable 5 at max
   unless an owner or project overlay says otherwise. Stop on any failed preflight.
2. Call `forge_start` once with role=`review`, read-only instructions, an empty product
   file reservation, the pinned plan/log, a stable idempotency key, explicit budget,
   and executable review admission. Persist the returned job ID, revision, and
   attempt/session evidence. Never invoke a provider directly or select a session by
   recency or guesswork.
3. Observe only with `forge_status` or bounded `forge_wait`; retrieve a terminal
   report and its receipt with `forge_result`. Before continuation, verify its job,
   attempt, session, prompt, idempotency, and refreeze hashes against the stored
   request. A provider or policy pause is not a verdict.

Its report contains exactly one final line:

```text
VERDICT: APPROVED
```

or:

```text
VERDICT: REVISE
```

A missing, duplicate, variant, or text after the verdict does not close Gate 1.

## Arbitrate, revise, and resume

Append each full critique, the full terminal receipt content and artifact hash, and
Codex's disposition to `PLAN-REVIEW-LOG.md`. Accept, reject, or defer every finding
with evidence and reasons. Keep every prior round byte-for-byte append-only; before
resume, pin the resulting log hash in `refreeze`. Claude never edits the plan and
never becomes the final planning or integration authority.
A deferred finding keeps Gate 1 open until it is resolved or reported to the owner
through the bounded owner deadlock.

Every terminal review consumes one round. On exact `REVISE`, an invalid verdict, or
`APPROVED` with deferred or unresolved findings, while rounds remain, Codex updates
the plan/log and calls `forge_resume` on the same explicit job at the exact stored
revision. Supply a revised prompt, per-round idempotency key, and `refreeze` with the
unchanged baseline plus current plan/log hashes. Require the returned attempt/session
evidence to preserve the recorded session and reassess the whole plan.

Gate 1 closes only when the latest valid report ends in exact `APPROVED`, there are
zero deferred or unresolved findings, and the final plan/log/receipt identity is
pinned. `REVISE`, invalid output, stale identity, or a pause does not close Gate 1.
If `MAX_ROUNDS` is exhausted, report every unresolved position and the competing
reasons and enter the bounded owner deadlock. No terminal result leaves the
review open outside `MAX_ROUNDS`. Never fabricate convergence.

This skill ends at the plan verdict. Begin execution only through
[Forge](../forge/SKILL.md) after a genuine Gate-1 approval and a separate owner
request to build.
