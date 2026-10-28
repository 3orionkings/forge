# Acts 3–4 in detail — assured execution (forge reference)

> **PROFILE NOTICE — this reference documents `assured` cadence.**
> Cross-model unit review, orchestrator proof rerun, fresh-database rebuilds and full
> per-unit gates described below apply **only under `assured`**. Under `sprint` the unit
> carries one smallest relevant proof, no review and no rerun — see
> [`../profiles/sprint.md`](../profiles/sprint.md). Loading this file does not change the
> active profile.
> **Do not load this procedure under `sprint`; the sprint profile is the complete
> execution procedure for that cadence.**


## ACT 3/4 — EXECUTION (complexity-routed; runs inside this skill)

Once the plan is APPROVED (Gate 1) and you've signed off, **assess complexity** per
`ENGINE.md` §5. A simple product build proceeds single-track on the default executor
(Sol 5.6 @ ultra) when the dispatch guard admits it. A complex build presents Gate 2's
routing/concurrency proposal — argued by both models, one line of reasoning per unit —
and waits. The user can adjust, stop, require serialization, or re-route any unit to
either side; no destination is forbidden to them.

Then run **Act 3** (simple) or **Act 4** (complex) directly below. Commands come from
`ENGINE.md` §4; don't reconstruct them.

### ACT 3 — BUILD (simple / narrow: single-track on the proposed executor)

For a small fix, single-system change, or mechanical migration. Tunables:
`SPEC_FILE=PLAN.md` · `MAX_FIX_ROUNDS=2` · `EXEC_LOG=PLAN-EXECUTION-LOG.md` ·
`PROOF_CMD` (from the plan; if absent, ask one question). `$ROUTE` defaults to
Sol 5.6 @ ultra; a single-track build that the plan routed elsewhere sets `$ROUTE`
(or dispatches the Claude-side executor) accordingly and logs the one-line reason in
`LOG_FILE` before the freeze. After freezing, all runtime entries go to `EXEC_LOG`.

1. **Build contract → temp file `$P`** (never inline-quote): GOAL · SPEC (read
   `SPEC_FILE`, implement exactly, report deviations not redesigns) · IMMUTABLE
   BASE SHA · BACKLOG ITEM IDs · EXACT DECLARED FILE SET · CONSTRAINTS · NON-GOALS ·
   PROOF COMMAND · WORK-LOG PATH · REPORT PATH · OUTPUT (files-changed report +
   proof + deviations). The contract must say **the executor never commits or merges**,
   whichever model the executor is. Create/verify the worktree first and capture the
   full base SHA from `git rev-parse`; never type or complete a prefix.
2. **Feasibility + freeze.** If the contract combines a byte/hash-exact oracle with a
   mandated writer, formatter, generator, or transport, run the disposable capability
   probe and write its separate receipt. Incompatibility returns to Act 2. Finalize the
   immutable Act-2 review log, then create a separate SHA-256 manifest over the plan,
   that immutable review log, `$P`, any project overlay, Forge skill/engine/routing,
   guard/tests/watchdog, and the capability receipt when required. The manifest never
   lists itself. Execution receipts go to a separate append-only execution log.
3. **Admission.** Immediately before launch, pass every `ENGINE.md` §8 declaration to
   `dispatch-guard.sh`: actual worktree/base/HEAD and clean state; absolute governing
   plan, immutable Act-2 log, execution contract, and project-overlay paths; manifest
   path + current manifest SHA-256; explicit `--oracle-exact`; and conditional
   capability receipt. The CLI policy must equal the overlay's machine-readable
   `executor_policy=` declaration. Any drift or rejection returns to review/re-freeze;
   no bypass. Recovery relaxes only actual cleanliness.
4. **Launch** — ENGINE.md §4c (fresh session, capture `THREAD_ID`); read the executor's
   report from its **per-run-unique** path (`/tmp/forge-build-$SLUG.txt` — never a
   shared fixed name; a stale file from a previous run parses exactly like a fresh
   success). >10-min work → `run_in_background: true`, then a loud
   `🔔 EXECUTOR FINISHED — <what> (exit ok/fail) — verifying now` banner on completion.
5. **Verify (the Claude-side orchestrator, always).** Read the complete diff like a
   contributor PR (correctness, spec fidelity, style, nothing out of scope). Rerun
   `PROOF_CMD` yourself — the executor's pasted output does not count as proof. Log
   reviewer identity, thread id, returned verdict, the full report-consumption receipt,
   and the current-run proof to the execution log.
6. **Fix loop.** Problems → write a new fix contract `$P2`, re-freeze and re-admit,
   then resume the SAME executor session (ENGINE.md §4d). After `MAX_FIX_ROUNDS` failed rounds, pause or re-dispatch
   a fresh recovery unit. Which model takes that recovery unit is a routing decision
   argued and logged like any other — including handing it to the *other* side, which
   is exactly what `/forge-recovery`'s watchdog does on repeat failure. The one thing
   that never transfers is commit authority.
7. **Autonomous checkpoint.** Present the 3-bullet summary + files changed + fresh
   proof output + rounds + deviations. Once the Claude-side orchestrator has read the
   complete diff and rerun proof successfully, **it commits autonomously; the executor
   never does.** There is no owner commit-approval gate.

### ACT 4 — ORCHESTRATED BUILD (complex: multi-model, parallel)

For multiple systems / non-trivial architecture / cross-cutting change. Composes
`superpowers:using-git-worktrees` + `superpowers:dispatching-parallel-agents`.
Tunables: `SPEC_FILE=PLAN.md` · `EXEC_LOG=PLAN-EXECUTION-LOG.md` ·
`MAX_ACTIVE_LANES=3` (total across every model and role) · `MAX_FIX_ROUNDS=2`.

1. **Decompose** the plan into the smallest units with a clear done-condition,
   immutable base SHA, backlog item IDs, exact declared file set, proof command,
   work-log path, and report path. Classify each pair independent (disjoint files →
   guard may admit parallel) or dependent (shared files/ordering → serial). When
   unsure, serialize. Unit lanes must not declare or edit `.ai/HANDOFF.md`.
2. **Gate 2 — routing proposal (argued, not preset).** Build the split by having both
   models argue it: each side proposes which units it should take and why, attacks the
   other's claim, and Claude arbitrates. Present the ENGINE.md §5 table — each unit, its
   **proposed** executor, scope, **one-line reason**, dependencies, worktree, declared
   files, weight, and proof — plus the concurrency plan, and write that table with its
   per-unit reasons into `LOG_FILE`. Defaults absent an argument: Sol 5.6 @ ultra
   (Codex side), Fable 5 (Claude side), leaning to Sol on token-heavy units for cost.
   **Stop and wait.** The user approves or redirects any unit to either side.
3. **Worktrees + contracts.** One isolated worktree per concurrent unit, named per the repo
   protocol (or `<model>/<slug>`). One writer maximum per tree; the guard rejects
   duplicate writers and overlapping declared file sets. Capture each full base SHA
   from Git and write its complete Act-3 contract before admission. Fix the absolute
   governing-plan, immutable Act-2-log, and active project-overlay paths too.
4. **Feasibility + freeze.** Run the Act-3 capability probe for every representation-
   exact unit, finalize its receipt, then create the separate control-plane manifest
   after all hashed inputs are final. A manifest never lists itself; runtime receipts
   go to the execution log, not the immutable Act-2 log.
5. **Admission.** From one current serialized scheduler snapshot, run the guard for
   each ready lane immediately before launch. Normal lanes require observed-clean Git
   roots at their declared base/HEAD. Explicit recovery may be dirty but must declare
   the observed state and passes every other proof/review/receipt/role/file/WIP/lease/
   machine check. No admission occurs before decomposition, routing, worktree, contract,
   capability, and freeze exist.
6. **Execute** only guard-admitted units, as routed at Gate 2, never more than three
   active lanes total:
   - **Codex-side unit** → `ROUTE="$SOL_ROUTE"` (or the Gate-2-approved Codex route);
     run Act 3's build steps (ENGINE.md §4c/§4d) in that worktree;
     `run_in_background: true`; `🔔 EXECUTOR FINISHED …` on completion.
   - **Claude-side unit** → dispatch a subagent at the routed model (default Fable 5)
     into that worktree with the same contract shape: immutable base SHA, exact
     declared file set, proof command, work-log path, report path, and **no commit
     authority**.
   Only one HEAVY lane may hold the machine-wide heavy/gate lease. Reading,
   patching, typechecking, linting, tests, and builds do not overlap freely.
7. **Review + succession receipts (every unit).** Review is **cross-model by
   construction: the reviewer is whichever side did not execute the unit.** Codex-executed
   work → the Claude-side orchestrator reads the complete diff and reruns proof.
   Claude-executed work → Codex/Sol reviews read-only (template 4a) *and* the
   orchestrator still reruns proof itself, because proof rerun is an
   orchestrator duty, not a reviewer duty. A same-model subagent review is not
   cross-model review — that substitution is how 0 of 23 review claims survived
   scrutiny on 2026-07-28. Before any successor starts, the predecessor must have both
   a report-consumption receipt and a cross-model review receipt containing reviewer
   identity, thread id, and `APPROVED` verdict; pass both to the guard. Failures return
   to the **same** builder up to `MAX_FIX_ROUNDS`, then re-route by the logged rules in
   step 5 of Act 3.
8. **Integrate.** The Claude-side orchestrator assembles passing units in dependency
   order, writes `.ai/HANDOFF.md` once in the uncommitted integration candidate, reads
   the complete combined diff, and reruns the integrated proof/full gate over that exact
   candidate. Only then does it commit, merge, and push autonomously. No executor —
   Codex or Claude — commits or merges; unit lanes never edit HANDOFF.
9. **Completion record.** Present per-unit summaries + combined diff summary +
   current-run integrated proof + deviations. There is no owner Gate 3; Claude's
   merge autonomy begins only after its complete-diff review and successful proof
   rerun.

> The four acts above run end-to-end inside `/forge`. Each is **also** a standalone
> skill for when you want just one piece — `/forge-review` (Act 2), `/forge-build`
> (Act 3), `/forge-orchestrate` (Act 4), `/forge-with-docs` (docs-aware Act 1) — and
> they share this same `ENGINE.md`, so behavior is identical whichever door you enter.
