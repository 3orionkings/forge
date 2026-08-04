# Usage limits and credit policy (forge reference)

> **PROFILE NOTICE.** Any cross-model unit review, complete-diff review, orchestrator
> proof rerun, fresh-database rebuild or full per-unit gate described below is
> **`assured`-only**. Under `sprint`: one smallest relevant proof, no unit review, no
> rerun, no unit gate; the commit check is changed-paths-match-admitted-set + no
> overlap/safety breach + executor proof exited 0. Loading this file does not change
> the active profile (see [`../profiles/sprint.md`](../profiles/sprint.md)).


## 6. Usage-limit resilience (Codex + Claude)

Applies whenever a usage/rate/quota limit appears on either model. **Invariant: a
limit never silently breaks, fakes, or quietly degrades work.** There are exactly two
regimes, and §6.0 decides which one you are in **before** you touch the ladder.

| Regime | Trigger | Behavior |
|---|---|---|
| **Credit boundary** (§6.0) | **≤5% remaining allowance on either side, or an outright cutoff** | **STOP and ASK the owner.** Never auto-degrade across this line. |
| **Transient limit** (§6.1–§6.2) | rate/quota pressure **above** 5% remaining — overload, back-pressure, a rung-level cap | Codex auto-degrades down its ladder and logs each step; a Claude *driver* limit is a resumable PAUSE. |

### 6.0 Credit boundary — ≤5% or cutoff: STOP and ASK (governs §6.1–§6.3)

Per `ROUTING-POLICY.md` §4, and it **supersedes any automatic degrade for this case**.

When either side reaches **≤5% remaining allowance**, or is cut off outright:

1. **Stop.** Do not start another unit on that side. Finish nothing halfway and
   claim nothing finished.
2. **Ask the owner which model to use.** This is an ask, not a menu-and-proceed:
   state the position plainly — which side, what is left, what is preserved, which
   unit is mid-flight — and **wait**.
3. **Propose one tier down on that side as the default:**
   - **Claude side: Fable 5 → propose Opus 5.**
   - **Codex side: Sol 5.6 ultra → no automatic proposal. Ask the owner.** (The Codex
     ladder's own next rung is *not* an acceptable silent answer at this boundary; the
     ladder is for the transient case in §6.1, not for a spent allowance.)
4. **Never fake, silently drop, or quietly degrade work to stay under a limit.** Not a
   trimmed scope presented as the full unit, not a lower rung presented as the routed
   one, not a skipped proof presented as a pass.

*Why this rule exists:* on 2026-07-28/29 a run consumed ~88% of a fresh allowance and
delivered **zero product**. Nothing stopped to ask; the spend was discovered afterwards.
A boundary that is only noticed in the postmortem is not a boundary. The 5% line exists
so the owner spends the last of an allowance on a unit they chose.

**Where to read the number:** the Codex side can be sampled pre-emptively via
`codex app-server` JSON-RPC `account/rateLimits/read` (usage/credits snapshot). The
Claude side has **no programmatic credit visibility** — treat a hard cutoff, or the
owner telling you the figure, as the trigger, and say so honestly rather than guessing.

### 6.1 Codex side — automatic degrade ladder (transient limits only, above the §6.0 line)

Ladder verified against the binary (effort order `low < medium < high < xhigh < max <
ultra`; Sol supports through `ultra`, **`gpt-5.5` tops out at `xhigh`** → the floor).
Rung 0 is identical to `$SOL_ROUTE`.

```bash
CODEX_LADDER=(
  '-m gpt-5.6-sol -c model_reasoning_effort="ultra"'   # 0  == $SOL_ROUTE (default)
  # '-m gpt-5.6-sol -c model_reasoning_effort="max"'   #    optional Sol-only rung (xhigh<max<ultra)
  '-m gpt-5.6-sol -c model_reasoning_effort="xhigh"'   # 1  (extra high)
  '-m gpt-5.6-sol -c model_reasoning_effort="high"'    # 2
  '-m gpt-5.6-sol -c model_reasoning_effort="medium"'  # 3
  '-m gpt-5.6-sol -c model_reasoning_effort="low"'     # 4  (Sol default; "light")
  '-m gpt-5.5      -c model_reasoning_effort="xhigh"'  # 5  FLOOR — nothing below this
)
```
- `"light" = low` (neither model advertises `minimal`). `max` is a Sol-only rung — uncomment it for a finer top step.
- **Non-monotonic seam:** rung 4→5 *raises* raw effort (5.5-xhigh > Sol-low) while dropping model tier — a deliberate quality/cost step-down matching the "5.5-at-xhigh floor."

**Detection** — capture the JSONL stream **and** stderr **and** the exit code (never
`2>/dev/null`-discard them; §4a). Classify (case-insensitive, on message text or JSON
`type`). **The first row is the §6.0 case and leaves this ladder entirely:**

| Bucket | Match | Action |
|--------|-------|--------|
| **Credit boundary → §6.0** | `out of credits` · `purchase more credits` · `*_credits_depleted`; any allowance reading at **≤5% remaining**; an outright cutoff | **STOP and ASK** (§6.0). Do **not** step down the ladder — the allowance is spent, and a quieter rung spending the rest of it is exactly the silent degrade §6.0 forbids. |
| **Rung-level hard cap** | `usage limit reached` · `reached your usage limit` · `quota exceeded`; types `usage_limit_reached`/`quota_exceeded`/`usage_not_included`/`rate_limit_reached` — with allowance **above** 5% | retry once, then step down one rung and log it |
| **Transient overload** | `high demand` · `high load` · `overloaded` · `at capacity`; type `server_overloaded` | back off (honor `Retry-After` / `try again in N s`), retry the **same** rung |
| **Other** (auth/model/git) | anything else | surface, never retry blind (per §3/§4) |

If the remaining allowance is unknown when a cap fires, **check it** (`account/rateLimits/read`)
before choosing a row. Guessing "probably still above 5%" and stepping down is how a
silent degrade gets rationalized.

**Credits-first (honest).** Codex credits don't auto-apply until *purchased*; once
purchased, drawdown is automatic server-side. forge can't buy/redeem them — but it
honors the intent by message: on a **rung-level** hard cap, **retry the current rung
once** (a positive balance would already be covering it; a persistent failure means
that rung is genuinely capped), then step down. A message naming credits directly
(`out of credits` / `purchase more credits`) is the **credit boundary, not a rung
cap** — go to §6.0, surface the buy/redeem link (`chatgpt.com/codex/settings/usage`),
and ask. *(Pre-emption is preferred over reaction: poll
`codex app-server` JSON-RPC `account/rateLimits/read` and raise §6.0 at the 5% line
**before** the error — best-effort, not verified against a live limit here.)*

**Procedure (Claude runs it rung by rung via the Bash tool):**
1. Run the current rung — the §4 template with the rung's `-m/-c` substituted for `$ROUTE` (builds) or the reviewer route (reviews).
2. Success → continue. Transient → wait + retry the **same** rung. Rung-level hard cap
   (allowance still above 5%) → retry once; still capped → **step down one rung**,
   re-run, and log the step-down to console plus the active phase log: the unfrozen
   `PLAN-REVIEW-LOG.md` during Act 2, otherwise `PLAN-EXECUTION-LOG.md`. **Credit boundary
   (≤5% or cutoff) → leave the ladder and run §6.0: stop and ask.**
3. Below the floor (5.5-xhigh also capped) → **STOP.** Write a STATUS block (unit, last rung, what's preserved), read `resets_at` if available, and ask:
   > Codex is exhausted through its floor (gpt-5.5 @ xhigh). Nothing lost —
   > PLAN.md + the worktree are intact. Choose: **(a)** buy credits / redeem a
   > reset at chatgpt.com/codex/settings/usage, then I resume; **(b)** wait for
   > reset; **(c)** re-route this unit to the Claude side (a normal routing change —
   > propose it with a one-line reason and Gate-2 it); **(d)** park the unit
   > preserved for a later recovery lane.

**Codex reviewer note (relaxes §4).** §4's Codex reviews normally run
`$SOL_ROUTE`. Under a **rung-level** cap that reviewer walks this same ladder (stays
Sol as long as possible, then the 5.5-xhigh floor); a degraded review is labeled with
its actual rung in the log — labeled, because an unlabeled degraded review is a silent
degrade. Every Codex-executed unit is still cross-reviewed on the Claude side (§5), so
a lower-effort output is never silently trusted. At the §6.0 credit boundary the
reviewer stops with everything else.

Reference wrapper (illustrative — Claude may run the steps directly instead):
```bash
# CREDIT_RE is checked FIRST and exits the ladder — it is the §6.0 stop-and-ask case.
# Stepping down on a spent allowance would be the silent degrade §6.0 forbids.
CREDIT_RE='out of credits|purchase more credits|credits_depleted|insufficient_quota'
HARDCAP_RE='usage limit reached|reached your usage limit|quota exceeded|usage_limit_reached|quota_exceeded|usage_not_included|rate_limit_reached'
TRANSIENT_RE='high demand|high load|overloaded|at capacity|server_overloaded'
forge_codex_run() {  # $1=prompt  $2=start rung (default 0).
                     # rc: 0 ok · 2 other-error · 3 floor-exhausted · 4 CREDIT BOUNDARY → §6.0 stop-and-ask
  local prompt="$1" i="${2:-0}" out rc frag
  for (( ; i<${#CODEX_LADDER[@]}; i++ )); do
    frag="${CODEX_LADDER[$i]}"
    out=$(eval "$CODEX_BIN exec $frag --skip-git-repo-check --json -o \"\$CODEX_OUT\" \"\$prompt\" < /dev/null 2>&1"); rc=$?
    if [[ $rc -eq 0 ]] && ! grep -Eq '"type":"(turn\.failed|error|stream_error)"' <<<"$out"; then return 0; fi
    if grep -Eqi "$CREDIT_RE" <<<"$out"; then
      echo "[forge] Codex CREDIT BOUNDARY at rung $i → §6.0: stopping, asking the owner (no auto step-down)" >&2; return 4; fi
    if grep -Eqi "$TRANSIENT_RE" <<<"$out"; then
      w=$(grep -oiE 'try again in *[0-9.]+ *(s|ms|seconds?)' <<<"$out" | grep -oE '[0-9.]+' | head -1); sleep "${w:-30}"; (( i-- )); continue; fi
    if grep -Eqi "$HARDCAP_RE" <<<"$out"; then echo "[forge] Codex rung cap at $i → stepping down (verify allowance >5%)" >&2; continue; fi
    echo "[forge] Codex non-limit error: $out" >&2; return 2
  done
  echo "[forge] FLOOR EXHAUSTED (gpt-5.5 @ xhigh). Work preserved; pausing." >&2; return 3
}
```

### 6.2 Claude side — pause, not break (honest boundary)

**forge CAN automate** only Act-4 *subagent* model/effort: dispatch a unit's Claude
subagent at a lower `effort` (`low|medium|high|xhigh|max`) or at a different model.
That is the Claude analogue of the ladder, and it applies to any unit routed to the
Claude side — but **not** to the session you're in, and **not** across the §6.0 credit
boundary, where the answer is stop-and-ask regardless of what forge could technically
automate.

**forge CANNOT** (user/harness only): change the **driver's** model/effort (skills
can't run `/model`, `/effort`, `/fast`), touch **credits/billing**, or **auto-resume**
after a limit.

**A driver limit is a PAUSE.** The turn errors and stops, but durable state survives —
git worktrees, edited files, the transcript, and forge's own `PLAN.md`, immutable
`PLAN-REVIEW-LOG.md`, and append-only `PLAN-EXECUTION-LOG.md` — so the limit lands on a checkpointed state.
On a Claude limit, forge prints:
> Claude hit its usage limit — nothing lost (plan in PLAN.md, history in
> PLAN-REVIEW-LOG.md, runtime receipts in PLAN-EXECUTION-LOG.md, in-flight work in its
> worktree). Per §6.0 the default proposal on
> this side is **one tier down: Fable 5 → Opus 5** — say the word and I switch, or name
> a different model. To continue: **(1)** add Claude credits via Anthropic billing — I
> can't do this for you; **(2)** lower the driver yourself with `/fast` or `/effort`
> (a skill cannot change the driver); **(3)** wait for the reset. Then run
> `claude --continue` (or `/resume`) and say "continue /forge from PLAN.md". I can keep
> delegable units moving on the Codex side meanwhile — tell me which.

**Correction to the "floor" model:** `/fast` *lowers* effort; **`ultracode` *raises*
it** (`xhigh` + dynamic workflows = higher cost) — so ultracode is a *ceiling*, not a
floor. The low-cost Claude floor is **Opus 4.8 in fast mode**, and it's a user action.

### 6.3 When forge asks vs. degrades automatically

| Situation | Behavior |
|-----------|----------|
| **≤5% remaining allowance or a cutoff, either side** | **ASK — always, §6.0.** Propose one tier down on that side (Claude: Fable 5 → Opus 5; Codex: no automatic proposal). No auto-degrade across this line. |
| Codex step-downs (ultra→…→5.5-xhigh) and transient retries, **above** the 5% line | **Automatic** — forge owns the flags; log the actual rung. Cross-model result review is `assured`-only. |
| Codex floor (5.5-xhigh) also capped | **Ask** (menu in 6.1) |
| Anything on the Claude driver (model/effort/credits) | **Ask / user-executed** |
| Buying credits / redeeming resets (either side) | **Ask** (billing is user-only) |
| Re-routing a blocked unit to a different model | **`sprint`:** apply the owner overlay/default and record one line; no Gate 2. **`assured`:** return through Gate 2 with a one-line reason. |

**Both sides limited:** above the 5% line, Codex auto-degrades to its floor (that unit
is blocked if the floor is also capped) and the Claude driver pauses; at or below it,
both stop and ask. forge writes one combined STATUS block to the active phase log
(`PLAN-REVIEW-LOG.md` only before freeze; `PLAN-EXECUTION-LOG.md` afterwards) and
one merged resume menu (both reset times, both buy/redeem links, the proposed one-tier-
down on each side, the manual driver-effort step) — never silent breakage.

### 6.4 Honest gaps vs. the requested policy
1. **"Credits first"** is *partly* automatable on Codex (detect + defer to auto-drawdown + retry-once); on **Claude** there's zero credit visibility — buying/redeeming is always a user action.
2. **Effort step-down** is fully automatable on **Codex**; on **Claude** only for Act-4 subagents, never the driver.
3. **The Claude floor "Opus 4.8 ultracode" is a contradiction** — ultracode is higher-cost. The low-cost floor is **Opus 4.8 fast mode**, user-selected.
4. **Neither side auto-resumes** — forge minimizes the cost by checkpointing artifacts, but resume is user-initiated.
5. **"Work is never silently broken"** — fully honored, no gap.
6. **The 5% trigger is only as good as its reading.** Codex exposes a usage snapshot
   (`account/rateLimits/read`); the Claude side does not, so on that side the trigger is
   an observed cutoff or the owner's own figure. Say which of the two you are acting on
   rather than implying a measurement you did not take.
