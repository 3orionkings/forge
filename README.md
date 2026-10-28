# Forge

Adversarial planning and guarded multi-model execution for Claude Code and Codex.

Forge makes two models check each other before and during a build. One model
interviews you until the plan is locked, the other model reviews that plan read-only
until it returns `VERDICT: APPROVED`, and then the work is built on one track or as
parallel units in isolated git worktrees. Shell scripts enforce the rules that matter:
no lane launches without an `ADMIT:` from the dispatch guard, the executor never
commits, and a run counts as alive only when its work log, CPU and stderr say so,
never when the model says so.

There are two editions, one for each side of the table:

- **Forge for Claude Code** (the repository root). Claude drives and integrates; Codex
  reviews the plan and builds the units routed to it through `codex exec`.
- **Forge for Codex** ([`codex/`](codex/)). The roles are reversed: Codex drives and
  integrates; Claude reviews and builds through a typed local MCP bridge.

| Path | What it is |
|---|---|
| [`skills/`](skills/) | The six Claude Code skills: the four acts, recovery, and the shared guard, profiles and references |
| [`agents/`](agents/) | Ten Claude Code agents: the driver and nine subagents |
| [`workflows/`](workflows/) | Four Claude Code workflows that run the acts deterministically across those subagents |
| [`hooks/`](hooks/) | One SessionStart hook that tells the agents where the plugin's scripts live |
| [`codex/`](codex/) | Forge for Codex: six skills, the MCP bridge, the workspace hook, scripts, tests, and five custom agents in [`codex/agents/`](codex/agents/) |
| [`tests/`](tests/) | Offline tests for the workflows, including an end-to-end run against the real dispatch guard |

## How a run goes

1. **Grill.** The driver asks one question at a time until `PLAN.md` is locked.
2. **Adversarial review.** The other model reviews the plan in one resumed, read-only
   session and answers `APPROVED` or `REVISE`. The driver decides what to act on,
   revises, and resubmits until the plan is approved or the round cap is reached.
3. **Single-track build.** For simple work, one executor implements an admitted unit
   and the driver checks and commits it.
4. **Orchestrated build.** For complex work, units run in parallel worktrees, each with
   a declared file set, a base SHA, a proof command and a receipt, at most three lanes
   at a time and one heavy lane machine-wide.

There are two execution profiles. `sprint` keeps each unit to its smallest relevant
proof and records the routing decision in one line. `assured` adds complete-diff
review, cross-model unit review, a driver rerun of the proof, and a routing gate you
approve. A repository picks one in `.ai/EXECUTION_PROFILE.md`; an instruction in chat
always wins. Details are in [`skills/forge/profiles/`](skills/forge/profiles/) and
[`skills/forge/ROUTING-POLICY.md`](skills/forge/ROUTING-POLICY.md). Every rule traces
back to a measured failure, recorded in
[`skills/forge/references/incidents.md`](skills/forge/references/incidents.md).

## Forge for Claude Code

### Skills

| Skill | Use it when |
|---|---|
| `forge` | You want the whole loop for a non-trivial change: interview, cross-model plan review, then a guarded build. |
| `forge-review` | You already have a plan and want an adversarial cross-model review of it, with no interview and no build. |
| `forge-build` | You have an approved work order for one executor, such as a bounded refactor, a migration or a bug fix with a known repro. |
| `forge-orchestrate` | An approved plan has several bounded units that need dependency ordering, separate worktrees or parallel execution. |
| `forge-with-docs` | You want planning grounded in an existing glossary (`CONTEXT.md`) and ADR set, with both kept up to date as decisions settle. |
| `forge-recovery` | A Forge run has actually crashed, stalled or stranded work. Don't load it for a healthy run. |

### Agents

`forge-driver` is a main-thread agent: start a session with it and it runs the whole
loop, talking to you and holding the only commit authority. The other nine are
subagents it (or a workflow) dispatches, each with only the tools its role needs.

| Agent | Role | Writes |
|---|---|---|
| `forge-driver` | Runs the four acts and both gates, arbitrates, integrates and commits | Yes; the only committer |
| `forge-codex-reviewer` | Runs one read-only Codex round: a plan, one unit's diff, or the routing split | No |
| `forge-unit-planner` | Splits an approved plan into units with exact files, proofs, weights and waves | No |
| `forge-admission` | Turns a lane declaration into one dispatch-guard call and returns `ADMIT` or `REJECT` verbatim | No |
| `forge-claude-executor` | Builds one admitted unit in its worktree, declared files only, then runs the proof | Its declared files only |
| `forge-codex-executor` | Launches and supervises one Codex-routed unit by the work-log clock | No product code |
| `forge-unit-verifier` | The commit check at the profile's depth; under `assured` also the complete diff and a proof rerun | No |
| `forge-liveness` | Judges done, alive or stalled from the work log, CPU and stderr | No; stops a lane only when told to |
| `forge-recovery-auditor` | Preserves a wrecked run (patches, archives, safety refs) and builds the State Ledger | Only the recovery directory and `refs/forge-recovery/` |
| `forge-docs-steward` | Checks a decision against the glossary and ADRs and drafts the updates | No |

### Workflows

The workflows run the acts as deterministic scripts over those subagents. They
never commit: each one returns receipts, and the driver integrates.

| Workflow | Act | What it does |
|---|---|---|
| `forge-plan-review` | 2 | Codex reviews the plan read-only; a Claude arbiter logs each critique and revises; repeats in the same Codex session until `APPROVED` or the round cap, and never fakes convergence |
| `forge-decompose` | 4 setup | Splits the approved plan into units and waves, sends a skeptic after every parallel claim, turns shared files and hidden coupling into dependencies, and under `assured` collects Codex's side of the routing argument |
| `forge-build-wave` | 3 and 4 | Builds one wave of independent units: a worktree and frozen contract per unit, serialized guard admission, at most three lanes and one HEAVY, guarded file-set expansion, fix rounds, profile-depth verification, and successor receipts in the guard's format |
| `forge-recovery-audit` | Recovery 1 and 2 | Inventories a wrecked run in parallel, preserves every dirty tree and tip, builds the State Ledger, and has an independent critic check the gate |

A one-unit build is a wave of one. A REJECT from the guard stops the line: units still
waiting are not launched, and running ones are left to finish.

### Install

As a plugin, from inside Claude Code:

```text
/plugin marketplace add 3orionkings/forge
```

```text
/plugin install forge@forge
```

Then start a run with the driver, or just ask for one ("forge this change") in an
ordinary session:

```bash
claude --agent forge-driver
```

The workflows run as `/forge:forge-plan-review`, `/forge:forge-decompose`,
`/forge:forge-build-wave` and `/forge:forge-recovery-audit`. Claude Code's plugin
inventory on 2.1.257 lists the skills, agents and hook but not workflows; if your
build does not offer them by name, the driver runs them by path
(`${CLAUDE_PLUGIN_ROOT}/workflows/<name>.js`).

Or install by hand, which is how the skills were first used:

```bash
git clone https://github.com/3orionkings/forge.git
```

```bash
cp -R forge/skills/* ~/.claude/skills/ && mkdir -p ~/.claude/agents ~/.claude/workflows && cp forge/agents/*.md ~/.claude/agents/ && cp forge/workflows/*.js ~/.claude/workflows/
```

With a manual install, pass `agentNamespace: ""` to the workflows, because the agents
are then named `forge-codex-reviewer` rather than `forge:forge-codex-reviewer`.

## Forge for Codex

The Codex edition keeps the same acts, profiles, admission rules and invariants with
the roles reversed. Codex conducts the grill, arbitrates, verifies, integrates, and
alone commits. Claude (Fable 5 at max by default) is the external reviewer and
executor, reached only through nine typed MCP tools (`forge_preflight`,
`forge_start`, `forge_status`, `forge_wait`, `forge_result`, `forge_resume`,
`forge_steer`, `forge_cancel`, `forge_list`) served by a local stdio bridge. A trusted
`PreToolUse` hook binds every bridge call to the current task's workspace. Codex-native
lanes reserve through `scripts/forge-native-lane.py` and run as native Codex
subagents. Automated recovery is paused in this release and fails closed. The design
is in [`codex/docs/superpowers/specs/`](codex/docs/superpowers/specs/).

### Install

```bash
codex plugin marketplace add 3orionkings/forge
```

```bash
codex plugin add forge@forge
```

Start a new session afterwards, and review and trust the plugin's hook in `/hooks`;
without it the bridge refuses every call. Codex plugins cannot ship custom agents, so
copy them from a clone:

```bash
git clone https://github.com/3orionkings/forge.git
```

```bash
mkdir -p ~/.codex/agents && cp forge/codex/agents/*.toml ~/.codex/agents/
```

### Agents

On Codex the skills are the workflows: Codex has no scripted workflow file, so a
skill's steps name the agent to spawn. The bridge handles every Claude role; these five
agents cover the native Codex side.

| Agent | Role |
|---|---|
| `forge-native-executor` | Builds one reserved Codex-owned unit, admitted files only, then runs the proof and reports |
| `forge-unit-planner` | Turns an approved plan into a dependency graph of frozen-unit candidates |
| `forge-unit-verifier` | Codex's own check of a unit at the profile's depth; never counted as cross-model |
| `forge-liveness-observer` | Reads job status, revisions and artifact clocks; never cancels or steers |
| `forge-docs-steward` | Checks decisions against the glossary and ADRs and drafts the updates |

Ask for them by name, for example "have forge-unit-planner draft the dependency graph
for PLAN.md".

## Requirements

- [Claude Code](https://claude.com/claude-code) 2.1.257 or later. The workflows need
  dynamic workflows, which paid plans and API access include (Pro users turn them on
  in `/config`).
- The Codex CLI. Forge prefers the Codex.app CLI and pins GPT-5.6 "Sol" at ultra
  effort; [`skills/forge/references/engine-resolution.md`](skills/forge/references/engine-resolution.md)
  has the resolver and fallbacks.
- git (worktrees) and zsh for the guard and its tests; Python 3 for the Codex bridge;
  Node.js for the workflow tests.

## Configuration

| Setting | Effect |
|---|---|
| `.ai/EXECUTION_PROFILE.md` | `sprint` or `assured` for the repository; otherwise `assured` |
| Project overlay | One `executor_policy=any`, `codex-only` or `claude-only` line binds product routing; with none, the build-wave workflow declares the global default `any` |
| `FORGE_GUARD_WIP_BASE` | The branch the guard measures unmerged lane branches against. Without it the guard uses the repository's default branch (`origin/HEAD`, then `main` or `master`) and refuses a repository where none resolves. Forge for Codex always measures against `origin/HEAD` and fails closed without it; `git remote set-head origin --auto` sets it |
| `FORGE_SKILLS` | Where the agents find the skills and scripts. The plugin's hook sets it; a manual install defaults to `~/.claude/skills` |

## Tests

| Suite | Command | Cases |
|---|---|---|
| Dispatch guard | `zsh skills/forge/dispatch-guard.test.sh` | 139 |
| Context policy | `zsh skills/forge/context-policy.test.sh` | 22 |
| Recovery watchdog | `zsh skills/forge-recovery/recovery-watchdog.test.sh` | 37 |
| Workflows, mocked agents | `node tests/workflows.test.mjs` | 21 |
| Workflows against the real guard | `node tests/workflow-admission.test.mjs` | 5 |
| Forge for Codex, offline gate | `bash codex/scripts/run_offline_gate.sh` | 582 Python tests and three shell suites |

The Codex gate needs Codex's `plugin-creator` validator and ripgrep, which Codex.app
provides. Two scenario-fingerprint tests also fail on the last committed version of
August 28, 2026: their recorded build replays fingerprint bridge sources
(`service.py`, `worker.py` and others) that changed after the replays were recorded. A
handful of process and cancellation tests are timing-sensitive on a loaded machine and
pass when run alone.

## History

The Claude Code skills date from July 9 to August 6, 2026, and Forge for Codex from
August 25 to 29, 2026; their commits carry those dates. The agents, workflows,
plugin packaging and the fixes below were added on October 28, 2026:

- `skills/forge/references/acts-3-4.md` had lost the start of Act 3 and the end of
  Act 4 when the skill was split into references on August 3; the full text is back.
- Both editions measured unmerged lane branches against one project's integration
  branch, so they refused every lane in any other repository. They now measure
  against the repository's own default branch.

## Credits

Forge adapts MIT-licensed work by others:

- **grill-me** and **grill-with-docs** by Matt Pocock
  ([mattpocock/skills](https://github.com/mattpocock/skills)): the Act 1 interview and
  the `CONTEXT-FORMAT.md` and `ADR-FORMAT.md` files.
- **grill-me-codex**, **grill-with-docs-codex**, **codex-review** and **codex-build**
  by Chase AI ([chaseai-yt/grill-me-codex](https://github.com/chaseai-yt/grill-me-codex)):
  the Codex review loop and the flipped single-track build.
- Peter Steinberger's **codex-first** pattern: Codex builds, Claude verifies.

Forge adds the engine resolver, model pinning, the orchestrated build, the dispatch
guard, liveness checks, the recovery playbook, the Codex edition and its bridge, and
the agents and workflows. See
[`skills/forge/THIRD-PARTY-NOTICES.md`](skills/forge/THIRD-PARTY-NOTICES.md) and
[`codex/THIRD-PARTY-NOTICES.md`](codex/THIRD-PARTY-NOTICES.md).

## License

MIT. See [LICENSE](LICENSE).
