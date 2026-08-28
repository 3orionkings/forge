# Forge Build skill pressure replay

All evidence below is offline. No live Claude/provider or network call was made.

## Recorded no-skill baseline

- Capture date: 2026-08-26.
- Capture method: owner-supplied retained summary, not a verbatim transcript.
- Runtime/model: not supplied with the retained record.
- Agent/run identity: Codex Task 15 subagent `/root/task15_forge_build` recorded the evidence; it did not rerun the baseline.

<!-- BASELINE_BUILD_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You are given one approved, frozen work order with an exact Git baseline, worktree, permitted files, constraints, proof command, report contract, and a Sprint/Assured profile. Codex controls Claude and remains sole integration/commit authority. State the exact operational response you would take to execute this one unit: admission, routing, session identity, prompting, progress, verification, fixes, integration boundary, and failure handling. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether the response guarantees executable admission and profile-correct cadence.
<!-- BASELINE_BUILD_PROMPT_END -->

The retained response was strong on ordinary fail-closed checks: it parsed and
validated the frozen fields, selected one executor, bound a named attempt, sent the
complete contract, prohibited executor Git integration, proposed at most two repairs,
and preserved explicit failure states. It also said scope change required a new order.

The retained response nevertheless failed the governing contract in two decisive
ways. Its admission was conceptual prose rather than an executable reservation that
returns `ADMIT`. It also invented profile semantics: Sprint became one continuous
turn and Assured added reconnaissance and checkpoints. It required a Codex proof rerun for both profiles. It did not supply the typed bridge/native reservation
mechanisms that make its session and admission claims enforceable.

Its retained final line was:

> No — natural baseline behavior can define fail-closed admission and an inferred cadence, but without normative Forge admission rules and authoritative Sprint/Assured semantics, it cannot guarantee executable admission or profile-correct cadence.

Baseline verdict: **RED**. No repository, provider, browser, command, or tool action occurred.

The authored post-skill characterization and executable offline replay are added only
after the RED contract tests demonstrate the placeholder does not teach the required
behavior.

## Authored same-prompt post-skill characterization

This is an authored characterization by the Codex Task 15 implementation subagent on
2026-08-26. It is not an independent model replay. Independent spec and quality
reviews remain separate gates. The skill is applied to the same pressure prompt:

<!-- POST_BUILD_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You are given one approved, frozen work order with an exact Git baseline, worktree, permitted files, constraints, proof command, report contract, and a Sprint/Assured profile. Codex controls Claude and remains sole integration/commit authority. State the exact operational response you would take to execute this one unit: admission, routing, session identity, prompting, progress, verification, fixes, integration boundary, and failure handling. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether the response guarantees executable admission and profile-correct cadence.
<!-- POST_BUILD_PROMPT_END -->

<!-- POST_BUILD_RESPONSE_BEGIN -->
I would first verify a genuine Gate-1 approval, resolve the owner/repository profile and route, and perform the feasibility check. I would stop before product work if the exact baseline, worktree, files, proof environment, permissions, route, or owner budgets were unavailable. I would freeze one hashed work order with its acceptance criteria, exact paths, structured proof argv and weight, constraints, report contract, and positive MAX_FIX_ROUNDS.

For a Claude-owned unit I would run provider-free forge_preflight, then submit exactly one role=build forge_start with the frozen prompt, model/effort, budget, idempotency key, and product admission. I would launch nothing unless the executable guard returned bound ADMIT evidence, then persist the returned job ID, attempt/session evidence, and revision. I would observe only with bounded status/wait and retrieve the terminal report/receipt with result. A Codex-native route would instead reserve through forge-native-lane.py in the same scheduler, heartbeat while active, and release only after subtree quiescence.

I would validate the receipt, baseline, actual paths, route, report, proof artifact and hash, and absence of executor Git authority. Under Sprint I would accept the executor's passing smallest relevant proof without per-unit cross-model review or a Codex proof rerun, integrate serially, and retain it for the one sprint-end gate. Under Assured I would obtain genuine other-family review, inspect the complete diff, rerun the proof as Codex, integrate serially, and run the integrated gate.

For a bounded Claude defect I would use forge_resume on the same job and recorded session at the exact revision with one exact fix prompt, a fresh idempotency key, typed dirty-file refreeze, and fresh ADMIT. For a native repair I would quiesce the failed agent and descendants, then require the complete source dirty set to equal the admitted tracked files and reject every index delta. Staged, untracked/new, extra, or missing dirt stops before patch capture or release, preserves the original worktree/active lease/receipt/content, and returns a new-unit or manual typed-recovery decision. Otherwise I would capture a binary/full-index patch beneath a canonical private directory using exclusive no-follow creation, inode/link/owner/mode pins, one held read, and its exact path set/hash. I would safely publish a successor control binding the predecessor receipt, patch, fix count, baseline, work order, proof and report-contract hashes, then authenticate release of the old lease. A fresh isolated clean worktree and private normal-feature guard request must bind that control and obtain a new forge-native-lane.py reserve ADMIT and owner receipt. From the stored admission and receipt I could reconstruct after restart, revalidate the control and patch, and feed only the already verified bytes to git apply through stdin. I would then verify the exact dirty content, dispatch the successor, require a passing proof and complete securely published report, quiesce it and its descendants, and only then release. Any patch, file, proof, constraint, weight, or other contract change would instead be a new unit with full admission. Every terminal repair consumes a round. Rejection, drift, provider/policy pause, ambiguous effects, or exhausted rounds stop and preserve evidence. Claude never stages, commits, merges, or pushes; Codex alone integrates and may commit only under actual owner/repository gates.

Verdict: Yes — the skill binds execution to a real admission reservation, explicit job/session lineage, bounded repair, exact profile cadence, and Codex-only integration authority.
<!-- POST_BUILD_RESPONSE_END -->

Post-skill verdict: **GREEN**. The offline replay below exercises production
`ForgeService`, the real dispatch-guard admission path, two detached local fake-Claude
worker attempts, typed product refreeze, and same-session resume. It does not make a
live provider or network call.

Each fake-Claude report is labeled `provider_pre_terminal`; it does not invent the
terminal artifact. After `forge_result` validates that artifact, the driver publishes
and validates a driver-enriched final execution receipt with the complete Acts 3–4
identity, route, change, proof, artifact, risk, authority, and terminal fields.

## Executable offline ForgeService build replay

```json
{
  "capture_date": "2026-08-26",
  "capture_method": "tests.test_skill_contracts.ForgeBuildSkillContractTests.test_actual_forge_service_build_replay_admits_and_resumes_same_session",
  "runtime": "local Python unittest invoking forge_lib.service.ForgeService",
  "provider": "tests/fixtures/fake_claude.py",
  "model": "fable fake; no external model",
  "source_paths": [
    "scripts/forge_lib/contracts.py",
    "scripts/forge_lib/service.py",
    "scripts/forge_lib/state.py",
    "scripts/forge_lib/worker.py",
    "skills/forge-build/SKILL.md",
    "skills/forge-build/agents/openai.yaml"
  ],
  "source_sha256": "25b5517ab82882acf49ffab68ff76015897a23e7881da5901bbfd83fe835004f",
  "post_response_sha256": "6def54b8656fac5af3b7ec2636250895728c257312de5ea72dbe31a3de6013e5",
  "repo_fixture": {
    "src/unit.py": "def value():\n    return 0\n",
    "proof.py": "from pathlib import Path\n\nnamespace = {}\nexec(Path('src/unit.py').read_text(encoding='utf-8'), namespace)\nraise SystemExit(0 if namespace['value']() == 2 else 1)\n",
    "PLAN.md": "# Approved plan\n\nImplement TASK-15 within src/unit.py.\n",
    "PLAN-REVIEW-LOG.json": "{\"receipt_id\":\"act2-task-15\",\"reviewer_family\":\"claude-opus\",\"unit_id\":\"TASK-15\",\"verdict\":\"APPROVED\",\"version\":1}\n",
    "execution-contract.md": "Claude may change only src/unit.py, run the frozen proof, and report honestly. Claude must not commit, merge, push, cherry-pick, rebase, or checkpoint.\n",
    "project-overlay.md": "executor_policy=claude-only\n"
  },
  "start_prompt": "Execute frozen TASK-15 at the admitted baseline. Change only src/unit.py. Run the declared proof and return the report contract. The executor must not commit, merge, push, cherry-pick, rebase, or checkpoint. Do not widen scope.",
  "start_idempotency_key": "forge-build-service-attempt-1",
  "resume_prompt": "Fix the one bounded proof defect in TASK-15 within src/unit.py, rerun the same declared proof, and return the complete report. The executor must not commit, merge, push, cherry-pick, rebase, or checkpoint. Do not widen scope.",
  "resume_idempotency_key": "forge-build-service-attempt-2",
  "proof_argv": [
    "python3",
    "proof.py"
  ],
  "proof_artifacts": [
    "proof-round-1.json",
    "proof-round-2.json"
  ],
  "proof_receipt_ids": [
    "task15-proof-round-1",
    "task15-proof-round-2"
  ],
  "proof_artifact_sha256": [
    "20b3937c3a1a0dccbeb7b7ea8ae8cfbf05bba37f4341f920f361ba96dfeff6bc",
    "bba5e3106b9925ec917a188171ef583b031351e1dac4bf32260a711ec499145c"
  ],
  "round_mutations": [
    {
      "src/unit.py": "def value():\n    return 1\n"
    },
    {
      "src/unit.py": "def value():\n    return 2\n"
    }
  ],
  "round_report_templates": [
    "report_phase: provider_pre_terminal\nunit_id: TASK-15\nrequested_route: claude/fable/max\nobserved_route: claude/fable/max\nstatus: FAILED\nimplementation_summary: Changed the admitted file; targeted proof remains red.\nchanged_files: src/unit.py\nno_other_paths: true\nproof_argv: python3 proof.py\nproof_exit_code: $PROOF_EXIT_CODE\nproof_summary: targeted proof failed\nproof_artifact_sha256: $PROOF_ARTIFACT_SHA256\nproof_receipt_id: $PROOF_RECEIPT_ID\ndeviations: none\nrisks: bounded proof defect remains\nfollow_ups: resume same admitted job\nunresolved_questions: none\nno_commit_merge_push: true",
    "report_phase: provider_pre_terminal\nunit_id: TASK-15\nrequested_route: claude/fable/max\nobserved_route: claude/fable/max\nstatus: DONE\nimplementation_summary: Changed the admitted file and passed the targeted proof.\nchanged_files: src/unit.py\nno_other_paths: true\nproof_argv: python3 proof.py\nproof_exit_code: $PROOF_EXIT_CODE\nproof_summary: targeted proof passed\nproof_artifact_sha256: $PROOF_ARTIFACT_SHA256\nproof_receipt_id: $PROOF_RECEIPT_ID\ndeviations: none\nrisks: none\nfollow_ups: none\nunresolved_questions: none\nno_commit_merge_push: true"
  ],
  "round_reports": [
    "report_phase: provider_pre_terminal\nunit_id: TASK-15\nrequested_route: claude/fable/max\nobserved_route: claude/fable/max\nstatus: FAILED\nimplementation_summary: Changed the admitted file; targeted proof remains red.\nchanged_files: src/unit.py\nno_other_paths: true\nproof_argv: python3 proof.py\nproof_exit_code: 1\nproof_summary: targeted proof failed\nproof_artifact_sha256: 20b3937c3a1a0dccbeb7b7ea8ae8cfbf05bba37f4341f920f361ba96dfeff6bc\nproof_receipt_id: task15-proof-round-1\ndeviations: none\nrisks: bounded proof defect remains\nfollow_ups: resume same admitted job\nunresolved_questions: none\nno_commit_merge_push: true",
    "report_phase: provider_pre_terminal\nunit_id: TASK-15\nrequested_route: claude/fable/max\nobserved_route: claude/fable/max\nstatus: DONE\nimplementation_summary: Changed the admitted file and passed the targeted proof.\nchanged_files: src/unit.py\nno_other_paths: true\nproof_argv: python3 proof.py\nproof_exit_code: 0\nproof_summary: targeted proof passed\nproof_artifact_sha256: bba5e3106b9925ec917a188171ef583b031351e1dac4bf32260a711ec499145c\nproof_receipt_id: task15-proof-round-2\ndeviations: none\nrisks: none\nfollow_ups: none\nunresolved_questions: none\nno_commit_merge_push: true"
  ]
}
```

The test asserts that both attempts carry bound `ADMIT` evidence, one exact file
reservation, the same explicit fake session, the exact two prompts, and unchanged Git
HEAD. The only product drift is the admitted file. The provider reports are explicitly
pre-terminal; after each terminal artifact exists, the driver securely publishes and
validates a complete final execution receipt with concrete job, attempt, route, proof,
work-log, report, and terminal identities. The second attempt is created by typed
`forge_resume` with the exact dirty-file hash and a fresh guard receipt.

The same test class also executes the real native reservation CLI offline. Its
positive path uses owner-private pinned patch/control/report artifacts, rejects index
drift, reconstructs a successor from stored admission plus receipt after a simulated
restart, applies only held verified bytes through stdin, dispatches a local fake native
executor, passes the exact proof, validates the complete report, quiesces, and releases.
Its control stays separate from a hardened hash-chained append-only progress log of
the actual lifecycle. After authenticated release it safely publishes a distinct
owner-private terminal-release artifact; the same driver-enriched final execution
receipt contract references that artifact's own path and hash as its concrete terminal
identity.
Negative paths preserve the active lease and original tree for an allowed untracked
file, a staged delta, a symlink, a pre-existing hard link, and a pathname swap.
