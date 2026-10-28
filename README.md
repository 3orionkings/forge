# Forge

Adversarial planning and guarded multi-model execution for Claude Code and Codex.

Forge is a pack of six Claude Code skills. Claude interviews you until the plan is
locked, Codex reviews that plan read-only until it returns `VERDICT: APPROVED`, and
then the work is built either on one track or as parallel units in isolated git
worktrees. Shell scripts enforce the rules that matter: no lane launches without an
`ADMIT:` from the dispatch guard, and a run counts as alive only when its work log,
CPU and stderr say so, never when the model says so.

## The skills

| Skill | Use it when |
|---|---|
| `forge` | You want the whole loop for a non-trivial change: interview, cross-model plan review, then a guarded build. |
| `forge-review` | You already have a plan and want an adversarial cross-model review of it, with no interview and no build. |
| `forge-build` | You have an approved work order for one executor, such as a bounded refactor, a migration or a bug fix with a known repro. |
| `forge-orchestrate` | An approved plan has several bounded units that need dependency ordering, separate worktrees or parallel execution. |
| `forge-with-docs` | You want planning grounded in an existing glossary (`CONTEXT.md`) and ADR set, with both kept up to date as decisions settle. |
| `forge-recovery` | A Forge run has actually crashed, stalled or stranded work. Don't load it for a healthy run. |

Each skill's `description` tells Claude when to pick it and when to hand off to a
sibling, so you can usually just ask for "forge" and let the routing work.

## How a run goes

1. **Grill.** Claude asks one question at a time until `PLAN.md` is locked.
2. **Adversarial review.** Codex reviews the plan in a resumed session and answers
   `APPROVED` or `REVISE`. Claude fixes and resubmits until it is approved or the
   round limit is reached.
3. **Single-track build.** For simple work, Codex implements and the Claude-side driver
   checks and commits. The executor never commits.
4. **Orchestrated build.** For complex work, units run in parallel worktrees, each
   with a declared file set, a base SHA, a proof command and a receipt.

There are two execution profiles. `sprint` keeps each unit to its smallest relevant
proof and records the routing decision in one line. `assured` adds complete-diff
review, cross-model unit review, a driver rerun of the proof, and a routing gate you
approve. Details are in [`skills/forge/profiles/`](skills/forge/profiles/) and
[`skills/forge/ROUTING-POLICY.md`](skills/forge/ROUTING-POLICY.md).

Every rule traces back to a measured failure. The evidence is in
[`skills/forge/references/incidents.md`](skills/forge/references/incidents.md).

## Requirements

- [Claude Code](https://claude.com/claude-code)
- The Codex CLI. Forge prefers the Codex.app CLI and pins GPT-5.6 "Sol" at ultra
  effort; see [`skills/forge/references/engine-resolution.md`](skills/forge/references/engine-resolution.md)
  for the resolver and fallbacks, and [`skills/forge/DESIGN.md`](skills/forge/DESIGN.md)
  for the optional `~/.codex/sol.config.toml` profile.
- git (worktrees), and zsh for the test suites.

## Install

```bash
git clone https://github.com/3orionkings/forge.git
```

```bash
cp -R forge/skills/* ~/.claude/skills/
```

The skills refer to their scripts under `~/.claude/skills/forge*/`, so install them
there to work across every repo.

## Tests

The dispatch guard, the context policy and the recovery watchdog each ship with a
test suite:

```bash
zsh ~/.claude/skills/forge/dispatch-guard.test.sh
```

```bash
zsh ~/.claude/skills/forge/context-policy.test.sh
```

```bash
zsh ~/.claude/skills/forge-recovery/recovery-watchdog.test.sh
```

## Credits

Forge adapts MIT-licensed work by others:

- **grill-me** and **grill-with-docs** by Matt Pocock
  ([mattpocock/skills](https://github.com/mattpocock/skills)): the Act 1 interview and
  the `CONTEXT-FORMAT.md` and `ADR-FORMAT.md` files.
- **grill-me-codex**, **grill-with-docs-codex**, **codex-review** and **codex-build**
  by Chase AI ([chaseai-yt/grill-me-codex](https://github.com/chaseai-yt/grill-me-codex)):
  the Codex review loop and the flipped single-track build.
- Peter Steinberger's **codex-first** pattern: Codex builds, Claude verifies.

Forge adds the engine resolver, model pinning, the orchestrated build (Act 4), the
dispatch guard, liveness checks and the recovery playbook. See
[`skills/forge/THIRD-PARTY-NOTICES.md`](skills/forge/THIRD-PARTY-NOTICES.md).

## License

MIT. See [LICENSE](LICENSE).
