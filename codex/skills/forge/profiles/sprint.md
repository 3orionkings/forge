# Profile: Sprint

Sprint favors short feedback and fast serial landing while retaining every admission
and authority invariant. It is selected only by owner instruction or repository
`.ai/EXECUTION_PROFILE.md`; Assured remains the fallback when neither selects a
profile.

## Per unit

| Concern | Sprint contract |
|---|---|
| Proof | One **smallest relevant proof**, run by the executor and recorded with exit status and artifacts. |
| Review | **No per-unit cross-model review.** |
| Codex rerun | **No Codex proof rerun.** |
| Integration | Codex performs **immediate serial integration** after path/overlap/safety checks plus the passing executor proof. |
| Parallelism | At most three active lanes total, one heavy lease, isolated worktrees, disjoint declared file sets. |

Sprint never waits at Gate 2. Routing applies the owner/project overlay or default
automatically and records one reason; it does not run a cross-model routing debate.

## A red proof

Fix the unit and rerun the same targeted proof. **Red proof alone never promotes Sprint.** Do not add a unit review, Codex rerun, complete-diff ceremony, or unit gate
because the test failed. A profile change requires an actual wider-risk finding and
the authority that selected the active profile.

Examples of wider risk include a shared schema, migration, authentication boundary,
cross-tenant behavior, destructive data path, or a dependency contract outside the
frozen unit. Name the evidence; repeated red alone is not evidence of wider scope.

## Commit check

Before Codex integrates a unit, check only:

1. changed paths are within the admitted exact file set;
2. no overlap, writer, baseline, lease, security, or repository-policy invariant was
   breached;
3. the executor's declared proof exited zero and its receipt matches the unit.

Then Codex may integrate immediately in dependency order, subject to real owner and
repository gates. No invented owner commit gate, complete-diff review, cross-model
unit review, or Codex proof rerun is added.

## Once at sprint end

Run **one comprehensive sprint-end gate** over the integrated change. It includes the
project's required combined review, artifact/governance regeneration, and full test,
lint, typecheck, build, or other declared final proof. The final gate holds the one
machine-wide heavy lease when it is broad.

Failure of the sprint-end gate is fixed against the integrated state and rerun. It is
not retroactively converted into per-unit Assured cadence.

## Safety that never changes

- Gate 1 exact approval before product execution.
- Executable admission before every lane; prose cannot admit.
- Maximum three active lanes total and one machine-wide heavy lease.
- One writer per worktree and disjoint parallel file sets.
- Immutable baseline and exact admitted paths.
- Executor never commits, merges, or pushes; Codex integrates.
- Owner/project overlays and no-substitution policy bind.
- Secrets, destructive Git, network, and permissions remain governed by repository
  and owner policy.

Read [admission](../references/admission.md), [Acts 3–4](../references/acts-3-4.md),
and [liveness](../references/liveness.md) only with this cadence still active.
