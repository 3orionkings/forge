# /forge — Design Spec

> **PROFILE NOTICE.** Any cross-model unit review, complete-diff review, orchestrator
> proof rerun, fresh-database rebuild or full per-unit gate described below is
> **`assured`-only**. Under `sprint`: one smallest relevant proof, no unit review, no
> rerun, no unit gate; the commit check is changed-paths-match-admitted-set + no
> overlap/safety breach + executor proof exited 0. Loading this file does not change
> the active profile (see [`../forge/profiles/sprint.md`](../forge/profiles/sprint.md)).


_Adversarial planning + multi-model execution, as a globally-installed Claude Code
skill pack. Adapts Chase AI's grill-me-codex (MIT) for this account's Codex GPT-5.6
"Sol" setup, and adds model-routed parallel execution._

Author: Claude (Opus 4.8) with the user · Created 2026-07-09 · Location:
`~/.claude/skills/forge*/` (global — works in every repo).

---

## 1. Goal & motivation

Automate the loop the user was running by hand: Claude plans, Codex adversarially
audits, Claude fixes, then the work gets built — but let the user **choose which
model executes which piece of work**, defaulting to Codex "Sol 5.6" and splitting
across models (Codex + Claude) for complex, multi-system builds.

The upstream `grill-me-codex` provided Acts 1–3 (grill → adversarial review →
single-track flipped build). This pack renames it to `/forge`, pins the model to
**Sol 5.6 at ultra effort**, wraps Codex behind a **tiered engine resolver**, and
adds **Act 4 (`/forge-orchestrate`)** — parallel, model-routed execution that
upstream lacks.

## 2. The four acts

| Act | Skill | Who drives | What |
|-----|-------|-----------|------|
| 1 — Grill | `forge` (or `forge-with-docs`) | You ↔ Claude | One-question-at-a-time interview; lock `PLAN.md` |
| 2 — Adversarial review | `forge` / `forge-review` | Claude ↔ Sol | Sol reviews `PLAN.md` read-only, resuming the same session, `VERDICT: APPROVED/REVISE` until approved or `MAX_ROUNDS` |
| 3 — Single-track build | `forge-build` | Sol writes, Claude drives | For **simple** work: Sol implements. **`sprint`**: driver checks admitted paths + no overlap/safety breach + executor proof exit 0, then commits. **`assured`**: complete-diff review + driver proof rerun, bounded fix rounds |
| 4 — Orchestrated build | `forge-orchestrate` | Profile decides routing | For **complex** work, units run in parallel worktrees. **`sprint`**: overlay/default executor applied automatically, one line recorded, no routing debate and no Gate-2 wait. **`assured`**: Claude proposes a per-unit assignment (+ reasons), you approve at Gate 2; per-unit cross-model review applies |

`forge-review` = Act 2 standalone (have a plan, skip the grill). `forge-with-docs` =
Act 1 that also maintains a `CONTEXT.md` glossary + ADRs.

## 3. The complexity gate (the user's core requirement)

After Act 2 approves the plan, Claude assesses **complexity, not "sprint count"** —
and what happens next depends on the active execution profile.

- **Simple / narrow** (small fix, single-system, mechanical migration) → auto
  single-track on Sol via `forge-build`. No interruption, at either profile.
- **Complex** (multiple systems, non-trivial architecture, cross-cutting):
  - **`sprint`** → apply the owner overlay, or the default executor if none, and
    **record one line** naming the executor and why. **No routing debate, no Gate-2
    wait**; execution starts immediately.
  - **`assured`** → Claude **proposes which model executes each unit, with a one-line
    reason**, and waits at **Gate 2**. The user replies "approved" or redirects the
    division. See `ENGINE.md` §5.

Routing is canonically governed by `ROUTING-POLICY.md`; the profiles live in
`profiles/`.

## 4. Model routing — Sol 5.6

- Default executor + reviewer: **`gpt-5.6-sol`** at **`model_reasoning_effort =
  "ultra"`** (the subagent-accelerated tier), standard speed.
- Pinned via `-m gpt-5.6-sol -c model_reasoning_effort="ultra"` on every call
  (works on both `codex exec` and `codex exec resume`; `resume` rejects `-p`).
- Named profile `~/.codex/sol.config.toml` is the interactive `-p sol` equivalent.
- Alternates (only when `forge-orchestrate` routes a unit off-default and the user
  approves): `gpt-5.6-terra` (cheaper Codex), or a Claude subagent (Opus 4.8 / Fable 5).
- Model slugs + effort values verified against the local binary
  (`/Applications/Codex.app/Contents/Resources/codex`, `codex-cli 0.144.0-alpha.4`).

## 5. Engine — tiered fallback (user's preference order)

1. Claude `codex` plugin runtime (`codex-companion.mjs` broker) → 2. **Codex.app
binary on the Mac** → 3. hybrid → 4. raw Homebrew `codex` CLI. See `ENGINE.md` §1.

**Reality on 2026-07-09:** tier 1 and tier 4 are **down** (the Homebrew
`@openai/codex` platform binary is missing → `ENOENT`), and tier 1 (companion v1.0.3)
tops out at `--effort xhigh` with no profiles, so it **cannot express Sol@ultra**.
The resolver therefore lands on **tier 2 — the healthy Codex.app CLI**, which fully
supports Sol@ultra. This honors the user's stated ordering ("use tier 1 *if it
works*"; it doesn't, so fall to tier 2). Repairing tier 1/4 is a documented
follow-up (`npm i -g @openai/codex@latest`), but even repaired, tier 1 needs an
OpenAI update before it can do ultra.

## 6. File layout

```
~/.claude/skills/
├── forge/                     # umbrella router + selected execution profile
│   ├── SKILL.md
│   ├── ENGINE.md              # shared interface/index; detail loads on demand
│   ├── profiles/              # sprint and assured cadence
│   ├── references/            # progressive-disclosure procedures
│   ├── DESIGN.md              # this file
│   ├── LICENSE  ·  THIRD-PARTY-NOTICES.md
├── forge-review/SKILL.md      # Act 2 standalone
├── forge-build/SKILL.md       # Act 3 single-track
├── forge-orchestrate/SKILL.md # Act 4 multi-model parallel (net-new)
└── forge-with-docs/           # Act 1 docs-aware variant
    ├── SKILL.md · ADR-FORMAT.md · CONTEXT-FORMAT.md · THIRD-PARTY-NOTICES.md
~/.codex/sol.config.toml       # the `sol` profile (gpt-5.6-sol @ ultra)
```

`ENGINE.md` indexes the safety-critical interfaces; exact commands live once in
`references/invocation-templates.md` and entrypoints route instead of duplicating them.

## 7. How to engage

Installed globally → works in any repo, no per-project setup. Type **`/forge`** (or
just describe the task — skills auto-trigger from their description). Sub-commands:
`/forge-review`, `/forge-build`, `/forge-with-docs`, `/forge-orchestrate`. Gates:
(1) plan `APPROVED` after Act 2, both profiles; (2) routing approval for complex builds —
**`assured` only**, `sprint` applies the overlay/default automatically and does not
wait. There is no owner commit sign-off gate: the driver commits once the active
profile's check passes. Between gates, execution runs autonomously.

## 8. Safety invariants (must never regress)

- Reviews are read-only every round (`-s read-only` fresh; `-c sandbox_mode="read-only"`
  on resume — because this machine's `config.toml` defaults to `danger-full-access`).
- `< /dev/null` / `- <"$file"` on every `codex exec` (non-TTY stdin-hang guard).
- Explicit `THREAD_ID` on resume (never `--last`). Long calls use background liveness checks.
- The executor never commits or merges; the Claude-side driver commits under the active profile's check. New launches need a clean tree.
- Usage/rate limits never break or fake work (ENGINE.md §6): follow the documented degrade ladder above the stop threshold, and stop/ask at ≤5%. Credits and driver model/effort are user actions, not skill-automatable.

## 9. Provenance & license

MIT throughout. Act 1 = Matt Pocock's grill-me/grill-with-docs; Acts 2–3 = Chase
AI's grill-me-codex/codex-build; Act 3 flip pattern = Peter Steinberger's
codex-first; Act 4 + Sol routing + engine resolver original to this pack. See
`LICENSE` and `THIRD-PARTY-NOTICES.md`.
