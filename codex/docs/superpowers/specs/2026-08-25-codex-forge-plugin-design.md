# Codex Forge Plugin — Role-Reversed Dual-FM Design

> Release safety override: automated recovery is disabled in the shipped build.
> Recovery starts and all mutating controls on historical recovery jobs fail before
> durable or provider effects. Exact-job observation remains read-only; stalled work
> is preserved for a manual owner handoff. Recovery material below is retained as
> historical design context, not executable release behavior.

**Status:** Owner selected Approach B on 2026-08-25. This is the implemented v0.1.0
release design. The typed bridge, trusted workspace-binding hook and review/build
routes ship; automated recovery remains disabled by the safety override above.

## 1. Goal

Create a personal Codex plugin named `forge` that preserves the intent, command
surface, execution profiles, routing discipline, admission rules and recovery model
of the Claude Forge pack while reversing the controlling role:

- Codex is the initiating driver, plan arbiter, verifier, integrator and commit
  authority.
- Claude is the default external adversarial reviewer and default external executor.
- Either foundation model may own routed build units and may spawn its own native
  subagents.
- A same-model subagent review never counts as cross-model review.
- The executor never commits, merges or pushes. The Codex driver does so only after
  the active profile's checks and all governing repository/owner gates pass.

The plugin must work globally from Codex without modifying a product repository to
install the control plane.

## 2. Source authority and parity boundary

The existing source pack is `~/.claude/skills/forge*` (the `skills/` directory at the root of this repository). Its intact
`SKILL.md`, `ENGINE.md`, `ROUTING-POLICY.md`, profiles, admission guard, recovery
watchdog and tests define the behavioral baseline. Its
`forge/references/acts-3-4.md` is truncated in the installed copy and is not safe to
copy byte-for-byte. The Codex version will reconstruct the missing assured Act 3/4
procedure from the intact sources and test the reconstructed behavior.

OpenAI's Apache-2.0 `codex-plugin-cc` is an architectural reference for detached
jobs, streamed events, result retrieval, cancellation and native-child tracking. Its
source is not copied into v1. Forge uses one Codex `PreToolUse` hook solely to bind
the current trusted task workspace to MCP calls; it never invokes a provider.

The existing MIT `LICENSE` and `THIRD-PARTY-NOTICES.md` will be retained and adapted
because the Forge workflow derives from the existing local pack and its attributed
upstreams.

## 3. User-facing skill surface

The plugin exposes six Codex skills with the same meanings as the Claude pack:

| Skill | Role-reversed behavior |
|---|---|
| `forge` | Complete Acts 1–4 router. Codex grills, Claude reviews, then Codex dispatches the approved build. |
| `forge-review` | Read-only Claude adversarial review of an existing plan, with bounded revise/approve rounds. |
| `forge-build` | Execute one frozen unit, normally through a Claude root, while Codex verifies and integrates. |
| `forge-orchestrate` | Decompose and route a complex approved build across Codex and Claude lanes/worktrees. |
| `forge-with-docs` | Forge plus the existing context glossary and ADR lifecycle. |
| `forge-recovery` | Refuse automated recovery without mutating work or state, and direct the owner to a manual handoff. |

Skills remain normally discoverable rather than explicit-only. Owner instructions,
repository policy and authorization boundaries override plugin defaults.

## 4. Four-act workflow

### Act 1 — Grill

Codex interviews the owner one question at a time, explores discoverable repository
facts instead of asking the owner, and locks the agreed result in `PLAN.md`. It
initializes `PLAN-REVIEW-LOG.md`. No product code is written.

### Act 2 — Adversarial plan review

The bridge launches Claude/Fable in read-only mode against the repository and locked
plan. The same explicit Claude session ID is resumed for bounded rounds. Claude must
end each response with exactly `VERDICT: APPROVED` or `VERDICT: REVISE`. Codex
arbitrates findings, revises the plan where warranted, records accepted and rejected
points, and never fabricates convergence. Gate 1 is an actual `APPROVED`; a bounded
deadlock returns to the owner.

### Act 3 — Single frozen build

For narrow work, Claude is the default executor unless an owner or project overlay
routes the unit differently. A complete work order declares the immutable baseline,
exact file set, worktree, proof command, constraints and report path. Admission must
print `ADMIT:` before execution. The executor never commits. Codex applies the active
profile's verification and then owns integration/commit.

### Act 4 — Orchestrated build

For complex work, Codex decomposes the plan into dependency-aware units with disjoint
file sets and isolated worktrees. At most three active lanes and one machine-wide
heavy/gate lease are allowed. Codex-native lanes use Codex subagents; Claude-native
lanes use bridge-launched Claude roots that may use Claude subagents. Integration is
serialized in dependency order. Codex is the final routing arbiter; an assured
complex build retains owner Gate 2 before lanes launch.

## 5. Profiles and routing

Profile selection order remains:

1. explicit owner instruction in the current task;
2. repository `.ai/EXECUTION_PROFILE.md`;
3. `assured` by default.

`sprint` preserves one smallest relevant proof per unit, no per-unit cross-model
review, no driver proof rerun and one comprehensive final gate. `assured` preserves
cross-model review receipts per unit, Codex's own proof rerun and the integrated
gate. A failed targeted proof is fixed and rerun; failure alone does not silently
promote the unit to `assured`.

Default routes are Sol 5.6 at ultra effort on the Codex side and Fable 5 at max
effort on the Claude side. Exact owner/project overlays bind. Complex work may be
balanced between both models; equality is assessed by planned complexity and effort,
not by pretending provider credit systems are comparable. No model fallback or
substitution occurs without owner authorization.

## 6. Plugin architecture

```text
Codex task (driver)
  |
  | loads Forge skills and calls typed MCP tools
  v
Forge stdio MCP server (Python standard library)
  |
  | validates request, cwd, policy, budget and idempotency key
  | records durable state in SQLite/WAL
  v
Detached Forge worker process
  |
  | structured argv; no shell interpolation
  | official `claude -p` stream-json interface
  v
Claude root session (review/build)
  `-- may launch Claude-native subagents
```

The MCP bridge is local stdio only in v1: no TCP, HTTP or WebSocket listener. A
detached worker lets a Claude job survive an MCP process restart. A later MCP process
can inspect, resume or cancel it through durable state.

The bridge controls Claude only. The initiating Codex root and its native subagents
remain controlled by Codex itself; v1 does not launch a second external Codex root.

## 7. MCP tool contract

The bridge exposes only bounded, typed tools:

- `forge_preflight`: report bridge schema/version, Claude CLI version, auth status,
  state health and effective allowed repository boundary without a provider call.
- `forge_start`: start one Claude review/build job with an idempotency key,
  exact cwd, role, requested model/effort, prompt/work-order hash, budget and tool
  policy.
- `forge_status`: return state, timestamps, session ID when known, progress summary,
  requested/observed model receipt and provider-error classification.
- `forge_wait`: wait at most 30 seconds for a state revision, then return; it never
  hides a long blocking call.
- `forge_result`: return the final report and paths/hashes for complete event, stderr
  and receipt artifacts. Large raw streams remain files rather than overflowing the
  tool response.
- `forge_resume`: resume only the explicitly recorded Claude session after validating
  run, worktree and baseline identity. It never uses "latest" or implicit continue.
- `forge_steer`: append a revision-checked instruction to the active session. Stale
  revisions are rejected and accepted steering is permanently receipted.
- `forge_cancel`: interrupt the Claude turn, terminate its process group after a
  bounded grace period and report `CANCELLED` only after descendant quiescence.
- `forge_list`: list bounded recent jobs for the current repository identity.

There is no arbitrary-shell MCP tool and callers cannot select a permission bypass
mode.

## 8. Claude adapter

The adapter resolves a healthy `claude` executable and uses the official
non-interactive stream-json interface. It passes arguments as an array, never through
shell evaluation. Each new job receives an explicit generated session ID; every
resume uses the recorded session ID. The adapter does not pass `--fallback-model`.

Modes are fixed by role:

- `review`: read/search and native Agent tools; no Edit, Write or Bash.
- `build`: Read, Edit, Write, Glob, Grep, Bash and native Agent tools inside the
  declared worktree, with network disabled unless the frozen work order explicitly
  authorizes it.
- `recovery`: recognized only to reject new and historical recovery mutation before
  state or provider effects.

Claude runs with its supported sandbox enabled and unsandboxed commands forbidden.
If the sandbox or requested permission contract cannot be established, the job fails
closed before provider work. The worktree is the only writable product root. The
source tree and other lanes are not writable.

## 9. Repository and path boundary

Before each Forge MCP call, Codex's trusted `PreToolUse` hook binds the canonical
active task workspace to the Codex session using a private, per-session capability.
The bridge authenticates that binding before it opens durable state. The model never
constructs or edits the reserved `_forge_workspace` field. A requested cwd is
allowed only when it is:

- contained within that workspace; or
- a Git worktree whose common Git directory belongs to a repository contained within
  that workspace; or
- inside an explicitly configured extra root.

Canonical realpaths, NUL/path traversal rejection and symlink containment checks are
mandatory. A Claude worker may write only in its admitted worktree. Remote review of
another lane uses a read-only immutable patch/artifact, never the opponent's writable
tree.

Normal execution requires a clean immutable baseline. Supporting a dirty source tree
requires an explicit future snapshot mode that reproduces a hashed binary diff and
approved untracked-file manifest into isolated lanes without modifying the source;
that mode is not silently inferred.

## 10. Durable state and receipts

State lives under `~/.codex/forge/` with directories mode `0700` and files/databases
mode `0600`. SQLite uses WAL and transactional compare-and-swap revisions. Per-job raw
events and reports live under `runs/<job-id>/`.

The state machine is:

```text
CREATED -> PREFLIGHT -> RUNNING -> SUCCEEDED
                    \-> PAUSED_PROVIDER_ACCESS
                    \-> PAUSED_POLICY
                    \-> INTERRUPTING -> CANCELLED
                    \-> FAILED

SUCCEEDED/PAUSED_* -> RESUMING -> RUNNING
```

Terminal states are immutable except for an explicit new resume attempt that creates
a child attempt record. Idempotency keys prevent duplicate provider turns.

Receipts include job/run identity, objective/work-order hash, repository realpath,
baseline HEAD and status fingerprint, worktree, provider, requested and observed
model/effort, Claude/bridge versions, session/turn IDs, permission-policy hash,
steering history, usage/cost when exposed, process termination evidence, event hash,
report hash and terminal reason.

Model truth is labelled separately as requested, CLI-accepted and observed. An
observed mismatch or evidence of an additional fallback model pauses the job. The
receipt never calls an unobserved route verified.

## 11. Provider errors, credits and retries

Errors are classified as authentication, billing/credits, rate limit, model
unavailable, permission, sandbox unavailable, transient transport, protocol,
cancellation or internal failure.

Authentication, billing, model, permission and sandbox failures stop without model
substitution. A post-token/tool failure is never automatically replayed. At most one
pre-first-token transport retry is allowed with the same idempotency identity. At or
below the owner's five-percent allowance threshold, Forge stops and asks which model
to use; it does not quietly degrade.

Budgets include wall time, maximum turns and maximum USD where Claude exposes it.
Usage and cost values are labelled provider-authoritative, estimated or unavailable.

## 12. Secrets and logging

The bridge never prints or stores the full child environment. Claude uses its native
credential store by default. Environment credential inheritance is deny-by-default;
an API-key environment variable is inherited only through an explicit plugin setting.
Bridge-control tokens and unrelated cloud credentials are always removed.

Event and stderr logs are private local artifacts and pass through conservative
secret-pattern redaction before summaries are returned through MCP. Forge cannot
guarantee that source files contain no secrets, so repository policy and Claude's
deny-read configuration remain binding.

## 13. Implemented plugin layout

```text
codex/
├── .codex-plugin/plugin.json
├── .mcp.json
├── hooks.json
├── LICENSE
├── THIRD-PARTY-NOTICES.md
├── skills/
│   ├── forge/
│   ├── forge-review/
│   ├── forge-build/
│   ├── forge-orchestrate/
│   ├── forge-with-docs/
│   └── forge-recovery/
├── scripts/
│   ├── forge_bridge.py
│   ├── forge_workspace_hook.py
│   ├── dispatch-guard.sh
│   ├── recovery-watchdog.sh
│   └── forge-liveness.sh
├── tests/
│   ├── test_mcp_protocol.py
│   ├── test_bridge_jobs.py
│   ├── test_security_boundaries.py
│   ├── test_claude_adapter.py
│   └── fixtures/fake_claude.py
└── docs/superpowers/
    └── specs/2026-08-25-codex-forge-plugin-design.md
```

The personal marketplace entry is registered at
`~/.agents/plugins/marketplace.json` with installation `AVAILABLE`,
authentication `ON_INSTALL`, no product override and source `./plugins/forge`.

## 14. Verification strategy

All ordinary verification is offline and uses a fake Claude executable, so it makes
zero provider calls.

Must-pass coverage includes:

1. MCP initialize/list/call and schema/error behavior.
2. Idempotent start, transactional state transitions, concurrent revision conflict,
   terminal immutability and crash/restart resume.
3. Explicit session resume, streamed result parsing, native-subagent event
   attribution, requested/observed model mismatch and provider-error classification.
4. Process-group cancellation with no surviving fake descendants.
5. Path traversal, symlink escape, unrelated repository/worktree and oversized input
   rejection.
6. No shell interpolation for prompts containing metacharacters.
7. Secret-canary exclusion from the child environment, returned summaries and
   receipts.
8. Review mode cannot edit or run Bash; build mode is confined to its worktree and
   network policy.
9. Adapted dispatch guard refusal tests: verdict, lane/heavy caps, one writer,
   disjoint files, guarded expansion, immutable baseline, executor-no-commit and
   profile-specific receipt rules.
10. Recovery/liveness tests for stopped, stalled, provider-paused and completed jobs.
11. Skill and plugin validators, manifest/MCP discovery, marketplace shape and a
    realistic independent forward-test of the `forge` skill.

An optional owner-authorized live contract test may perform one read-only Claude
start/result/resume/cancel sequence. It is not part of the default gate. Quota or
authentication failure is recorded as a provider-access gate, not rewritten as a
bridge defect.

## 15. Acceptance criteria

- The personal `forge` plugin appears through the default Codex personal marketplace
  and validates with the official local plugin validator.
- All six skills are discoverable and faithfully reverse the controlling roles.
- Codex can start, monitor, retrieve, steer, resume and cancel an independently
  running Claude root without a TCP listener.
- Claude may use native subagents under the root's bounded permissions and budget.
- Jobs survive MCP process restart and retain explicit session/worktree identity.
- No executor can be treated as the committer; every integration is owned by the
  Codex driver and governed by active owner/repository gates.
- No silent model fallback, provider substitution, retry after effects or fabricated
  cross-model review is possible through the supported workflow.
- Parallel Forge lanes retain the three-lane, one-heavy-lease, one-writer and
  disjoint-file invariants.
- Offline unit/integration/security tests, shell guard tests, skill validation and
  plugin validation all pass on final bytes.
- Any unexecuted live provider check is labelled an open owner/provider gate rather
  than claimed green.

## 16. Non-goals for v1

- A web UI, TCP/HTTP service or remote multi-machine orchestration.
- Replacing Codex's own native subagent control with a second external Codex daemon.
- Importing or rewriting historical Claude/Codex transcript databases.
- Automatically repairing provider billing, authentication or usage limits.
- Silently handling a dirty source tree or committing from an executor lane.
- Copying community bridge implementations or depending on undocumented Claude
  sockets/session formats.
