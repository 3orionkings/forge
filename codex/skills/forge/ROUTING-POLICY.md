# Forge routing policy

This is the shared authority for assigning work: owner and project overlays bind.
Routing never changes Codex's driver, arbitration, verification, integration, or sole
commit authority.

## Profile selection

Resolve once, before routing:

1. explicit owner instruction in the current task;
2. repository `.ai/EXECUTION_PROFILE.md`;
3. **Assured** by default.

Record the source. A failed proof does not change it. Red proof alone never promotes
Sprint; only a named wider-risk finding plus an authorized profile change can do so.

## Defaults and overlays

Absent a binding overlay, the defaults are Sol 5.6 at ultra for a Codex-native lane
and Fable 5 at max for an external Claude lane. Either foundation model may execute a
routed unit and use its own native subagents.

An owner instruction or project overlay may bind unit ownership, model, effort,
permissions, tools, network, or proof. Apply it exactly. No fallback or substitution without owner approval. In particular, provider pressure does not authorize moving a
unit, lowering a model, or presenting a same-model check as cross-model review.

Balance a multi-unit plan by **planned complexity and effort**, not provider-credit equality. Credit systems are not comparable accounting units. Costs may inform an
owner choice, but never fabricate parity.

## Sprint routing

Sprint applies the binding overlay, or the default when none exists, automatically.
Record **one logged reason** per unit. There is **no routing debate** and no Gate 2.
Codex still rejects a route that conflicts with owner/repository policy or executable
admission.

Sprint order:

1. apply overlay/default;
2. record executor and one reason;
3. freeze and admit the unit;
4. integrate serially after its path/overlap/safety checks and passing executor proof;
5. run one comprehensive sprint-end gate.

## Assured routing

Assured argues the route per unit. Codex records each side's material case, considers
capability, context, risk, expected effort, dependencies, and review independence,
then **Codex arbitrates**. The review log records the chosen owner and one reason.

For simple Assured work, Codex may proceed after Gate 1 and its logged route. For
complex Assured work, the complete dependency/routing table waits at Gate 2 for owner
approval before any execution lane launches. **Gate 2 applies only to Assured complex
work.** A later route change reopens Gate 2 only when it materially changes that
approved complex routing decision.

## Gates

- **Gate 1 applies to both profiles:** the external plan reviewer returns exact
  `APPROVED`, or the bounded unresolved set goes to the owner. `REVISE` is not approval.
- **Gate 2 applies only to Assured complex work:** owner approval of the proposed
  dependency-aware unit routing before lanes launch.
- No default Gate 2 exists for Sprint or simple Assured work.
- No invented owner commit-approval gate exists. Codex commits, merges, or pushes only
  after the active profile and real owner/repository gates pass.

## Review independence

When Assured requires unit review, the reviewer must be the other foundation-model
family. A Claude-built unit may receive Codex review; a Codex-built unit receives
Claude review through the [typed bridge](references/bridge-tools.md). A native
subagent of the executor's own family can improve quality but cannot produce the
cross-model receipt.

## Re-routing and limits

Do not silently re-route. At or below the allowance boundary, at a provider cutoff,
or at a policy pause, preserve the job and ask the owner. The only default proposal
on the Claude side is Fable 5 to Opus; it remains a proposal, not permission. See
[usage-limits.md](references/usage-limits.md).

## Routing log record

For each unit record:

- profile and profile source;
- binding overlay, or `none`;
- unit owner and exact model/effort;
- one reason (Sprint) or summarized arguments plus Codex's reason (Assured);
- required review family in Assured;
- Gate 2 receipt when and only when Assured plus complex;
- any owner-approved substitution, including what it replaced.
