# Invocation templates (forge reference)

> **PROFILE NOTICE.** Any cross-model unit review, complete-diff review, orchestrator
> proof rerun, fresh-database rebuild or full per-unit gate described below is
> **`assured`-only**. Under `sprint`: one smallest relevant proof, no unit review, no
> rerun, no unit gate; the commit check is changed-paths-match-admitted-set + no
> overlap/safety breach + executor proof exited 0. Loading this file does not change
> the active profile (see [`../profiles/sprint.md`](../profiles/sprint.md)).


Loaded on demand from `ENGINE.md`. Copy these exactly — the safety is in the details.

## 4. Invocation templates (copy these exactly — safety is in the details)

`$CODEX_BIN`, `$SOL_ROUTE`, and (for builds) `$ROUTE` come from §1–§2. Reviews use
`$SOL_ROUTE`; builds use `$ROUTE` (defaults to Sol). Word-splitting of the route
token is intended, so it is unquoted in the command. Always run via the Bash tool
with `timeout: 600000` (a real review/build exceeds the default 2-min tool timeout).

### 4a. Adversarial review — Round 1 (fresh session, capture thread_id)

```bash
# ROUND=1 here. $SLUG is this run's unique stamp — e.g. SLUG="$(date +%Y%m%d-%H%M%S)-<plan>".
# The -o path is per-run-unique and stderr goes to a file, NEVER /dev/null: see the two
# incident notes directly below. Both were real failures caused by the shortcuts.
eval "$CODEX_BIN exec $SOL_ROUTE --skip-git-repo-check -s read-only --json \
  -o /tmp/forge-verdict-$SLUG-r1.txt \"\$(cat \"\$REVIEW_PROMPT\")\" \
  < /dev/null 2>/tmp/forge-review-$SLUG-r1.err" \
  | grep '"type":"thread.started"'
```
Parse `thread_id` from the `{"type":"thread.started","thread_id":"…"}` line →
`THREAD_ID`. Critique text lands in the `-o` file. No verdict file **and**
no `thread.started` line ⇒ failed run (auth/model, or a non-git dir — see §3) →
**read the `.err` file, then** stop and tell the user what it said.
(`--skip-git-repo-check` lets the review run in a non-git
dir; harmless inside a repo. On codex-cli ≥0.144 the **resume** call accepts it
too — include it there as well; older CLIs reject it on resume, so on old CLIs
run review loops from inside a git repo.)

**Verdict paths are per-run-unique** — `/tmp/forge-verdict-<slug>-r<round>.txt`,
never a shared fixed name, and that applies to **round 1 as much as to resumes**.
A stale verdict file from a *previous* run parses exactly like a fresh success
(observed 2026-07-10: a killed round read yesterday's verdict for a different plan —
a fixed round-1 path in this very template is what made that possible). Before
trusting any verdict file, confirm its mtime postdates the round's launch.

**Stderr always goes to a per-run `.err` file, never `/dev/null` — round 1 included.**
A failed call with discarded stderr is undiagnosable (observed 2026-07-10: a resume
died on the git-repo check with exit 1 and zero visible reason), and §6's limit
detection *requires* the captured stream to tell a hard cap from a transient overload.

### 4b. Adversarial review — Rounds 2..MAX (resume SAME session)

```bash
# resume REJECTS -s. Force read-only via -c sandbox_mode, or Codex inherits
# config.toml (this machine defaults to danger-full-access!) and could WRITE
# files mid-review. THIS IS THE MOST IMPORTANT SAFETY LINE IN THE PACK.
eval "$CODEX_BIN exec resume \"\$THREAD_ID\" $SOL_ROUTE --skip-git-repo-check \
  -c sandbox_mode=\"read-only\" \
  --json -o /tmp/forge-verdict-$SLUG-r\$ROUND.txt \
  \"I revised the plan. Re-review PLAN.md — check whether prior findings are \
addressed and flag anything new. End with VERDICT: APPROVED or VERDICT: REVISE.\" \
  < /dev/null 2>/tmp/forge-review-$SLUG-r\$ROUND.err >/dev/null"
```

### 4c. Build — launch (fresh session, write access, capture thread_id)

```bash
# $P is a temp file holding the build contract (never inline-quote the prompt).
# $SLUG: same per-run-unique stamp as §4a — build reports go stale exactly like verdicts.
eval "$CODEX_BIN exec $ROUTE --dangerously-bypass-approvals-and-sandbox --json \
  -o /tmp/forge-build-$SLUG.txt - <\"\$P\" 2>/tmp/forge-build-$SLUG.err" \
  | grep '"type":"thread.started"'
```

### 4d. Build — fix round (resume SAME session, still write-capable)

```bash
# resume has no --yolo/-s: spell the long bypass flag or Codex inherits config's
# sandbox and may be unable to write. $P2 = the fix-list contract temp file.
eval "$CODEX_BIN exec resume \"\$THREAD_ID\" $ROUTE \
  --dangerously-bypass-approvals-and-sandbox --json \
  -o /tmp/forge-build-$SLUG-fix\$ROUND.txt \
  - <\"\$P2\" 2>/tmp/forge-build-$SLUG-fix\$ROUND.err >/dev/null"
```

### Non-negotiable safety rules (all templates)

- **`< /dev/null` (or `- <"$file"`) is mandatory.** `codex exec` reads stdin *in
  addition to* the prompt arg; under a non-TTY driver (Claude Code's Bash tool, CI)
  it blocks forever waiting on stdin EOF — a silent ~0% CPU hang.
- **Reviews are read-only EVERY round** — `-s read-only` on the fresh call,
  `-c sandbox_mode="read-only"` on every resume (resume has no `-s`). A review that
  can write is a bug, not a feature.
- **Builds require `dispatch-guard.sh` admission before launch.** A new launch requires
  a clean tree; a guarded expansion may carry its active writer's expected dirty tree.
  Explicit recovery otherwise relaxes only cleanliness. **The executor never commits
  or merges, whichever model it is.** Under `sprint`, the orchestrator commits after
  admitted paths, overlap/safety state, and the executor's passing proof receipt check;
  under `assured`, it first reads the complete diff and reruns proof. There is no owner
  commit gate.
- **Capture and pass the explicit `THREAD_ID`.** Never resume with `--last` — a
  garbage/empty id silently falls back to the most recent session and a wrong-target
  resume looks exactly like a success. (Verified 2026-07-09: an empty id *did*
  silently resolve to the most recent thread.)
- **Non-git dirs need `--skip-git-repo-check` on EVERY call — fresh AND resume.**
  On codex-cli ≥0.144 resume accepts it (verified 2026-07-10: resume *errored*
  without it — "Not inside a trusted directory and --skip-git-repo-check was not
  specified" — and ran with it). Older CLIs reject it on resume → run loops from
  inside a git repo there. Builds always need a real repo regardless (clean-tree gate).
- **Never send stderr to /dev/null — send it to a per-run `.err` file.** A failed
  call with discarded stderr is undiagnosable (observed 2026-07-10: a resume died
  on the git-repo check with exit 1 and zero visible reason). §6's resilience
  detection *requires* the captured stream.
- **Sol@ultra reviews legitimately exceed 10 minutes.** Prefer `run_in_background:
  true` for @ultra/max reviews and read the `-o` file on completion. If a foreground
  call trips its `timeout: 600000` ceiling *after* `thread.started` was captured,
  that is NOT a failed run — resume the SAME `THREAD_ID` backgrounded and ask it to
  deliver its findings (context is preserved server-side). Only a trip with no
  thread id is a failure to surface.
- Read the report/verdict from the `-o` file; don't parse the JSONL stream for
  content — but verify the file's mtime postdates this round's launch (stale-verdict
  trap, §4a).

---
