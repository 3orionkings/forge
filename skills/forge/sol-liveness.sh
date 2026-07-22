#!/usr/bin/env bash
# sol-liveness.sh — is a background model (Sol/codex) run ACTUALLY progressing,
# or hung while the model self-reports "still working"?
#
# The lesson this encodes: never trust a model's "it's progressing" agent_message.
# Check the WORK artifact's clock, the process CPU, and the stderr for an error loop.
# All time math is epoch-seconds (no local-vs-UTC string comparison — that bug is
# exactly what this guards against).
#
# Usage:
#   sol-liveness.sh --work <workLog> [--jsonl <codex.jsonl>] [--err <codex.err>]
#                   [--pattern <ps-grep>] [--max-stale-min N] [--max-runtime-min N]
#
#   --work         REQUIRED. The file the run writes its real work into (the gate
#                  log, build log, test output) — NOT the codex agent_message stream.
#   --jsonl        codex --json stream; used only to detect turn.completed (=DONE).
#   --err          codex stderr; scanned for a repeated identical error (=error loop).
#   --pattern      ps grep to find the run's live processes (e.g. the worktree path).
#   --max-stale-min   work-log silence that counts as stalled (default 25; set above
#                     the longest legitimately-quiet step — e.g. the ~20m kill-switch).
#   --max-runtime-min hard cap; past this with no turn.completed => investigate (default 90).
#
# Exit: 0 DONE · 0 ALIVE · 3 STALLED · 2 usage/error. Prints STATUS=... + evidence.

set -u
WORK="" JSONL="" ERR="" PAT="" MAXSTALE=25 MAXRUN=90
while [ $# -gt 0 ]; do case "$1" in
  --work) WORK="$2"; shift 2;; --jsonl) JSONL="$2"; shift 2;; --err) ERR="$2"; shift 2;;
  --pattern) PAT="$2"; shift 2;; --max-stale-min) MAXSTALE="$2"; shift 2;;
  --max-runtime-min) MAXRUN="$2"; shift 2;; *) echo "unknown arg $1" >&2; exit 2;; esac; done
[ -n "$WORK" ] || { echo "STATUS=ERROR reason=--work-required" >&2; exit 2; }

now=$(date +%s)
mtime_epoch() { stat -f%m "$1" 2>/dev/null || stat -c%Y "$1" 2>/dev/null || echo 0; }

# 1) DONE? turn.completed in the codex stream is authoritative.
if [ -n "$JSONL" ] && grep -q '"type":"turn.completed"' "$JSONL" 2>/dev/null; then
  echo "STATUS=DONE evidence=turn.completed"; exit 0
fi

# 2) work-log age (epoch — timezone-safe)
if [ -f "$WORK" ]; then wm=$(mtime_epoch "$WORK"); age=$(( now - wm )); else age=999999; fi
age_min=$(( age / 60 ))

# 3) live CPU — sum %cpu of the run's processes. -1 = not measured (no --pattern).
cpu=-1
if [ -n "$PAT" ]; then
  cpu=$(ps aux | grep -E "$PAT" | grep -v grep | awk '{s+=$3} END{printf "%d", (s+0)}')
  cpu=${cpu:-0}
fi

# 4) error loop — same stderr line repeated in the tail (broken tool-call retry, etc.).
#    Drop field 1 (the leading ISO timestamp) so only the message text is compared —
#    do NOT try to regex-strip the timestamp (BSD vs GNU sed differ; that silently no-ops).
loop=0; cnt=0; loopline=""
if [ -n "$ERR" ] && [ -f "$ERR" ]; then
  line=$(tail -25 "$ERR" 2>/dev/null | awk 'NF{$1=""; sub(/^ /,""); print}' \
    | sort | uniq -c | sort -rn | head -1)
  cnt=$(printf '%s' "$line" | awk '{print $1+0}')
  loopline=$(printf '%s' "$line" | sed 's/^ *[0-9]* *//')
  [ "${cnt:-0}" -ge 3 ] && loop=1
fi

# 5) verdict. error-loop is the strongest signal; "silent AND idle CPU" is the next;
#    the hard cap is the backstop. CPU rule only applies when CPU was measured.
verdict=ALIVE; why=""
if [ "$loop" = 1 ]; then verdict=STALLED; why="error-loop(x${cnt}): ${loopline:0:60}"; fi
if [ "$cpu" -ge 0 ] && [ "$cpu" -lt 20 ] && [ "$age_min" -ge 8 ]; then
  verdict=STALLED; why="${why:+$why; }idle: work-log silent ${age_min}m AND cpu ${cpu}%<20 (not logging, not computing)"
fi
if [ "$age_min" -ge "$MAXRUN" ]; then
  verdict=STALLED; why="${why:+$why; }quiet ${age_min}m >= hard cap ${MAXRUN}m — investigate regardless"
fi
cpud=$cpu; [ "$cpu" -lt 0 ] && cpud="n/a"
echo "STATUS=$verdict work_age_min=$age_min cpu=${cpud} error_loop=$loop ${why:+reason=\"$why\"}"
[ "$verdict" = STALLED ] && exit 3 || exit 0
