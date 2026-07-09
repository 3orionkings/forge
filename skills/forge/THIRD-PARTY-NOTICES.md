# Third-Party Notices — /forge skill pack

The `/forge` family adapts prior MIT-licensed work. Attributions:

## grill-me (Act 1 — the grill)
The Act 1 interview ("GRILL") is adapted from **grill-me** by **Matt Pocock**
(https://github.com/mattpocock/skills), MIT License, Copyright (c) 2026 Matt Pocock.

## grill-me-codex / codex-review / codex-build (Acts 2–3)
The Codex adversarial-review loop (Act 2) and the flipped single-track build (Act 3)
are adapted from **grill-me-codex**, **codex-review**, and **codex-build** by
**Chase AI** (https://github.com/chaseai-yt/grill-me-codex), MIT License,
Copyright (c) 2026 Chase AI.

## codex-first (flipped-execution pattern)
Act 3's "Codex builds, Claude verifies" role-flip follows **Peter Steinberger's
codex-first** pattern.

## Adaptations original to /forge
- Model routing pinned to **Codex GPT-5.6 Sol at ultra effort** via `-m`/`-c`
  (and the `~/.codex/sol.config.toml` profile), replacing upstream's "don't pin the
  model" guidance (valid here because this account can pin models).
- The **tiered engine resolver** (`ENGINE.md` §1) selecting a working Codex binary.
- **Act 4 — `forge-orchestrate`**: multi-model parallel execution across worktrees
  with cross-model review, which upstream does not provide.

See `LICENSE` for the full MIT terms covering all of the above.
