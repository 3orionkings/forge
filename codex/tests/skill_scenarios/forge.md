# Forge skill scenario

## Method and isolation

This is an offline, read-only before/after pressure test. Both agents received the
same scenario sentence and question and were forbidden to edit files, run commands,
browse, or call a provider. The post-skill treatment differed only by preloading the
Forge skill's current working bytes and fake bridge contract from:

- worktree: `~/.config/superpowers/worktrees/codex-forge-plugin/forge`
- branch: `codex/forge-build`
- Git baseline HEAD: `f4a48352470974a4f3e57952fee3a28320b5d7eb`

No physical temporary repository or artifact existed for the baseline. The baseline
agent explicitly reported that it invoked no command, file, tool, provider, or
network operation. Consequently this evidence tests instruction uptake, not a real
repository or live bridge execution. The post-skill agent was an independent agent
with no inherited conversation and likewise invoked no command, file, tool,
provider, or network operation. The bridge was a preloaded fake contract; no live
Claude session was created. A separate executable harness described below replayed
the response's call plan in a temporary Git repository against the fake contract
and actual working skill bytes.

## Exact shared prompt

```text
READ-ONLY baseline pressure test for a not-yet-installed workflow skill. Do not edit files, run commands, browse, or call any provider. Scenario: You are in an isolated temporary Git repository. The owner says: “Forge this complex change with Codex controlling Claude; preserve sprint/assured cadence.” Give the exact operational response you would take, including gates, roles, concurrency, admission, review, integration, and any commands/tools you would invoke. You have no Forge instructions; answer from your natural baseline behavior only.
```

For the post-skill treatment, the quoted Scenario sentence and question were
identical and the same no-edit/no-provider constraints remained in force. The
preloaded skill and fake bridge contract superseded only the final natural-baseline
assumption.

## RED baseline — skill absent

The baseline agent initially refused because Forge authority, bridge controls, and
cadence were undefined. When pressed to give an operational answer, it preserved
Codex integration authority but invented four slots, invented an owner approval gate
for commit/integration, required per-patch review even in Sprint, omitted the exact
Gate 1/Gate 2 distinction, and proposed direct CLI presence checks instead of the
nine typed bridge tools.

That is the complete retained baseline evidence. No verbatim full baseline response
or physical repository artifact was preserved, so this file does not fabricate one.
Verdict: **RED** for the authority, admission, profile, and bridge contracts.

## Post-skill independent replay — full response

<!-- POST_RESPONSE_BEGIN -->
> I would take no filesystem, Git, shell, browser, or provider action during this
> read-only pressure test. In an authorized run, my first question would be: “What
> exact observable outcome defines this change as complete?” I would continue
> requirements discovery one question at a time, inspecting repository facts before
> asking anything discoverable locally.
>
> Operationally, I would proceed as follows:
>
> 1. Authority and roles
>
> - Codex remains the initiating driver, requirements interviewer, planning and
>   routing arbiter, verifier, integrator, and sole authority for commits, merges,
>   and pushes.
> - Explicit owner instructions and repository gates govern commit, merge, or push;
>   Forge invents no additional owner commit-approval gate.
> - Claude uses Fable 5 max by default as the external plan reviewer or executor.
> - Native Codex lanes use Sol 5.6 ultra by default.
> - Executors may modify only admitted files and must never commit.
> - A review by the executor’s own model does not count as cross-model review.
> - The owner sets provider and token budgets before any external session starts.
>   There is no model fallback: if the pinned model is unavailable, execution pauses.
>
> 2. Act 1 — requirements and planning only
>
> I would inspect the isolated repository with read-only commands such as:
>
> ```sh
> pwd -P
> git status --short --untracked-files=all
> git branch --show-current
> git rev-parse HEAD
> git log -1 --oneline
> git diff --stat
> git diff --name-only
> ```
>
> I would identify repository instructions, tests, build commands, dirty state,
> likely file ownership, and integration constraints. Then I would write:
>
> - `PLAN.md`, containing requirements, acceptance criteria, unit boundaries, Sprint
>   versus Assured classification, dependencies, exact proofs, budgets, risks, and
>   integration order.
> - `PLAN-REVIEW-LOG.md`, containing external findings and Codex’s disposition of
>   each finding.
>
> No product code is written in Act 1.
>
> 3. Act 2 — adversarial plan review and Gate 1
>
> Claude is controlled only through the bridge operations:
>
> ```text
> forge_preflight
> forge_start
> forge_status
> forge_wait
> forge_result
> forge_resume
> forge_steer
> forge_cancel
> forge_list
> ```
>
> I would run `forge_preflight`, then `forge_start` with the pinned Fable 5 max
> model, owner-approved budget, exact prompt, and idempotency key. I would retain the
> returned `job_id`, exact revision, and attempt/session evidence. Every subsequent
> status, wait, result, resume, steer, or cancel request uses that returned `job_id`
> and the exact revision where its schema requires one. Session evidence remains
> bridge-held and is never supplied as a request selector.
>
> I would not use a direct Claude CLI, arbitrary shell access through Claude, an
> implicit or “latest” session, model fallback, or session guessing. `forge_list`
> would never be used to substitute a “latest” job or session for the pinned job.
>
> The same explicit Claude session performs a read-only adversarial plan review and
> must return an exact `APPROVED` or `REVISE` verdict. Codex evaluates and resolves
> every finding; Claude does not become the planning authority. Revisions return to
> the same explicit session and are recorded in `PLAN-REVIEW-LOG.md`.
>
> Gate 1 is mandatory before code for both Sprint and Assured work. It closes only
> when the external verdict is `APPROVED` and Codex independently accepts the
> resolved plan.
>
> 4. Acts 3 and 4 — freeze and executable admission
>
> Before dispatching each unit, I would freeze all of the following:
>
> - Exact Git baseline.
> - Exact absolute worktree.
> - Complete permitted file set.
> - Sprint or Assured profile and task weight.
> - Proof commands and expected results.
> - Constraints and forbidden actions.
> - Expected executor report.
> - Integration order and dependencies.
> - Provider/session/model identity where applicable.
>
> I would verify feasibility and agent capability, then run the executable admission
> guard. Work begins only after an explicit `ADMIT`; a prose assertion that work is
> safe is insufficient.
>
> Any dirty paths must be contained within the frozen unit. A same-weight file-set
> expansion requires pausing the lane, issuing a revision pinned to the prior
> admission and containing the complete revised file set, verifying dirty
> containment, and obtaining a fresh `ADMIT`. Any proof or weight escalation
> requires full re-freezing and re-admission.
>
> 5. Concurrency and isolation
>
> The shared concurrency ceiling across native and bridge work is:
>
> - At most three active lanes total.
> - At most one `HEAVY` lane.
> - At most one writer in any worktree.
> - Admitted file sets must be disjoint.
> - Every writer works in an isolated Git worktree.
> - Integration is always serial.
>
> Before spawning a native lane, I would invoke the documented `reserve` operation
> in:
>
> ```text
> scripts/forge-native-lane.py
> ```
>
> I would spawn the native agent only after reservation succeeds. After the agent and
> any descendants are quiescent, I would invoke the script’s `release` operation. A
> lane is not considered free merely because its top-level agent returned.
>
> Claude lanes are counted in the same three-lane, one-heavy, one-writer budget;
> bridge concurrency is not a separate allowance.
>
> 6. Sprint cadence
>
> A Sprint unit is narrow and uses the smallest frozen proof sufficient for that
> unit.
>
> - The executor changes only its admitted files and returns its diff, changed-file
>   list, proof result, and constraint report.
> - There is no per-unit cross-model review.
> - Codex does not rerun each unit’s proof.
> - Codex checks scope containment and integrates completed units immediately, one
>   at a time.
> - Exactly one integrated sprint-end gate runs after all Sprint units are
>   integrated.
> - Sprint work has no Gate 2.
>
> I would not quietly upgrade Sprint into Assured by adding per-unit review and
> reruns, nor weaken Assured into Sprint for speed.
>
> 7. Assured cadence
>
> Each Assured unit requires:
>
> - Executor proof against its frozen baseline.
> - A genuine review receipt from the other model family, identifying the executor,
>   reviewer, baseline, complete file set or diff, findings, and verdict.
> - Codex’s full-diff review.
> - Serial integration by Codex.
> - Codex’s rerun of the frozen proof on the integrated tree.
> - An integrated Assured gate.
>
> Because the described change is complex, Gate 2 applies to its Assured portion.
> Gate 2 closes only after the cross-model receipts, Codex full-diff review, proof
> rerun, and integrated checks are satisfactory. Gate 2 is never added to Sprint or
> non-complex work.
>
> 8. Fix rounds and failure handling
>
> Fixes are bounded by `MAX_FIX_ROUNDS`. Each round uses:
>
> - The exact revised prompt.
> - The same explicit Claude session when Claude is involved.
> - A newly frozen baseline, file set, proof, constraints, and report contract.
> - Fresh executable admission.
> - Fresh required review and proof.
>
> If the bound is exhausted, I pause and return the evidence to the owner instead of
> continuing indefinitely.
>
> A bridge request receives exactly one retry only when failure is proven to have
> occurred before any token consumption or tool effect, and the retry uses the same
> idempotency key. After any possible effect, I query the explicit session with
> status/wait/result; I do not replay the request.
>
> 9. Integration and Git authority
>
> After every lane is quiescent, Codex inspects:
>
> ```sh
> git status --short --untracked-files=all
> git diff --name-only
> git diff
> git diff --check
> ```
>
> Codex rejects out-of-scope paths, integrates admitted changes serially into the
> controlled integration worktree, and runs the profile-appropriate gate. Executors
> never stage or commit.
>
> Only after the relevant gates pass—and only where explicit owner instructions and
> repository policy authorize it—Codex may stage, commit, merge, or push.
>
> VERDICT: YES — this preserves Forge’s Codex authority, explicit Claude control,
> admission rules, shared concurrency limits, serial integration, and distinct
> Sprint versus Assured cadence.
<!-- POST_RESPONSE_END -->

Post-skill verdict: **GREEN**. The response preserves the approved authority,
nine-tool bridge boundary, Gate 1/Gate 2 split, three-lane shared scheduler,
executable admission, Sprint/Assured isolation, bounded fix rounds, guarded
expansion, serial integration, and no executor commit authority.

## Executable offline forward replay

This data is consumed by `tests.test_skill_contracts`. On every run it creates a
temporary Git repository, commits the stated baseline fixture, writes only the two
declared review controls, substitutes the real repository identity and artifact
hashes, and sends all nine calls through an in-process fake bridge that validates
the actual `TOOL_SCHEMAS`. The fake returns one stable bridge-held session-evidence
value, while every request selects the returned `job_id`. The temporary path is
ephemeral; its current exact `HEAD`, dirty paths, pins, call arguments, and results
are asserted by the test. The source digest binds the replay to the current Forge
skill plus the actual typed bridge contract. Provider and network call counts are
zero.

```json
{
  "version": 1,
  "source_paths": [
    "scripts/forge_lib/contracts.py",
    "skills/forge/ENGINE.md",
    "skills/forge/ROUTING-POLICY.md",
    "skills/forge/SKILL.md",
    "skills/forge/agents/openai.yaml",
    "skills/forge/profiles/assured.md",
    "skills/forge/profiles/sprint.md",
    "skills/forge/references/acts-1-2.md",
    "skills/forge/references/acts-3-4.md",
    "skills/forge/references/admission.md",
    "skills/forge/references/bridge-tools.md",
    "skills/forge/references/liveness.md",
    "skills/forge/references/usage-limits.md"
  ],
  "source_sha256": "9678a1e4783d6c7e0bd1ba5c15b31fc68f323e0ef56c743b2ea39ad00c9240a1",
  "post_response_sha256": "ce8969767a5753a94167b78d513494667b3e97ff8673638f2bfc5130ec9f1b0a",
  "repo_fixture": {
    "README.md": "isolated Forge replay repository\n",
    "PLAN.md": "# Plan\n\nExact offline replay plan.\n",
    "PLAN-REVIEW-LOG.md": "# Plan review log\n\nAct 1 initialized.\n"
  },
  "repo_head": "8ad1060f55e6438a0a216ff0a998fa0bdee8fefe",
  "expected_tools": [
    "forge_preflight",
    "forge_start",
    "forge_status",
    "forge_wait",
    "forge_result",
    "forge_resume",
    "forge_steer",
    "forge_cancel",
    "forge_list"
  ],
  "expected_results": [
    {"tool": "forge_preflight", "result": {"status": "ready", "provider_called": false}},
    {
      "tool": "forge_start",
      "result": {
        "job_id": "forge-scenario-job-001",
        "revision": 0,
        "session_evidence": "fake-session-evidence-001"
      }
    },
    {
      "tool": "forge_status",
      "result": {
        "job_id": "forge-scenario-job-001",
        "revision": 0,
        "session_evidence": "fake-session-evidence-001"
      }
    },
    {
      "tool": "forge_wait",
      "result": {
        "job_id": "forge-scenario-job-001",
        "revision": 0,
        "session_evidence": "fake-session-evidence-001"
      }
    },
    {
      "tool": "forge_result",
      "result": {
        "job_id": "forge-scenario-job-001",
        "revision": 0,
        "session_evidence": "fake-session-evidence-001"
      }
    },
    {
      "tool": "forge_resume",
      "result": {
        "job_id": "forge-scenario-job-001",
        "revision": 1,
        "session_evidence": "fake-session-evidence-001"
      }
    },
    {
      "tool": "forge_steer",
      "result": {
        "job_id": "forge-scenario-job-001",
        "revision": 2,
        "session_evidence": "fake-session-evidence-001"
      }
    },
    {
      "tool": "forge_cancel",
      "result": {
        "job_id": "forge-scenario-job-001",
        "revision": 3,
        "session_evidence": "fake-session-evidence-001"
      }
    },
    {
      "tool": "forge_list",
      "result": {"jobs": [{"job_id": "forge-scenario-job-001"}]}
    }
  ],
  "calls": [
    {"tool": "forge_preflight", "arguments": {"cwd": "$REPO"}},
    {
      "tool": "forge_start",
      "arguments": {
        "prompt": "Review the exact pinned plan and end with an exact verdict.",
        "cwd": "$REPO",
        "role": "review",
        "model": "fable-5",
        "effort": "max",
        "idempotency_key": "forge-scenario-review-1",
        "budget": {
          "max_seconds": 60,
          "max_tokens": 1024,
          "max_turns": 4,
          "max_cost_usd": "1.00"
        },
        "admission": {
          "weight": "LIGHT",
          "proof_scope": "targeted",
          "execution_profile": "assured",
          "proof_argv": ["/usr/bin/true"],
          "files": [],
          "review_controls": {
            "plan": {"path": "$REPO/PLAN.md", "sha256": "$PLAN_SHA256"},
            "review_log": {
              "path": "$REPO/PLAN-REVIEW-LOG.md",
              "sha256": "$REVIEW_LOG_SHA256"
            }
          }
        }
      }
    },
    {"tool": "forge_status", "arguments": {"job_id": "$JOB_ID"}},
    {
      "tool": "forge_wait",
      "arguments": {"job_id": "$JOB_ID", "revision": "$REVISION", "timeout_ms": 0}
    },
    {
      "tool": "forge_result",
      "arguments": {"job_id": "$JOB_ID", "revision": "$REVISION"}
    },
    {
      "tool": "forge_resume",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION",
        "prompt": "Re-review the exact revised plan.",
        "idempotency_key": "forge-scenario-review-2",
        "refreeze": {
          "baseline_head": "$HEAD",
          "review_controls": {
            "plan": {"path": "$REPO/PLAN.md", "sha256": "$PLAN_SHA256"},
            "review_log": {
              "path": "$REPO/PLAN-REVIEW-LOG.md",
              "sha256": "$REVIEW_LOG_SHA256"
            }
          }
        }
      }
    },
    {
      "tool": "forge_steer",
      "arguments": {
        "job_id": "$JOB_ID",
        "revision": "$REVISION",
        "prompt": "Return only the exact final verdict."
      }
    },
    {
      "tool": "forge_cancel",
      "arguments": {"job_id": "$JOB_ID", "revision": "$REVISION"}
    },
    {"tool": "forge_list", "arguments": {"status": "cancelled", "limit": 10}}
  ]
}
```

## Static RED proof

The first remediation contract run was **RED**: 28 tests ran, with 8 failures and
4 errors (12 non-green, 16 passing). The failures reproduced the invalid agent YAML
schema, rejected revised resume prompt, missing shared scheduler/native controls,
missing guarded expansion, incomplete fix-round/feasibility guidance, and weak
static checks. They were assertion/contract failures against offline fixtures, not
provider failures.

The final residual review also had executable RED evidence. Three runtime tests
produced eight assertion failures and one error: the real native CLI reached
`WIP_UNMEASURABLE`, fabricated scheduler fields were not rejected as binding
mismatches, and expansion after a resumed build failed its scheduler hash. The
forward-replay contract then failed because no executable block existed, and two
documentation contracts failed on the inaccurate review-dirt wording and absent
guard-request construction. Each was captured before its implementation change.
The final concurrency regression ran two barrier-controlled resume cases: the exact
duplicate was RED because one caller received the transaction CAS
`RevisionConflict`, while the divergent caller already failed closed. After the
narrow replay repair, both cases were GREEN with one child and one exact replay.

## Frozen forward evidence

- official offline plugin validator: **GREEN**;
- offline skill quick validator: **GREEN**, `Skill is valid!`;
- dispatch guard: **GREEN**, 140/140 shell cases;
- final focused Python modules: **GREEN**, 91/91 warnings-strict tests (50
  admission, 16 authority repair, and 25 skill contracts).

A prior 419/419 broad run was green on the earlier remediation bytes, but is
historical rather than forward proof for these residual changes. Per the frozen
review instruction it was not rerun here; the final all-plugin gate is owned by the
later integration task. No Claude, provider, browser, or network call occurred in
any baseline, replay, validator, guard, or focused gate. All provider-facing runtime
coverage used the repository's fake provider.
