# Recovery acts in detail (forge-recovery reference)

> **PROFILE NOTICE — per-unit review, orchestrator proof rerun and full per-unit gates
> below are `assured`-only.** Recovery honours the active profile: under `sprint` a unit
> carries one smallest relevant proof, no unit review and no rerun
> ([`../../forge/profiles/sprint.md`](../../forge/profiles/sprint.md)). Loading this file
> does not change the active profile.


Loaded on demand from `../SKILL.md`. The mechanics of each act.

## ACT 0 — WATCH (armed throughout; fires inside any act)

**Arm it before Act 1 and never disarm it.** Every unit is registered, and every act
declares the **objective metric** the watchdog measures. Activity is not progress: on
2026-07-29 the orchestrator read reports, wrote contracts and repaired a tracker for
hours while units-landed stayed at zero, and an activity-based check called that
healthy.

```bash
RUN=recovery-$(date +%Y%m%d)      # stamp once; reuse all run
WD="${FORGE_SKILLS:-$HOME/.claude/skills}/forge-recovery/recovery-watchdog.sh"   # FORGE_SKILLS: plugin installs
$WD init --run "$RUN" --unit R1 --owner sol --peer claude --role executor --objective "units landed"
$WD attempt  --run "$RUN" --unit R1 --result fail --detail "<verbatim failing command + tail>"
$WD progress --run "$RUN" --unit R1 --evidence "<hash/proof/commit proving the objective moved>"
$WD alive    --run "$RUN" --unit R1 --evidence "<exact sol-liveness.sh output>"
$WD block    --run "$RUN" --unit R1 --detail "<guard reject code + metrics>"
$WD check    --run "$RUN" --unit R1     # 0 OK | 10 handoff | 20 escalate
$WD status   --run "$RUN"               # JSON the peer or the owner reads directly
```

**Check cadence:** after every attempt, every guard rejection, and on a heartbeat
(`ScheduleWakeup`, ≤ `HEARTBEAT`) for any unit expected to exceed 20 minutes. A stalled
orchestrator emits no notification — that is the whole point.

| Trip | Meaning | Action |
|---|---|---|
| `REPEAT` | `FAIL_N` consecutive failed attempts under one owner | **Handoff to peer** |
| `STALL` | No objective progress for `STALL_T` | **Handoff to peer** |
| `BLOCKED` | A precondition has refused for `ENV_MAX` | **Handoff, block declared** |
| `PINGPONG` | Both models have had their turn | **Escalate to owner** |
| *(process-level)* | Build alive but work log frozen | `../forge/sol-liveness.sh` → §7 |

### The handoff (this is the mechanism, not a formality)

On `REPEAT`/`STALL`/`BLOCKED`, **hand the stuck role to an eligible peer.** Follow the
active project executor overlay: a Codex-only product route transfers to another Codex
executor, never to Claude. If the overlay leaves no eligible peer, park the preserved
unit and ask the owner; do not violate policy to keep the watchdog moving.
Use [`HANDOFF-PACKET.md`](../HANDOFF-PACKET.md) — the packet is deliberately small and
self-contained, modelled on the briefing that actually worked when the owner manually
told Sol "Claude was failing to fix this."

Invariants — all four were violated on 28 July and each one cost real work:
1. **One owner per unit, ever.** Freeze and preserve the failed attempt (Act 1 rules)
   *before* the peer starts. Never two models on one unit.
2. **Only the stuck ROLE transfers.** Role lock holds: if Claude is stuck as decider,
   Sol executes and **Claude still commits**. If the *committer* role is what is
   blocked, the unit **parks preserved** — the executor still never commits.
3. **The proof is immutable.** The taker may change approach freely; it may **not**
   change the acceptance command. Otherwise handoff becomes "succeed by weakening
   the test," which is how the 28th produced green-looking nothing.
4. **Symmetric within policy.** Either side can receive a role when no overlay forbids
   it. Project executor restrictions remain binding during recovery.

### Escalation (last resort, `PINGPONG` only)

One **named ask** plus **the default action you will take anyway** — never a menu.
"I can't launch; pick (a), (b) or (c)" produced twelve hours of zero. Correct form:
*"Pause Dropbox sync to free ~1.9 GB. If I hear nothing in 30 minutes I will proceed
with the no-compute queue and re-check the guard every 10."*

**Never end a turn idle while safe work remains.** If the guard refuses heavy work,
run no-compute work *and* keep the watchdog clock honest — side work does not count
as progress and will correctly trip `STALL`.

---

## ACT 1 — HALT & PRESERVE

**Objective metric:** preservation receipts written.

1. **Freeze dispatch.** No new lanes. Existing lanes are not killed yet.
2. **Adjudicate every live lane by evidence, never by a label** — `sol-liveness.sh`
   (work-artifact mtime in **epoch seconds**, summed CPU, repeated-stderr scan). On the
   28th a healthy lane was declared hung and relaunched, duplicating its work; the
   "reading" label meant only "command not classified."
3. **Preserve everything before touching anything.** For each dirty worktree: a binary
   patch **plus** an untracked-file manifest, written **outside the repository**, with
   exact HEAD, `status`, index diff and worktree diff. Every detached tip gets a named
   safety ref.
4. **Forbidden until the owner explicitly says otherwise:** `reset`, `clean`,
   destructive `restore`, `checkout .`, branch deletion, force-push, rebase of landed
   commits.

**Gate:** every live lane classified; every dirty tree has a receipt; every tip has a
ref; nothing deleted.

---

## ACT 2 — RECONSTRUCT

**Objective metric:** State Ledger sections completed.

Rebuild ground truth **from artifacts, not from claims**. The postmortem, the lane
reports and the dashboard are all *claims to verify*.

- **Void every unreceipted claim.** "Independently reviewed" without a reviewer thread
  ID **and** a returned verdict is void — 0 of 23 such claims survived scrutiny.
- **De-duplicate the commit arithmetic.** Sum-of-branch-ahead double-counts stacked
  history: 349 claimed vs **254 real unique**. Report the SHA union.
- **Re-run the "current" failure at HEAD.** A quoted failure may be historical — one
  was, against a commit whose artifacts had since been regenerated.
- **Read every lane report in full and record a consumption receipt.** On the 28th,
  reading them was worth more than any lane launched; the most expensive lane's report
  was never opened at all, and it contained the correction to a headline number.

**Gate:** a **State Ledger** exists — true act reached · verdict state · SHA-deduped
commit union · per-artifact `land-now | rework | dead` · the actually-current failing
check · every claim marked verified/refuted **with an artifact citation**.

---

## ACT 3 — REPAIR THE CONTROL PLANE

**Objective metric:** guard self-tests passing.

Fix the machine that permitted the failure **before** running product work through it.

- Run `../forge/dispatch-guard.test.sh` and `./recovery-watchdog.test.sh`. Both green,
  or repair them first.
- If a rule was missing or wrong, patch `../forge/SKILL.md` / `ENGINE.md` / the guard
  under the active profile: `sprint` uses the executor's focused self-test plus the
  path/safety check; `assured` adds complete-diff review and a driver rerun.
- **Append the lesson to [`PLAYBOOK.md`](../PLAYBOOK.md)** so it survives this run.
- A guard change without a self-test that proves the new refusal **and** a still-passing
  baseline admit is not done. ENGINE once said "launch immediately; do not hold for
  machine contention" — prose rules that contradict reality get followed off a cliff.

**Gate:** both suites green; every new rule has a refusal test; playbook updated.

---

## ACT 4 — RE-LEGALIZE

**Objective metric:** critique rounds closed.

**No product code is written in this act.**

1. Any open review round is **logged verbatim and answered** — every finding
   accepted/rejected **with a reason**. A `REVISE` that is bypassed and never written
   down is the single most expensive defect in the record.
2. Resume the **same review thread** across rounds (ENGINE §4b). Four rounds across four
   thread IDs is not a review loop; the reviewer re-litigates settled points.
3. Drive to `APPROVED` or an **explicitly logged deadlock**. Never fake convergence.
4. Write the **recovery ledger/plan** from Act 2's State Ledger — value-ordered,
   dependency-ordered, oversized work **split into bounded slices** (a 401-file sweep is
   not a unit), and name the genuinely dead work so it can be dropped without guilt.
5. Send the recovery plan itself for adversarial cross-model review. It is a plan; it
   gets the same treatment.

**Gate:** governing plan `APPROVED` (or logged deadlock) **and** recovery plan
`APPROVED`. Zero product edits so far.

---

## ACT 5 — DRAIN

**Objective metric:** units landed + gate state.

Now, and only now, product work — through the guard, one bounded unit at a time.

- **Cheapest proof first.** Run the narrow check that is currently failing before any
  full gate. A 37-minute serial gate is not a debugging instrument.
- **Freeze the target.** A gate runs against an **immutable SHA in a dedicated
  worktree**. Never move a branch under a live gate — that happened, and it invalidated
  a correct 16-minute fix.
- **Every unit carries a receipt set:** base SHA · exact declared files · executor
  proof output · commit SHA. `assured` additionally carries the driver's rerun and
  cross-model review ID + verdict.
- **Roles:** use global routing policy plus the active project overlay. Where the overlay
  says so, Codex executes product work and the Claude-side driver commits under the active profile's
  check. Complete-diff review and driver proof reruns are `assured`-only. Whoever
  executes never commits; no owner Gate 3 exists.
- **One HANDOFF writer.** Unit lanes do not touch it; the integration owner writes it
  once. `sprint` covers that content in the single sprint-end pass; `assured` covers it
  in the unit review and gate before commit. Fifteen lanes editing it blocked integration for a day.
- **Drain before you dispatch.** Above `WIP_CAP` unmerged branches, no new feature lanes.

**Gate:** `sprint` runs the full gate once at sprint end; `assured` runs it at the unit
boundary. In either case use a frozen SHA, land in dependency order, and keep the
backlog under `WIP_CAP`.

---

## ACT 6 — REJOIN

**Objective metric:** resume prompt written and the original plan re-entered.

1. Update the canonical `HANDOFF.md` (single writer). Recovery state living only in chat
   is why a restart needed a 200-line reconstruction prompt.
2. Write a **self-contained resume prompt**: assume the reader has no memory of the
   incident. State the factual current state, name the next unit **and its exact proof
   command**, and carry the rules that would have prevented the failure.
3. Re-enter the original `/forge` plan **at its true act** (Act 2 if the verdict was
   never closed; Act 3/4 if it was).
4. Exit recovery mode. Leave the watchdog armed.

**Gate:** HANDOFF current; resume prompt self-contained; original plan re-entered.

---
