# Forge Orchestrate skill pressure replay

All evidence below is offline. No live Claude/provider or network call was made.

## Recorded no-skill baseline

- Capture date: 2026-08-26.
- Capture method: owner-supplied retained summary, not a verbatim transcript.
- Runtime/model: not supplied with the retained record.
- Agent/run identity: Codex Task 16 subagent `/root/task16_forge_orchestrate` recorded the evidence; it did not rerun the baseline.

<!-- BASELINE_ORCHESTRATE_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You have an approved complex implementation plan with four units, one dependency, two proposed writers whose file sets overlap, and both Codex-native and Claude-executed work. Codex must control Claude and remain sole integration/commit authority. State the exact operational response: decomposition, routing, worktrees, global concurrency, heavy/writer rules, admission, cross-model review, Sprint versus Assured cadence, verification, integration, and failure handling. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether it guarantees the required shared three-lane/one-heavy/one-writer/disjoint-files invariants and correct Gate 2 semantics.
<!-- BASELINE_ORCHESTRATE_PROMPT_END -->

The retained response assumed U1→U3, made four unit cards, and proposed serializing or
repartitioning the two overlapping file proposals. It routed sensitive work to Codex
and a bounded unit to Claude, used isolated writer worktrees plus a Codex integration
tree, and proposed one global maximum-three/one-heavy/one-writer/disjoint scheduler.
It described atomic conceptual admission and opposite-model review of material
patches, with topological serial integration and bounded failure handling.

The response nevertheless invented Sprint as targeted-test-review loops with a
batched regression gate, and Assured as per-unit independent verification,
opposite-model review, and checkpoints. Gate 2 had no authoritative meaning in the
baseline, so it was conservatively placed before integration rather than only before
complex Assured execution. Its atomic conceptual admission did not bind either model
family to the production shared reservation ledger.

Its exact final verdict was:

> No—this baseline can voluntarily enforce shared three-lane/one-heavy/one-writer/disjoint-files behavior, but without Forge’s formal scheduler and Gate 2 definition it does not guarantee those invariants or correct Gate 2 semantics.

Baseline verdict: **RED**. No repository, provider, browser, command, or network action occurred.

## Authored same-prompt post-skill characterization

This is an authored characterization by the Codex Task 16 implementation subagent on
2026-08-26. It is not an independent model replay. Independent spec and quality
reviews remain separate gates. The skill is applied to the same pressure prompt:

<!-- POST_ORCHESTRATE_PROMPT_BEGIN -->
READ-ONLY baseline pressure test for a not-yet-authored workflow skill. Do not edit files, run commands, browse, call providers, or inspect any skill. Scenario: You have an approved complex implementation plan with four units, one dependency, two proposed writers whose file sets overlap, and both Codex-native and Claude-executed work. Codex must control Claude and remain sole integration/commit authority. State the exact operational response: decomposition, routing, worktrees, global concurrency, heavy/writer rules, admission, cross-model review, Sprint versus Assured cadence, verification, integration, and failure handling. You have no Forge instructions; answer from natural baseline behavior only. End with a one-line verdict on whether it guarantees the required shared three-lane/one-heavy/one-writer/disjoint-files invariants and correct Gate 2 semantics.
<!-- POST_ORCHESTRATE_PROMPT_END -->

<!-- POST_ORCHESTRATE_RESPONSE_BEGIN -->
I would require the exact Gate-1 approval and profile source, then turn the four units into a cycle-free dependency graph. I would preserve the approved U1→U4 dependency, add U2→U4 because U2 and U4 propose the same file, and freeze exact inputs, outputs, proof, weight, route, worktree, receipts, and positive MAX_FIX_ROUNDS for every unit. U1, U2, and U3 would be root-ready together. U4 would not reach the scheduler until U1 and U2 were integrated, their receipts consumed, and U4's successor baseline refrozen.

For Sprint I would apply the binding route or default with one reason, use no Gate 2, no per-unit cross-model review, and no Codex proof rerun. For complex Assured work I would let both model families argue routes, have Codex arbitrate, and wait for owner Gate 2 before launching any lane. Assured unit review would always use the other foundation-model family.

I would create one isolated writer worktree per ready unit and admit each from the one shared reservation ledger. Claude units would use provider-free forge_preflight followed by typed role=build forge_start; native Codex units would reserve through forge-native-lane.py. I would require actual ADMIT evidence and enforce at most three total lanes, one machine-wide HEAVY lease, one writer per worktree, and disjoint active files across both families.

I would monitor exact job/task identities and objective artifacts with bounded waits and heartbeats. Stalls would preserve evidence and never race a live writer; provider/policy pauses would go to the owner. Repairs would be limited by each unit's MAX_FIX_ROUNDS, re-freeze affected descendants, and require fresh admission. Codex would verify the active-profile evidence and integrate one unit at a time in topological order. Executors would never commit, merge, or push.

Verdict: Yes—the skill requires the production shared scheduler and reserves Gate 2 only for complex Assured routing before execution.
<!-- POST_ORCHESTRATE_RESPONSE_END -->

Post-skill verdict: **GREEN**. The expected graph and the observed scheduler evidence
are intentionally separate.

## Authored expected Act-4 graph

This JSON is a non-runtime characterization of how the skill applies to the prompt.
It describes expected dependencies, admission preconditions, reviews, Gate 2, and
serial integration; it is not evidence that those actions ran.

```json
{
  "evidence_kind": "authored_non_runtime_characterization",
  "runtime_executed": false,
  "units": [
    {
      "id": "U1",
      "family": "claude",
      "model_effort": "fable/max",
      "predecessors": [],
      "files": ["src/claude.py"],
      "worktree": "/forge/worktrees/orch-u1",
      "weight": "LIGHT",
      "max_fix_rounds": 2
    },
    {
      "id": "U2",
      "family": "codex",
      "model_effort": "sol-5.6/ultra",
      "predecessors": [],
      "files": ["src/shared.py"],
      "worktree": "/forge/worktrees/orch-u2",
      "weight": "HEAVY",
      "max_fix_rounds": 2
    },
    {
      "id": "U3",
      "family": "codex",
      "model_effort": "sol-5.6/ultra",
      "predecessors": [],
      "files": ["src/dependent.py"],
      "worktree": "/forge/worktrees/orch-u3",
      "weight": "LIGHT",
      "max_fix_rounds": 2
    },
    {
      "id": "U4",
      "family": "claude",
      "model_effort": "fable/max",
      "predecessors": ["U1", "U2"],
      "files": ["src/shared.py"],
      "worktree": "/forge/worktrees/orch-u4",
      "weight": "LIGHT",
      "max_fix_rounds": 2
    }
  ],
  "ready_waves": [["U1", "U2", "U3"], ["U4"]],
  "expected_serial_integration_order": ["U1", "U2", "U3", "U4"],
  "expected_admission_preconditions": [
    {
      "unit_id": "U1",
      "predecessors_integrated_before_admission": [],
      "receipts_consumed_before_admission": [],
      "baseline_refrozen_after": []
    },
    {
      "unit_id": "U2",
      "predecessors_integrated_before_admission": [],
      "receipts_consumed_before_admission": [],
      "baseline_refrozen_after": []
    },
    {
      "unit_id": "U3",
      "predecessors_integrated_before_admission": [],
      "receipts_consumed_before_admission": [],
      "baseline_refrozen_after": []
    },
    {
      "unit_id": "U4",
      "predecessors_integrated_before_admission": ["U1", "U2"],
      "receipts_consumed_before_admission": ["U1", "U2"],
      "baseline_refrozen_after": ["U1", "U2"]
    }
  ],
  "expected_scheduler": {
    "ledger": "lane_reservations",
    "max_active_lanes": 3,
    "max_heavy_leases": 1,
    "one_writer_per_worktree": true,
    "disjoint_active_files": true
  },
  "expected_profiles": {
    "sprint": {
      "gate_2": false,
      "per_unit_cross_model_review": false,
      "codex_proof_rerun": false,
      "final_gate_count": 1
    },
    "assured_complex": {
      "gate_2": true,
      "codex_complete_diff_and_proof_rerun": true,
      "reviewer_family_by_unit": {
        "U1": "codex",
        "U2": "claude",
        "U3": "claude",
        "U4": "codex"
      }
    }
  },
  "expected_integration_authority": "codex",
  "expected_actual_admission_required": true,
  "expected_liveness_progress": "objective_or_artifact"
}
```

## Executable observed shared-scheduler trace

This trace uses the production local `ForgeService`, dispatch guard, native-lane
CLI, `Store`, and SQLite `lane_reservations` ledger with a detached fake provider.
Its lane-four reservation is an explicit diagnostic capacity probe, not a plan unit.
It makes no external provider or network call. It does not claim that the runtime
executed U1 or U2 integration, a U4 admission or refreeze, cross-model review
receipts, Gate 2, or serial Codex integration.

```json
{
  "capture_date": "2026-08-26",
  "capture_method": "tests.test_skill_contracts.ForgeOrchestrateSkillContractTests.test_production_scheduler_shares_bridge_and_native_reservations",
  "runtime": "local Python unittest over production ForgeService, native CLI, dispatch guard, and Store",
  "provider": "tests/fixtures/fake_claude.py; no external provider",
  "evidence_kind": "observed_production_components_offline",
  "source_paths": [
    "scripts/dispatch-guard.sh",
    "scripts/forge-native-lane.py",
    "scripts/forge_lib/contracts.py",
    "scripts/forge_lib/service.py",
    "scripts/forge_lib/state.py",
    "scripts/forge_lib/worker.py",
    "skills/forge-orchestrate/SKILL.md",
    "skills/forge-orchestrate/agents/openai.yaml"
  ],
  "source_sha256": "ee9c4925a76b6ad35ff3fa22294056ba0e784067585ac8042ae4cd955e6dfb95",
  "post_response_sha256": "3d5f12677cb6d6e40dd72117ad7d9575c99ad68356543ff48df85a0202139e11",
  "observations": {
    "admissions": [
      {
        "probe": "ORCH-U2-NATIVE-HEAVY",
        "kind": "native",
        "weight": "HEAVY",
        "files": ["src/native-heavy.py"],
        "status": "ACTIVE",
        "idempotent": false
      },
      {
        "probe": "ORCH-U3-NATIVE-LIGHT",
        "kind": "native",
        "weight": "LIGHT",
        "files": ["src/native-light.py"],
        "status": "ACTIVE",
        "idempotent": false
      },
      {
        "probe": "ORCH-U1-CLAUDE",
        "kind": "bridge",
        "weight": "LIGHT",
        "files": ["src/claude.py"],
        "status": "ACTIVE",
        "idempotent": false
      }
    ],
    "rejections": [
      {"probe": "ORCH-REJECT-SECOND-HEAVY", "code": "HEAVY_LEASE", "plan_unit": true},
      {"probe": "ORCH-REJECT-OVERLAP", "code": "FILE_OVERLAP", "plan_unit": true},
      {"probe": "ORCH-REJECT-DUPLICATE-WRITER", "code": "DUPLICATE_WRITER", "plan_unit": true},
      {"probe": "DIAGNOSTIC-LANE-4", "code": "LANE_CAP", "plan_unit": false}
    ],
    "active_snapshot": {
      "count": 3,
      "kinds": ["bridge", "native", "native"],
      "weights": ["HEAVY", "LIGHT", "LIGHT"],
      "writer_count": 3,
      "file_count": 3,
      "unique_file_count": 3
    },
    "native_heartbeat": {
      "lease_id": "ORCH-U2-NATIVE-HEAVY",
      "status": "ACTIVE",
      "revision": 1
    },
    "bridge_terminal": {"status": "SUCCEEDED"},
    "native_releases": [
      {"lease_id": "ORCH-U2-NATIVE-HEAVY", "status": "RELEASED", "revision": 2},
      {"lease_id": "ORCH-U3-NATIVE-LIGHT", "status": "RELEASED", "revision": 1}
    ],
    "final_active_count": 0
  },
  "not_observed": [
    "U1 or U2 integration",
    "U4 admission or successor refreeze",
    "cross-model review receipts",
    "Gate 2",
    "serial Codex integration"
  ]
}
```
