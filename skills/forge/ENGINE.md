# FORGE ENGINE — interface and index

Every forge skill reads this before invoking Codex. It is an **index and interface**,
not a rulebook: each area is one paragraph here and a reference file on demand.

Routing is governed by **[`ROUTING-POLICY.md`](./ROUTING-POLICY.md)** (canonical).
Cadence is governed by the active **[profile](./profiles/)**. Where either conflicts
with this file, that file wins.

## Index

| Area | One line | Detail |
|---|---|---|
| Engine resolution | Prefer the Codex.app CLI; it is the only tier that expresses Sol@ultra | [`references/engine-resolution.md`](./references/engine-resolution.md) |
| Invocation templates | Copy verbatim; the safety is in the flags | [`references/invocation-templates.md`](./references/invocation-templates.md) |
| Routing mechanics | Complexity assessment, per-unit assignment | [`references/routing-detail.md`](./references/routing-detail.md) |
| Usage limits / credits | Degrade ladder above the threshold; **stop and ask at ≤5%** | [`references/usage-limits.md`](./references/usage-limits.md) |
| Liveness | Is it hung? Clock + CPU + stderr, never a self-report | [`references/liveness.md`](./references/liveness.md) · `sol-liveness.sh` |
| Admission | No lane launches without `ADMIT:` | [`references/admission.md`](./references/admission.md) · `dispatch-guard.sh` |
| Why the rules exist | Measured incidents behind each rule | [`references/incidents.md`](./references/incidents.md) |

## Interfaces (scripts are the enforcement; prose is not)

```
dispatch-guard.sh   --verdict … --active-lanes N --heavy-leases N --weight LIGHT|HEAVY
                    --execution-profile sprint|assured   (omitted => assured)
                    --admission-action launch|expand      (omitted => launch)
                    --owns-heavy-lease true|false         (expansion state)
                    --proof-scope targeted|broad          (omitted => broad/HEAVY)
                    --lane-type feature|integration|recovery|control-plane|review
                    --mode normal|recovery --worktree PATH --clean-tree BOOL
                    --builder ID --base-sha SHA --backlog-id ID --file PATH…
                    --proof-command CMD --work-log PATH --report PATH
                    --predecessor none|ID --instruction TEXT --unmerged-branches N
                    --active-writer-worktree PATH --parallel-file PATH…
                    --current-file PATH…                  (expansion prior set)
                    → "ADMIT: …" exit 0 | "REJECT: <code>: <reason>" non-zero

sol-liveness.sh     --work <realWorkLog> --jsonl <codex.jsonl> --err <codex.err>
                    --pattern <worktree-path>
                    → STATUS=DONE|ALIVE|STALLED   (exit 0/0/3)

--admission-action expand = guarded re-admission of an ALREADY-active writer with a
widened file set: it does not consume lane-cap headroom, and its full expanded set is
checked against the prior set, dirty Git paths, and other active lanes. An expanding
HEAVY lane declares whether it owns the sole existing lease. Not "atomic" — scheduler state is
caller-supplied, so it is a guarded check against the declared snapshot.

--execution-profile sprint drops the successor cross-model review receipt (sprint runs
no unit review); the report-consumption receipt is still required at both profiles.

--help on either prints the current flag set. Trust that over any prose here.
```

`dispatch-guard.test.sh` and `../forge-recovery/recovery-watchdog.test.sh` prove these
instruments refuse. `context-policy.test.sh` proves the always-loaded descriptions and
common references do not restore the wrong profile. A guard or context change without
its relevant test — and a still-passing baseline — is not done.

## 1. Resolve `CODEX_BIN` (once per invocation, before any Codex call)

```bash
CODEX_BIN=""
for c in "/Applications/Codex.app/Contents/Resources/codex" "$(command -v codex 2>/dev/null)"; do
  if [ -n "$c" ] && "$c" --version >/dev/null 2>&1; then CODEX_BIN="$c"; break; fi
done
[ -z "$CODEX_BIN" ] && { echo "FORGE: no working codex CLI found"; exit 1; }
"$CODEX_BIN" --version
```

If empty, **stop and tell the user** (`npm i -g @openai/codex@latest`, or open
Codex.app once). Never silently drop the Codex half of a forge flow — that collapses it
to single-model and defeats the point.

## 2. Routes

| Side | Default | Effort |
|---|---|---|
| Codex | **Sol 5.6** (`gpt-5.6-sol`) | **ultra** |
| Claude | **Fable 5** | session default |

```bash
SOL_ROUTE='-m gpt-5.6-sol -c model_reasoning_effort="ultra"'   # default executor + reviewer
TERRA_ROUTE='-m gpt-5.6-terra -c model_reasoning_effort="high"' # cheaper Codex rung
ROUTE="${ROUTE:-$SOL_ROUTE}"
```

Slugs: `gpt-5.6-sol|terra|luna`. Effort: `minimal|low|medium|high|xhigh|max|ultra`.
Reviews always use `$SOL_ROUTE` unless the plan argued otherwise; only builds are
routed per unit. `resume` accepts `-m`/`-c` but **not** `-p` or `-s`.

## 3. Preflight (fast, once per flow)

1. Resolve `CODEX_BIN`; echo its version.
2. **Git-repo gate** — `codex exec resume` accepts no `--skip-git-repo-check`, so the
   multi-round loop only works inside a git repo. Act 1 works anywhere; Acts 2–4 need it.
3. Confirm auth is live — surface an auth/model error, never silently retry.
4. **Echo the resolved engine, model and per-unit route with its one-line reason**
   before the first round, so a wrong route is visible before it burns a round. If the
   owner objects, stop and re-route — any unit may go to either side.
