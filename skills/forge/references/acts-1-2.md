# Acts 1–2 in detail (forge reference)

> **PROFILE NOTICE.** Acts 1–2 are identical at both execution profiles and write no
> product code. Execution cadence is selected only after the plan verdict.


The grill and adversarial-review procedure. Loaded on demand from `../SKILL.md`.

## ACT 1 — GRILL (you ↔ Claude)

> Interview me relentlessly about every aspect of this plan until we reach a shared
> understanding. Walk down each branch of the design tree, resolving dependencies
> between decisions one-by-one. For each question, provide your recommended answer.
>
> Ask the questions one at a time, waiting for my answer before continuing.
>
> If a question can be answered by exploring the codebase, explore the codebase instead.

When the decision tree is resolved and you're aligned, **write the agreed plan to
`PLAN.md`**, then move to Act 2:

```markdown
# Plan: <task>
_Locked via forge — by Claude + <user>_

## Goal
<one paragraph — reflects what the grilling actually settled>

## Approach
<numbered, concrete steps>

## Key decisions & tradeoffs
<the contestable choices the grill resolved — name them so Sol has something to bite>

## Risks / open questions
<anything still genuinely open>

## Out of scope
<bounds the grill established>
```

Initialize `PLAN-REVIEW-LOG.md`:
```markdown
# Plan Review Log: <task>
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=<n>.
```

---

## ACT 2 — ADVERSARIAL REVIEW (Claude ↔ Codex/Sol)

Hand the locked plan to Sol. **Follow `ENGINE.md` §3 (preflight) then §4a/§4b
(templates).** The mechanics — read-only every round, `< /dev/null`, explicit
`THREAD_ID`, 10-min ceiling — are all in ENGINE.md and are non-negotiable.

### Tunables (read from args, else default)
| Var | Default | Meaning |
|-----|---------|---------|
| `MAX_ROUNDS` | `5` | Hard cap on review rounds. The loop ALWAYS terminates here. |
| `PLAN_FILE` | `PLAN.md` | The plan Act 1 produced. |
| `LOG_FILE` | `PLAN-REVIEW-LOG.md` | Act-1/2 + routing transcript; immutable once frozen. |
| `EXEC_LOG` | `PLAN-EXECUTION-LOG.md` | Acts-3/4 receipts; append-only hash chain. |

If invoked with `rounds=3`, use that for `MAX_ROUNDS`. Echo resolved values +
the resolved engine/model (ENGINE.md §3) before Round 1.

### The review prompt (write to a temp file `$REVIEW_PROMPT`, sent each round)
> You are an adversarial reviewer for an implementation plan. Be skeptical and
> specific — your job is to find what breaks, not to be agreeable. Read the plan at
> `PLAN.md` and any repo files you need (you are read-only). Identify concrete
> flaws: security holes, race conditions, missing edge cases, schema conflicts,
> wrong assumptions, observability gaps, simpler alternatives. For each, give a
> one-line fix. Do NOT modify any files. End your reply with EXACTLY one line:
> `VERDICT: APPROVED` if the plan is sound enough to implement, or `VERDICT: REVISE`
> if it still has material problems.

### Each round, after the reviewer returns
1. Read **this round's own** verdict file — `/tmp/forge-verdict-$SLUG-r<n>.txt`, the
   per-run-unique path from ENGINE.md §4a, never a shared fixed name — and confirm its
   mtime postdates the round's launch before trusting it. (A stale file from a previous
   run parses exactly like a fresh success; that is how a killed round once read
   yesterday's verdict for a different plan.) Append to `LOG_FILE`:
   `## Round <n> — <reviewer model>` + the full critique. On a failed round, read the
   round's `.err` file too — stderr is captured, never discarded.
2. Grep the last line for the verdict:
   - `VERDICT: APPROVED` → break to Resolution (converged).
   - `VERDICT: REVISE` → **Claude decides what's actually worth acting on** (Claude is
     final arbiter — Sol advises, doesn't command). Revise `PLAN_FILE`. Append
     `### Claude's response` to `LOG_FILE`: what changed, what was rejected, why. Increment.
3. If round > `MAX_ROUNDS` → break to Resolution (deadlock).

### Resolution (Gate 1: plan verdict)
- **APPROVED:** present the final `PLAN_FILE`, a 3-bullet summary of what the two acts
  improved, and the round count. Then go to **Act 3/4 dispatch** below.
- **Deadlock (MAX_ROUNDS hit, no APPROVED):** do NOT fake convergence. List each
  unresolved point + Claude's counter-position; hand it to you to break the tie.

---

After `APPROVED`, return to [`../SKILL.md`](../SKILL.md), resolve the active profile,
and dispatch Act 3 or 4. This reference deliberately contains no build procedure.
