# Profile: Assured

Assured is the default when neither the owner nor `.ai/EXECUTION_PROFILE.md` selects a
profile. Use it when late discovery is expensive: security, auth, migrations,
concurrency, money, tenancy, destructive data paths, shared contracts, or other
irreversible/high-blast-radius work.

## Per unit

| Concern | Assured contract |
|---|---|
| Executor proof | The executor runs the frozen proof and returns artifacts and an execution receipt. |
| Independent review | A **per-unit genuine cross-model receipt** identifies the other model family, session/thread, exact diff or artifact reviewed, and verdict. |
| Codex verification | **Codex reviews the complete diff** and **reruns the proof** itself. A pasted executor result is not enough. |
| Gate | Codex runs the required **integrated gate** before closing the change. |
| Succession | A dependent unit waits for required report-consumption and cross-model receipts. |

A same-model subagent review may supplement quality but never satisfies the genuine
cross-model receipt.

## Routing and Gate 2

Both foundation-model sides may argue the route per unit; Codex arbitrates and logs
the reason. Gate 1 always applies. Gate 2 applies only when the Assured build is
complex: the owner approves the dependency-aware routing table before lanes launch.
Simple Assured work has no invented Gate 2.

## Verification sequence

1. Confirm admission evidence, immutable baseline, exact paths, executor identity,
   worktree, proof, report, and receipt hashes.
2. Obtain genuine cross-model review from the non-builder family. Claude review is
   controlled only through the [typed bridge](../references/bridge-tools.md); Codex
   review uses native Codex control.
3. Codex reads the complete diff, resolves every material review finding, and reruns
   the declared proof from the admitted environment.
4. Codex integrates serially in dependency order.
5. Run the project's integrated gate on the combined state. Broad gates hold the one
   machine-wide heavy lease.
6. Codex alone commits, merges, or pushes after real owner/repository gates pass.

## Safety that remains binding

- Exact Gate 1 approval; Gate 2 only for Assured complex work.
- Maximum three active lanes total and one machine-wide heavy lease.
- One writer per isolated worktree and disjoint parallel exact file sets.
- No baseline/file widening without a fresh admitted contract.
- No executor commit, merge, or push.
- No provider/model substitution without owner approval.
- Provider/policy pauses are preserved and never treated as handoff-ready.

A red Sprint proof does not select Assured. The profile changes only through a named
wider-risk finding and the authority that selected the active profile.

Procedure: [Acts 1–2](../references/acts-1-2.md),
[Acts 3–4](../references/acts-3-4.md), and
[admission](../references/admission.md).
