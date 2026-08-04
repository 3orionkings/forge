---
name: forge
description: Use when the user asks to forge, plan and build, or cross-model stress-test a non-trivial change before guarded execution. Use forge-review for plan review only, forge-build for one frozen work order, forge-orchestrate for a multi-unit build, and forge-recovery only for a stalled or stranded run. Do not use for trivial edits or review of already-written code.
---

# FORGE — Grill, Cross-Model Review the PLAN, Then Build at the Active Cadence

*(Act-2 cross-model review is of the **plan** and runs at both profiles. Per-**unit**
code review is cadence, and is `assured`-only — see the profile table below.)*

Four acts, one artifact (`PLAN-REVIEW-LOG.md`):

1. **Grill** (you ↔ Claude) — kills *building the wrong thing*. Interrogate until intent
   is locked, then write `PLAN.md`.
2. **Adversarial review** (Claude ↔ another model) — kills *a plan that sounds right but
   breaks*. Read-only, cross-model, ends `VERDICT: APPROVED` or `REVISE`.
3. **Build** (simple) / 4. **Orchestrated build** (complex) — kills *the right plan built
   wrong, or uncontrolled concurrency eating the machine*.

Acts 1–2 write **no product code**. Product code starts only after `APPROVED`.

## Pick the execution profile first

| Source, in order | |
|---|---|
| 1 | Explicit owner instruction in chat |
| 2 | The repo's `.ai/EXECUTION_PROFILE.md` |
| 3 | Otherwise `assured` |

- **[`profiles/sprint.md`](./profiles/sprint.md)** — smallest relevant proof once,
  **no per-unit review, no orchestrator proof rerun**, immediate integration, max three
  disjoint lanes, one comprehensive gate at sprint end. A failed proof means **fix and
  re-run that same targeted proof** — escalation to `assured` requires an actual
  wider-risk finding, not a red result.
- **[`profiles/assured.md`](./profiles/assured.md)** — per-unit cross-model review with
  receipts, orchestrator re-runs the proof itself, integrated gate before the closing commit.

The profile controls **cadence only**. It never relaxes the safety invariants below.

**Cross-model unit review, orchestrator proof rerun, fresh-database rebuilds and full
per-unit gates are `assured`-only.** Opening an Act 3/4 reference does not re-enable
them — the active profile still governs.

## Routing

Governed by **[`ROUTING-POLICY.md`](./ROUTING-POLICY.md)** — canonical, owner-set. Where
anything here conflicts with it, that file wins and this one is wrong.

**Under `sprint`:** apply the owner overlay — or the default executor if none —
**automatically**, and record one line saying which and why. No cross-model routing
debate, no Gate-2 wait.

**Under `assured`:** routing is **proposed per unit and argued** by both models, Claude
arbitrates, and the agreed split is logged with a one-line reason per unit; Gate 2
applies to complex builds.

At both profiles an explicit owner project overlay binds and is passed mechanically to
the guard. Absent a preference the defaults are **Sol 5.6 @ ultra** (Codex side) and
**Fable 5** (Claude side), leaning toward Sol for token-heavy units on cost.

## Safety invariants — binding at every profile

- **Owner instructions win**, always.
- **No lane launches without `dispatch-guard.sh` printing `ADMIT:`.** A `REJECT` is
  stop-the-line — never overridden, never "just this once".
- **Three active lanes total** (not per model); **one heavy/gate lease** machine-wide.
- **One writer per worktree**; parallel units hold **disjoint declared file sets**.
  Declared sets are **binding during parallel writing**. Widen one only via a single
  guarded re-admission through the guard (`--admission-action expand`), continuing in
  the same lane;
  never write an undeclared path that overlaps another active lane.
- **The executor never commits or merges.** The orchestrator owns the commit. Under
  `sprint` it checks only the admitted paths, overlap/safety state, and the executor's
  passing proof receipt; under `assured` it reads the complete diff and reruns proof.
  This is a git-safety property, not a vendor rule.
- **Additive integration only.** Destructive Git — `reset --hard` over landed commits,
  force-push, `--amend` of pushed work, branch deletion, `clean` — needs explicit owner
  approval.
- **No secrets** in diffs, logs, commits or reports.
- Codex is **read-only** through Acts 1–2 whatever the routing says.

## Where the detail lives — load on demand

| Need | File |
|---|---|
| Grill + adversarial-review procedure | [`references/acts-1-2.md`](./references/acts-1-2.md) |
| Assured-only Act-3/4 procedure | [`references/acts-3-4.md`](./references/acts-3-4.md) (`sprint` uses its profile file instead) |
| Engine, routes, preflight | [`ENGINE.md`](./ENGINE.md) |
| Exact `codex exec` templates | [`references/invocation-templates.md`](./references/invocation-templates.md) |
| Assured-only argued routing mechanics | [`references/routing-detail.md`](./references/routing-detail.md) (`sprint` applies overlay/default directly) |
| Usage limits, credit policy | [`references/usage-limits.md`](./references/usage-limits.md) |
| Liveness — is it hung? | [`references/liveness.md`](./references/liveness.md) · `sol-liveness.sh` |
| Admission declaration fields | [`references/admission.md`](./references/admission.md) · `dispatch-guard.sh` |
| Why these rules exist | [`references/incidents.md`](./references/incidents.md) |

Do not reconstruct `codex exec` commands from memory — use the templates verbatim.

## Gates

**Gate 1** — plan `APPROVED` (after Act 2), both profiles.
**Gate 2** — routing approval, **`assured` + complex builds only**; `sprint` applies the
overlay/default automatically and does not wait.
There is no owner commit-approval gate.

**Sprint commit check** — the whole of it: changed paths match the admitted set · no
overlap or safety breach · the executor's proof exited 0. Then the orchestrator commits
and pushes. No complete-diff review, no proof rerun, no unit gate.
**Assured commit check** — complete-diff review + the orchestrator's own proof rerun +
the integrated gate.

## What NOT to do

- Don't review already-written code — that's `/codex:review`.
- Don't skip Act 1; the grill is half the value.
- Don't let a same-model subagent review count as cross-model.
- Don't dispatch around the guard, or ask the owner for the removed commit gate.
- Don't run `assured` ceremony on a `sprint` unit — no unit review, no proof rerun, no
  per-unit gate — whatever a loaded reference describes.
- Don't promote a unit to `assured` just because its proof went red; fix and re-run it.
