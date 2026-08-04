# FORGE ROUTING POLICY — canonical

Single source of truth for **which model does which work**, shared by `forge`,
`forge-review`, `forge-build`, `forge-orchestrate`, `forge-with-docs`, and
`forge-recovery`. Owner-set, 2026-07-29. Where any skill, ENGINE section, or guard
check disagrees with this file, **this file wins** and the other must be corrected.

## 1. Routing is profile-dependent; explicit project overlays always bind

Absent an owner project overlay, no role is locked to a vendor. Either side may
**execute**, **decide**, or **review**. How the assignment is *reached* depends on the
active execution profile (`profiles/sprint.md`, `profiles/assured.md`).

**Under `sprint` — apply, do not debate.** Apply the owner overlay, or the default
executor if no overlay applies, **automatically**. Record **one line** naming the
executor and why. There is **no cross-model routing debate and no Gate-2 wait**.

**Under `assured` — argue it.** Both models may argue the split, Claude arbitrates, and
the agreed split is written into the review log with a one-line reason per unit.
**Gate 2 applies to complex work.** At this profile a routing table that was never
argued is not a routing decision.

An explicit owner project/session overlay overrides the neutral default at **either**
profile and must be passed mechanically to the guard. For example, a
project may amend routing so that Codex executes product work, the **Claude-side driver
orchestrates and commits**, and **per-unit review occurs only under `assured`**. Such an
amendment remains binding until the owner explicitly rescinds it.

## 2. Defaults when the plan expresses no preference

| Side | Default model |
|---|---|
| Codex | **Sol 5.6 @ ultra** |
| Claude | **Fable 5** |

**Skew the split toward Sol** — it is cheaper per token, so token-heavy units (large
inventories, wide refactors, long build lanes) go to Sol unless the plan argues
otherwise. This is a lean, not a rule; the adversarial plan may override it with a
logged reason.

## 3. Commit authority is not a routing decision

Whoever executes a unit, **the executor never commits or merges**. The Claude-side
driver commits. That is a git-safety property, independent of which model wrote the
code, and it is vendor neutral: it binds a Claude executor exactly as it binds a Codex
executor.

**What the driver checks before committing is profile-dependent:**

| Profile | Commit check |
|---|---|
| **`sprint`** | Changed paths match the admitted set · no overlap or safety breach · the **executor's** proof exited 0. Then the driver commits and pushes. |
| **`assured`** | Complete-diff review **and** the driver's own proof rerun, plus the integrated gate. |

**Complete-diff review and orchestrator proof rerun are `assured`-only.** Under `sprint`
the executor's own proof result stands and the driver does not re-run it.

The owner has granted **merge autonomy** — no owner sign-off gate before commit.

## 4. Credit exhaustion — stop and ask, never silently degrade

When either side reaches **≤5% remaining allowance**, or is cut off outright:

1. **Stop.** Do not start another unit on that side.
2. **Ask the owner** which model to use.
3. **Propose one tier down on that side as the default:**
   - Claude side: Fable 5 → propose **Opus 5**.
   - Codex side: Sol 5.6 ultra → **no automatic proposal; ask the owner.**
4. Never fake, silently drop, or quietly degrade work to stay under a limit. State
   the position plainly and wait.

This supersedes any automatic degrade ladder for the ≤5%/cutoff case. ENGINE's Codex
ladder still applies to transient rate limits *above* that threshold.

## 5. What the guard enforces from this file

- **Verdict gate is for PRODUCT work only.** Lanes that write no product code —
  `control-plane`, `review` — are admissible whatever the governing verdict says.
  Product lanes (`feature`, `integration`, `recovery`) still require `APPROVED`.
  Rationale: recovery exists because a plan that *did* exist went wrong. A guard that
  refuses the repair work and the review that would produce the verdict is a
  catch-22 that cannot be escaped.
- **Global default neutral; project overlay binding.** Any model may build when no
  overlay applies. `--executor-policy codex-only|claude-only` binds product lanes for
  a project while leaving review/control-plane lanes open.
- **The executor never commits.** Reject an executor instruction that orders commit
  or merge, regardless of which model it is addressed to.
