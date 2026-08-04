# Routing detail (forge reference)

> **PROFILE NOTICE — this reference documents `assured` cadence.**
> Cross-model unit review, orchestrator proof rerun, fresh-database rebuilds and full
> per-unit gates described below apply **only under `assured`**. Under `sprint` the unit
> carries one smallest relevant proof, no review and no rerun — see
> [`../profiles/sprint.md`](../profiles/sprint.md). Loading this file does not change the
> active profile.
> **Do not load this argued-routing procedure under `sprint`; apply the owner
> overlay/default directly as the sprint profile requires.**


Canonical policy: [`../ROUTING-POLICY.md`](../ROUTING-POLICY.md). This file holds the complexity/assignment mechanics.

## 5. Model-routing policy (used by `forge` §Act-4 and `forge-orchestrate`)

Governed by `ROUTING-POLICY.md`; this section is its operational form. **Routing is
proposed and argued, never hard-coded.** No unit is owned by a vendor.

After a plan is APPROVED, assess **complexity** (not "sprint count"):

- **Simple / narrow** (a small fix, a single-system change, a mechanical migration)
  → request admission from `dispatch-guard.sh`, then run **single-track on the default
  executor (Sol 5.6 @ ultra)** automatically via `forge-build` if admitted — unless the
  plan argued a different executor, in which case use that one. Do **not** interrupt the
  user for a redundant execution or commit approval.
- **Complex** (multiple systems, non-trivial architecture, cross-cutting change)
  → **do not auto-run.** Propose a work breakdown and, for each unit, the model
  that should execute it **with a one-line reason**, then wait at Gate 2. The user
  replies "approved" (guarded execution commences) or re-routes any unit to either side.

### How the split is produced (this is the part that was missing)

1. **Both models propose.** Claude drafts a split; Sol is asked, in the same read-only
   review thread as the plan review, to attack it — which units it should hold, which
   Claude should, and why. Cost is a legitimate argument and usually Sol's strongest.
2. **Claude arbitrates.** Claude is final arbiter, as on every REVISE: adopt the good
   arguments, reject the bad ones *with a reason*.
3. **Log it.** The agreed table, **with the one-line reason per unit**, goes into
   `PLAN-REVIEW-LOG.md`. *A routing table that was never argued is not a routing
   decision, and a unit with no logged reason has not been routed — it has been assumed.*
4. **Defaults if nobody argues:** Sol 5.6 @ ultra (Codex side), Fable 5 (Claude side),
   **leaning toward Sol for token-heavy units because Sol is cheaper per token**. This
   is a lean the plan may override with a logged reason, not a lock.

Proposal table format (present this, then stop for sign-off):

| # | Unit | Scope | Proposed executor | Why (one line) | Worktree | Files | Weight | Proof |
|---|------|-------|----------|-----|----------|-------|--------|-------|
| 1 | Payments schema migration | product | Codex / Sol 5.6 @ ultra | Token-heavy schema sweep; cheapest per token on Sol | `codex/payments-schema` | `db/payments.sql` | HEAVY | `pnpm test payments` |
| 2 | Checkout UI implementation | product | Claude / Fable 5 | Small surface, heavy design-system context Claude already holds | `claude/checkout-ui` | `src/Checkout.tsx` | HEAVY | `pnpm test:e2e checkout` |
| 3 | Forge receipt audit | control-plane/review | Claude / Fable 5 | Orchestrator-side receipt work; no worktree contention | `claude/receipt-audit` | `PLAN-REVIEW-LOG.md` | LIGHT | syntax/receipt check |
| 4 | Log-format normalizer | control-plane | Codex / Terra @ high | Mechanical, high volume, no judgement — cheapest rung that suffices | `codex/log-format` | `REVIEW-FORMAT-CHECK.md` | LIGHT | syntax check |

*(The rows are illustrative of arguing, not a template to copy. Rows 2 and 4 exist
precisely to show that neither side is reserved for a class of work.)*

Routing rules:
- **Either side may execute any unit** — product implementation included — when the
  argued proposal says so and the reason is logged.
- **Skew token-heavy units to Sol** on cost, absent a better argument.
- **The executor never commits or merges** (§4, `ROUTING-POLICY.md` §3). Vendor neutral.
- **Parallel** ← only when `dispatch-guard.sh` admits every unit: fewer than three
  active lanes before launch, one writer per worktree, disjoint exact file sets,
  and no competing HEAVY/gate lease.
- **Cross-model review** ← the reviewer is **whichever side did not execute the unit**.
  Codex-executed units get a Claude-side complete-diff review plus a Claude-side proof
  rerun; Claude-executed units get a Codex read-only review (template 4a) *and* the
  orchestrator still reruns proof itself. A same-model subagent review is not
  cross-model review.

Autonomy: the grill (Act 1) is interactive; after that, execution runs without
round-by-round approval between two **code gates** — **Gate 1** plan APPROVED and
**Gate 2** routing approved (complex builds only). The former Gate 3 owner
diff/commit approval is abolished; the owner has granted merge autonomy. The
Claude-side orchestrator commits or merges autonomously after (a) reading the complete
diff and (b) rerunning the declared proof itself and seeing it pass. A pasted executor
proof never satisfies this condition, and no executor ever commits.

---
