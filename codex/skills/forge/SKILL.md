---
name: forge
description: Use when a non-trivial software change needs independent cross-model challenge or bounded multi-unit implementation.
---

# Forge

Forge is the shared authority for reviewed Codex-and-Claude builds. Codex remains
the driver and integration authority; external Claude work is bounded by typed bridge
tools, immutable work orders, admission, receipts, and the selected execution profile.

## Start here

1. Read the role and control boundary in [ENGINE.md](ENGINE.md).
2. Resolve the profile in this order: explicit owner instruction, repository
   `.ai/EXECUTION_PROFILE.md`, then `assured`.
3. Read [ROUTING-POLICY.md](ROUTING-POLICY.md) before assigning any unit.
4. Complete [Acts 1–2](references/acts-1-2.md). Neither act writes product code.
5. After Gate 1 is genuinely approved, select the narrow Act 3 or dependency-aware
   Act 4 procedure in [Acts 3–4](references/acts-3-4.md).

Owner instructions and repository policy always bind. Forge adds no authority beyond
the current task, repository, or environment.

## Profiles

| Profile | Cadence authority |
|---|---|
| [Sprint](profiles/sprint.md) | Smallest relevant proof per unit, immediate serial integration, one comprehensive sprint-end gate. |
| [Assured](profiles/assured.md) | Genuine cross-model receipt per unit, Codex complete-diff review and proof rerun, integrated gate. |

Gate 1 applies to both profiles. Gate 2 exists only for Assured complex work. A red
Sprint proof is fixed and rerun at the same scope; it never promotes the profile by
itself. There is no invented owner commit-approval gate.

## Non-negotiable authority

- Codex conducts the grill, decides what enters the plan, arbitrates routes and
  findings, verifies results, integrates, and alone may commit, merge, or push after
  owner and repository gates pass.
- Fable 5 at max is the default external reviewer and executor. Sol 5.6 at ultra is
  the default Codex-side route. Either model may own a routed unit and use native
  subagents.
- Same-model review is never called cross-model. The executor never commits, merges,
  or pushes.
- The bridge controls Claude only. Codex-native lanes use native Codex subagents.
- Never control Claude outside the [typed bridge contract](references/bridge-tools.md).
- Let the trusted plugin hook bind the active task workspace automatically. Never
  create or modify the reserved `_forge_workspace` tool input.
- No lane executes until executable admission returns `ADMIT`. At most three lanes
  may be active, with one machine-wide heavy lease, one writer per worktree, and
  disjoint parallel file sets.

## Load detail only when needed

| Need | Authority |
|---|---|
| Roles, model defaults, integration boundary | [Engine](ENGINE.md) |
| Profile-sensitive assignment and gates | [Routing policy](ROUTING-POLICY.md) |
| Grill and adversarial plan review | [Acts 1–2](references/acts-1-2.md) |
| Frozen and orchestrated execution | [Acts 3–4](references/acts-3-4.md) |
| Exact MCP surface for all Claude control | [Bridge tools](references/bridge-tools.md) |
| Executable guard and atomic reservation | [Admission](references/admission.md) |
| Owner budgets and provider pauses | [Usage limits](references/usage-limits.md) |
| Objective progress and recovery observation | [Liveness](references/liveness.md) |

## Stop conditions

Stop rather than improvise when Gate 1 has no approval, admission rejects, the owner
budget reaches its cutoff, a provider/policy pause occurs, the baseline or allowed
files drift, a required receipt is missing, or a requested route would require an
unapproved model substitution. Preserve the plan, worktree, logs, job and revision,
then ask for the decision that is actually missing.
