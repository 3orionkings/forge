# Liveness — never trust "still working" (forge reference)

> **PROFILE NOTICE.** Any cross-model unit review, complete-diff review, orchestrator
> proof rerun, fresh-database rebuild or full per-unit gate described below is
> **`assured`-only**. Under `sprint`: one smallest relevant proof, no unit review, no
> rerun, no unit gate; the commit check is changed-paths-match-admitted-set + no
> overlap/safety breach + executor proof exited 0. Loading this file does not change
> the active profile (see [`../profiles/sprint.md`](../profiles/sprint.md)).


Helper: `../sol-liveness.sh`


A Codex build/gate can HANG while the model keeps emitting `agent_message`s like
"progressing, not hung." Those are model self-reports and are NOT evidence. A real
2026-07-22 incident: a resumed build looped on `Custom tool call output is missing
for call id: …` every ~30s for ~20 min while claiming progress; its actual gate log
had stopped advancing. Two failure modes to catch:
- **Silent hang:** the WORK log (gate/build output) stops advancing while CPU goes idle.
- **Error loop:** stderr repeats one identical error line (broken tool call, model
  refresh timeout) — the turn is spinning, not working.

**The check (authoritative signals, in order):**
1. `turn.completed` in the `--json` stream → DONE.
2. The WORK artifact's mtime — compare in **epoch seconds**, never local-vs-UTC
   strings (that mixup caused a false "4 hours stale" mis-diagnosis in the same
   incident; the real gap was ~15 min).
3. Summed `%cpu` of the run's processes (via a `ps` grep on the worktree path).
4. Repeated identical stderr line in the tail (drop field-1 timestamp with awk;
   do NOT regex-strip it — BSD/GNU sed differ and it silently no-ops).

Use the helper: `./sol-liveness.sh --work <gateLog> --jsonl <codex.jsonl>
--err <codex.err> --pattern <worktree-path>` → prints `STATUS=DONE|ALIVE|STALLED`
(exit 0/0/3). STALLED when: an error-loop (≥3 identical tail lines), OR silent
≥8 min AND CPU <20% (not logging AND not computing), OR a hard cap (~90 min) of
quiet regardless. The kill-switch suite is legitimately log-quiet for ~20 min but
stays CPU-active — that's why the CPU term prevents a false positive.

**Cadence.** Check liveness (a) at every task-notification for still-running Codex
work, and (b) — because a hung run emits NO notification — on a heartbeat for any
Codex run expected to exceed ~20 min: schedule a wake (`ScheduleWakeup` in a dynamic
loop, or a short `CronCreate`) every ~20–30 min while it runs, and run the check.
Don't poll faster than the work produces observable change.

**On STALLED:** read the tail to confirm, then KILL the degraded turn
(`pkill -f <worktree-path>`), verify the work-tree files survived (they do — the
turn's edits are on disk), and re-dispatch a fresh finish or recovery contract if
implementation remains. **Which model takes that re-dispatch follows the active
profile**: under `sprint`, apply the owner overlay/default and log one line; under
`assured`, use its routing decision. If implementation is already complete, close with
that profile's commit check: admitted paths + no overlap/safety breach + executor proof
exit 0 under `sprint`; complete-diff review + orchestrator proof rerun under `assured`.
The work is rarely lost; only the flaky turn is.

**--work MUST be the REAL work log, never the codex `--json`/agent-message stream.**
(2026-07-22 refinement.) The chatter stream stays fresh (the model keeps narrating)
even when the actual gate is frozen — so a watchdog pointed at the jsonl reports a
false ALIVE. Point `--work` at the file the gate/build actually writes (have the
contract emit it to a KNOWN path, e.g. `/tmp/<unit>-gate.log`, and pass THAT). A
watchdog on the jsonl only catches error-loops and full CPU-idle, not "gate log
frozen while the model keeps talking."

**New non-hang case: work DONE but turn still running (redundant re-validation).**
A turn can finish the real work (merge + gate PASS, exit 0 in the gate log) yet keep
running — re-running an already-green suite (e.g. the kill-switch a second time) for
another 20–30 min without committing, the same tail as the billing-Phase-1 resume.
So don't only ask "is it alive" — ask "is the WORK done?": if the real gate log shows
the full chain passed (0 ELIFECYCLE, final test summary) AND the required deliverables
are on disk (merge complete, migrations/hardening present) AND it's still uncommitted,
CLOSE THE ORCHESTRATION — kill the turn and apply the active profile's commit check.
Under `sprint`, trust the executor's passing targeted-proof receipt after the
path/overlap/safety check; under `assured`, read the complete diff and rerun the root
and MR proofs. Don't wait on a turn that's past its useful
work, and don't let the orchestrator quietly finish the executor's unit inside the
closeout — if code still needs writing, that is a re-dispatch with a logged route,
not a closeout.

**Zombie kill-by-PID before any resume/re-dispatch (2026-07-24 refinement).** A
codex turn that reports exit-1 does NOT guarantee its process died: orphaned
`codex exec` processes (reparented to launchd, or respawned by the Codex.app
`app-server` supervisor) keep editing the worktree for HOURS and collide with any
resume — the apply_patch "Failed to find expected lines" and oscillating typecheck
counts are the fingerprints. `pkill -f <worktree>` does NOT catch them (a codex
command line names its `-o` OUTPUT file, not the worktree). Before any resume,
re-dispatch, or orchestration closeout: `ps aux | grep "[c]odex exec"` → kill the exec PIDs
(and `codex-code-mode-host`), verify zero remain AND that they don't respawn
(re-check after ~5s); if they respawn, the Codex.app supervisor holds them — move
the work to a FRESH worktree path the zombies don't reference (git worktree add
from the last reviewed commit; preserve the dirty tree as a quarantine artifact
instead of making an unreviewed WIP commit).
