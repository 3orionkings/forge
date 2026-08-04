# Profile: `sprint`

Optimised for **landing work**. Validation is comprehensive but performed **once, at
sprint end** — not per unit.

Selected when the repo's `.ai/EXECUTION_PROFILE.md` says `sprint`, or the owner says so.
Owner instruction in chat always wins.

## Per unit

| | |
|---|---|
| **Proof** | **One** smallest relevant proof — the narrowest command that actually exercises the change. Run it once. |
| **Review** | **None.** No cross-model unit review, and **no orchestrator proof rerun**. |
| **Integration** | **Immediate.** A unit whose direct proof passes integrates and pushes straight away. |
| **Concurrency** | Maximum **three** lanes, declared file sets **disjoint**. |

## Explicitly NOT performed per unit

Every item below is **`assured`-only**. Doing any of them during `sprint` is the
cadence error this profile exists to prevent:

- Cross-model unit review.
- Orchestrator proof rerun (the executor's own proof result stands).
- Full per-unit gate.
- Fresh-database rebuilds.
- Governance/artifact regeneration, documentation campaigns.

## When the direct proof fails

**Fix and re-run that same targeted proof.** A red proof is an ordinary iteration, not
a promotion trigger — it does **not** move the unit to `assured`, and it does not add a
review or a gate.

Escalate to `assured` only on an **actual wider-risk finding** — evidence that the
defect reaches beyond the unit's declared scope (a shared contract, a migration, a
cross-tenant or auth boundary, data loss). Name the finding when you escalate; "the
proof failed twice" is not one.

## Declared file sets during parallel writing

Declared file sets are **binding, not advisory**, whenever more than one lane is
writing. They are the only thing preventing two lanes from silently overwriting each
other.

To write a path outside the declared set:

1. Stop before writing it.
2. Run **one guarded re-admission** — `dispatch-guard.sh --admission-action expand` with
   the full expanded set, checked against the union of active lane file sets. It does
   not consume lane-cap headroom. It is **not atomic**: the scheduler state is
   caller-supplied, so this is a guarded check against the declared snapshot, not a lock
   over a shared registry.
3. On `ADMIT:`, continue in the **same lane**. The lane is not restarted and no work is
   discarded.
4. On `REJECT:`, the path belongs to another writer — do not write it.

**Never write an undeclared path that overlaps another active lane.** Expansion is
cheap; an undetected concurrent overwrite is not.

## Once, at sprint end

One comprehensive pass: combined review · artifact/governance regeneration ·
`typecheck` · the full gate. This is not optional — it is the reason per-unit reviews
and gates can be skipped.

## Still binding (safety, not cadence)

These do not relax under `sprint`:

- `dispatch-guard.sh` admission before any lane launches.
- **One writer per worktree**; parallel units hold **disjoint** declared file sets.
- **The executor never commits or merges** — the orchestrator does.
- **Additive integration only.** Destructive Git (`reset --hard` over landed commits,
  force-push, `--amend` of pushed work, branch deletion, `clean`) requires explicit
  owner approval.
- No secrets in diffs, logs, commits or reports.
- Owner instructions override this file.
