# Acts 3–4

These acts begin only after Gate 1 binds an approved plan. Codex remains driver,
routing arbiter, verifier, integrator, and sole commit/merge/push authority. The
active [Sprint](../profiles/sprint.md) or [Assured](../profiles/assured.md) profile
controls cadence; neither profile relaxes admission or ownership safety.

Every Claude interaction uses the [typed bridge tools](bridge-tools.md). Codex-native
lanes use native Codex subagents. The bridge never controls a Codex lane, and a native
Codex delegation is never presented as a Claude bridge receipt.

## Choose Act 3 or Act 4

Use Act 3 when the approved change can be expressed as one coherent unit with one
baseline, one exact file set, one worktree, one proof, and one report. Use Act 4 when
there are independent or ordered units, multiple owners, disjoint parallel files, or
more than one proof/integration boundary.

Do not force a complex dependency graph into Act 3 to evade Gate 2. Do not split a
narrow unit into parallel ceremony merely to use all lanes.

## Act 3 — Frozen-unit execution

Act 3 executes exactly one unit. Fable 5 at max is the default external executor;
owner/project overlays or routing policy may assign the unit to a native Sol 5.6 at
ultra lane instead. Executor choice never changes Codex authority.

### Feasibility and capability

Before freezing work, Codex performs a **feasibility and capability** check. Confirm
the requested acceptance evidence can be produced with the available repository,
tools, permissions, sandbox/network policy, model route, owner budget, worktree, and
proof environment. Confirm the exact files are identifiable, the baseline exists,
and the route can use its required tools without widening authority. Record each
capability as present, absent, or owner-gated.

An absent material capability stops the unit before product work. Do not disguise an
infeasible route with a model fallback, weaker proof, broader file set, or optimistic
report. Return the smallest owner/repository decision needed, or re-plan through
Gate 1 when the approved design itself must change.

### Freeze the unit

Codex derives a versioned work order from the approved plan. Freeze it before any
product edit. The work order includes all of the following:

1. unit ID, approved plan identity, Gate-1 receipt, and active profile/source;
2. objective and observable acceptance criteria;
3. full **immutable baseline** SHA plus clean/status/content fingerprints;
4. absolute isolated **worktree** and repository identity;
5. **exact allowed files** as canonical repo-relative paths, no globs;
6. explicit exclusions and out-of-scope behavior;
7. exact executor family, model, effort, and native-subagent allowance;
8. predecessor report/consumption/review receipts, or explicit `none`;
9. structured **proof command** argv, expected evidence, proof scope, and
   LIGHT/HEAVY classification;
10. tool, permission, sandbox, network, secret, destructive-Git, and time/cost
    **constraints**;
11. owner-supplied wall, token, turn, and USD bounds;
12. real work-log and terminal-report artifact locations;
13. the **report contract** below;
14. the statement that the executor cannot commit, merge, push, publish, rebase, or
    widen the unit;
15. Codex as integration authority and the active profile's verification contract.

Hash or otherwise immutably identify the work order and every pinned control artifact.
Under Assured, include the full freeze manifest required by
[admission.md](admission.md). Under Sprint, retain its lean control set. Exact-output
oracles require a validated disposable capability receipt at either profile.

If any frozen field changes, the old unit is no longer executable. Stop, preserve the
old evidence, write a new work order identity, and obtain fresh admission. Ordinary
steering cannot change baseline, proof, budget, permission, route, profile, or weight.
Only the guarded same-weight file expansion in [admission.md](admission.md) may widen
the exact files, with prior/full sets and a fresh `ADMIT`.

### Report contract

The executor's report is structured and bounded. It states:

- unit ID, work-order hash, admitted job/attempt, requested and observed route;
- status: `DONE`, `BLOCKED`, or `FAILED`;
- concise implementation summary;
- exact files changed and confirmation that no other path changed;
- proof argv, exit code, relevant result summary, and artifact hashes;
- work-log and report artifact identities;
- deviations from the work order, which must be `none` for a completed unit;
- risks, follow-ups, and unresolved questions;
- explicit confirmation of no commit/merge/push;
- terminal receipt identity.

The report plus admission and terminal artifacts form the unit's **execution receipt**.
A prose claim without those bindings is not a receipt.

### Admit before execution

For a Claude-owned unit, Codex first performs the provider-free bridge preflight, then
submits the frozen role=`build` request with exact model/effort, owner budget,
idempotency key, and structured admission. The start call is the atomic admission and
reservation boundary described in [admission.md](admission.md).

The guard must return `ADMIT` before execution. The accepted evidence must bind the
same baseline, exact files, worktree, route, proof, controls, predecessor receipts,
profile, and report paths as the frozen unit. An accepted MCP envelope without a valid
guard receipt is not admission.

On `REJECT`, launch nothing. Record the exact code/reason and correct only the cause.
Never start a provider turn, edit product code, or ask an executor to work ahead while
waiting for capacity.

For a Codex-native route, Codex invokes the executable guard against the shared current
scheduler snapshot through `scripts/forge-native-lane.py reserve` before native
delegation and records its private receipt. The native lane counts against the same
global scheduler. Heartbeat while active and release after quiescence. The bridge
remains reserved for Claude control and may not manufacture the native lane.

### Execute without authority drift

The executor reads the complete frozen unit before editing. It may use native
subagents from its own model family within the same admitted scope and budget. Native
subagents do not gain separate writers, file sets, worktrees, lanes, heavy leases, or
commit authority.

During execution:

- write only exact admitted files in the admitted worktree;
- maintain one writer per worktree;
- run only the frozen proof command and allowed supporting commands;
- keep network disabled unless the frozen contract explicitly authorizes it;
- write objective/artifact progress to the real work log;
- never edit driver-owned handoff/control artifacts;
- never commit, merge, push, publish, rebase, checkpoint, or run destructive Git;
- stop before any unexpected path, baseline, dependency, permission, or proof change;
- return the report contract even when blocked.

Codex monitors Claude-owned work through typed status and bounded wait. Material
instruction changes use revision-checked typed steering only when they remain inside
the frozen contract. Native Codex lanes use native Codex monitoring. Apply
[liveness.md](liveness.md) to objective evidence, not narration.

A provider/policy pause preserves state and is never handoff-ready. A stalled verdict
does not kill anything. If cancellation is chosen, Codex uses only the typed bridge
cancellation operation and waits for quiescence evidence before re-admitting work.

### Verify and integrate

First validate the execution receipt, terminal state, admitted paths, actual changed
paths, baseline, worktree, route, proof artifact, and absence of executor Git
authority. A blocked/failed unit is preserved and returned to planning, a manual
owner handoff, or a fresh frozen unit; it is not integrated as partial success.

Under **Sprint**:

1. confirm changed paths match the admitted set;
2. confirm no overlap, writer, lease, baseline, permission, secret, or safety breach;
3. confirm the executor's smallest relevant proof exited zero and matches the receipt;
4. perform immediate **serial integration** as Codex;
5. do not add per-unit cross-model review, complete-diff review, Codex proof rerun, or
   per-unit gate;
6. retain the unit for the one comprehensive sprint-end gate.

Under **Assured**:

1. obtain a genuine cross-model review receipt from the other foundation-model family;
2. if Claude built, Codex performs the independent review natively; if Codex built,
   Claude review uses a read-only admitted bridge job tied to the exact patch/artifact;
3. resolve all material findings with reasons and require an approved review receipt;
4. Codex reviews the **complete diff** against the approved plan and frozen unit;
5. Codex reruns the proof from the admitted environment;
6. Codex performs serial integration and runs the integrated gate;
7. only then may Codex commit/merge/push, subject to actual owner/repository gates.

Same-model review never satisfies step 1. There is no invented owner commit-approval
gate. If a real repository policy requires approval, record and honor that policy.

### Bounded repair loop

Before execution, the plan declares a positive integer `MAX_FIX_ROUNDS`. A failed
proof, material review finding, or integration defect may enter at most that many
repair rounds. Each round identifies one bounded defect set, writes an exact fix
prompt, and preserves the preceding attempt and receipts.

For Claude-owned work, send the new fix prompt with `forge_resume` to the **same
explicit session**. Bind its new prompt hash and request hash, re-freeze the changed
PLAN/review controls or exact product state, and obtain a fresh `ADMIT` before the
next effect. For native work, re-freeze and re-admit the same native lease or create a
new frozen unit as required. Every round reruns the complete profile-required review
and proof; an old partial pass is not promoted.

If the defect requires a new route, proof/weight, baseline, permission, profile, or
non-guarded scope, stop this loop and return to the applicable owner/Gate decision.
At **fix-round exhaustion**, preserve all attempts, pause execution, and ask for a
manual owner handoff, re-planning, or non-recovery cancellation. Automated recovery
is disabled in this release. Never continue with an unbounded retry.

### Close Act 3

Append the route, admission hash, execution receipt, verification results, integration
identity, and remaining gates to the execution log. Mark product work landed only
after the active profile permits integration. Mark unrun live/provider checks as
unrun rather than green.

## Act 4 — Dependency-aware orchestration

Act 4 turns the approved plan into a bounded directed acyclic graph of frozen units.
Codex owns decomposition, route arbitration, scheduling, verification, and integration.

### Build the dependency graph

Create one row per unit with:

- stable unit ID and approved-plan acceptance criteria;
- predecessors and the exact artifact/report consumed from each;
- immutable baseline from which its worktree is created;
- exact files and explicit out-of-scope paths;
- executor family/model/effort and native-subagent permission;
- proof argv, expected evidence, scope, and weight;
- controls, oracle, budget, work log, report, and receipt paths;
- profile-specific reviewer and verification requirements;
- integration order and successor list.

An edge exists whenever a unit consumes another unit's code, schema, generated
artifact, decision, or report. Units that touch a shared file are not parallel: add an
edge or combine them. Compute topological order and reject cycles before routing.

Parallel-ready units must have **disjoint parallel file sets**. A unit's exact files
include generated outputs it will write, not only source inputs. Integration-owned
files belong to a later serial unit, never two concurrent builders.

Freeze each unit independently using Act 3's complete work-order and report contracts.
A successor baseline is the exact state that includes its required integrated
predecessors; do not launch it early against an imagined future merge.

Before routing the graph, repeat Act 3's feasibility and capability stage for every
unit and dependency edge. Verify predecessor artifacts exist, successor baselines can
be created, route/tool permissions are available, and the declared proof can run in
the isolated worktree. One infeasible dependency keeps its descendants unready; it
does not authorize speculative execution.

### Route the graph

Under Sprint, apply the binding owner/project overlay or default automatically and
record one reason per unit. There is no routing debate and no Gate 2.

Under Assured, both model sides may present material route arguments per unit. Codex
arbitrates based on planned complexity/effort, capabilities, context, dependencies,
risk, and review independence. Balance does not mean provider-credit equality.

For **Assured plus complex**, present the complete unit/dependency/routing table to the
owner at Gate 2 and wait for approval before any execution lane launches. Gate 2 does
not apply to Sprint or simple Assured work. A material post-approval route change
returns to Gate 2; no silent fallback or substitution is allowed.

Default routes remain Fable 5 at max externally and Sol 5.6 at ultra natively. Either
foundation model may execute a routed unit and use its own native subagents. Codex
still owns every route decision and all integration.

### Schedule isolated lanes

Create a separate isolated worktree for every simultaneously active product unit.
Bind each to its full baseline and exact file set. Before each launch, refresh the
shared scheduler and admission snapshot.

The scheduler enforces a **maximum of three active lanes total** across both model
families and **one machine-wide heavy lease**. It also enforces **one writer per
worktree**, disjoint files, exact baselines, machine headroom, WIP limits, and
profile-specific receipts. Three lanes is a cap, not a target.

Ready-queue order:

1. all predecessors integrated and required receipts consumed;
2. Gate 1 valid; Gate 2 valid when Assured plus complex;
3. immutable baseline/worktree created and rechecked;
4. exact files disjoint from all active writers;
5. lane slot and heavy lease available;
6. executable guard returns `ADMIT` and reservation is durable;
7. dispatch to the routed owner.

Claude-owned lanes use independent typed bridge jobs with explicit IDs and revisions.
Codex-native lanes use native Codex subagents. Either root may use its own native
subagents inside that root's unit, but it remains one lane and one writer.

Do not choose the most recent session, share a writable worktree between roots, or
allow one root to edit another lane. Cross-model review consumes an immutable patch or
artifact read-only.

A broad proof/build/gate holds the heavy lease for its actual duration. Targeted LIGHT
work may overlap only when machine headroom and file/worktree isolation pass. Release
reservations when terminal and quiescent before admitting successors.

### Monitor lanes and hand off manually

Track each lane by unit, owner, job/attempt or native-task ID, revision, worktree,
baseline, files, proof, work log, last objective evidence, and reservation. Report
state changes without generating polling chatter.

- DONE: retrieve/validate report and receipts; it is not yet integration approval.
- STALLED/STOPPED: preserve evidence and request a manual owner handoff; never race a live writer.
- provider/policy pause: escalate to owner, no handoff or substitution.
- REJECT: do not dispatch; correct admission state.
- changed contract: cancel/preserve as needed, freeze a new unit, and re-admit.

Automated recovery and role transfer are disabled in this release. Any replacement
requires an owner-approved, freshly planned and admitted unit; Codex integration and
commit authority never transfer.

### Verify completed units

Use Act 3's profile-specific verification for each terminal unit.

Sprint validates admitted paths, overlap/safety, and passing executor proof, then
places the unit into the serial integration queue immediately. It has no per-unit
cross-model receipt and no Codex proof rerun.

Assured obtains the genuine other-family review receipt, has Codex inspect the
complete diff and rerun proof, and records the result before integration. A successor
waits for predecessor report consumption and the Assured review receipt.

### Repair failed graph units

The graph declares one `MAX_FIX_ROUNDS` bound for each unit. Repair only the failed
unit and affected descendants; never rerun unrelated ready lanes for ceremony. Use
the Act 3 fix contract: exact fix prompt, same explicit session for a Claude resume,
re-freeze, fresh `ADMIT`, complete profile-required re-review/proof, and serial
integration. Recompute descendant baselines and receipts after a repaired predecessor.

At fix-round exhaustion, pause that unit and its descendants, preserve every lane and
artifact, release only quiescent reservations, and ask the owner for a manual handoff
or a new plan. Other dependency-independent units may proceed only if their files, baseline,
receipts, lane cap, and heavy lease remain valid.

### Integrate serially

Only Codex integrates, one unit at a time, in topological dependency order. Even when
multiple lanes finish together, integration is never concurrent.

For each unit:

1. lock the integration queue and refresh target branch/HEAD;
2. confirm the unit was verified under the active profile;
3. confirm predecessor integrations and receipts;
4. apply the unit additively without destructive Git;
5. resolve conflicts as new explicit work, not by silently discarding either side;
6. record integrated HEAD and release the unit reservation;
7. create/rebase a successor worktree only through repository-approved operations and
   freeze its new exact baseline before admission;
8. launch newly ready units within caps.

The executor's branch or patch is input to Codex, not authorization to land. Codex
alone may commit, merge, or push after the active profile and real owner/repository
gates pass.

### Final profile gate

After all units are integrated:

- Sprint runs one comprehensive sprint-end gate over the combined state, including
  required combined review and artifact/governance generation. It acquires the heavy
  lease if broad. Failure is fixed and the final gate rerun; do not retroactively
  invent per-unit Assured reviews.
- Assured confirms every genuine cross-model receipt and Codex rerun, reviews the
  integrated complete change, and runs the integrated gate. Resolve failures with a
  new frozen/admitted unit or explicit integration fix under repository policy.

No profile labels incomplete evidence as green. Provider checks not run remain open.

### Close the act

Codex appends a bounded execution summary:

- approved plan and Gate-1 identity;
- active profile/source and Gate-2 receipt only when Assured plus complex;
- final dependency graph and routes with reasons;
- every work-order/admission/execution/review/report-consumption receipt;
- lane and heavy-lease history;
- serial integration order and resulting HEADs;
- sprint-end or Assured integrated-gate results;
- remaining owner, repository, provider, or deployment gates;
- exact commits/merges/pushes performed by Codex, or `none`.

Release all reservations and verify no admitted writer remains. Preserve failed or
cancelled artifacts for manual handoff and audit. Report landed work separately from proposals
or unverified follow-ups. The act is complete only when the active profile's required
evidence is present and no execution lane remains ambiguous.
