---
name: forge-build
description: Use when the user supplies an approved or frozen work order for one executor, including a bounded refactor, migration, bug fix with a known repro, or test-writing task. Use forge-orchestrate for multiple parallel units and forge for unresolved design. Do not use for tiny edits, code review, or work that requires Claude-session-only tools.
---

# FORGE-BUILD — Hand a Frozen Spec to One Model

Act 3 standalone: a locked spec goes to a single executor; the Claude-side driver
owns the outcome. Use it when the work reads as a work order for ONE model. For a
multi-unit split use `/forge-orchestrate`; to plan first use `/forge`.

## Unique to this skill

0. **Route first.** Read [`../forge/SKILL.md`](../forge/SKILL.md), resolve the active
   profile, and use [`../forge/ENGINE.md`](../forge/ENGINE.md) for preflight before any
   launch. The steps below do not override that profile.
1. **Preconditions.** `SPEC_FILE` must read as a work order. `git status -sb` — a
   **launch** needs a clean tree (a guarded expansion of an already-writing lane may
   carry its expected dirty tree).
2. **Build contract → temp file** (never inline-quote): GOAL · SPEC (implement exactly;
   report deviations, not redesigns) · KEY PATHS · CONSTRAINTS · NON-GOALS · **PROOF**
   (the exact command, and whether it is `targeted` or `broad`) · OUTPUT (files-changed
   report + proof output + deviations).
3. **Launch** per [`../forge/references/invocation-templates.md`](../forge/references/invocation-templates.md)
   §4c, capture `THREAD_ID`. Work over ~10 min runs backgrounded with a loud completion
   banner; check liveness by the clock, never a self-report
   ([`../forge/references/liveness.md`](../forge/references/liveness.md)).
4. **Fix loop.** Problems resume the SAME session (§4d) with a fix-list temp file, up to
   `MAX_FIX_ROUNDS` (default 2), then the driver takes over and logs it.
5. **Log.** Append the round and its verdict to `LOG_FILE`.

Tunables: `SPEC_FILE=PLAN.md` · `MAX_FIX_ROUNDS=2` · `LOG_FILE=PLAN-REVIEW-LOG.md` ·
`PROOF_CMD` (from the spec; if absent, ask one question).

## Execution profile

Before product execution, read [`../forge/SKILL.md`](../forge/SKILL.md) and the one
selected file in [`../forge/profiles/`](../forge/profiles/). Those sources alone own
cadence, review, proof, routing, and integration. Do not reconstruct or add ceremony
here. Every lane still passes [`../forge/dispatch-guard.sh`](../forge/dispatch-guard.sh).

## Canonical references

[`../forge/ROUTING-POLICY.md`](../forge/ROUTING-POLICY.md) (routing) ·
[`../forge/profiles/`](../forge/profiles/) (cadence) ·
[`../forge/ENGINE.md`](../forge/ENGINE.md) (engine, templates, admission) ·
[`../forge/references/`](../forge/references/) (detail, on demand)

## What NOT to do

- Don't review already-written code — that's `/codex:review`.
- Don't restate Act-3/4 cadence here; the profile owns it.
- Don't run `assured` ceremony under `sprint`, or wait for a Gate-2/Gate-3 that does not exist.
- Don't let the executor commit or merge.
