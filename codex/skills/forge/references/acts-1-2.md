# Acts 1–2

Acts 1 and 2 are identical under Sprint and Assured. Codex is the interviewer and plan
arbiter; Fable is the default external adversarial reviewer. **No product code** is
written in either act.

## Act 1 — Codex grill

Codex interviews the owner **one question at a time** and waits for each answer. Each
question includes Codex's recommended answer and the tradeoff it resolves. Walk the
decision tree until goal, constraints, behavior, interfaces, risks, acceptance proof,
and out-of-scope boundaries are explicit.

Before asking a factual repository question, **explore the code**. Search files,
tests, schemas, project instructions, build commands, and existing conventions rather
than asking the owner for discoverable facts. Ask only for intent, preference,
authority, external facts, or genuinely ambiguous choices.

Do not write implementation code, generated product artifacts, migrations, or tests.
Repository exploration is read-only. The only Act-1 writes are the declared
`PLAN.md` and `PLAN-REVIEW-LOG.md` controls; pin their exact paths and hashes for
Act 2. No product or unrelated dirty path is allowed.

### Question discipline

For every unresolved branch:

1. state the decision in plain language;
2. cite the discoverable repository evidence already inspected;
3. recommend one answer with its material tradeoff;
4. ask one question;
5. wait;
6. record the answer before opening the next branch.

Do not batch ten questions into a questionnaire. Do not treat silence as assent. Do
not offer an implementation plan while a controlling requirement remains unresolved.

### Lock the plan

When the decision tree is resolved, Codex writes `PLAN.md`:

```markdown
# Plan: <task>

## Goal
<observable outcome>

## Requirements
<locked behavior and constraints>

## Approach
<ordered implementation units with dependencies>

## Acceptance and proof
<exact evidence required>

## Key decisions and tradeoffs
<each contestable decision from the grill>

## Risks and open questions
<only genuinely unresolved items>

## Out of scope
<explicit boundaries>
```

Codex also initializes `PLAN-REVIEW-LOG.md` with the task, plan hash or immutable
identity, chosen maximum review rounds, date, profile source if already known, and an
Act-1 summary. The log remains append-only through Act 2.

The plan is Codex's artifact. The owner controls intent; Codex decides how confirmed
requirements are represented and remains responsible for later revisions.

## Act 2 — External adversarial plan review

Act 2 is a **read-only adversarial plan review** by Claude/Fable 5 at max. All Claude
control uses the [typed bridge tools](bridge-tools.md). Never invoke a provider
command, reconstruct shell, or select a session by recency.

Set a finite `MAX_ROUNDS` before the first external turn; use the owner's bound or a
default of five. Also set owner-supplied per-job wall, token, turn, and USD bounds as
described in [usage-limits.md](usage-limits.md).

### Provider-free preflight

Run the bridge preflight for the exact repository and verify:

- status is ready and `provider_called` is false;
- repository identity/baseline is the intended one;
- state and cancellation containment are healthy;
- the resolved external model and effort are Fable 5 at max unless a binding overlay
  says otherwise.

A failed preflight stops before provider work. Do not replace it with a direct CLI
presence check.

### Start the review

Start one role=`review` job with:

- a stable unique idempotency key;
- the locked plan and relevant repository paths;
- read-only reviewer instructions;
- active profile and review admission from [admission.md](admission.md);
- empty product file reservation;
- exact model/effort and owner budget.

The reviewer prompt requires concrete flaws in security, concurrency, data/schema,
compatibility, observability, rollout, acceptance proof, and simpler alternatives.
Every finding includes a specific fix. The response ends with exactly one final line:

```text
VERDICT: APPROVED
```

or:

```text
VERDICT: REVISE
```

Extra verdicts, missing verdicts, variants, or text after the verdict do not pass.

Persist the returned job ID, attempt/session evidence, and revision. Observe with
bounded status/wait calls, then retrieve the terminal result and receipts.

### Arbitrate a round

Codex appends the full critique and exact external receipt to
`PLAN-REVIEW-LOG.md`. Codex then **accept or reject each finding with reasons**:

- accepted: name the plan change and why it improves correctness;
- rejected: cite repository/requirement evidence and why the finding does not apply;
- deferred: identify the owner decision or external fact required; it remains open.

Claude advises; Codex is the **plan arbiter**. Codex does not accept a finding merely
to manufacture convergence, and does not hide a rejected finding.

### Revise and resume

On exact `REVISE`, Codex updates `PLAN.md`, appends a point-by-point response and new
plan identity to the log, and resumes the **same explicit session** at the exact
stored revision. The bridge revalidates session, repository identity, and admission.
Never create an unlinked reviewer and call it the same conversation.

The next prompt identifies what changed and asks the reviewer to reassess the whole
plan, not only the accepted points. Continue until exact approval or the round bound.

If baseline or admission identity prevents a valid explicit resume after a legitimate
plan-artifact update, stop and reconcile the control-artifact placement/identity. Do
not use latest/implicit continuation to bypass the check.

### Gate 1

**Gate 1 applies to both profiles.** It has only two outcomes:

1. `APPROVED`: the latest external response ends exactly
   `VERDICT: APPROVED`, every finding has a recorded Codex disposition, and the log
   binds the final plan and receipt.
2. **bounded owner deadlock**: `MAX_ROUNDS` is exhausted without approval. Codex lists
   each unresolved point, its own reason, and the reviewer's counter-position, then
   asks the owner to decide. It never fabricates approval.

`REVISE`, provider/policy pause, invalid verdict, missing receipt, or stale session is
not Gate-1 approval. No product code starts while any of those states remains.

### Hand off to execution

After genuine approval, Codex:

1. records Gate 1 and the exact approved plan identity;
2. resolves profile authority: owner instruction, repo profile, then Assured;
3. classifies the plan as one narrow frozen unit (Act 3) or a dependency-aware set of
   units (Act 4);
4. routes under [ROUTING-POLICY.md](../ROUTING-POLICY.md);
5. opens [Acts 3–4](acts-3-4.md).

Act 2 never transfers driver, routing, integration, or commit authority to the
reviewer.
