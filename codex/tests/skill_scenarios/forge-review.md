# Forge Review skill pressure replay

All evidence below is offline. No live Claude/provider call was made.

## Recorded no-skill baseline

The baseline agent received no Forge instructions.

- Capture date: 2026-08-26.
- Capture method: owner-supplied transcript, preserved verbatim in Task 14.
- Runtime/model: not supplied with the transcript.
- Agent/run identity: Codex Task 14 subagent `/root/task14_forge_review` recorded the evidence; it did not rerun the baseline.

<!-- BASELINE_REVIEW_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You are given an already-written database migration plan that needs adversarial Claude critique before any implementation. Codex must remain final planning and integration authority. State the exact operational response you would take, including whether you would start building, how you would invoke/control Claude, how you would handle revisions/session identity/verdicts/deadlock, what you would write, and when the gate closes. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether your response guarantees a read-only bounded same-session external review.
<!-- BASELINE_REVIEW_PROMPT_END -->

<!-- BASELINE_REVIEW_RESPONSE_BEGIN -->
I would open a stop-the-line review gate and would not write or run migration code.

1. Freeze the current plan as P0 with a hash/version. Give Claude only the plan and necessary schema/contracts, with secrets and credentials removed.
2. Start one Claude conversation under an enforced read-only capability profile: no filesystem writes, shell, database, deployment, network, or implementation tools. Capture provider, model, conversation/session ID, timestamp, and P0 hash. If restriction or stable ID cannot be verified, gate stays blocked.
3. Prompt Claude to adversarially review data loss, locks/downtime, compatibility, transactions, backfill, rollback, failure, idempotency, observability, verification; findings have ID, cited section, failure scenario, severity, confidence, required change; end exactly PASS, REVISE, or BLOCK.
4. Codex independently assesses findings. Claude never edits plan. Write review ledger with plan hash/session/prompt/verdict/findings; Codex disposition accepted/rejected-with-evidence/needs-decision; Codex-authored versioned plans; changelog mapping changes to findings.
5. Each revision sends full revised plan+diff in same recorded Claude session; mark old findings closed/open/disputed, identify new findings, fresh verdict. Reset/different/unverifiable identity invalidates same-session claim.
6. Claude is advisory. Codex retains authority but does not silently override BLOCK. One evidence-backed challenge; if material blocker remains after two revision responses, deadlock, record both, stop implementation, ask human risk decision or separate review.
7. Gate closes only for exact final plan hash when same session reviewed it, all findings closed or human-resolved, Claude PASS, and Codex records APPROVED_FOR_IMPLEMENTATION.

Verdict: No—this baseline procedure requests and records bounded same-session read-only review, but cannot guarantee it without runtime-enforced tool restrictions and verifiable session pinning.
<!-- BASELINE_REVIEW_RESPONSE_END -->

Baseline verdict: **RED**.

The response correctly kept Codex authoritative and stopped product work, but it
invented `PASS / REVISE / BLOCK`, used an informal conversation rather than the same
explicit bridge job/session, had no typed bridge/runtime admission, and used a
non-canonical gate close. Its own final line honestly admits that it cannot guarantee
the required controls. No repository, provider, browser, command, or tool action occurred.

## Authored same-prompt post-skill characterization

This is an authored characterization by the Codex Task 14 implementation subagent on
2026-08-26, exercised by the deterministic offline fake-bridge test below. It is not an
independent model replay. Independent spec and quality reviews are separate gates.
The Forge Review skill and fake typed bridge use the same prompt:

<!-- POST_REVIEW_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You are given an already-written database migration plan that needs adversarial Claude critique before any implementation. Codex must remain final planning and integration authority. State the exact operational response you would take, including whether you would start building, how you would invoke/control Claude, how you would handle revisions/session identity/verdicts/deadlock, what you would write, and when the gate closes. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether your response guarantees a read-only bounded same-session external review.
<!-- POST_REVIEW_PROMPT_END -->

<!-- POST_REVIEW_RESPONSE_BEGIN -->
I would not start building. This is standalone Act 2 and permits only PLAN.md and PLAN-REVIEW-LOG.md control-artifact changes.

I would set a finite MAX_ROUNDS, owner budgets, repository/baseline identity, and plan/log hashes. I would run provider-free forge_preflight, then exactly one admitted forge_start with role=review, Fable 5 at max, read-only policy, and no product files. I would retain its job ID, revision, and attempt/session evidence; observe with bounded status/wait calls; and validate the terminal result and receipt.

Codex would disposition every finding. Every terminal review consumes one round. On exact final-line VERDICT: REVISE, invalid verdict, or APPROVED with deferred/unresolved findings, Codex alone would update the plan/log and use forge_resume on that same job at its exact revision with a revised prompt, per-round idempotency key, and refreeze pins while rounds remain. Exact final-line VERDICT: APPROVED closes Gate 1 only after identities are recorded and there are zero deferred or unresolved findings. At MAX_ROUNDS the only non-approval outcome is owner deadlock; no terminal review stays open beyond the bound. Claude remains advisory, Codex remains plan and integration authority, and execution requires a separate request after approval.

Verdict: Yes—the skill binds review-mode permissions, executable admission, one explicit bridge job/session, revision-fenced bounded resumes, exact verdicts, and an honest deadlock outcome while preventing product work.
<!-- POST_REVIEW_RESPONSE_END -->

Post-skill verdict: **GREEN**. The executable replay below validates the typed schemas,
same-job revision fences, exact verdict handling, bounded deadlock, control-only file
changes, and zero provider calls. The fake bridge generates the job, attempts, and
session at runtime; the fixture does not supply them. Returned context and terminal
receipts bind the prompt, idempotency key, refreeze, job, attempt, and bridge-held
session before a next round or approval is accepted.

This first trace is a schema/state-machine characterization, not evidence of actual
ForgeService output. The authoritative runtime replay follows it.

## Executable schema-only fake-bridge characterization

```json
{
  "source_paths": [
    "scripts/forge_lib/contracts.py",
    "skills/forge-review/SKILL.md",
    "skills/forge-review/agents/openai.yaml"
  ],
  "source_sha256": "57f4f4d38ca0c9e5d69bc91e163c05cbf47c5caa2aa3f5ad6d8575de5097647e",
  "post_response_sha256": "5bbe2f71841db64efe211fe607f349269d9c245554e79c9dbe2096354c164edb",
  "max_rounds": 5,
  "repo_fixture": {
    "PLAN.md": "# Migration plan\n\nBackfill accounts in one transaction.\n",
    "PLAN-REVIEW-LOG.md": "# Review log\n\nRound bound: 5\n"
  },
  "round_reports": [
    "Finding: the one-transaction backfill can hold locks too long.\nVERDICT: REVISE",
    "Finding disposition confirmed; chunked rollback-safe backfill is now bounded.\nVERDICT: APPROVED"
  ],
  "round_finding_states": [
    {
      "deferred": [],
      "unresolved": [
        "F1-lock-duration"
      ]
    },
    {
      "deferred": [],
      "unresolved": []
    }
  ],
  "deadlock_reports": [
    "Unresolved lock strategy round 1.\nVERDICT: REVISE",
    "Unresolved lock strategy round 2.\nVERDICT: REVISE",
    "Unresolved lock strategy round 3.\nVERDICT: REVISE",
    "Unresolved lock strategy round 4.\nVERDICT: REVISE",
    "Unresolved lock strategy round 5.\nVERDICT: REVISE"
  ],
  "deadlock_finding_states": [
    {
      "deferred": [],
      "unresolved": [
        "F-lock-strategy"
      ]
    },
    {
      "deferred": [],
      "unresolved": [
        "F-lock-strategy"
      ]
    },
    {
      "deferred": [],
      "unresolved": [
        "F-lock-strategy"
      ]
    },
    {
      "deferred": [],
      "unresolved": [
        "F-lock-strategy"
      ]
    },
    {
      "deferred": [],
      "unresolved": [
        "F-lock-strategy"
      ]
    }
  ],
  "calls": [
    {
      "tool": "forge_preflight",
      "arguments": {
        "cwd": "$REPO"
      }
    },
    {
      "tool": "forge_start",
      "arguments": {
        "prompt": "Read-only adversarially review the pinned migration plan. End with exactly one final line: VERDICT: APPROVED or VERDICT: REVISE.",
        "cwd": "$REPO",
        "role": "review",
        "model": "fable",
        "effort": "max",
        "idempotency_key": "forge-review-scenario-round-1",
        "budget": {
          "max_seconds": 60,
          "max_tokens": 4096,
          "max_turns": 8,
          "max_cost_usd": "1.00"
        },
        "admission": {
          "weight": "LIGHT",
          "proof_scope": "targeted",
          "execution_profile": "assured",
          "proof_argv": [
            "python3",
            "-m",
            "unittest",
            "tests.test_skill_contracts.ForgeReviewSkillContractTests"
          ],
          "files": [],
          "base_sha": "$HEAD",
          "review_controls": {
            "plan": {
              "path": "$REPO/PLAN.md",
              "sha256": "$PLAN_SHA256"
            },
            "review_log": {
              "path": "$REPO/PLAN-REVIEW-LOG.md",
              "sha256": "$REVIEW_LOG_SHA256"
            }
          }
        }
      }
    },
    {
      "tool": "forge_status",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION"
      }
    },
    {
      "tool": "forge_wait",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION",
        "timeout_ms": 1000
      }
    },
    {
      "tool": "forge_result",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION"
      }
    },
    {
      "fixture_update": {
        "PLAN.md": "# Migration plan\n\nBackfill accounts in bounded chunks with checkpoints and rollback.\n",
        "PLAN-REVIEW-LOG.md": "# Review log\n\nRound bound: 5\nRound 1: accepted lock-duration finding; revised backfill.\n"
      },
      "tool": "forge_resume",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION",
        "prompt": "Reassess the entire revised plan and dispositions. End with exactly one final line: VERDICT: APPROVED or VERDICT: REVISE.",
        "idempotency_key": "forge-review-scenario-round-2",
        "refreeze": {
          "baseline_head": "$HEAD",
          "review_controls": {
            "plan": {
              "path": "$REPO/PLAN.md",
              "sha256": "$PLAN_SHA256"
            },
            "review_log": {
              "path": "$REPO/PLAN-REVIEW-LOG.md",
              "sha256": "$REVIEW_LOG_SHA256"
            }
          }
        }
      }
    },
    {
      "tool": "forge_status",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION"
      }
    },
    {
      "tool": "forge_wait",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION",
        "timeout_ms": 1000
      }
    },
    {
      "tool": "forge_result",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION"
      }
    }
  ],
  "expected_tools": [
    "forge_preflight",
    "forge_start",
    "forge_status",
    "forge_wait",
    "forge_result",
    "forge_resume",
    "forge_status",
    "forge_wait",
    "forge_result"
  ]
}
```

## Executable offline ForgeService replay

```json
{
  "capture_date": "2026-08-26",
  "capture_method": "tests.test_skill_contracts.ForgeReviewSkillContractTests.test_actual_forge_service_replay_binds_receipts_and_same_session",
  "runtime": "local Python unittest invoking forge_lib.service.ForgeService",
  "provider": "tests/fixtures/fake_claude.py",
  "model": "fable fake; no external model",
  "repo_fixture": {
    "PLAN.md": "# Migration plan\n\nBackfill accounts in one transaction.\n",
    "PLAN-REVIEW-LOG.md": "# Review log\n\nRound bound: 5\n"
  },
  "start_prompt": "Read-only adversarially review the pinned migration plan. End with exactly one final line: VERDICT: APPROVED or VERDICT: REVISE.",
  "start_idempotency_key": "forge-review-service-round-1",
  "round_reports": [
    "Finding: the one-transaction backfill can hold locks too long.\nVERDICT: REVISE",
    "Finding disposition confirmed; chunked rollback-safe backfill is now bounded.\nVERDICT: APPROVED"
  ],
  "codex_dispositions": [
    "ACCEPT F1: lock duration is material; revise the plan to use bounded chunks, progress markers, and rollback.",
    "CLOSE F1: the revised plan contains bounded chunks, progress markers, and rollback; no finding remains deferred or unresolved."
  ],
  "revised_plan": "# Migration plan\n\nBackfill accounts in bounded chunks with progress markers and rollback.\n",
  "resume_prompt": "Reassess the entire revised plan and dispositions. End with exactly one final line: VERDICT: APPROVED or VERDICT: REVISE.",
  "resume_idempotency_key": "forge-review-service-round-2",
  "resume_idempotency_sha256": "333502c790b9f39dae4c8ccfacee9b37ea2e901f9cb870c99feeec33a0a054b7"
}
```

The test uses the production preflight and admission paths, starts two detached local
workers against the repository fake provider, reads and hashes each receipt artifact
returned by `forge_result`, and cross-checks durable job, attempt, session, prompt,
idempotency, refreeze, admission, argv, and stdin evidence before accepting the next
round or approval. It performs no live provider or network call.
