# Typed bridge tools

All Claude interaction in Forge uses this bounded MCP surface. The bridge controls
Claude only; Codex-native lanes use native Codex subagents. Schemas are strict and
reject unknown fields. Invalid arguments are not permission to reconstruct another
control path.

**Never invoke `claude` directly. Never reconstruct a shell command. Never select a latest or implicit session. Never expose arbitrary shell through MCP.** Do not select
sessions by recency or infer one from process state. Store the explicit job,
attempt/session, and revision returned by the bridge.

## The complete surface

The nine tools below are the whole supported control plane:

| Tool | Input contract | Use |
|---|---|---|
| `forge_preflight` | optional `cwd` | Validate the repository boundary, state schema, cancellation containment, local CLI/auth status, and bridge version without a provider call. Confirm `provider_called: false`. |
| `forge_start` | required `prompt`, `admission`; optional `cwd`, `role`, `model`, `effort`, `idempotency_key`, `budget` | Atomically admit/reserve and start one detached review or build job. A recovery role is refused before any durable row or worker effect. Returns promptly with job state and idempotency result. |
| `forge_status` | required `job_id`; optional schema `revision` | Read current uppercase state, revision, attempt/session evidence, bounded progress, requested/observed model data, accounting, and pause/failure classification. |
| `forge_wait` | required `job_id`; optional `revision`, `timeout_ms` | Wait for a revision change, terminal state, or timeout. Runtime caps every call at 30,000 ms even though the input schema accepts a larger value. |
| `forge_result` | required `job_id`; optional schema `revision` | Retrieve and cross-validate terminal receipt plus events, stderr, report, receipt, and worker-log artifact hashes. A report larger than the inline bound remains an artifact. |
| `forge_resume` | required `job_id`, exact `revision`; optional `model_override`, revised `prompt`, `idempotency_key`, exact `refreeze` | Resume only the explicitly recorded session. A changed review plan/log or product state needs exact hashes; unrelated drift fails closed. Returns prompt/context hash receipts plus source attempt/session. |
| `forge_steer` | required `job_id`, exact `revision`, `prompt`; optional full `admission_expansion` | Queue one revision-checked, receipted instruction. Expansion additionally binds lease revision, prior files, and the full strict-superset files to a fresh atomic guard receipt. |
| `forge_cancel` | required `job_id`, exact `revision` | Request bounded process-tree cancellation. Unsupported containment pauses by policy; terminal cancellation requires descendant-quiescence evidence. |
| `forge_list` | optional public `status`, `limit` | List bounded recent jobs for the bridge repository. Public status is `queued`, `running`, `completed`, `failed`, or `cancelled`; default limit is 50 and maximum is 1000. |

There is no tenth tool, arbitrary-command field, permission-bypass mode, provider
fallback field, or session selector.

## Active workspace binding

Codex automatically runs the plugin's trusted `PreToolUse` hook before any of these
nine tools. The hook binds the current Codex task's canonical working directory to
that session, and the bridge authenticates the binding before opening durable state.
The reserved `_forge_workspace` input is injected by Codex; never write, copy,
persist, or attempt to synthesize it. If the hook is unavailable, untrusted, corrupt,
or points at a different workspace, the call fails closed before state or provider
effects.

## Start contract

Always pass the owner-approved `cwd`, role, exact model and effort, unique stable
idempotency key, explicit budget, and structured admission. Do not rely on runtime
defaults for a live job. Executable roles in this release are `review` and `build`;
efforts are `low`, `medium`, `high`, `xhigh`, or `max`. The `recovery` value remains
recognizable only so historical state can be identified and refused safely.

The budget object has all four required fields:

| Field | Meaning |
|---|---|
| `max_seconds` | wall-time bound, 1–86,400 |
| `max_tokens` | token bound, 1–10,000,000 |
| `max_turns` | turn bound, 1–1,000,000 |
| `max_cost_usd` | positive decimal string with at most six fractional places |

The admission object is documented in [admission.md](admission.md). A provider job
does not begin unless the guard admits and the reservation is published atomically.
Replay the same semantic request with the same idempotency key; never reuse that key
for a different request.

## Review lifecycle

1. Run the provider-free preflight and inspect its returned repository fingerprints.
2. Start one role=`review` job using Fable 5 at max, a read-only prompt, empty product
   files, the active profile, owner bounds, and exact `review_controls`. The required
   `plan` and `review_log` pins may be accompanied by a sorted unique list of at most
   64 worktree-relative `domain_docs` pins for settled glossary/context/ADR inputs.
3. Persist returned job ID and revision. Use status or bounded wait; do not freehand a
   long blocking loop.
4. At a terminal state, retrieve the result and validate the exact verdict/report.
5. For another bounded round, resume the same explicit job at the exact revision.
   Supply the revised prompt and stable idempotency key. When PLAN.md or the review
   log evolved, `refreeze` pins both same-path controls, the exact current `HEAD`,
   and the exact same domain-document path set with current hashes. The bridge
   verifies the stored session, accepts only those declared control changes, and
   rejects every unrelated dirty path; no caller supplies a guessed session.

The typed shape is:

```json
{
  "review_controls": {
    "plan": {"path": "/worktree/PLAN.md", "sha256": "<64 lowercase hex>"},
    "review_log": {
      "path": "/worktree/PLAN-REVIEW-LOG.md",
      "sha256": "<64 lowercase hex>"
    },
    "domain_docs": [
      {"path": "docs/glossary.md", "sha256": "<64 lowercase hex>"},
      {"path": "docs/adr/0001-settled.md", "sha256": "<64 lowercase hex>"}
    ]
  }
}
```

Omit `domain_docs` when none are needed. Never use absolute domain-document paths,
change their path set on resume, or place them in product `files`. See
[admission.md](admission.md) for canonical path, alias, dirty-set, and worker-boundary
rules. All three review-control roles use exact directory-entry case and Unicode
spelling, single-link regular-file pins, and an immediate pre-provider-spawn worker
revalidation.

## Build lifecycle and recovery refusal

Build requires product admission and starts clean at the immutable baseline.
**Automated recovery is disabled in this release.** `forge_start` rejects a recovery
role before state or provider effects, the worker refuses historical recovery
attempts before processing, and mutating controls refuse historical recovery jobs.
Exact-job status and bounded listing remain read-only. Preserve the evidence and use
a manual handoff; do not retry, resume, steer, cancel, or re-admit the recovery job.

Ordinary steering changes instructions, not the frozen contract. The one exception is
`admission_expansion`, which must name the current bridge lease/revision, its prior
exact files, and the full exact strict superset. The bridge atomically preserves the
same baseline/lane/writer/weight, rechecks dirty containment and cross-kind overlap,
runs the guard, publishes fresh admission, and queues the prompt. Proof, weight,
model route, budget, permissions, and profile still require a new admitted unit.

## State handling

Important internal states are `CREATED`, `PREFLIGHT`, `RUNNING`, `SUCCEEDED`,
`PAUSED_PROVIDER_ACCESS`, `PAUSED_POLICY`, `INTERRUPTING`, `CANCELLED`, and `FAILED`.
Use the returned revision as a compare-and-swap fence.

- `SUCCEEDED`, `FAILED`, and `CANCELLED` still require a validated terminal receipt.
- Provider and policy pauses preserve artifacts and are not approval, completion,
  automatic retry, model substitution, or handoff readiness.
- A nonterminal result request returns `not_ready`; continue with bounded observation.
- Repository-scoped ownership hides foreign jobs as not found.

## Cancellation boundary

Liveness observation never signals a process. Request cancellation only through the
typed cancellation tool above, with the latest exact revision. `INTERRUPTING` is not
yet cancelled. Treat `CANCELLED` as terminal only after the returned receipt proves
the admitted process tree is quiescent. If recursive containment cannot be proven,
the bridge returns `PAUSED_POLICY` and does not improvise a kill path.

See [liveness.md](liveness.md) for bounded observation and
[usage-limits.md](usage-limits.md) for provider/policy pauses.
