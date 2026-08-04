# Forge recovery playbook — load only for a wrecked run

**Load only after `/forge-recovery` detects a real wreck signal:**
> Read `~/.claude/skills/forge-recovery/PLAYBOOK.md` and execute it throughout this session.

Rules an orchestrator must follow during **recovery and rejoin**. A healthy Forge run
uses `../forge/SKILL.md` and must not load this playbook pre-emptively. Each rule is here because
it was violated and the violation was measured. Where a rule is mechanically enforced,
the enforcing instrument is named — prose rules get followed off a cliff, which is how
`ENGINE.md` came to say *"launch immediately; do not hold for machine contention"* on a
machine that hit load 507.

---

## 1. Progress means the objective moved

Reading, planning, writing contracts and fixing dashboards are not delivery. On
2026-07-29 an orchestrator did all four for hours with units-landed at zero and reported
itself healthy. **Declare an objective metric per act and record `progress` only when it
moves** (`recovery-watchdog.sh`). If you have not moved it in 20 minutes, you are stalled
regardless of how busy you were.

**Never end a turn with status while safe work remains.** A turn that ends with "waiting
for the machine" and nothing running is a twelve-hour outage in the making.

## 2. Two failures is the limit, then hand to an eligible peer

Same unit, `FAIL_N=2` consecutive failures under one owner → **hand the stuck role to
an eligible peer** with a [handoff packet](./HANDOFF-PACKET.md). Project executor
overlays still bind: under Codex-only, transfer product execution to another Codex
executor, never to Claude. If no eligible peer exists, preserve and ask. Do not retry a
third time with the same approach. After both eligible owners have had a turn
(`HANDOFF_MAX=2`), escalate with one named ask and a default action, never a menu.

The taker may change approach freely. **The proof command is immutable.** Otherwise
handoff degenerates into passing the test by weakening it.

## 3. Claims are not facts — no receipt, no credit

- Under `assured`, "independently reviewed" requires a **reviewer thread ID and a returned verdict**.
  0 of 23 such claims once survived scrutiny; a same-model subagent review is not
  cross-model.
- A lane report is not consumed until you have **read it in full** and written a
  consumption receipt. Thirteen reports, zero read, and the most expensive one held the
  correction to a headline number.
- Re-run the "current" failure yourself before acting on it. A quoted failure is often
  historical.
- Sum-of-branch-ahead double-counts stacked history (349 claimed → 254 real). Report the
  **SHA union**.

## 4. One bounded unit, executable brief

Every brief: one backlog item or one tightly-coupled slice · **complete untruncated
text** · immutable base SHA · exact declared file set (no globs) · acceptance command ·
forbidden scope · expected output. A named reviewer is `assured`-only.

The worst brief measured — *"every table … every column capable of holding free text …
report the full inventory before fixing anything … fix what leaks"* — became the day's
single largest consumer (8h, 326.7M input tokens) and delivered nothing landable. The
best — *"One job: make `pnpm check:p0a-relineage` pass honestly. Nothing else."* —
finished in 16 minutes. **Length of brief correlates inversely with delivery.**

Never bundle unrelated items into one lane. Never ship a brief that ends mid-word.

## 5. The guard decides launches — and a refusing guard must route, not idle

No lane launches without `dispatch-guard.sh` printing `ADMIT:`. Every product call
includes the finalized contract, real Git base/HEAD/clean state, active profile and
active project overlay. `assured` additionally supplies the freeze manifest/hash;
either profile supplies a capability receipt when it declares an exact oracle. A `REJECT` is
stop-the-line: never override it, never "just this once" — that is the 28 July failure
by definition.

**But refusal is not an excuse to stop.** On rejection: run no-compute work, keep the
watchdog honest (side work does not count as progress), and past `ENV_MAX` **hand the
unit over** rather than waiting. A guard that refuses correctly still produced a day of
zero.

Caps: **3 active lanes total** (not per model), **1 heavy/gate process**, **10 unmerged
branches** before new feature lanes stop and integration drains.

## 6. Adjudicate liveness by the clock, never by a label

A model saying "still progressing" is a self-report, not evidence. Use
`sol-liveness.sh`: work-artifact mtime in **epoch seconds** (never local-vs-UTC strings),
summed CPU, repeated-stderr scan. Point `--work` at the **real work log**, never the
JSON chatter stream — the chatter stays fresh while the gate is frozen.

A dashboard label of "reading" that actually means "command not classified" once caused a
healthy lane to be killed and duplicated. Instrument the active command, PID, CPU, log
growth and commit count instead.

## 7. Freeze what you validate

A gate runs against an **immutable SHA in a dedicated worktree**. Do not move a branch,
merge, or regenerate artifacts under a live gate — doing so invalidated a correct
16-minute fix. Cheapest failing check first; a 37-minute serial gate is not a debugging
instrument.

## 8. Project roles bind; only the stuck role transfers

Use global routing narrowed by the active owner overlay. Where a project overlay
says so, Codex executes product code and the Claude-side driver commits under the active profile's check.
**Complete-diff review, cross-model unit review, and orchestrator proof rerun are
`assured`-only** — under `sprint` the executor's own proof stands
(`../forge/profiles/sprint.md`). There is no owner
approval gate. Whoever executes never commits or merges.

Unit lanes never edit `HANDOFF.md`; the integration owner writes it once. Under
`sprint`, it is covered by the one sprint-end pass; under `assured`, the unit review
and gate cover it before commit. Fifteen lanes editing it blocked integration for a day.

## 9. Preserve before you touch

Before any abort, cleanup or retry: binary patch + untracked manifest **outside the
repo**, exact HEAD/status recorded, named safety refs on detached tips. Forbidden without
explicit owner instruction: `reset`, `clean`, destructive `restore`, branch deletion,
force-push, rebase of landed commits.

## 10. Drain before you dispatch

Close one unit — read the report, apply the **active profile's** validation (under
`assured`: cross-model review + gate; under `sprint`: the one direct proof), land it or
reject it explicitly — **before opening its successor**. The failure mode is not slowness; it is 26
branches and 254 unique commits with nothing merged and no gate ever green (historical).

---

## The three that prevent the most waste

1. **A mechanical dispatcher that refuses execution before `APPROVED`, refuses lane
   four, and enforces one heavy gate.** (`dispatch-guard.sh`)
2. **Objective-progress watchdog with automatic cross-model handoff at two failures.**
   (`recovery-watchdog.sh`) — it catches the failure mode where everyone is busy,
   honest, and delivering nothing.
3. **One bounded item, immutable base SHA, exact file set, exact proof — apply the
   active profile's commit check and land it before starting its successor.**
