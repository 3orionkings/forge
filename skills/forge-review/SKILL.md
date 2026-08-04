---
name: forge-review
description: Use when the user already has a plan or clear proposal and wants an adversarial cross-model review of that plan without a requirements interview or product build. Especially useful for auth, schema, concurrency, migrations, payments, and other high-cost decisions. Do not use for reviewing already-written code or trivial changes.
---

# FORGE-REVIEW — Adversarial Plan Review, Standalone

Act 2 on its own: you already have a plan (or a clear idea) and want the cross-model
stress test without the grill. Claude drafts/loads the plan; a **different** model
attacks it read-only and returns `VERDICT: APPROVED` or `REVISE`; Claude revises and
re-submits to the SAME session until APPROVED or `MAX_ROUNDS`.

Reach for it on high-stakes work — auth, schema, concurrency, migrations, payments.
Skip it for obvious or cheap changes.

## Unique to this skill

1. **Kickoff.** Confirm scope in one line. If no task was given, ask for it (one
   question). Then proceed — no round-by-round approvals.
2. **Plan.** Do real planning — read the relevant code, surface decisions and tradeoffs
   — and write `PLAN_FILE` with: Goal · Approach · **Key decisions & tradeoffs** (name
   the contestable choices so the reviewer has something to bite) · Risks / open
   questions · Out of scope. Initialize `LOG_FILE`.
3. **The loop.** First perform the engine/auth/git preflight in
   [`../forge/ENGINE.md`](../forge/ENGINE.md), then open Round 1 in a fresh read-only session
   ([`../forge/references/invocation-templates.md`](../forge/references/invocation-templates.md) §4a)
   and captures `THREAD_ID`; rounds 2..MAX **resume that same session** (§4b) so the
   reviewer never re-litigates settled points. Read **this round's own** verdict file —
   a per-run-unique path, never a shared fixed name — and confirm its mtime postdates
   the round's launch before trusting it.
4. **Each round:** append the full critique to `LOG_FILE`; on `REVISE`, Claude decides
   what is worth acting on (**Claude is final arbiter — the reviewer advises**), revises
   the plan, and appends what changed and what was rejected **with reasons**.
5. **Resolution.** On APPROVED, present the plan, a 3-bullet summary of what the argument
   improved, and the round count. On deadlock at `MAX_ROUNDS`, **do not fake
   convergence** — list each unresolved point with Claude's counter-position and hand it
   to the owner to break the tie.

Tunables: `MAX_ROUNDS=5` · `PLAN_FILE=PLAN.md` · `LOG_FILE=PLAN-REVIEW-LOG.md`.

**The reviewer is read-only every round** — `-s read-only` on the fresh call,
`-c sandbox_mode="read-only"` on every resume. That is a safety property, not cadence;
it never relaxes. **No product code is written in this skill**, at either profile.

## Handoff after the verdict

This skill ends at the plan verdict. If the user then asks to build, hand off to
[`../forge/SKILL.md`](../forge/SKILL.md) and its selected execution profile. Do not
load or restate Act-3/Act-4 cadence inside a plan-only review.

## Canonical references

[`../forge/ROUTING-POLICY.md`](../forge/ROUTING-POLICY.md) (routing) ·
[`../forge/profiles/`](../forge/profiles/) (cadence) ·
[`../forge/ENGINE.md`](../forge/ENGINE.md) (engine, templates, admission) ·
[`../forge/references/`](../forge/references/) (detail, on demand)

## What NOT to do

- Don't review already-written code — that's `/codex:review`.
- Don't let the reviewer edit files, ever.
- Don't skip the log — the argument transcript is the deliverable.
- Don't reconstruct `codex exec` commands from memory.
