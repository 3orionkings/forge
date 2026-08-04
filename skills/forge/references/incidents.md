# Why these rules exist (forge reference)

Load when you want the evidence behind a rule, or are tempted to relax one.
Every rule in the pack traces to a measured failure, not a preference.

## 2026-07-28 — 23 lanes, ~88% of an allowance, zero product delivered

- Peak load **507 on 10 cores**; swap down to **0.93 GB free**. Build phases do not
  overlap freely: reading, patching, typechecking, linting, testing and builds all
  consume CPU and memory. → three-lane cap, one heavy lease, machine admission.
- **613 lane-path assignments over 499 unique paths**; 69 paths touched by ≥2 lanes,
  20 by ≥3. `.ai/HANDOFF.md` touched by 15 lanes, which alone blocked integration.
  → disjoint declared file sets, one writer per worktree, single HANDOFF writer.
- A `VERDICT: REVISE` was **bypassed and never logged**; four review rounds ran across
  four different thread IDs. → verdict gate on product lanes, same-session resume.
- **0 of 23** claimed cross-model reviews survived scrutiny; a same-model subagent
  review was labelled independent. → review receipts with reviewer id, thread, verdict.
- **13 lane reports, 0 read in full.** The most expensive lane's report contained the
  correction to a headline number. → report-consumption receipt before succession.
- 26 branches, 349 claimed commits (**254 real** — the rest was stacked double-count),
  none merged, no full gate ever green. → WIP cap, drain before dispatch, SHA union.
- Worst brief measured: *"every table … every column capable of holding free text …
  report the full inventory before fixing anything … fix what leaks"* — 8h, 326.7M
  input tokens, nothing landable. Best: *"One job: make `pnpm check:p0a-relineage`
  pass honestly. Nothing else."* — 16 minutes. → one bounded unit, executable brief.
- A healthy lane was killed as "hung" because a dashboard label read `reading`, which
  actually meant *command not classified*. → adjudicate liveness by the artifact clock,
  CPU and stderr, never by a label.
- A branch was moved under a live gate, invalidating a correct 16-minute fix.
  → freeze what you validate: immutable SHA in a dedicated worktree.

## 2026-07-29 — the orchestrator itself stalled, and nothing detected it

Alive, honest and busy for ~12 hours — reading reports, writing contracts, repairing a
tracker — while **units landed stayed at zero**. An activity-based instrument reports
that as healthy. The owner detected it by manually asking for status, then manually
routed the work to the other model, which then made progress.

→ progress means the **objective metric** moved, not that work happened; automatic
cross-model handoff after two failed attempts; escalation carries one named ask **plus
the default action that will be taken anyway**, never an (a)/(b)/(c) menu. A guard that
refuses correctly still produced a day of zero — refusal must route, not idle.

## A rule that was followed off a cliff

`ENGINE.md` once said *"launch immediately; do not hold Sol for machine-contention
reasons"* on the machine that hit load 507. Prose rules get obeyed literally. Where a
rule matters, it is enforced by an instrument — `dispatch-guard.sh`,
`sol-liveness.sh`, `forge-recovery/recovery-watchdog.sh` — and the instrument has tests
proving it refuses.
