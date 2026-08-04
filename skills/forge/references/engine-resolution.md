# Engine resolution — full tier table (forge reference)

The user's preference order is: **(1) the Claude `codex` plugin runtime → (2) the
Codex app on the Mac → (3) hybrid → (4) raw `codex exec` CLI.** Resolve the
highest tier that is both *available* and *capable of what the call needs*.

| Tier | Engine | Invocation shape | Status / capability (2026-07-09) |
|------|--------|------------------|----------------------------------|
| 1 | Claude `codex` plugin runtime (`codex-companion.mjs`, persistent `app-server` broker) | `node "$CODEX_PLUGIN_ROOT/scripts/codex-companion.mjs" task/review …` | **Down + capability-limited.** It spawns `codex app-server` off `PATH`, and the Homebrew `codex` on `PATH` is currently broken (missing platform binary → `ENOENT`). v1.0.3 also tops out at `--effort xhigh` and has no `-p`/profile — it **cannot express Sol@ultra**. Use only for *non-ultra* Codex delegation, and only once its binary is repaired. |
| 2 | **Codex app on the Mac** — its bundled CLI at `/Applications/Codex.app/Contents/Resources/codex` | `codex exec …` (full: `-m`, `-c`, `-p`, `resume`, `ultra`) | **Healthy. This is the forge workhorse today.** Fully supports Sol@ultra. |
| 3 | Hybrid | mix of the above | Fallback only. |
| 4 | Raw Homebrew `codex` on `PATH` (`/opt/homebrew/bin/codex`) | `codex exec …` | **Broken today** (same missing-binary `ENOENT`). Repairable via `npm i -g @openai/codex@latest`; identical shape to tier 2 once fixed. |

**Why forge's core loops use tier 2, not tier 1:** the user configured "Sol 5.6 at
ultra." Tier 1 cannot produce ultra effort, so honoring the *capability* the user
asked for takes precedence over the *engine* preference for the grill/review/build
loops. Tier 1 remains the preferred path for any future *non-ultra* Codex
delegation. This is a deliberate, documented reconciliation of two of the user's
requests that currently conflict.

### Resolve `CODEX_BIN` (run once per skill invocation, before any Codex call)

```bash
# Pick the first Codex CLI that (a) runs and (b) supports the `exec` shape.
# Prefers the healthy Codex.app binary (tier 2), falls back to PATH (tier 4).
CODEX_BIN=""
for c in "/Applications/Codex.app/Contents/Resources/codex" "$(command -v codex 2>/dev/null)"; do
  if [ -n "$c" ] && "$c" --version >/dev/null 2>&1; then CODEX_BIN="$c"; break; fi
done
[ -z "$CODEX_BIN" ] && { echo "FORGE: no working codex CLI found — cannot reach Codex/Sol"; exit 1; }
"$CODEX_BIN" --version   # echo it so the user sees which engine resolved
```

If `CODEX_BIN` is empty, **stop and tell the user** their Codex CLI is unreachable
(offer: `npm i -g @openai/codex@latest`, or open the Codex.app once). Never
silently skip the Codex half of a forge flow — that would collapse it to
single-model, defeating the whole point.

---

