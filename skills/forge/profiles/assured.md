# Profile: `assured`

The full Forge discipline. Selected when `.ai/EXECUTION_PROFILE.md` says `assured`,
when the owner asks, or when no profile is discoverable.

A **red `sprint` proof does NOT select this profile** — it is fixed and re-run as the
same targeted proof. Escalation requires an actual wider-risk finding (a shared
contract, migration, auth or cross-tenant boundary, data loss), named when you escalate.

Use it where a defect is expensive to find late: auth, schema/migrations, concurrency,
payments, cross-tenant boundaries, anything irreversible.

## Per unit

| | |
|---|---|
| **Proof** | The unit's declared proof command, run by the executor **and re-run by the orchestrator itself** — a pasted result is not evidence. |
| **Review** | **Cross-model review of every unit** by whoever did not build it, with a receipt: reviewer identity, thread id, returned verdict. A same-model subagent review is never labelled cross-model. |
| **Gate** | Full integrated gate before the commit that closes the change. |
| **Succession** | A successor waits for both predecessor receipts — report consumption and cross-model review. |
| **File sets** | Exact declared file set, no globs; immutable base SHA. |

## Procedure

Acts 1–2 (grill, adversarial review): [`../references/acts-1-2.md`](../references/acts-1-2.md)
Acts 3–4 (build, orchestrated build): [`../references/acts-3-4.md`](../references/acts-3-4.md)

## Still binding

Everything in `sprint`'s safety list, plus the per-unit review/proof/gate above —
which are **`assured`-only** and must not be applied while `sprint` is active.
`dispatch-guard.sh` enforces admission, the three-lane cap, the single heavy lease,
writer/file isolation, the WIP cap and receipt-before-succession mechanically —
prose is not the enforcement.
