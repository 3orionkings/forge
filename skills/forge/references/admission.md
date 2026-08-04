# Admission control (forge reference)

> **PROFILE NOTICE.** Admission safety is common; cadence is not. `sprint` uses the
> lean declarations stated below. Capability-freeze machinery, unit review, driver
> proof reruns, fresh databases, and per-unit gates are `assured`-only.


Instrument: `../dispatch-guard.sh` · tests: `../dispatch-guard.test.sh`


The 2026-07-28 incident disproved the old premise that build phases overlap
freely. Twenty-three concurrent lanes on a 10-core Mac drove peak load to 507 and
left only 0.93 GB swap free; product delivered was zero. Reading, patching,
typechecking, linting, testing, and builds all consume CPU and memory. **Build
phases do not overlap freely.** A lane launches only when
`./dispatch-guard.sh` exits 0 with `ADMIT:`. Any non-zero `REJECT: <code>:
<specific reason>` is stop-the-line.

The orchestrator supplies an explicit declaration. For product lanes the guard then
verifies the real Git root, base resolution, HEAD equality, observed clean/dirty state,
execution contract, and active project overlay. Under `assured` it also verifies the
control freeze; under either profile an explicitly oracle-exact lane must supply its
verified capability receipt.
Admission calls use one current scheduler snapshot and are serialized: after an admit,
record the new active lane (and its HEAVY lease, if any) before considering another.

Under `assured`, the orchestrator also completes these two preflights. `sprint` skips
both unless the unit explicitly declares an exact oracle, in which case only the
disposable capability receipt is required:

1. **Contract feasibility.** Any byte/hash-exact output combined with a mandated
   writer, formatter, generator, or transport is exercised on a disposable target.
   A separate version-1 receipt binds the contract SHA-256, mechanism, disposable
   artifact, exit 0, oracle SHA-256, and identical observed SHA-256. Incompatibility
   returns to Act 2 before a product attempt.
2. **Approved control-plane freeze.** First finalize the immutable Act-2 review log and
   execution contract. Then create a separate SHA-256 manifest covering the plan,
   immutable Act-2 log, execution contract, active project overlay, `SKILL.md`,
   `ENGINE.md`, `ROUTING-POLICY.md`, guard/tests/watchdog, and conditional capability
   receipt. The manifest never lists itself. Execution/proof receipts go to a separate
   hash-chained execution log. Drift requires reread, explicit verdict revalidation,
   and a new manifest. Immutable Git SHAs come from `git rev-parse`, never manual
   completion of an abbreviation.

Required shared declaration fields for every lane:

- governing plan verdict (binding only for product lanes; control-plane/review may
  declare `REVISE` or omit it);
- active lane count and active HEAVY/gate lease count;
- lane weight (`LIGHT` or `HEAVY`), `targeted|broad` proof scope, lane type, and
  normal/recovery mode;
- absolute worktree path, caller-observed clean-tree state, and builder identity;
- owner-declared project executor policy when present (`--executor-policy
  codex-only|claude-only`; global default `any`);
- full immutable base SHA, every backlog item ID, and exact canonical
  repo-relative declared file set (no globs);
- proof command, real work-log path, report path, and executor instruction;
- predecessor ID (`none` for an initial unit) and current unmerged-branch count;
- all active-writer worktrees and the union of active parallel file sets;
- on expansion, the lane's complete current file-set snapshot and whether it owns the
  existing HEAVY lease. The widened `--file` set must be a superset.

Additional required fields for every product lane at both profiles:

- absolute governing-plan and immutable Act-2-review-log paths;
- absolute finalized execution-contract path;
- absolute active project-overlay path. It must contain exactly one
  `executor_policy=any|codex-only|claude-only` declaration matching the CLI policy.

Profile-specific fields:

- both profiles: explicit `--oracle-exact true|false`;
- `assured`: absolute freeze-manifest path plus SHA-256;
- either profile with `--oracle-exact true`: absolute capability-receipt path plus
  SHA-256. `sprint` otherwise needs no freeze.

The guard enforces all of the following:

1. **Verdict — product lanes only.** No **product** lane (`feature`, `integration`,
   `recovery`) starts while the governing verdict is absent or `REVISE`; only
   `APPROVED` is admissible. Lanes that write no product code — `control-plane`,
   `review` — are admissible whatever the verdict says. *Why: recovery exists because
   a plan that did exist went wrong. A guard that refuses both the repair work and the
   review that would produce the verdict is a catch-22 with no exit* (`ROUTING-POLICY.md` §5).
2. **Roles — global default neutral; project overlays binding.** Any model may be declared builder for
   any lane; the guard checks that a builder *was declared and is recognized*, not
   which vendor it is. It rejects commit/publish/destructive-Git directions in the
   instruction, execution contract, or proof command — commit authority belongs to the
   Claude-side orchestrator under the active profile's commit check
   (`ROUTING-POLICY.md` §§1, 3). The global default is vendor-neutral, but an explicit owner project overlay
   is binding: `--executor-policy codex-only|claude-only` rejects the other family for
   product lanes while leaving review/control-plane lanes available for cross-model
   review and repair.
3. **Lane cap:** maximum **three active lanes total across the whole forge**, not
   three per model.
4. **Heavy lease:** maximum one broad-proof HEAVY process across the machine.
   A whole-suite test/lint, gate, typecheck, production build/compile, coverage run, or
   database rebuild is broad and HEAVY. A named file/path/pattern proof is targeted and
   may be LIGHT. The guard inspects the proof command and rejects under-declaration.
5. **Writer/file isolation:** one writer per worktree and disjoint exact file
   sets across parallel units. A duplicate writer or any file overlap is rejected.
   Expansion requires the prior set, a superset declaration, and every dirty Git path
   inside that expanded set; an active HEAVY lane may retain only its own sole lease.
6. **Git, freeze, capability, and recovery:** a product worktree must be the actual
   Git top level; its declared base must resolve exactly and equal HEAD. Under
   `assured`, the guard recomputes the separate freeze manifest and verifies its
   bindings. Under either profile, an oracle-exact lane gets its capability receipt
   verified directly. Normal launches require observed clean state; a guarded
   expansion of an active writer may carry its expected dirty tree. Recovery mode is
   only for a recovery lane and otherwise relaxes only cleanliness. No mode bypasses
   the active profile's commit check, receipt rules, roles, WIP cap, leases, machine
   admission, or executor-never-commits rule.
7. **WIP cap:** when unmerged branches exceed ten, reject every new product
   `feature` lane. Integration and recovery lanes remain eligible for the other
   admission checks. This cap exists because 2026-07-28 ended with 26 branches
   and 349 stacked commits, none landed.
8. **Receipts before succession:** both profiles require the predecessor's
   report-consumption receipt, whose absolute report path must exist and be non-empty.
   `assured` additionally requires a cross-model review
   receipt with predecessor, builder, reviewer identity, thread id, `APPROVED` verdict,
   and receipt id. If a review receipt is supplied under `sprint`, the guard validates
   it rather than accepting a fake one.
9. **HANDOFF ownership:** unit lanes may not declare or edit
   `.ai/HANDOFF.md`. The integration owner writes it once. Under `sprint` it joins the
   single sprint-end pass; under `assured` the unit's complete-diff review and gate
   cover it before commit/merge.

### Lane-weighted machine headroom

Machine admission distinguishes the work being proposed:

- **HEAVY** (a broad proof: whole-suite test/lint, gate, typecheck, production
  build/compile, coverage, or database rebuild): always reject
  when swap-growth headroom is below 4 GB. At load up to core count, admit on the
  normal rule. Above core count, admit only through the measured I/O-load escape:
  CPU idle must be at least 50% **and** system-wide memory free must be at least
  50%. If either probe is unavailable or either threshold fails, reject. This is
  not a load override; it distinguishes macOS I/O-wait load from real CPU/memory
  pressure using two independent headroom signals.
- **LIGHT** (edits plus a targeted named file/path/pattern proof): reject when free swap
  is below 1 GB or one-minute load exceeds four times core count.

The distinction is essential on this machine: its zero-lane desktop baseline can
sit near load 34 on 10 cores from Chrome (~2.9 GB), Spotlight `mds_stores`
(~2.3 GB), Dropbox, and Claude.app (~1 GB). One absolute threshold would refuse
every lane forever, which is not discipline; it is a broken instrument. Admission
must measure **headroom for this lane**, not absolute machine quiet.

Tests can override every metric and threshold deterministically:

```text
FORGE_GUARD_LOAD_1
FORGE_GUARD_FREE_SWAP_GB
FORGE_GUARD_CORE_COUNT
FORGE_GUARD_CPU_IDLE_PCT
FORGE_GUARD_MEMORY_FREE_PCT
FORGE_GUARD_HEAVY_MAX_LOAD_PER_CORE   # default 1
FORGE_GUARD_HEAVY_MIN_SWAP_GB         # default 4
FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_CPU_IDLE_PCT    # default 50
FORGE_GUARD_HEAVY_HIGH_LOAD_MIN_MEMORY_FREE_PCT # default 50
FORGE_GUARD_LIGHT_MAX_LOAD_PER_CORE   # default 4
FORGE_GUARD_LIGHT_MIN_SWAP_GB         # default 1
```

### Successor receipt shapes

Pass `--predecessor none` for an initial unit. A successor passes both
semicolon-delimited receipts:

```text
--report-receipt 'predecessor=UNIT-1;consumed_by=claude-opus;report=/abs/report.md;receipt_id=RR-1'
--review-receipt 'predecessor=UNIT-1;builder=codex;reviewer=claude-opus;thread=THREAD-ID;verdict=APPROVED;receipt_id=XR-1'
```

Every admitted contract still mandates a real work log at a known path
(`tee -a /tmp/<unit>-work.log`) so the §7 watchdog has a truthful liveness target
from minute one. A watchdog covering every running stream is part of launch, not
an afterthought: declare → guard → dispatch → watchdog → report → receipts.
