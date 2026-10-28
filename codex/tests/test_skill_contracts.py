import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "forge"
SCENARIO_PATH = PLUGIN_ROOT / "tests" / "skill_scenarios" / "forge.md"
REVIEW_SKILL_ROOT = PLUGIN_ROOT / "skills" / "forge-review"
REVIEW_SCENARIO_PATH = PLUGIN_ROOT / "tests" / "skill_scenarios" / "forge-review.md"
BUILD_SKILL_ROOT = PLUGIN_ROOT / "skills" / "forge-build"
BUILD_SCENARIO_PATH = PLUGIN_ROOT / "tests" / "skill_scenarios" / "forge-build.md"
ORCHESTRATE_SKILL_ROOT = PLUGIN_ROOT / "skills" / "forge-orchestrate"
ORCHESTRATE_SCENARIO_PATH = (
    PLUGIN_ROOT / "tests" / "skill_scenarios" / "forge-orchestrate.md"
)
WITH_DOCS_SKILL_ROOT = PLUGIN_ROOT / "skills" / "forge-with-docs"
WITH_DOCS_SCENARIO_PATH = (
    PLUGIN_ROOT / "tests" / "skill_scenarios" / "forge-with-docs.md"
)

REVIEW_BASELINE_PROMPT = (
    "READ-ONLY baseline pressure test for a not-yet-authored workflow skill. "
    "Do not edit files, run commands, browse, call providers, or inspect any skill. "
    "Scenario: You are given an already-written database migration plan that needs "
    "adversarial Claude critique before any implementation. Codex must remain final "
    "planning and integration authority. State the exact operational response you "
    "would take, including whether you would start building, how you would invoke/control "
    "Claude, how you would handle revisions/session identity/verdicts/deadlock, what you "
    "would write, and when the gate closes. You have no Forge instructions; answer from "
    "natural baseline behavior only. End with a one-line verdict on whether your response "
    "guarantees a read-only bounded same-session external review."
)

REVIEW_BASELINE_RESPONSE = """I would open a stop-the-line review gate and would not write or run migration code.

1. Freeze the current plan as P0 with a hash/version. Give Claude only the plan and necessary schema/contracts, with secrets and credentials removed.
2. Start one Claude conversation under an enforced read-only capability profile: no filesystem writes, shell, database, deployment, network, or implementation tools. Capture provider, model, conversation/session ID, timestamp, and P0 hash. If restriction or stable ID cannot be verified, gate stays blocked.
3. Prompt Claude to adversarially review data loss, locks/downtime, compatibility, transactions, backfill, rollback, failure, idempotency, observability, verification; findings have ID, cited section, failure scenario, severity, confidence, required change; end exactly PASS, REVISE, or BLOCK.
4. Codex independently assesses findings. Claude never edits plan. Write review ledger with plan hash/session/prompt/verdict/findings; Codex disposition accepted/rejected-with-evidence/needs-decision; Codex-authored versioned plans; changelog mapping changes to findings.
5. Each revision sends full revised plan+diff in same recorded Claude session; mark old findings closed/open/disputed, identify new findings, fresh verdict. Reset/different/unverifiable identity invalidates same-session claim.
6. Claude is advisory. Codex retains authority but does not silently override BLOCK. One evidence-backed challenge; if material blocker remains after two revision responses, deadlock, record both, stop implementation, ask human risk decision or separate review.
7. Gate closes only for exact final plan hash when same session reviewed it, all findings closed or human-resolved, Claude PASS, and Codex records APPROVED_FOR_IMPLEMENTATION.

Verdict: No—this baseline procedure requests and records bounded same-session read-only review, but cannot guarantee it without runtime-enforced tool restrictions and verifiable session pinning."""

BUILD_BASELINE_PROMPT = (
    "READ-ONLY baseline pressure test for a not-yet-authored workflow skill. "
    "Do not edit files, run commands, browse, call providers, or inspect any skill. "
    "Scenario: You are given one approved, frozen work order with an exact Git baseline, "
    "worktree, permitted files, constraints, proof command, report contract, and a "
    "Sprint/Assured profile. Codex controls Claude and remains sole integration/commit "
    "authority. State the exact operational response you would take to execute this one "
    "unit: admission, routing, session identity, prompting, progress, verification, "
    "fixes, integration boundary, and failure handling. You have no Forge instructions; "
    "answer from natural baseline behavior only. End with a one-line verdict on whether "
    "the response guarantees executable admission and profile-correct cadence."
)

ORCHESTRATE_BASELINE_PROMPT = (
    "READ-ONLY baseline pressure test for a not-yet-authored workflow skill. "
    "Do not edit files, run commands, browse, call providers, or inspect any skill. "
    "Scenario: You have an approved complex implementation plan with four units, one "
    "dependency, two proposed writers whose file sets overlap, and both Codex-native "
    "and Claude-executed work. Codex must control Claude and remain sole "
    "integration/commit authority. State the exact operational response: "
    "decomposition, routing, worktrees, global concurrency, heavy/writer rules, "
    "admission, cross-model review, Sprint versus Assured cadence, verification, "
    "integration, and failure handling. You have no Forge instructions; answer from "
    "natural baseline behavior only. End with a one-line verdict on whether it "
    "guarantees the required shared three-lane/one-heavy/one-writer/disjoint-files "
    "invariants and correct Gate 2 semantics."
)

WITH_DOCS_BASELINE_PROMPT = (
    "READ-ONLY baseline pressure test for a not-yet-authored workflow skill. "
    "Do not edit files, run commands, browse, call providers, or inspect any skill. "
    "Scenario: You must plan a complex legal-domain change that alters established "
    "terminology and architecture. The repository contains a glossary, domain/context "
    "documentation, and ADRs. Codex controls Claude and remains planning/integration "
    "authority. State the exact operational response: what you load first, how you "
    "question the owner, inspect code, resolve terminology, update living docs/ADRs, "
    "lock the plan, and hand it to an existing shared multi-model build workflow without "
    "adding extra gates. You have no Forge instructions; answer from natural baseline "
    "behavior only. End with a one-line verdict on whether it guarantees docs-first "
    "terminology control and no cadence drift."
)

REQUIRED_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "ENGINE.md",
    "ROUTING-POLICY.md",
    "profiles/sprint.md",
    "profiles/assured.md",
    "references/acts-1-2.md",
    "references/acts-3-4.md",
    "references/bridge-tools.md",
    "references/admission.md",
    "references/usage-limits.md",
    "references/liveness.md",
}

BRIDGE_TOOLS = {
    "forge_preflight",
    "forge_start",
    "forge_status",
    "forge_wait",
    "forge_result",
    "forge_resume",
    "forge_steer",
    "forge_cancel",
    "forge_list",
}

SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from forge_lib.contracts import (  # noqa: E402
    ContractError,
    TOOL_SCHEMAS,
    validate_tool_arguments,
)
from forge_lib.service import ForgeService  # noqa: E402
from forge_lib.state import StateError, Store, canonical_json  # noqa: E402
from tests.admission_fixture import (  # noqa: E402
    machine_headroom,
    review_admission,
    seed_wip_base,
)


FAKE_CLAUDE = PLUGIN_ROOT / "tests" / "fixtures" / "fake_claude.py"
ADMISSION_BRIDGE = PLUGIN_ROOT / "tests" / "fixtures" / "forge_bridge_admission.py"
FAKE_CONTROL_NAME = "forge-fake-claude-control.json"


def read(relative_path: str) -> str:
    return (SKILL_ROOT / relative_path).read_text(encoding="utf-8")


def frontmatter(document: str) -> dict[str, str]:
    match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", document, re.DOTALL)
    if not match:
        return {}
    parsed: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            parsed[key.strip()] = value.strip().strip("'\"")
    return parsed


def scalar(value: str):
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value


class ForgeSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = {
            path: read(path)
            for path in REQUIRED_FILES
            if path.endswith(".md") and (SKILL_ROOT / path).is_file()
        }
        cls.corpus = "\n".join(cls.documents.values())

    def test_required_forge_skill_files_exist(self):
        missing = sorted(path for path in REQUIRED_FILES if not (SKILL_ROOT / path).is_file())
        self.assertEqual(missing, [])

    def test_markdown_relative_links_resolve(self):
        missing: list[str] = []
        for relative_path, document in self.documents.items():
            source = SKILL_ROOT / relative_path
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document):
                target = target.split("#", 1)[0]
                if not target or re.match(r"^[a-z]+://", target):
                    continue
                resolved = (source.parent / target).resolve()
                if not resolved.exists():
                    missing.append(f"{relative_path} -> {target}")
        self.assertEqual(missing, [])

    def test_entrypoint_progressively_links_all_shared_authorities(self):
        entrypoint = self.documents.get("SKILL.md", "")
        expected_targets = {
            "ENGINE.md",
            "ROUTING-POLICY.md",
            "profiles/sprint.md",
            "profiles/assured.md",
            "references/acts-1-2.md",
            "references/acts-3-4.md",
            "references/bridge-tools.md",
            "references/admission.md",
            "references/usage-limits.md",
            "references/liveness.md",
        }
        linked_targets = {
            target.split("#", 1)[0]
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", entrypoint)
        }
        self.assertTrue(expected_targets.issubset(linked_targets))

    def test_frontmatter_is_lowercase_and_trigger_only(self):
        metadata = frontmatter(self.documents.get("SKILL.md", ""))
        self.assertEqual(metadata.get("name"), "forge")
        description = metadata.get("description", "")
        self.assertTrue(description.startswith("Use when "), description)
        self.assertLess(len(description), 500)
        self.assertNotRegex(
            description.lower(),
            r"\b(first|then|finally|interview|write a plan|review the plan|route|commit|merge|push)\b",
        )

    def test_openai_agent_interface_is_exact(self):
        path = SKILL_ROOT / "agents" / "openai.yaml"
        self.assertTrue(path.is_file(), path)
        document = path.read_text(encoding="utf-8")
        self.assertEqual(
            document,
            'interface:\n'
            '  display_name: "Forge"\n'
            '  short_description: "Coordinate reviewed Codex and Claude builds"\n'
            '  default_prompt: "Use $forge to review and execute an implementation plan with Codex as the integration authority."\n'
            'policy:\n'
            '  allow_implicit_invocation: true\n',
        )

    def test_official_plugin_validator_accepts_agent_schema_offline(self):
        validator = (
            Path.home()
            / ".codex/skills/.system/plugin-creator/scripts/validate_plugin.py"
        )
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--offline",
                "--no-project",
                "--with",
                "PyYAML",
                "python",
                str(validator),
                str(PLUGIN_ROOT),
            ],
            cwd=PLUGIN_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_codex_owns_driver_arbitration_integration_and_commits(self):
        authority = self.documents.get("ENGINE.md", "")
        for phrase in (
            "initiating driver",
            "requirements interviewer",
            "plan arbiter",
            "verifier",
            "routing arbiter",
            "integrator",
            "sole commit, merge, and push authority",
        ):
            self.assertIn(phrase, authority)
        self.assertIn("owner and repository gates", authority)

    def test_external_and_native_model_roles_are_explicit(self):
        authority = self.documents.get("ENGINE.md", "")
        self.assertIn("Fable 5 at max", authority)
        self.assertIn("default external plan reviewer", authority)
        self.assertIn("default external executor", authority)
        self.assertIn("Sol 5.6 at ultra", authority)
        self.assertIn("Codex-native lanes", authority)
        self.assertIn("native Codex subagents", authority)
        self.assertIn("Either foundation model may own routed units", authority)

    def test_role_reversal_forbids_old_or_unsafe_authority(self):
        lowered = self.corpus.lower()
        self.assertNotIn("claude-side driver", lowered)
        self.assertNotIn("claude commits", lowered)
        self.assertIn("executor never commits, merges, or pushes", lowered)
        self.assertIn("same-model review is never called cross-model", lowered)
        self.assertIn("no invented owner commit-approval gate", lowered)

    def test_acts_one_and_two_keep_gate_one_before_product_code(self):
        acts = self.documents.get("references/acts-1-2.md", "")
        for phrase in (
            "# Acts 1–2",
            "## Act 1",
            "one question at a time",
            "explore the code",
            "PLAN.md",
            "PLAN-REVIEW-LOG.md",
            "## Act 2",
            "read-only adversarial plan review",
            "same explicit session",
            "APPROVED",
            "REVISE",
            "accept or reject each finding with reasons",
            "Gate 1",
            "bounded owner deadlock",
            "No product code",
        ):
            self.assertIn(phrase, acts)

    def test_acts_three_and_four_are_complete_and_non_truncated(self):
        acts = self.documents.get("references/acts-3-4.md", "")
        self.assertGreaterEqual(len(acts.splitlines()), 150)
        for heading in (
            "## Act 3 — Frozen-unit execution",
            "### Freeze the unit",
            "### Admit before execution",
            "### Execute without authority drift",
            "### Verify and integrate",
            "## Act 4 — Dependency-aware orchestration",
            "### Build the dependency graph",
            "### Schedule isolated lanes",
            "### Integrate serially",
            "### Close the act",
        ):
            self.assertIn(heading, acts)
        for phrase in (
            "immutable baseline",
            "exact allowed files",
            "worktree",
            "proof command",
            "constraints",
            "report contract",
            "ADMIT",
            "maximum of three active lanes total",
            "one machine-wide heavy lease",
            "one writer per worktree",
            "disjoint parallel file sets",
            "serial integration",
            "complete diff",
            "execution receipt",
        ):
            self.assertIn(phrase, acts)
        self.assertFalse(acts.rstrip().endswith(("-", ":", "and", "or")), "acts file looks truncated")

    def test_profiles_are_isolated_and_gate_two_is_assured_complex_only(self):
        sprint = self.documents.get("profiles/sprint.md", "")
        assured = self.documents.get("profiles/assured.md", "")
        routing = self.documents.get("ROUTING-POLICY.md", "")
        self.assertIn("smallest relevant proof", sprint)
        self.assertIn("No per-unit cross-model review", sprint)
        self.assertIn("No Codex proof rerun", sprint)
        self.assertIn("immediate serial integration", sprint)
        self.assertIn("one comprehensive sprint-end gate", sprint)
        self.assertIn("Red proof alone never promotes Sprint", sprint)
        self.assertIn("per-unit genuine cross-model receipt", assured)
        self.assertIn("Codex reviews the complete diff", assured)
        self.assertIn("reruns the proof", assured)
        self.assertIn("integrated gate", assured)
        self.assertIn("Gate 1 applies to both profiles", routing)
        self.assertIn("Gate 2 applies only to Assured complex work", routing)
        self.assertIn("Sprint never waits at Gate 2", sprint)

    def test_routing_preserves_overlays_arbitration_and_no_substitution(self):
        routing = self.documents.get("ROUTING-POLICY.md", "")
        for phrase in (
            "owner instruction",
            ".ai/EXECUTION_PROFILE.md",
            "Assured",
            "one logged reason",
            "no routing debate",
            "Codex arbitrates",
            "planned complexity and effort",
            "not provider-credit equality",
            "No fallback or substitution without owner approval",
            "owner and project overlays bind",
        ):
            self.assertIn(phrase, routing)

    def test_admission_maps_to_executable_runtime_invariants(self):
        admission = self.documents.get("references/admission.md", "")
        for phrase in (
            "scripts/dispatch-guard.sh",
            "atomic `forge_start` admission and reservation",
            "three active lanes",
            "one machine-wide heavy lease",
            "exact baseline",
            "disjoint file ownership",
            "one writer per worktree",
            "execution receipt",
            "prose is not enforcement",
        ):
            self.assertIn(phrase, admission)

    def test_all_claude_control_uses_only_nine_typed_bridge_tools(self):
        bridge = self.documents.get("references/bridge-tools.md", "")
        discovered = set(re.findall(r"`(forge_[a-z]+)`", bridge))
        self.assertEqual(set(TOOL_SCHEMAS), BRIDGE_TOOLS)
        self.assertEqual(discovered, set(TOOL_SCHEMAS))
        for relative_path in (
            "SKILL.md",
            "ENGINE.md",
            "references/acts-1-2.md",
            "references/acts-3-4.md",
            "references/liveness.md",
        ):
            self.assertIn("bridge-tools.md", self.documents.get(relative_path, ""), relative_path)
        self.assertIn("Never invoke `claude` directly", bridge)
        self.assertIn("Never reconstruct a shell command", bridge)
        self.assertIn("Never select a latest or implicit session", bridge)
        self.assertIn("Never expose arbitrary shell through MCP", bridge)
        self.assertNotRegex(bridge, r"```(?:bash|sh|shell)\s+claude\b")

    def test_corpus_rejects_direct_cli_implicit_session_fallback_and_old_authority(self):
        def violations(corpus: str) -> list[str]:
            lowered = corpus.lower()
            found = []
            if re.search(r"(?m)^\s*(?:\$\s*)?claude\s+--?[a-z]", lowered):
                found.append("direct_cli")
            positive_lines = [
                line
                for line in lowered.splitlines()
                if not re.search(r"\b(?:never|do not|must not|no)\b", line)
            ]
            positive = "\n".join(positive_lines)
            if re.search(
                r"\b(?:select|choose|use|resume)\s+(?:the\s+)?"
                r"(?:latest|most recent|implicit) session\b",
                positive,
            ):
                found.append("latest_session")
            if re.search(
                r"\b(?:use|allow|perform)\s+(?:an?\s+)?(?:automatic|silent) fallback\b",
                positive,
            ):
                found.append("silent_fallback")
            if re.search(
                r"\bclaude(?:-side)? (?:driver|integrator|commits)\b",
                positive,
            ):
                found.append("old_authority")
            return found

        self.assertEqual(violations(self.corpus), [])
        for injected in (
            "\nclaude --resume latest\n",
            "Select the most recent session.",
            "Use automatic fallback when blocked.",
            "Claude-side " + "driver owns integration.",
        ):
            self.assertTrue(violations(self.corpus + injected), injected)

    def test_profile_contracts_resist_contradictory_additions(self):
        sprint = self.documents.get("profiles/sprint.md", "")
        assured = self.documents.get("profiles/assured.md", "")

        def sprint_contradictions(text: str) -> list[str]:
            lowered = text.lower()
            forbidden = (
                r"(?:require|must|always).{0,50}per-unit cross-model review",
                r"(?:require|must|always).{0,50}codex proof rerun",
                r"codex (?:must|always) rerun.{0,30}proof.{0,30}each unit",
            )
            return [pattern for pattern in forbidden if re.search(pattern, lowered)]

        self.assertEqual(sprint_contradictions(sprint), [])
        self.assertTrue(
            sprint_contradictions(
                sprint + "\nSprint must require per-unit cross-model review."
            )
        )
        self.assertTrue(
            sprint_contradictions(
                sprint + "\nCodex must rerun the proof for each unit."
            )
        )
        self.assertRegex(assured.lower(), r"per-unit genuine cross-model receipt")
        self.assertRegex(assured.lower(), r"reruns? the (?:unit's )?proof")

    def test_acts_require_feasibility_and_bounded_fix_rounds(self):
        acts = self.documents.get("references/acts-3-4.md", "")
        for phrase in (
            "feasibility and capability",
            "MAX_FIX_ROUNDS",
            "same explicit session",
            "re-freeze",
            "fresh `ADMIT`",
            "fix-round exhaustion",
        ):
            self.assertIn(phrase, acts)

    def test_usage_retry_contract_is_exactly_once_and_pre_effect_only(self):
        limits = self.documents.get("references/usage-limits.md", "")
        self.assertIn("exactly one retry", limits)
        self.assertIn("before the first token", limits)
        self.assertIn("before any tool effect", limits)
        self.assertIn("same idempotency identity", limits)
        self.assertIn("Never replay after any token or tool effect", limits)

    def test_native_reservation_and_guarded_expansion_are_executable_contracts(self):
        admission = self.documents.get("references/admission.md", "")
        acts = self.documents.get("references/acts-3-4.md", "")
        combined = admission + "\n" + acts
        for phrase in (
            "scripts/forge-native-lane.py",
            "reserve before",
            "release after quiescence",
            "same-weight expansion",
            "prior exact file set",
            "full exact file set",
            "dirty-path containment",
            "fresh `ADMIT`",
            "proof or weight escalation",
        ):
            self.assertIn(phrase, combined)

    def test_act1_review_dirty_controls_are_narrow_and_runtime_accurate(self):
        acts = self.documents.get("references/acts-1-2.md", "")
        admission = self.documents.get("references/admission.md", "")
        acts = " ".join(acts.split())
        admission = " ".join(admission.split())
        for phrase in (
            "only Act-1 writes",
            "`PLAN.md`",
            "`PLAN-REVIEW-LOG.md`",
            "exact paths and hashes",
            "No product or unrelated dirty path",
        ):
            self.assertIn(phrase, acts)
        for phrase in (
            "Build requires a clean repository",
            "Initial Act-1 to Act-2 review",
            "two distinct pinned plan/review-log controls",
            "same-path controls",
            "unrelated dirt fails closed",
        ):
            self.assertIn(phrase, admission)
        self.assertNotIn("Build and review require a clean repository", admission)

    def test_native_guard_request_construction_and_binding_are_exact(self):
        admission = " ".join(
            self.documents.get("references/admission.md", "").split()
        )
        for phrase in (
            "JSON array",
            "flag/value",
            "mode-`0600`",
            "single-link",
            "`--worktree`",
            "`--base-sha`",
            "`--lane-type`",
            "`--weight`",
            "repeated `--file`",
            "expected assertions",
            "scheduler binding",
            "trusted WIP and machine probes",
            "Inside the shared admission lock",
            "fresh guard request",
        ):
            self.assertIn(phrase, admission)

    def test_usage_limits_stop_and_preserve_without_silent_fallback(self):
        limits = self.documents.get("references/usage-limits.md", "")
        for phrase in (
            "owner-supplied",
            "wall-time",
            "turn",
            "USD",
            "at or below 5%",
            "stop",
            "preserve",
            "ask the owner",
            "Fable 5 to Opus",
            "never silently substitute",
            "No default live provider calls",
        ):
            self.assertIn(phrase, limits)

    def test_liveness_is_bounded_non_destructive_and_progress_based(self):
        liveness = self.documents.get("references/liveness.md", "")
        for phrase in (
            "`forge_status`",
            "bounded `forge_wait`",
            "scripts/forge-liveness.sh",
            "scripts/recovery-watchdog.sh",
            "objective or artifact progress",
            "not chatter",
            "never signals or kills",
            "only through `forge_cancel`",
            "paused_provider",
            "paused_policy",
            "never handoff-ready",
            "schema-v12",
            "scripts/forge-native-lane.py",
            "stale native lease",
            "authenticated release or explicit manual owner action",
        ):
            self.assertIn(phrase, liveness)

    def test_independent_forward_replay_records_exact_offline_treatment(self):
        scenario = SCENARIO_PATH.read_text(encoding="utf-8")
        normalized = " ".join(scenario.split()).replace("> ", "")
        exact_prompt = (
            "READ-ONLY baseline pressure test for a not-yet-installed workflow skill. "
            "Do not edit files, run commands, browse, or call any provider. Scenario: "
            "You are in an isolated temporary Git repository. The owner says: “Forge "
            "this complex change with Codex controlling Claude; preserve sprint/assured "
            "cadence.” Give the exact operational response you would take, including "
            "gates, roles, concurrency, admission, review, integration, and any "
            "commands/tools you would invoke. You have no Forge instructions; answer "
            "from your natural baseline behavior only."
        )
        self.assertEqual(scenario.count(exact_prompt), 1)
        for evidence in (
            "No physical temporary repository or artifact existed for the baseline",
            "No verbatim full baseline response",
            "independent agent with no inherited conversation",
            "f4a48352470974a4f3e57952fee3a28320b5d7eb",
            "~/.config/superpowers/worktrees/codex-forge-plugin/forge",
            "preloaded fake contract",
            "no live Claude session was created",
            "separate executable harness",
            "Post-skill verdict: **GREEN**",
            "At most three active lanes total",
            "There is no per-unit cross-model review",
            "Gate 2 is never added to Sprint or non-complex work",
            "Fixes are bounded by `MAX_FIX_ROUNDS`",
            "Executors never stage or commit",
        ):
            self.assertIn(evidence, normalized)
        match = re.search(
            r"## Executable offline forward replay\n\n.*?```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))
        expected_sources = sorted(
            [f"skills/forge/{path}" for path in REQUIRED_FILES]
            + ["scripts/forge_lib/contracts.py"]
        )
        self.assertEqual(replay["source_paths"], expected_sources)
        source_digest = hashlib.sha256()
        for relative in expected_sources:
            source_digest.update(relative.encode("utf-8") + b"\0")
            source_digest.update((PLUGIN_ROOT / relative).read_bytes() + b"\0")
        self.assertEqual(replay["source_sha256"], source_digest.hexdigest())

        response_match = re.search(
            r"<!-- POST_RESPONSE_BEGIN -->\n(.*?)\n<!-- POST_RESPONSE_END -->",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(response_match)
        self.assertEqual(
            replay["post_response_sha256"],
            hashlib.sha256(response_match.group(1).encode("utf-8")).hexdigest(),
        )

        class FakeBridge:
            def __init__(inner_self):
                inner_self.job_id = "forge-scenario-job-001"
                inner_self.revision = 0
                inner_self.session_evidence = "fake-session-evidence-001"
                inner_self.calls = []
                inner_self.provider_calls = 0

            def execute(inner_self, name, arguments):
                validated = validate_tool_arguments(name, arguments)
                inner_self.calls.append((name, validated))
                if name == "forge_preflight":
                    return {"status": "ready", "provider_called": False}
                if name == "forge_start":
                    return {
                        "job_id": inner_self.job_id,
                        "revision": inner_self.revision,
                        "session_evidence": inner_self.session_evidence,
                    }
                if name in {"forge_resume", "forge_steer", "forge_cancel"}:
                    self.assertEqual(validated["job_id"], inner_self.job_id)
                    self.assertEqual(validated["revision"], inner_self.revision)
                    inner_self.revision += 1
                if name in {
                    "forge_status",
                    "forge_wait",
                    "forge_result",
                    "forge_resume",
                    "forge_steer",
                    "forge_cancel",
                }:
                    self.assertEqual(validated["job_id"], inner_self.job_id)
                    return {
                        "job_id": inner_self.job_id,
                        "revision": inner_self.revision,
                        "session_evidence": inner_self.session_evidence,
                    }
                if name == "forge_list":
                    return {"jobs": [{"job_id": inner_self.job_id}]}
                self.fail(f"unexpected fake bridge tool: {name}")

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve(strict=True) / "scenario-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Replay"],
                check=True,
            )
            fixture = replay["repo_fixture"]
            (repository / "README.md").write_text(fixture["README.md"], encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            }
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "scenario baseline"],
                check=True,
                env=commit_environment,
            )
            for name in ("PLAN.md", "PLAN-REVIEW-LOG.md"):
                (repository / name).write_text(fixture[name], encoding="utf-8")
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            self.assertEqual(head, replay["repo_head"])
            dirty = subprocess.run(
                ["git", "-C", str(repository), "status", "--porcelain=v1"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(sorted(line[3:] for line in dirty), ["PLAN-REVIEW-LOG.md", "PLAN.md"])
            replacements = {
                "$REPO": str(repository),
                "$HEAD": head,
                "$PLAN_SHA256": hashlib.sha256((repository / "PLAN.md").read_bytes()).hexdigest(),
                "$REVIEW_LOG_SHA256": hashlib.sha256(
                    (repository / "PLAN-REVIEW-LOG.md").read_bytes()
                ).hexdigest(),
            }

            def substitute(value, bridge):
                dynamic = {
                    **replacements,
                    "$JOB_ID": bridge.job_id,
                    "$REVISION": bridge.revision,
                }
                if isinstance(value, str):
                    if value in dynamic:
                        return dynamic[value]
                    for before, after in dynamic.items():
                        value = value.replace(before, str(after))
                    return value
                if isinstance(value, list):
                    return [substitute(item, bridge) for item in value]
                if isinstance(value, dict):
                    return {name: substitute(item, bridge) for name, item in value.items()}
                return value

            bridge = FakeBridge()
            observed_results = []
            for call in replay["calls"]:
                arguments = substitute(call["arguments"], bridge)
                observed_results.append(
                    {
                        "tool": call["tool"],
                        "result": bridge.execute(call["tool"], arguments),
                    }
                )
                if "job_id" in arguments:
                    invalid = dict(arguments)
                    invalid["session_id"] = invalid.pop("job_id")
                    with self.assertRaisesRegex(ContractError, "unknown field: session_id"):
                        validate_tool_arguments(call["tool"], invalid)
            self.assertEqual([name for name, _args in bridge.calls], replay["expected_tools"])
            self.assertEqual(observed_results, replay["expected_results"])
            self.assertEqual(set(replay["expected_tools"]), BRIDGE_TOOLS)
            self.assertEqual(bridge.provider_calls, 0)
            self.assertTrue(
                all("session_id" not in arguments for _name, arguments in bridge.calls)
            )


class ForgeReviewSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (REVIEW_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.normalized_skill = " ".join(cls.skill.split())

    def test_required_files_links_and_trigger_metadata(self):
        required = {"SKILL.md", "agents/openai.yaml"}
        missing = sorted(path for path in required if not (REVIEW_SKILL_ROOT / path).is_file())
        self.assertEqual(missing, [])
        self.assertTrue(REVIEW_SCENARIO_PATH.is_file(), REVIEW_SCENARIO_PATH)

        metadata = frontmatter(self.skill)
        self.assertEqual(metadata.get("name"), "forge-review")
        description = metadata.get("description", "")
        self.assertTrue(description.startswith("Use when "), description)
        self.assertLess(len(description), 500)

        missing_links = []
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", self.skill):
            target = target.split("#", 1)[0]
            if target and not re.match(r"^[a-z]+://", target):
                resolved = (REVIEW_SKILL_ROOT / target).resolve()
                if not resolved.exists():
                    missing_links.append(target)
        self.assertEqual(missing_links, [])

    def test_openai_metadata_uses_supported_top_level_policy(self):
        metadata_path = REVIEW_SKILL_ROOT / "agents" / "openai.yaml"
        self.assertTrue(metadata_path.is_file(), metadata_path)
        self.assertEqual(
            metadata_path.read_text(encoding="utf-8"),
            'interface:\n'
            '  display_name: "Forge Review"\n'
            '  short_description: "Challenge plans before implementation"\n'
            '  default_prompt: "Use $forge-review to run a bounded adversarial review of this implementation plan."\n'
            'policy:\n'
            '  allow_implicit_invocation: true\n',
        )

    def test_standalone_act_two_preserves_authority_and_product_boundary(self):
        for phrase in (
            "standalone Act 2",
            "Codex is the plan arbiter",
            "Claude advises",
            "No product code",
            "PLAN.md",
            "PLAN-REVIEW-LOG.md",
            "only control artifacts Codex may edit",
            "MAX_ROUNDS",
            "bounded owner deadlock",
            "zero deferred or unresolved findings",
            "A deferred finding keeps Gate 1 open",
            "full terminal receipt content and artifact hash",
            "byte-for-byte append-only",
            "Every terminal review consumes one round",
            "while rounds remain",
            "enter the bounded owner deadlock",
            "No terminal result leaves the review open outside `MAX_ROUNDS`",
        ):
            self.assertIn(phrase, self.normalized_skill)
        self.assertNotIn("Claude is the final arbiter", self.skill)
        self.assertNotIn("APPROVED_FOR_IMPLEMENTATION", self.skill)

    def test_review_uses_only_typed_same_job_lifecycle(self):
        expected = {
            "forge_preflight",
            "forge_start",
            "forge_status",
            "forge_wait",
            "forge_result",
            "forge_resume",
        }
        self.assertEqual(set(re.findall(r"`(forge_[a-z]+)`", self.skill)), expected)
        for phrase in (
            "role=`review`",
            "empty product file reservation",
            "same explicit job",
            "exact stored revision",
            "revised prompt",
            "idempotency key",
            "refreeze",
            "attempt/session evidence",
            "verify its job, attempt, session, prompt, idempotency, and refreeze hashes",
        ):
            self.assertIn(phrase, self.normalized_skill)
        lowered = self.skill.lower()
        self.assertNotRegex(lowered, r"(?m)^\s*(?:\$\s*)?claude\s+--?[a-z]")
        self.assertNotRegex(lowered, r"\b(?:latest|most recent|implicit) session\b")

    def test_exact_verdict_and_deadlock_contract(self):
        for phrase in (
            "VERDICT: APPROVED",
            "VERDICT: REVISE",
            "exactly one final line",
            "text after the verdict",
            "does not close Gate 1",
            "`MAX_ROUNDS` is exhausted",
        ):
            self.assertIn(phrase, self.normalized_skill)
        self.assertNotRegex(self.skill, r"(?m)^(?:PASS|BLOCK)$")

    def test_exact_baseline_is_preserved_as_honest_red_evidence(self):
        scenario = REVIEW_SCENARIO_PATH.read_text(encoding="utf-8")
        normalized_scenario = " ".join(scenario.split())
        self.assertEqual(scenario.count(REVIEW_BASELINE_PROMPT), 2)
        self.assertEqual(scenario.count(REVIEW_BASELINE_RESPONSE), 1)
        for phrase in (
            "Baseline verdict: **RED**",
            "No repository, provider, browser, command, or tool action occurred",
            "PASS / REVISE / BLOCK",
            "no typed bridge/runtime admission",
            "Post-skill verdict: **GREEN**",
            "Capture date: 2026-08-26",
            "Capture method: owner-supplied transcript",
            "Runtime/model: not supplied",
            "authored characterization",
            "not an independent model replay",
        ):
            self.assertIn(phrase, normalized_scenario)

    @staticmethod
    def _exact_verdict(report: str) -> str | None:
        lines = report.splitlines()
        matches = re.findall(r"(?m)^VERDICT: (APPROVED|REVISE)$", report)
        if len(matches) != 1 or not lines or lines[-1] != f"VERDICT: {matches[0]}":
            return None
        return matches[0]

    def test_verdict_parser_rejects_variants_duplicates_and_trailing_text(self):
        self.assertEqual(self._exact_verdict("finding\nVERDICT: APPROVED"), "APPROVED")
        self.assertEqual(self._exact_verdict("finding\nVERDICT: REVISE"), "REVISE")
        for invalid in (
            "finding\nPASS",
            "finding\nBLOCK",
            "finding\nVERDICT: approved",
            "VERDICT: REVISE\nVERDICT: APPROVED",
            "finding\nVERDICT: APPROVED\nthanks",
        ):
            self.assertIsNone(self._exact_verdict(invalid), invalid)

    def test_invalid_or_open_terminal_is_bounded_resume_or_deadlock(self):
        def outcome(reports, finding_states, max_rounds):
            consumed = min(len(reports), max_rounds)
            for index, report in enumerate(reports[:max_rounds]):
                verdict = self._exact_verdict(report)
                state = finding_states[index]
                open_findings = state["deferred"] or state["unresolved"]
                if verdict == "APPROVED" and not open_findings:
                    return "APPROVED"
            if consumed >= max_rounds:
                return "OWNER_DEADLOCK"
            return "RESUME_SAME_JOB"

        invalid = "A report without the required final line."
        open_approved = "F9 remains owner-dependent.\nVERDICT: APPROVED"
        open_state = [{"deferred": ["F9"], "unresolved": []}]
        closed_state = [{"deferred": [], "unresolved": []}]
        self.assertEqual(outcome([invalid], closed_state, 2), "RESUME_SAME_JOB")
        self.assertEqual(outcome([invalid], closed_state, 1), "OWNER_DEADLOCK")
        self.assertEqual(outcome([open_approved], open_state, 2), "RESUME_SAME_JOB")
        self.assertEqual(outcome([open_approved], open_state, 1), "OWNER_DEADLOCK")
        self.assertEqual(
            outcome(["All findings closed.\nVERDICT: APPROVED"], closed_state, 2),
            "APPROVED",
        )

    def test_actual_forge_service_replay_binds_receipts_and_same_session(self):
        scenario = REVIEW_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable offline ForgeService replay\n\n```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))

        def sha256_file(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        def evidence_sha256(value) -> str:
            return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            repository = temporary_root / "review-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Review Replay"],
                check=True,
            )
            (repository / "README.md").write_text("product baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "review baseline"],
                check=True,
            )
            seed_wip_base(repository)
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            plan = repository / "PLAN.md"
            review_log = repository / "PLAN-REVIEW-LOG.md"
            plan.write_text(replay["repo_fixture"]["PLAN.md"], encoding="utf-8")
            review_log.write_text(
                replay["repo_fixture"]["PLAN-REVIEW-LOG.md"],
                encoding="utf-8",
            )

            state_path = temporary_root / "state" / "state.sqlite3"
            argv_log = temporary_root / "argv.jsonl"
            stdin_log = temporary_root / "stdin.jsonl"
            environment_log = temporary_root / "environment.jsonl"
            control_path = temporary_root / FAKE_CONTROL_NAME

            def write_control(report):
                control_path.write_text(
                    json.dumps(
                        {
                            "argv_log": str(argv_log),
                            "stdin_log": str(stdin_log),
                            "environment_log": str(environment_log),
                            "assistant_text": report,
                            "parent_tool_use_id": "forge-review-parent",
                            "control_window_seconds": 0.05,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )

            write_control(replay["round_reports"][0])
            environment = {
                "HOME": str(temporary_root),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "LC_CTYPE": "C.UTF-8",
                "TMPDIR": str(temporary_root),
                "SHELL": "/bin/sh",
                "USER": "forge-review-test",
                "FORGE_STATE_PATH": str(state_path),
                "FORGE_LAUNCH_WORKSPACE": str(repository),
                "FORGE_CLAUDE_EXECUTABLE": str(FAKE_CLAUDE),
            }
            service = ForgeService(
                state_path=state_path,
                launch_workspace=repository,
                claude_executable=str(FAKE_CLAUDE),
                bridge_path=ADMISSION_BRIDGE,
                environment=environment,
                _admission_machine_probe=machine_headroom,
            )
            preflight = service.preflight({"cwd": str(repository)}).body
            self.assertEqual(preflight["status"], "ready")
            self.assertFalse(preflight["provider_called"])
            self.assertEqual(preflight["claude"]["version"], "2.1.248 (Claude Code)")

            admission = review_admission(module="tests.test_skill_contracts")
            admission.update(
                {
                    "review_controls": {
                        "plan": {"path": str(plan), "sha256": sha256_file(plan)},
                        "review_log": {
                            "path": str(review_log),
                            "sha256": sha256_file(review_log),
                        },
                    },
                }
            )
            start_arguments = {
                "prompt": replay["start_prompt"],
                "cwd": str(repository),
                "role": "review",
                "model": "fable",
                "effort": "max",
                "idempotency_key": replay["start_idempotency_key"],
                "budget": {
                    "max_seconds": 10,
                    "max_tokens": 4096,
                    "max_turns": 8,
                    "max_cost_usd": "1.00",
                },
                "admission": admission,
            }
            validate_tool_arguments("forge_start", start_arguments)
            started = service.start(start_arguments).body
            job_id = started["job_id"]

            def wait_terminal(revision):
                deadline = time.monotonic() + 10
                last = None
                while time.monotonic() < deadline:
                    arguments = {
                        "job_id": job_id,
                        "revision": revision,
                        "timeout_ms": 1000,
                    }
                    last = service.wait(arguments).body
                    revision = last["revision"]
                    if last["status"] in {
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "PAUSED_PROVIDER_ACCESS",
                        "PAUSED_POLICY",
                    }:
                        return last
                self.fail(f"actual ForgeService job did not terminate: {last}")

            def terminal_receipt(result_body):
                artifact = result_body["terminal_receipt"]
                path = Path(artifact["path"])
                self.assertTrue(path.is_file())
                self.assertEqual(artifact["sha256"], sha256_file(path))
                self.assertEqual(artifact, result_body["artifacts"]["receipt"])
                receipt_text = path.read_text(encoding="utf-8")
                return json.loads(receipt_text), artifact, receipt_text

            def append_review_round(
                *, round_number, report, receipt_artifact, receipt_text, disposition
            ):
                prior = review_log.read_bytes()
                entry = (
                    f"\n## Round {round_number}\n\n"
                    f"### Claude critique\n\n```text\n{report}\n```\n\n"
                    "### Terminal receipt\n\n"
                    f"Artifact SHA-256: `{receipt_artifact['sha256']}`\n\n"
                    f"```json\n{receipt_text.rstrip()}\n```\n\n"
                    f"### Codex disposition\n\n{disposition}\n"
                ).encode("utf-8")
                review_log.write_bytes(prior + entry)
                current = review_log.read_bytes()
                self.assertEqual(current[: len(prior)], prior)
                self.assertEqual(current[len(prior) :], entry)
                self.assertIn(report.encode("utf-8"), current)
                self.assertIn(receipt_text.rstrip().encode("utf-8"), current)
                self.assertIn(receipt_artifact["sha256"].encode("ascii"), current)
                self.assertIn(disposition.encode("utf-8"), current)
                return current

            terminal_one = wait_terminal(started["revision"])
            self.assertEqual(terminal_one["status"], "SUCCEEDED", terminal_one)
            result_one = service.result(
                {"job_id": job_id, "revision": terminal_one["revision"]}
            ).body
            receipt_one, receipt_artifact_one, receipt_text_one = terminal_receipt(
                result_one
            )
            attempt_one = terminal_one["attempt_id"]
            session_id = terminal_one["session_id"]
            context_one = service.store.get_attempt_context(attempt_one)
            admission_one = service.store.get_attempt_admission(attempt_one)
            self.assertEqual(result_one["report"], replay["round_reports"][0])
            self.assertEqual(self._exact_verdict(result_one["report"]), "REVISE")
            self.assertEqual(receipt_one["job_id"], job_id)
            self.assertEqual(receipt_one["attempt_id"], attempt_one)
            self.assertEqual(receipt_one["session_id"], session_id)
            self.assertEqual(
                context_one["prompt_sha256"],
                hashlib.sha256(start_arguments["prompt"].encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                service.store.get_job(job_id).idempotency_key,
                start_arguments["idempotency_key"],
            )
            self.assertEqual(
                result_one["admission_evidence_sha256"], admission_one["sha256"]
            )

            round_one_log = append_review_round(
                round_number=1,
                report=result_one["report"],
                receipt_artifact=receipt_artifact_one,
                receipt_text=receipt_text_one,
                disposition=replay["codex_dispositions"][0],
            )
            plan.write_text(replay["revised_plan"], encoding="utf-8")
            write_control(replay["round_reports"][1])
            refreeze = {
                "baseline_head": head,
                "review_controls": {
                    "plan": {"path": str(plan), "sha256": sha256_file(plan)},
                    "review_log": {
                        "path": str(review_log),
                        "sha256": sha256_file(review_log),
                    },
                },
            }
            round_one_log_sha256 = hashlib.sha256(round_one_log).hexdigest()
            self.assertEqual(
                refreeze["review_controls"]["review_log"]["sha256"],
                round_one_log_sha256,
            )
            resume_arguments = {
                "job_id": job_id,
                "revision": terminal_one["revision"],
                "prompt": replay["resume_prompt"],
                "idempotency_key": replay["resume_idempotency_key"],
                "refreeze": refreeze,
            }
            validate_tool_arguments("forge_resume", resume_arguments)
            resumed = service.resume(resume_arguments).body
            attempt_two = resumed["attempt_id"]
            self.assertEqual(resumed["job_id"], job_id)
            self.assertNotEqual(attempt_two, attempt_one)
            self.assertEqual(resumed["resumed_from_attempt_id"], attempt_one)
            self.assertEqual(resumed["resume_session_id"], session_id)

            context_two = service.store.get_attempt_context(attempt_two)
            expected_request = {
                "job_id": job_id,
                "revision": terminal_one["revision"],
                "prompt": resume_arguments["prompt"],
                "model_override": None,
                "idempotency_key": resume_arguments["idempotency_key"],
                "refreeze": refreeze,
            }
            expected_request_hash = evidence_sha256(expected_request)
            prompt_hash = hashlib.sha256(
                resume_arguments["prompt"].encode("utf-8")
            ).hexdigest()
            idempotency_hash = hashlib.sha256(
                resume_arguments["idempotency_key"].encode("utf-8")
            ).hexdigest()
            refreeze_hash = evidence_sha256(refreeze)
            self.assertEqual(resumed["prompt_sha256"], prompt_hash)
            self.assertEqual(resumed["context_sha256"], context_two["sha256"])
            self.assertEqual(context_two["request_hash"], expected_request_hash)
            self.assertEqual(context_two["prompt_sha256"], prompt_hash)
            self.assertEqual(
                context_two["review_controls"]["review_log"]["sha256"],
                round_one_log_sha256,
            )
            self.assertEqual(idempotency_hash, replay["resume_idempotency_sha256"])
            self.assertEqual(refreeze_hash, evidence_sha256(resume_arguments["refreeze"]))
            mutated_key = dict(expected_request)
            mutated_key["idempotency_key"] += "-changed"
            mutated_refreeze = json.loads(canonical_json(expected_request))
            mutated_refreeze["refreeze"]["baseline_head"] = "0" * 40
            self.assertNotEqual(evidence_sha256(mutated_key), context_two["request_hash"])
            self.assertNotEqual(
                evidence_sha256(mutated_refreeze), context_two["request_hash"]
            )
            admission_two = service.store.get_attempt_admission(attempt_two)
            self.assertEqual(
                admission_two["resume"]["context_request_sha256"],
                expected_request_hash,
            )
            self.assertEqual(admission_two["resume"]["prompt_sha256"], prompt_hash)

            terminal_two = wait_terminal(resumed["revision"])
            self.assertEqual(terminal_two["status"], "SUCCEEDED", terminal_two)
            result_two = service.result(
                {"job_id": job_id, "revision": terminal_two["revision"]}
            ).body
            receipt_two, receipt_artifact_two, receipt_text_two = terminal_receipt(
                result_two
            )
            self.assertEqual(result_two["report"], replay["round_reports"][1])
            self.assertEqual(self._exact_verdict(result_two["report"]), "APPROVED")
            self.assertEqual(receipt_two["job_id"], job_id)
            self.assertEqual(receipt_two["attempt_id"], attempt_two)
            self.assertEqual(receipt_two["session_id"], session_id)
            self.assertEqual(terminal_two["session_id"], session_id)
            self.assertEqual(
                result_two["admission_evidence_sha256"], admission_two["sha256"]
            )
            self.assertEqual(
                admission_two["review_controls"]["review_log"]["sha256"],
                round_one_log_sha256,
            )
            round_two_log = append_review_round(
                round_number=2,
                report=result_two["report"],
                receipt_artifact=receipt_artifact_two,
                receipt_text=receipt_text_two,
                disposition=replay["codex_dispositions"][1],
            )
            self.assertEqual(round_two_log[: len(round_one_log)], round_one_log)

            provider_argv = [
                json.loads(line)
                for line in argv_log.read_text(encoding="utf-8").splitlines()
                if "--input-format" in json.loads(line)
            ]
            self.assertEqual(len(provider_argv), 2)
            self.assertEqual(
                provider_argv[0][provider_argv[0].index("--session-id") + 1],
                session_id,
            )
            self.assertEqual(
                provider_argv[1][provider_argv[1].index("--resume") + 1],
                session_id,
            )
            user_frames = [
                json.loads(line)
                for line in stdin_log.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("type") == "user"
            ]
            self.assertEqual(len(user_frames), 2)
            self.assertEqual(
                user_frames[1]["message"]["content"][0]["text"],
                resume_arguments["prompt"],
            )

            dirty = subprocess.run(
                ["git", "-C", str(repository), "status", "--porcelain=v1"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(
                sorted(line[3:] for line in dirty),
                ["PLAN-REVIEW-LOG.md", "PLAN.md"],
            )

    def test_schema_characterization_is_same_job_bounded_and_provider_free(self):
        scenario = REVIEW_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable schema-only fake-bridge characterization\n\n```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))

        expected_sources = [
            "scripts/forge_lib/contracts.py",
            "skills/forge-review/SKILL.md",
            "skills/forge-review/agents/openai.yaml",
        ]
        self.assertEqual(replay["source_paths"], expected_sources)
        digest = hashlib.sha256()
        for relative in expected_sources:
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update((PLUGIN_ROOT / relative).read_bytes() + b"\0")
        self.assertEqual(replay["source_sha256"], digest.hexdigest())

        response_match = re.search(
            r"<!-- POST_REVIEW_RESPONSE_BEGIN -->\n(.*?)\n<!-- POST_REVIEW_RESPONSE_END -->",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(response_match)
        self.assertEqual(
            replay["post_response_sha256"],
            hashlib.sha256(response_match.group(1).encode("utf-8")).hexdigest(),
        )

        def value_sha256(value) -> str:
            return hashlib.sha256(
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()

        class FakeBridge:
            def __init__(inner_self, reports):
                inner_self.reports = reports
                inner_self.round_index = 0
                inner_self.job_id = None
                inner_self.session_id = None
                inner_self.attempt_id = None
                inner_self.context_receipt = None
                inner_self.revision = 0
                inner_self.state = "NEW"
                inner_self.calls = []
                inner_self.provider_calls = 0

            def new_context(inner_self, validated, *, refreeze):
                binding = {
                    "job_id": inner_self.job_id,
                    "attempt_id": inner_self.attempt_id,
                    "session_id": inner_self.session_id,
                    "prompt_sha256": hashlib.sha256(
                        validated["prompt"].encode("utf-8")
                    ).hexdigest(),
                    "idempotency_key": validated["idempotency_key"],
                    "idempotency_key_sha256": hashlib.sha256(
                        validated["idempotency_key"].encode("utf-8")
                    ).hexdigest(),
                    "refreeze_sha256": (
                        None if refreeze is None else value_sha256(refreeze)
                    ),
                }
                binding["context_sha256"] = value_sha256(binding)
                return binding

            def execute(inner_self, name, arguments):
                validated = validate_tool_arguments(name, arguments)
                inner_self.calls.append((name, validated))
                if name == "forge_preflight":
                    return {"status": "ready", "provider_called": False}
                if name == "forge_start":
                    self.assertEqual(inner_self.state, "NEW")
                    self.assertEqual(validated["role"], "review")
                    self.assertEqual(validated["model"], "fable")
                    self.assertEqual(validated["effort"], "max")
                    self.assertEqual(validated["admission"]["files"], [])
                    inner_self.job_id = f"job-{secrets.token_hex(8)}"
                    inner_self.attempt_id = f"attempt-{secrets.token_hex(8)}"
                    inner_self.session_id = f"session-{secrets.token_hex(12)}"
                    inner_self.context_receipt = inner_self.new_context(
                        validated,
                        refreeze=None,
                    )
                    inner_self.state = "RUNNING"
                    return {
                        "job_id": inner_self.job_id,
                        "revision": inner_self.revision,
                        "attempt_id": inner_self.attempt_id,
                        "session_id": inner_self.session_id,
                        "context_receipt": dict(inner_self.context_receipt),
                    }
                if validated["job_id"] != inner_self.job_id:
                    raise ContractError("job identity mismatch")
                if validated["revision"] != inner_self.revision:
                    raise ContractError("stale revision")
                if name == "forge_status":
                    return {
                        "status": inner_self.state,
                        "job_id": inner_self.job_id,
                        "attempt_id": inner_self.attempt_id,
                        "session_id": inner_self.session_id,
                        "revision": inner_self.revision,
                    }
                if name == "forge_wait":
                    self.assertEqual(inner_self.state, "RUNNING")
                    inner_self.revision += 1
                    inner_self.state = "SUCCEEDED"
                    return {
                        "status": inner_self.state,
                        "job_id": inner_self.job_id,
                        "attempt_id": inner_self.attempt_id,
                        "session_id": inner_self.session_id,
                        "revision": inner_self.revision,
                    }
                if name == "forge_result":
                    self.assertEqual(inner_self.state, "SUCCEEDED")
                    report = inner_self.reports[inner_self.round_index]
                    terminal_receipt = {
                        **inner_self.context_receipt,
                        "report_sha256": hashlib.sha256(
                            report.encode("utf-8")
                        ).hexdigest(),
                    }
                    return {
                        "status": inner_self.state,
                        "job_id": inner_self.job_id,
                        "attempt_id": inner_self.attempt_id,
                        "session_id": inner_self.session_id,
                        "revision": inner_self.revision,
                        "report": report,
                        "terminal_receipt": terminal_receipt,
                    }
                if name == "forge_resume":
                    self.assertEqual(inner_self.state, "SUCCEEDED")
                    self.assertIn("prompt", validated)
                    self.assertIn("idempotency_key", validated)
                    self.assertIn("review_controls", validated["refreeze"])
                    parent_attempt_id = inner_self.attempt_id
                    inner_self.round_index += 1
                    inner_self.revision += 1
                    inner_self.attempt_id = f"attempt-{secrets.token_hex(8)}"
                    inner_self.context_receipt = inner_self.new_context(
                        validated,
                        refreeze=validated["refreeze"],
                    )
                    inner_self.state = "RUNNING"
                    return {
                        "job_id": inner_self.job_id,
                        "revision": inner_self.revision,
                        "attempt_id": inner_self.attempt_id,
                        "resumed_from_attempt_id": parent_attempt_id,
                        "resume_session_id": inner_self.session_id,
                        "prompt_sha256": inner_self.context_receipt["prompt_sha256"],
                        "idempotency_key": inner_self.context_receipt[
                            "idempotency_key"
                        ],
                        "idempotency_key_sha256": inner_self.context_receipt[
                            "idempotency_key_sha256"
                        ],
                        "refreeze_sha256": inner_self.context_receipt[
                            "refreeze_sha256"
                        ],
                        "context_sha256": inner_self.context_receipt[
                            "context_sha256"
                        ],
                        "context_receipt": dict(inner_self.context_receipt),
                    }
                self.fail(f"unexpected fake bridge tool: {name}")

        def outcome(reports, finding_states, max_rounds):
            for index, report in enumerate(reports[:max_rounds]):
                verdict = self._exact_verdict(report)
                if verdict is None:
                    return "INVALID"
                state = finding_states[index]
                open_findings = state["deferred"] or state["unresolved"]
                if verdict == "APPROVED" and not open_findings:
                    return "APPROVED"
                if index + 1 == max_rounds:
                    return "OWNER_DEADLOCK"
            return "CONTINUE"

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve(strict=True) / "review-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Review Replay"],
                check=True,
            )
            (repository / "README.md").write_text("product baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "review baseline"],
                check=True,
            )
            for name, content in replay["repo_fixture"].items():
                (repository / name).write_text(content, encoding="utf-8")

            def replacements(bridge):
                return {
                    "$REPO": str(repository),
                    "$HEAD": subprocess.run(
                        ["git", "-C", str(repository), "rev-parse", "HEAD"],
                        check=True,
                        stdout=subprocess.PIPE,
                        text=True,
                    ).stdout.strip(),
                    "$PLAN_SHA256": hashlib.sha256((repository / "PLAN.md").read_bytes()).hexdigest(),
                    "$REVIEW_LOG_SHA256": hashlib.sha256(
                        (repository / "PLAN-REVIEW-LOG.md").read_bytes()
                    ).hexdigest(),
                    "$JOB_ID": bridge.job_id,
                    "$REVISION": bridge.revision,
                }

            def substitute(value, bridge):
                dynamic = replacements(bridge)
                if isinstance(value, str):
                    if value in dynamic:
                        return dynamic[value]
                    for before, after in dynamic.items():
                        value = value.replace(before, str(after))
                    return value
                if isinstance(value, list):
                    return [substitute(item, bridge) for item in value]
                if isinstance(value, dict):
                    return {key: substitute(item, bridge) for key, item in value.items()}
                return value

            bridge = FakeBridge(replay["round_reports"])
            result_reports = []
            start_session_id = None
            expected_context = None
            result_receipt_validated = False
            for call in replay["calls"]:
                for name, content in call.get("fixture_update", {}).items():
                    (repository / name).write_text(content, encoding="utf-8")
                arguments = substitute(call["arguments"], bridge)
                if call["tool"] == "forge_resume":
                    self.assertTrue(result_receipt_validated)
                    parent_attempt_id = bridge.attempt_id
                    expected_resume_prompt_hash = hashlib.sha256(
                        arguments["prompt"].encode("utf-8")
                    ).hexdigest()
                    expected_resume_key_hash = hashlib.sha256(
                        arguments["idempotency_key"].encode("utf-8")
                    ).hexdigest()
                    expected_refreeze_hash = value_sha256(arguments["refreeze"])
                result = bridge.execute(call["tool"], arguments)
                if call["tool"] == "forge_start":
                    start_session_id = result["session_id"]
                    self.assertNotIn(start_session_id, json.dumps(replay))
                    self.assertNotIn(result["job_id"], json.dumps(replay))
                    expected_context = result["context_receipt"]
                    self.assertEqual(
                        expected_context["prompt_sha256"],
                        hashlib.sha256(arguments["prompt"].encode("utf-8")).hexdigest(),
                    )
                    self.assertEqual(
                        expected_context["idempotency_key"],
                        arguments["idempotency_key"],
                    )
                    self.assertEqual(
                        expected_context["idempotency_key_sha256"],
                        hashlib.sha256(
                            arguments["idempotency_key"].encode("utf-8")
                        ).hexdigest(),
                    )
                    self.assertIsNone(expected_context["refreeze_sha256"])
                    self.assertEqual(
                        expected_context["context_sha256"],
                        value_sha256(
                            {
                                key: value
                                for key, value in expected_context.items()
                                if key != "context_sha256"
                            }
                        ),
                    )
                if call["tool"] == "forge_resume":
                    self.assertEqual(result["job_id"], bridge.job_id)
                    self.assertEqual(result["resumed_from_attempt_id"], parent_attempt_id)
                    self.assertNotEqual(result["attempt_id"], parent_attempt_id)
                    self.assertEqual(result["resume_session_id"], start_session_id)
                    self.assertEqual(result["prompt_sha256"], expected_resume_prompt_hash)
                    self.assertEqual(result["idempotency_key"], arguments["idempotency_key"])
                    self.assertEqual(
                        result["idempotency_key_sha256"], expected_resume_key_hash
                    )
                    self.assertEqual(result["refreeze_sha256"], expected_refreeze_hash)
                    expected_context = result["context_receipt"]
                    self.assertEqual(
                        result["context_sha256"],
                        value_sha256(
                            {
                                key: value
                                for key, value in expected_context.items()
                                if key != "context_sha256"
                            }
                        ),
                    )
                    result_receipt_validated = False
                if call["tool"] == "forge_result":
                    result_reports.append(result["report"])
                    receipt = result["terminal_receipt"]
                    self.assertEqual(result["job_id"], receipt["job_id"])
                    self.assertEqual(result["attempt_id"], receipt["attempt_id"])
                    self.assertEqual(result["session_id"], start_session_id)
                    self.assertEqual(receipt["session_id"], start_session_id)
                    self.assertEqual(
                        receipt["report_sha256"],
                        hashlib.sha256(result["report"].encode("utf-8")).hexdigest(),
                    )
                    for field in (
                        "job_id",
                        "attempt_id",
                        "session_id",
                        "prompt_sha256",
                        "idempotency_key",
                        "idempotency_key_sha256",
                        "refreeze_sha256",
                        "context_sha256",
                    ):
                        self.assertEqual(receipt[field], expected_context[field], field)
                    result_receipt_validated = True

            self.assertEqual([name for name, _args in bridge.calls], replay["expected_tools"])
            self.assertEqual([self._exact_verdict(report) for report in result_reports], ["REVISE", "APPROVED"])
            self.assertTrue(result_receipt_validated)
            self.assertEqual(
                outcome(
                    result_reports,
                    replay["round_finding_states"],
                    replay["max_rounds"],
                ),
                "APPROVED",
            )
            self.assertEqual(
                outcome(
                    replay["deadlock_reports"],
                    replay["deadlock_finding_states"],
                    replay["max_rounds"],
                ),
                "OWNER_DEADLOCK",
            )
            self.assertEqual(
                outcome(
                    ["Reviewer says approved but F9 needs the owner.\nVERDICT: APPROVED"],
                    [{"deferred": ["F9"], "unresolved": []}],
                    1,
                ),
                "OWNER_DEADLOCK",
            )
            self.assertEqual(bridge.provider_calls, 0)
            self.assertEqual(bridge.session_id, start_session_id)

            stale = {"job_id": bridge.job_id, "revision": bridge.revision - 1}
            with self.assertRaisesRegex(ContractError, "stale revision"):
                bridge.execute("forge_status", stale)
            with self.assertRaisesRegex(ContractError, "job identity mismatch"):
                bridge.execute(
                    "forge_status",
                    {"job_id": "another-job", "revision": bridge.revision},
                )
            with self.assertRaisesRegex(ContractError, "unknown field: session_id"):
                validate_tool_arguments(
                    "forge_resume",
                    {
                        "job_id": bridge.job_id,
                        "revision": bridge.revision,
                        "session_id": bridge.session_id,
                    },
                )

            dirty = subprocess.run(
                ["git", "-C", str(repository), "status", "--porcelain=v1"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(
                sorted(line[3:] for line in dirty),
                ["PLAN-REVIEW-LOG.md", "PLAN.md"],
            )

class ForgeBuildSkillContractTests(unittest.TestCase):
    FINAL_EXECUTION_RECEIPT_FIELDS = {
        "version",
        "report_phase",
        "unit_id",
        "work_order_sha256",
        "admission",
        "requested_route",
        "observed_route",
        "status",
        "implementation_summary",
        "changed_files",
        "no_other_paths",
        "proof",
        "work_log_artifact",
        "report_artifact",
        "deviations",
        "risks",
        "follow_ups",
        "unresolved_questions",
        "no_commit_merge_push",
        "terminal_receipt",
    }

    @classmethod
    def setUpClass(cls):
        cls.skill = (BUILD_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.normalized_skill = " ".join(cls.skill.split())

    @staticmethod
    def _private_parent_fd(path: Path) -> int:
        if not path.is_absolute() or path.parent / path.name != path:
            raise ValueError("artifact path must be exact and absolute")
        try:
            canonical_parent = path.parent.resolve(strict=True)
            parent_information = os.stat(path.parent, follow_symlinks=False)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("artifact parent is unavailable") from error
        if (
            canonical_parent != path.parent
            or not stat.S_ISDIR(parent_information.st_mode)
            or parent_information.st_uid != os.geteuid()
            or stat.S_IMODE(parent_information.st_mode) != 0o700
        ):
            raise ValueError("artifact parent must be canonical owner-only 0700")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(canonical_parent, flags)
        except OSError as error:
            raise ValueError("artifact parent cannot be opened safely") from error
        held = os.fstat(descriptor)
        if (held.st_dev, held.st_ino, held.st_uid, stat.S_IMODE(held.st_mode)) != (
            parent_information.st_dev,
            parent_information.st_ino,
            parent_information.st_uid,
            0o700,
        ):
            os.close(descriptor)
            raise ValueError("artifact parent identity changed")
        return descriptor

    @classmethod
    def _publish_private_artifact(cls, path: Path, data: bytes) -> dict[str, object]:
        if not isinstance(data, bytes) or len(data) > 1024 * 1024:
            raise ValueError("artifact bytes are invalid")
        parent_descriptor = cls._private_parent_fd(path)
        descriptor = None
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise ValueError("artifact write was incomplete")
                offset += written
            os.fsync(descriptor)
            held = os.fstat(descriptor)
            named = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_uid != os.geteuid()
                or stat.S_IMODE(held.st_mode) != 0o600
                or held.st_nlink != 1
                or held.st_size != len(data)
                or (held.st_dev, held.st_ino, held.st_uid)
                != (named.st_dev, named.st_ino, named.st_uid)
            ):
                raise ValueError("artifact publication identity is invalid")
            return {
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "device": held.st_dev,
                "inode": held.st_ino,
                "owner": held.st_uid,
                "mode": stat.S_IMODE(held.st_mode),
                "links": held.st_nlink,
            }
        except (OSError, ValueError) as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError("artifact cannot be published safely") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)

    @classmethod
    def _read_private_artifact(
        cls,
        path: Path,
        *,
        expected_pin: dict[str, object] | None = None,
        after_open=None,
    ) -> tuple[bytes, dict[str, object]]:
        parent_descriptor = cls._private_parent_fd(path)
        descriptor = None
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            before = os.fstat(descriptor)
            named_before = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size > 1024 * 1024
                or (before.st_dev, before.st_ino, before.st_uid)
                != (named_before.st_dev, named_before.st_ino, named_before.st_uid)
            ):
                raise ValueError("artifact identity is invalid")
            if after_open is not None:
                after_open()
            chunks = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    raise ValueError("artifact read was incomplete")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ValueError("artifact grew while held")
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            named_after = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                (
                    before.st_dev,
                    before.st_ino,
                    before.st_uid,
                    before.st_mode,
                    before.st_nlink,
                    before.st_size,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_uid,
                    after.st_mode,
                    after.st_nlink,
                    after.st_size,
                )
                or (after.st_dev, after.st_ino, after.st_uid)
                != (named_after.st_dev, named_after.st_ino, named_after.st_uid)
            ):
                raise ValueError("artifact identity changed during held read")
            pin = {
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "device": after.st_dev,
                "inode": after.st_ino,
                "owner": after.st_uid,
                "mode": stat.S_IMODE(after.st_mode),
                "links": after.st_nlink,
            }
            if expected_pin is not None and pin != expected_pin:
                raise ValueError("artifact differs from its frozen pin")
            return data, pin
        except (OSError, ValueError) as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError("artifact cannot be read safely") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)

    @staticmethod
    def _native_lane_module():
        script = PLUGIN_ROOT / "scripts" / "forge-native-lane.py"
        spec = importlib.util.spec_from_file_location(
            f"forge_native_lane_skill_{secrets.token_hex(8)}", script
        )
        if spec is None or spec.loader is None:
            raise AssertionError("native lane CLI cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _invoke_native_lane(module, state_path: Path, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(module, "_machine_snapshot", return_value=machine_headroom()),
            mock.patch.object(module, "_measured_wip", return_value=0),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
        ):
            status = module.main(["--state", str(state_path), *arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_required_files_links_and_trigger_metadata(self):
        required = {"SKILL.md", "agents/openai.yaml"}
        missing = sorted(path for path in required if not (BUILD_SKILL_ROOT / path).is_file())
        self.assertEqual(missing, [])
        self.assertTrue(BUILD_SCENARIO_PATH.is_file(), BUILD_SCENARIO_PATH)

        metadata = frontmatter(self.skill)
        self.assertEqual(metadata.get("name"), "forge-build")
        description = metadata.get("description", "")
        self.assertTrue(description.startswith("Use when "), description)
        self.assertLess(len(description), 500)
        self.assertNotRegex(
            description.lower(),
            r"\b(first|then|finally|freeze|admit|execute|verify|integrate|commit)\b",
        )

        missing_links = []
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", self.skill):
            target = target.split("#", 1)[0]
            if target and not re.match(r"^[a-z]+://", target):
                resolved = (BUILD_SKILL_ROOT / target).resolve()
                if not resolved.exists():
                    missing_links.append(target)
        self.assertEqual(missing_links, [])

    def test_openai_metadata_uses_supported_top_level_policy(self):
        metadata_path = BUILD_SKILL_ROOT / "agents" / "openai.yaml"
        self.assertTrue(metadata_path.is_file(), metadata_path)
        self.assertEqual(
            metadata_path.read_text(encoding="utf-8"),
            'interface:\n'
            '  display_name: "Forge Build"\n'
            '  short_description: "Execute one frozen build unit"\n'
            '  default_prompt: "Use $forge-build to execute this approved frozen work order with Codex as integration authority."\n'
            'policy:\n'
            '  allow_implicit_invocation: true\n',
        )

    def test_standalone_act_three_freezes_complete_feasible_unit(self):
        for phrase in (
            "standalone Act 3",
            "one coherent unit",
            "genuine Gate-1 `APPROVED`",
            "feasibility and capability",
            "stops before product work",
            "immutable baseline",
            "exact allowed files",
            "structured proof argv",
            "owner-supplied budgets",
            "work-log and report paths",
            "report contract",
            "`DONE`, `BLOCKED`, or `FAILED`",
            "driver-enriched final execution receipt",
            "provider report is pre-terminal",
            "unit ID, work-order SHA-256, admitted job and attempt IDs",
            "requested and observed route",
            "implementation summary",
            "no other path changed",
            "proof argv, exit code, summary, and artifact hashes",
            "work-log and report artifact path, SHA-256, and receipt ID",
            "follow-ups and unresolved questions",
            "concrete terminal-receipt identity",
            "append-only native progress work-log",
            "distinct from the successor control",
            "owner-private terminal-release artifact",
            "release artifact's own path and SHA-256",
            "no commit, merge, or push",
            "MAX_FIX_ROUNDS",
        ):
            self.assertIn(phrase, self.normalized_skill)

    def test_executable_admission_and_explicit_job_lifecycle(self):
        for phrase in (
            "provider-free `forge_preflight`",
            "role=`build`",
            "`ADMIT`",
            "atomic admission and reservation",
            "returned job ID, attempt/session evidence, and revision",
            "`forge_status`",
            "bounded `forge_wait`",
            "`forge_result`",
            "scripts/forge-native-lane.py",
            "`reserve` before native delegation",
            "`heartbeat` while active",
            "`release` only after quiescence",
            "same global scheduler",
        ):
            self.assertIn(phrase, self.normalized_skill)
        lowered = self.skill.lower()
        self.assertNotRegex(lowered, r"(?m)^\s*(?:\$\s*)?claude\s+--?[a-z]")
        self.assertNotRegex(lowered, r"\b(?:latest|most recent|implicit) session\b")
        self.assertNotIn("session_id", self.skill)

    def test_profiles_have_exactly_different_per_unit_cadence(self):
        for phrase in (
            "smallest relevant proof",
            "no per-unit cross-model review",
            "no Codex proof rerun",
            "immediate serial integration",
            "one comprehensive sprint-end gate",
            "genuine other-family review receipt",
            "complete diff",
            "Codex reruns the proof",
            "integrated gate",
            "Red proof alone never promotes Sprint",
        ):
            self.assertIn(phrase, self.normalized_skill)
        self.assertIn("Codex alone integrates", self.normalized_skill)
        self.assertIn("No invented owner commit-approval gate", self.normalized_skill)

    def test_fix_and_failure_paths_remain_bounded_and_same_job(self):
        for phrase in (
            "`forge_resume`",
            "same explicit job",
            "same recorded session",
            "exact stored revision",
            "exact fix prompt",
            "typed product `refreeze`",
            "fresh `ADMIT`",
            "Every terminal repair attempt consumes one round",
            "fix-round exhaustion",
            "provider or policy pause",
            "No model or provider substitution",
            "On `REJECT`, launch nothing",
            "baseline or scope drift",
            "Claude repairs use `forge_resume`",
            "quiesce the failed native agent and every descendant",
            "full source Git dirty set exactly equals the admitted file set",
            "every dirty path is already tracked",
            "reject any index delta",
            "before patch capture or lease release",
            "`git diff --binary --full-index`",
            "canonical owner-only `0700` parent",
            "`O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`",
            "`0600`",
            "`fchmod` and `fsync`",
            "regular, single-link, owner, mode, device, and inode",
            "one held descriptor/read sequence",
            "owner-private patch artifact",
            "exact declared path set and SHA-256",
            "authenticated `release` of the current lease",
            "fresh isolated clean worktree at the frozen baseline",
            "incremented fix-round count",
            "fresh private guard request",
            "new `ADMIT` and owner-private receipt",
            "successor control",
            "predecessor lease and receipt hash",
            "patch hash, size, and path set",
            "fix round and `MAX_FIX_ROUNDS`",
            "baseline and exact work-order, proof, and report-contract hashes",
            "control path and hash",
            "Only after `ADMIT`",
            "without staging or committing",
            "exact declared dirty set and content hashes",
            "`git apply --binary -` through stdin",
            "never reopen an artifact pathname",
            "reconstruct the successor after restart",
            "complete report contract",
            "passing exact proof",
            "quiesced executor and descendants",
            "untracked/new, extra, or missing dirt",
            "preserve the original worktree, active lease, owner receipt, and content",
            "new-unit/manual typed-recovery decision",
            "Any patch, file, proof, constraint, weight, or other contract change",
            "new unit with full admission",
            "Both paths remain bounded by `MAX_FIX_ROUNDS`",
        ):
            self.assertIn(phrase, self.normalized_skill)

    def test_private_native_artifacts_reject_aliases_links_and_path_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True) / "artifacts"
            root.mkdir(mode=0o700)
            root.chmod(0o700)

            source = root / "source.bin"
            source_pin = self._publish_private_artifact(source, b"source bytes\n")
            self.assertEqual(
                self._read_private_artifact(source, expected_pin=source_pin)[0],
                b"source bytes\n",
            )

            symlink = root / "symlink.bin"
            symlink.symlink_to(source.name)
            with self.assertRaisesRegex(ValueError, "safely"):
                self._read_private_artifact(symlink)
            with self.assertRaisesRegex(ValueError, "published safely"):
                self._publish_private_artifact(symlink, b"replacement\n")

            hardlink = root / "hardlink.bin"
            os.link(source, hardlink)
            with self.assertRaisesRegex(ValueError, "identity"):
                self._read_private_artifact(hardlink)
            with self.assertRaisesRegex(ValueError, "published safely"):
                self._publish_private_artifact(hardlink, b"replacement\n")

            swap = root / "swap.bin"
            swap_pin = self._publish_private_artifact(swap, b"frozen bytes\n")
            displaced = root / "swap-displaced.bin"

            def replace_pathname():
                os.rename(swap, displaced)
                self._publish_private_artifact(swap, b"hostile replacement\n")

            with self.assertRaisesRegex(ValueError, "identity changed"):
                self._read_private_artifact(
                    swap,
                    expected_pin=swap_pin,
                    after_open=replace_pathname,
                )
            self.assertEqual(displaced.read_bytes(), b"frozen bytes\n")
            self.assertEqual(swap.read_bytes(), b"hostile replacement\n")

    def test_native_cli_repair_carries_hashed_patch_into_clean_successor_offline(self):
        native_module = self._native_lane_module()
        max_fix_rounds, fix_round = 2, 1
        proof_argv = ["python3", "proof.py"]
        executor_report_fields = {
            "version", "report_phase", "unit_id", "work_order_sha256",
            "admission", "requested_route", "observed_route", "status",
            "implementation_summary", "changed_files", "no_other_paths",
            "proof", "work_log_artifact", "deviations", "risks", "follow_ups",
            "unresolved_questions", "no_commit_merge_push",
        }

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            artifacts = temporary_root / "artifacts"
            artifacts.mkdir(mode=0o700)
            artifacts.chmod(0o700)
            repository = temporary_root / "native-repair-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Native Replay"],
                check=True,
            )
            report_contract = (
                canonical_json(
                    {
                        "version": 1,
                        "provider_phase": "executor_pre_terminal",
                        "final_required_fields": sorted(
                            self.FINAL_EXECUTION_RECEIPT_FIELDS
                        ),
                    }
                )
                + "\n"
            )
            fixture = {
                "src/native.py": "def value():\n    return 0\n",
                "proof.py": (
                    "from pathlib import Path\n\nnamespace = {}\n"
                    "exec(Path('src/native.py').read_text(encoding='utf-8'), namespace)\n"
                    "raise SystemExit(0 if namespace['value']() == 2 else 1)\n"
                ),
                "PLAN.md": "# Approved plan\n\nImplement TASK-15-NATIVE in src/native.py.\n",
                "PLAN-REVIEW-LOG.json": (
                    '{"receipt_id":"act2-task-15-native","reviewer_family":"claude-opus",'
                    '"unit_id":"TASK-15-NATIVE","verdict":"APPROVED","version":1}\n'
                ),
                "execution-contract.md": (
                    "Change only src/native.py, run python3 proof.py, and return every "
                    "field in report-contract.json. Never commit, merge, push, rebase, "
                    "or checkpoint.\n"
                ),
                "project-overlay.md": "executor_policy=codex-only\n",
                "report-contract.json": report_contract,
            }
            for relative, content in fixture.items():
                destination = repository / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "native baseline"],
                check=True,
            )
            seed_wip_base(repository)
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            def digest(data):
                return hashlib.sha256(data).hexdigest()

            def append_progress(events, event, evidence):
                record = {
                    "sequence": len(events) + 1,
                    "event": event,
                    "previous_event_sha256": (
                        events[-1]["event_sha256"] if events else "0" * 64
                    ),
                    "evidence": evidence,
                }
                record["event_sha256"] = digest(
                    canonical_json(record).encode("utf-8")
                )
                events.append(record)

            def progress_bytes(events):
                return b"".join(
                    (canonical_json(event) + "\n").encode("utf-8")
                    for event in events
                )

            def repository_id(worktree):
                common = subprocess.run(
                    ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip()
                common_path = Path(common)
                if not common_path.is_absolute():
                    common_path = worktree / common_path
                return digest(
                    canonical_json(
                        {"common_dir": str(common_path.resolve(strict=True))}
                    ).encode("utf-8")
                )

            repo_id = repository_id(repository)
            work_order = {
                "version": 1,
                "unit_id": "TASK-15-NATIVE",
                "baseline": head,
                "files": ["src/native.py"],
                "proof_argv": proof_argv,
                "report_contract_sha256": digest(report_contract.encode("utf-8")),
            }
            work_order_sha256 = digest(canonical_json(work_order).encode("utf-8"))
            proof_sha256 = digest(canonical_json(proof_argv).encode("utf-8"))
            report_contract_sha256 = digest(report_contract.encode("utf-8"))

            state_path = temporary_root / "state" / "state.sqlite3"
            initial_receipt = artifacts / "native-attempt-00-owner.json"
            successor_receipt = artifacts / "native-attempt-01-owner.json"
            initial_request = artifacts / "native-attempt-00-guard.json"
            successor_request = artifacts / "native-attempt-01-guard.json"
            initial_control = artifacts / "native-attempt-00-control.json"
            successor_control = artifacts / "native-attempt-01-control.json"
            patch_artifact = artifacts / "native-attempt-00.patch"
            report_artifact = artifacts / "native-attempt-01-report.json"
            admission_progress_artifact = (
                artifacts / "native-attempt-01-progress-0001.jsonl"
            )
            final_progress_artifact = (
                artifacts / "native-attempt-01-progress-final.jsonl"
            )
            terminal_release_artifact = (
                artifacts / "native-attempt-01-terminal-release.json"
            )
            final_receipt_artifact = (
                artifacts / "native-attempt-01-final-execution-receipt.json"
            )
            initial_control_pin = self._publish_private_artifact(
                initial_control,
                (canonical_json({"version": 1, "work_order_sha256": work_order_sha256}) + "\n").encode(),
            )
            exact_instruction = (
                "Execute TASK-15-NATIVE. Change only src/native.py, run python3 proof.py, "
                "and never commit, merge, push, rebase, or checkpoint."
            )

            def guard_request(worktree, work_log, report, instruction):
                return [
                    "--verdict", "APPROVED", "--weight", "LIGHT",
                    "--lane-type", "feature", "--mode", "normal",
                    "--worktree", str(worktree), "--builder", "Sol 5.6 at ultra",
                    "--executor-policy", "codex-only", "--base-sha", head,
                    "--plan-file", str(worktree / "PLAN.md"),
                    "--act2-review-log", str(worktree / "PLAN-REVIEW-LOG.json"),
                    "--execution-contract", str(worktree / "execution-contract.md"),
                    "--project-overlay", str(worktree / "project-overlay.md"),
                    "--oracle-exact", "false", "--proof-command", "python3 proof.py",
                    "--proof-scope", "targeted", "--execution-profile", "sprint",
                    "--backlog-id", "TASK-15-NATIVE", "--work-log", str(work_log),
                    "--report", str(report), "--predecessor", "none",
                    "--instruction", instruction, "--file", "src/native.py",
                ]

            initial_guard = guard_request(
                repository,
                initial_control,
                artifacts / "native-attempt-00-report.json",
                exact_instruction,
            )
            initial_request_pin = self._publish_private_artifact(
                initial_request, (canonical_json(initial_guard) + "\n").encode()
            )

            def invoke(*arguments):
                status, output, error = self._invoke_native_lane(
                    native_module, state_path, arguments
                )
                self.assertEqual(status, 0, error)
                return json.loads(output)

            def reserve_arguments(lease_id, receipt, request, worktree):
                return (
                    "reserve", "--lease-id", lease_id, "--receipt", str(receipt),
                    "--guard-request", str(request), "--repo-id", repo_id,
                    "--base-sha", head, "--lane", "feature", "--weight", "LIGHT",
                    "--writer", str(worktree), "--file", "src/native.py",
                )

            initial_args = reserve_arguments(
                "TASK-15-NATIVE-attempt-00", initial_receipt, initial_request, repository
            )
            initial = invoke(*initial_args)
            self.assertFalse(initial["idempotent"])
            self.assertTrue(invoke(*initial_args)["idempotent"])
            self.assertEqual(initial["reservation"]["revision"], 0)
            self.assertEqual(
                self._read_private_artifact(
                    initial_request, expected_pin=initial_request_pin
                )[1],
                initial_request_pin,
            )
            self.assertEqual(
                self._read_private_artifact(
                    initial_control, expected_pin=initial_control_pin
                )[1],
                initial_control_pin,
            )

            (repository / "src" / "native.py").write_text(
                "def value():\n    return 1\n", encoding="utf-8"
            )
            status = subprocess.run(
                [
                    "git", "-C", str(repository), "status", "--porcelain=v1",
                    "--untracked-files=all",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(status, [" M src/native.py"])
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "diff", "--cached", "--quiet", "--exit-code"],
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "ls-files", "--error-unmatch", "src/native.py"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.splitlines(),
                ["src/native.py"],
            )
            patch_bytes = subprocess.run(
                [
                    "git", "-C", str(repository), "diff", "--binary", "--full-index",
                    "--", "src/native.py",
                ],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            patch_paths = subprocess.run(
                ["git", "-C", str(repository), "diff", "--name-only", "--", "src/native.py"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertTrue(patch_bytes)
            self.assertEqual(patch_paths, ["src/native.py"])
            patch_pin = self._publish_private_artifact(patch_artifact, patch_bytes)
            predecessor_receipt_bytes, predecessor_receipt_pin = self._read_private_artifact(
                initial_receipt
            )
            self.assertEqual(
                json.loads(predecessor_receipt_bytes)["lease_id"],
                "TASK-15-NATIVE-attempt-00",
            )
            control_payload = {
                "version": 1,
                "predecessor": {
                    "lease_id": "TASK-15-NATIVE-attempt-00",
                    "owner_receipt_sha256": predecessor_receipt_pin["sha256"],
                },
                "patch": {
                    "path": str(patch_artifact),
                    "sha256": patch_pin["sha256"],
                    "size": patch_pin["size"],
                    "paths": patch_paths,
                },
                "repair": {"fix_round": fix_round, "max_fix_rounds": max_fix_rounds},
                "baseline": head,
                "work_order_sha256": work_order_sha256,
                "proof_argv_sha256": proof_sha256,
                "report_contract_sha256": report_contract_sha256,
            }
            successor_control_pin = self._publish_private_artifact(
                successor_control, (canonical_json(control_payload) + "\n").encode()
            )
            released = invoke(
                "release", "--lease-id", "TASK-15-NATIVE-attempt-00",
                "--receipt", str(initial_receipt),
                "--revision", str(initial["reservation"]["revision"]),
            )
            self.assertEqual(released["status"], "RELEASED")

            successor_repository = temporary_root / "native-repair-successor"
            subprocess.run(
                [
                    "git", "-C", str(repository), "worktree", "add", "--detach",
                    str(successor_repository), head,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(repository_id(successor_repository), repo_id)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(successor_repository), "status", "--porcelain=v1"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout,
                "",
            )
            progress_events = []
            append_progress(
                progress_events,
                "source_repair_gate_passed",
                {
                    "dirty_paths": status,
                    "dirty_status_sha256": digest(
                        canonical_json(status).encode("utf-8")
                    ),
                    "index_clean": True,
                },
            )
            append_progress(
                progress_events,
                "patch_captured",
                {
                    "path": str(patch_artifact),
                    "sha256": patch_pin["sha256"],
                    "size": patch_pin["size"],
                    "paths": patch_paths,
                },
            )
            append_progress(
                progress_events,
                "predecessor_released",
                {
                    "lease_id": "TASK-15-NATIVE-attempt-00",
                    "status": released["status"],
                    "revision": released["revision"],
                    "owner_receipt_sha256": predecessor_receipt_pin["sha256"],
                },
            )
            append_progress(
                progress_events,
                "successor_worktree_clean",
                {"baseline": head, "repository_id": repo_id},
            )
            append_progress(
                progress_events,
                "successor_control_published",
                {
                    "path": str(successor_control),
                    "sha256": successor_control_pin["sha256"],
                },
            )
            admission_progress_bytes = progress_bytes(progress_events)
            admission_progress_pin = self._publish_private_artifact(
                admission_progress_artifact, admission_progress_bytes
            )
            binding = (
                f" successor_control_path={successor_control}"
                f" successor_control_sha256={successor_control_pin['sha256']}"
            )
            successor_guard = guard_request(
                successor_repository,
                admission_progress_artifact,
                report_artifact,
                exact_instruction + binding,
            )
            successor_request_pin = self._publish_private_artifact(
                successor_request, (canonical_json(successor_guard) + "\n").encode()
            )
            successor_args = reserve_arguments(
                "TASK-15-NATIVE-attempt-01-fix-01",
                successor_receipt,
                successor_request,
                successor_repository,
            )
            successor = invoke(*successor_args)
            successor_replay = invoke(*successor_args)
            self.assertFalse(successor["idempotent"])
            self.assertTrue(successor_replay["idempotent"])
            self.assertEqual(successor_replay["reservation"], successor["reservation"])

            restarted_store = Store(state_path)
            with restarted_store.connection() as connection:
                successor_admission = json.loads(
                    connection.execute(
                        "SELECT admission_json FROM lane_reservations WHERE lease_id = ?",
                        ("TASK-15-NATIVE-attempt-01-fix-01",),
                    ).fetchone()[0]
                )
            self.assertEqual(successor_admission["result"]["decision"], "ADMIT")
            self.assertTrue(successor_admission["result"]["repository_clean"])
            self.assertEqual(
                successor_admission["guard_request_sha256"],
                successor_request_pin["sha256"],
            )
            stored_argv = successor_admission["argv"]
            stored_work_log = stored_argv[stored_argv.index("--work-log") + 1]
            stored_instruction = stored_argv[stored_argv.index("--instruction") + 1]
            self.assertEqual(stored_work_log, str(admission_progress_artifact))
            self.assertIn(str(successor_control_pin["sha256"]), stored_instruction)
            self.assertNotEqual(stored_work_log, str(successor_control))
            reconstructed_progress_bytes, reconstructed_progress_pin = (
                self._read_private_artifact(
                    Path(stored_work_log), expected_pin=admission_progress_pin
                )
            )
            self.assertEqual(reconstructed_progress_bytes, admission_progress_bytes)
            self.assertEqual(reconstructed_progress_pin, admission_progress_pin)
            successor_receipt_bytes, successor_receipt_pin = self._read_private_artifact(
                successor_receipt
            )
            self.assertEqual(
                json.loads(successor_receipt_bytes)["lease_id"],
                "TASK-15-NATIVE-attempt-01-fix-01",
            )
            append_progress(
                progress_events,
                "successor_admitted",
                {
                    "lease_id": "TASK-15-NATIVE-attempt-01-fix-01",
                    "guard_request_sha256": successor_request_pin["sha256"],
                    "owner_receipt_sha256": successor_receipt_pin["sha256"],
                    "admission_sha256": successor["reservation"][
                        "admission_sha256"
                    ],
                },
            )
            stored_control_path = re.search(
                r"(?:^| )successor_control_path=([^ ]+)", stored_instruction
            )
            stored_control_sha256 = re.search(
                r"(?:^| )successor_control_sha256=([0-9a-f]{64})(?: |$)",
                stored_instruction,
            )
            self.assertIsNotNone(stored_control_path)
            self.assertIsNotNone(stored_control_sha256)
            self.assertEqual(stored_control_path.group(1), str(successor_control))
            self.assertEqual(
                stored_control_sha256.group(1), successor_control_pin["sha256"]
            )
            reconstructed_control_bytes, reconstructed_control_pin = (
                self._read_private_artifact(
                    Path(stored_control_path.group(1)),
                    expected_pin=successor_control_pin,
                )
            )
            reconstructed_control = json.loads(reconstructed_control_bytes)
            self.assertEqual(reconstructed_control, control_payload)
            verified_patch_bytes, verified_patch_pin = self._read_private_artifact(
                Path(reconstructed_control["patch"]["path"]), expected_pin=patch_pin
            )
            self.assertEqual(verified_patch_pin["sha256"], control_payload["patch"]["sha256"])
            self.assertEqual(digest(verified_patch_bytes), patch_pin["sha256"])

            apply_commands = []
            for arguments in (
                ["apply", "--numstat", "--binary", "-"],
                ["apply", "--check", "--binary", "-"],
                ["apply", "--binary", "-"],
            ):
                command = ["git", "-C", str(successor_repository), *arguments]
                apply_commands.append(command)
                applied = subprocess.run(
                    command,
                    input=verified_patch_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(applied.returncode, 0, applied.stderr)
                if "--numstat" in arguments:
                    self.assertEqual(
                        [line.split("\t", 2)[-1] for line in applied.stdout.decode().splitlines()],
                        ["src/native.py"],
                    )
            self.assertTrue(all(str(patch_artifact) not in command for command in apply_commands))
            carried_patch = subprocess.run(
                [
                    "git", "-C", str(successor_repository), "diff", "--binary",
                    "--full-index", "--", "src/native.py",
                ],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(carried_patch, verified_patch_bytes)
            append_progress(
                progress_events,
                "verified_patch_applied",
                {
                    "patch_sha256": verified_patch_pin["sha256"],
                    "changed_file_sha256": digest(
                        (successor_repository / "src" / "native.py").read_bytes()
                    ),
                    "changed_paths": ["src/native.py"],
                },
            )

            fake_executor_code = """
import hashlib, json, subprocess, sys
from pathlib import Path
config = json.loads(sys.stdin.read())
Path(config['file']).write_text(config['replacement'], encoding='utf-8')
proof = subprocess.run(config['proof_argv'], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
proof_receipt = {'argv': config['proof_argv'], 'exit_code': proof.returncode,
                 'stdout_sha256': hashlib.sha256(proof.stdout).hexdigest(),
                 'stderr_sha256': hashlib.sha256(proof.stderr).hexdigest()}
proof_receipt_sha256 = hashlib.sha256(
    json.dumps(proof_receipt, sort_keys=True, separators=(',', ':')).encode()
).hexdigest()
report = {
    'version': 1, 'report_phase': 'executor_pre_terminal',
    'unit_id': config['unit_id'],
    'work_order_sha256': config['work_order_sha256'],
    'admission': config['admission'],
    'requested_route': config['requested_route'],
    'observed_route': config['observed_route'],
    'status': 'DONE' if proof.returncode == 0 else 'FAILED',
    'implementation_summary': 'Set native value to the admitted target and ran proof.',
    'changed_files': [config['file']], 'no_other_paths': True,
    'proof': {**proof_receipt, 'summary': 'targeted proof passed',
              'artifact_hashes': {'proof_receipt_sha256': proof_receipt_sha256}},
    'work_log_artifact': config['work_log_artifact'],
    'deviations': [], 'risks': [], 'follow_ups': [],
    'unresolved_questions': [],
    'no_commit_merge_push': True,
}
sys.stdout.write(json.dumps(report, sort_keys=True, separators=(',', ':')) + '\\n')
raise SystemExit(proof.returncode)
"""
            executor_config = {
                "file": "src/native.py",
                "replacement": "def value():\n    return 2\n",
                "proof_argv": proof_argv,
                "unit_id": "TASK-15-NATIVE",
                "work_order_sha256": work_order_sha256,
                "admission": {
                    "decision": "ADMIT",
                    "job_id": "native:TASK-15-NATIVE",
                    "attempt_id": "TASK-15-NATIVE-attempt-01-fix-01",
                    "receipt_id": "native-owner:TASK-15-NATIVE-attempt-01-fix-01",
                    "guard_request_sha256": successor_request_pin["sha256"],
                },
                "requested_route": {
                    "family": "codex", "model": "Sol 5.6", "effort": "ultra"
                },
                "observed_route": {
                    "family": "codex", "model": "Sol 5.6", "effort": "ultra",
                    "lane": "feature",
                },
                "work_log_artifact": {
                    "path": str(admission_progress_artifact),
                    "sha256": admission_progress_pin["sha256"],
                    "receipt_id": "native-progress-segment:fix-01:0001",
                },
            }
            executor = subprocess.run(
                [sys.executable, "-c", fake_executor_code],
                cwd=successor_repository,
                input=canonical_json(executor_config),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(executor.returncode, 0, executor.stderr)
            self.assertIsNotNone(executor.stdout)
            report = json.loads(executor.stdout)
            self.assertEqual(set(report), executor_report_fields)
            self.assertEqual(report["report_phase"], "executor_pre_terminal")
            self.assertEqual(report["status"], "DONE")
            self.assertEqual(report["proof"]["argv"], proof_argv)
            self.assertEqual(report["proof"]["exit_code"], 0)
            self.assertEqual(report["changed_files"], ["src/native.py"])
            self.assertTrue(report["no_other_paths"])
            self.assertTrue(report["no_commit_merge_push"])
            report_pin = self._publish_private_artifact(
                report_artifact, executor.stdout.encode("utf-8")
            )
            report_bytes, _ = self._read_private_artifact(
                report_artifact, expected_pin=report_pin
            )
            self.assertEqual(json.loads(report_bytes), report)
            append_progress(
                progress_events,
                "executor_report_published",
                {
                    "path": str(report_artifact),
                    "sha256": report_pin["sha256"],
                    "proof_receipt_sha256": report["proof"]["artifact_hashes"][
                        "proof_receipt_sha256"
                    ],
                    "proof_exit_code": report["proof"]["exit_code"],
                },
            )
            self.assertEqual(
                subprocess.run(
                    proof_argv,
                    cwd=successor_repository,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                ).returncode,
                0,
            )
            final_status = subprocess.run(
                ["git", "-C", str(successor_repository), "status", "--porcelain=v1"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(final_status, [" M src/native.py"])
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(successor_repository), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                head,
            )
            executor_and_descendants_quiesced = executor.returncode == 0
            self.assertTrue(executor_and_descendants_quiesced)
            append_progress(
                progress_events,
                "executor_subtree_quiesced",
                {
                    "executor_exit_code": executor.returncode,
                    "descendants_active": 0,
                },
            )
            released_successor = invoke(
                "release", "--lease-id", "TASK-15-NATIVE-attempt-01-fix-01",
                "--receipt", str(successor_receipt),
                "--revision", str(successor["reservation"]["revision"]),
            )
            self.assertEqual(released_successor["status"], "RELEASED")
            append_progress(
                progress_events,
                "successor_released",
                {
                    "lease_id": "TASK-15-NATIVE-attempt-01-fix-01",
                    "status": released_successor["status"],
                    "revision": released_successor["revision"],
                    "released_at": released_successor["updated_at"],
                    "owner_receipt_sha256": successor_receipt_pin["sha256"],
                },
            )
            final_progress_bytes = progress_bytes(progress_events)
            self.assertTrue(final_progress_bytes.startswith(admission_progress_bytes))
            final_progress_pin = self._publish_private_artifact(
                final_progress_artifact, final_progress_bytes
            )
            final_progress_read, final_progress_read_pin = (
                self._read_private_artifact(
                    final_progress_artifact, expected_pin=final_progress_pin
                )
            )
            self.assertEqual(final_progress_read, final_progress_bytes)
            self.assertEqual(final_progress_read_pin, final_progress_pin)
            terminal_release = {
                "version": 1,
                "evidence_id": "native-terminal-release:fix-01",
                "lease_id": "TASK-15-NATIVE-attempt-01-fix-01",
                "attempt_id": "TASK-15-NATIVE-attempt-01-fix-01",
                "status": released_successor["status"],
                "revision": released_successor["revision"],
                "owner_receipt_sha256": successor_receipt_pin["sha256"],
                "released_at": released_successor["updated_at"],
            }
            terminal_release_pin = self._publish_private_artifact(
                terminal_release_artifact,
                (canonical_json(terminal_release) + "\n").encode("utf-8"),
            )
            terminal_release_bytes, terminal_release_read_pin = (
                self._read_private_artifact(
                    terminal_release_artifact, expected_pin=terminal_release_pin
                )
            )
            self.assertEqual(json.loads(terminal_release_bytes), terminal_release)
            self.assertEqual(terminal_release_read_pin, terminal_release_pin)
            final_receipt = {
                "version": 1,
                "report_phase": "driver_enriched_final",
                "unit_id": "TASK-15-NATIVE",
                "work_order_sha256": work_order_sha256,
                "admission": {
                    **executor_config["admission"],
                    "receipt_path": str(successor_receipt),
                    "receipt_sha256": successor_receipt_pin["sha256"],
                },
                "requested_route": executor_config["requested_route"],
                "observed_route": executor_config["observed_route"],
                "status": "DONE",
                "implementation_summary": report["implementation_summary"],
                "changed_files": ["src/native.py"],
                "no_other_paths": True,
                "proof": report["proof"],
                "work_log_artifact": {
                    "path": str(final_progress_artifact),
                    "sha256": final_progress_pin["sha256"],
                    "receipt_id": "native-progress-log:fix-01:final",
                },
                "report_artifact": {
                    "path": str(report_artifact),
                    "sha256": report_pin["sha256"],
                    "receipt_id": "native-executor-report:fix-01",
                },
                "deviations": [],
                "risks": [],
                "follow_ups": [],
                "unresolved_questions": [],
                "no_commit_merge_push": True,
                "terminal_receipt": {
                    "kind": "native_terminal_release_artifact",
                    "receipt_id": terminal_release["evidence_id"],
                    "path": str(terminal_release_artifact),
                    "sha256": terminal_release_pin["sha256"],
                    "job_id": "native:TASK-15-NATIVE",
                    "attempt_id": "TASK-15-NATIVE-attempt-01-fix-01",
                    "status": released_successor["status"],
                    "revision": released_successor["revision"],
                },
            }
            final_receipt_pin = self._publish_private_artifact(
                final_receipt_artifact,
                (canonical_json(final_receipt) + "\n").encode("utf-8"),
            )
            final_receipt_bytes, final_receipt_read_pin = (
                self._read_private_artifact(
                    final_receipt_artifact, expected_pin=final_receipt_pin
                )
            )
            self.assertEqual(final_receipt_read_pin, final_receipt_pin)
            self.assertEqual(json.loads(final_receipt_bytes), final_receipt)
            self.assertEqual(
                set(final_receipt), self.FINAL_EXECUTION_RECEIPT_FIELDS
            )
            self.assertEqual(
                final_receipt["admission"]["job_id"], "native:TASK-15-NATIVE"
            )
            self.assertEqual(
                final_receipt["admission"]["attempt_id"],
                "TASK-15-NATIVE-attempt-01-fix-01",
            )
            self.assertEqual(
                final_receipt["proof"]["artifact_hashes"],
                report["proof"]["artifact_hashes"],
            )
            self.assertEqual(final_receipt["terminal_receipt"]["status"], "RELEASED")
            self.assertEqual(
                final_receipt["terminal_receipt"]["sha256"],
                terminal_release_pin["sha256"],
            )
            self.assertNotEqual(
                final_receipt["work_log_artifact"]["sha256"],
                successor_control_pin["sha256"],
            )
            self.assertNotEqual(
                final_receipt["terminal_receipt"]["sha256"],
                successor_receipt_pin["sha256"],
            )

    def test_native_cli_repair_refuses_allowed_untracked_dirty_file_without_release(self):
        native_module = self._native_lane_module()

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            artifacts = temporary_root / "artifacts"
            artifacts.mkdir(mode=0o700)
            artifacts.chmod(0o700)
            repository = temporary_root / "native-untracked-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Native Negative"],
                check=True,
            )
            fixture = {
                "proof.py": "raise SystemExit(0)\n",
                "PLAN.md": "# Approved plan\n\nCreate src/new.py for TASK-15-NATIVE-NEW.\n",
                "PLAN-REVIEW-LOG.json": (
                    '{"receipt_id":"act2-task-15-native-new","reviewer_family":"claude-opus",'
                    '"unit_id":"TASK-15-NATIVE-NEW","verdict":"APPROVED","version":1}\n'
                ),
                "execution-contract.md": (
                    "Change only src/new.py and run python3 proof.py. "
                    "Never commit, merge, push, rebase, or checkpoint.\n"
                ),
                "project-overlay.md": "executor_policy=codex-only\n",
            }
            for relative, content in fixture.items():
                destination = repository / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "untracked baseline"],
                check=True,
            )
            seed_wip_base(repository)
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            common_raw = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "--git-common-dir"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            common_dir = Path(common_raw)
            if not common_dir.is_absolute():
                common_dir = repository / common_dir
            repo_id = hashlib.sha256(
                canonical_json(
                    {"common_dir": str(common_dir.resolve(strict=True))}
                ).encode("utf-8")
            ).hexdigest()

            state_path = temporary_root / "state" / "state.sqlite3"
            owner_receipt = artifacts / "native-new-owner.json"
            guard_request = artifacts / "native-new-guard.json"
            guard = [
                "--verdict", "APPROVED",
                "--weight", "LIGHT",
                "--lane-type", "feature",
                "--mode", "normal",
                "--worktree", str(repository),
                "--builder", "Sol 5.6 at ultra",
                "--executor-policy", "codex-only",
                "--base-sha", head,
                "--plan-file", str(repository / "PLAN.md"),
                "--act2-review-log", str(repository / "PLAN-REVIEW-LOG.json"),
                "--execution-contract", str(repository / "execution-contract.md"),
                "--project-overlay", str(repository / "project-overlay.md"),
                "--oracle-exact", "false",
                "--proof-command", "python3 proof.py",
                "--proof-scope", "targeted",
                "--execution-profile", "sprint",
                "--backlog-id", "TASK-15-NATIVE-NEW",
                "--work-log", str(artifacts / "native-new-work.json"),
                "--report", str(artifacts / "native-new-report.json"),
                "--predecessor", "none",
                "--instruction", (
                    "Execute TASK-15-NATIVE-NEW. Change only src/new.py, run python3 "
                    "proof.py, and never commit, merge, push, rebase, or checkpoint."
                ),
                "--file", "src/new.py",
            ]
            self._publish_private_artifact(
                guard_request, (canonical_json(guard) + "\n").encode("utf-8")
            )
            reserve_status, reserve_output, reserve_error = self._invoke_native_lane(
                native_module,
                state_path,
                [
                    "reserve",
                    "--lease-id", "TASK-15-NATIVE-NEW-attempt-00",
                    "--receipt", str(owner_receipt),
                    "--guard-request", str(guard_request),
                    "--repo-id", repo_id,
                    "--base-sha", head,
                    "--lane", "feature",
                    "--weight", "LIGHT",
                    "--writer", str(repository),
                    "--file", "src/new.py",
                ],
            )
            self.assertEqual(reserve_status, 0, reserve_error)
            reservation = json.loads(reserve_output)["reservation"]
            receipt_before, receipt_pin = self._read_private_artifact(owner_receipt)

            untracked = repository / "src" / "new.py"
            untracked.parent.mkdir(parents=True, exist_ok=True)
            untracked_content = b"def value():\n    return 1\n"
            untracked.write_bytes(untracked_content)
            status_before = subprocess.run(
                [
                    "git", "-C", str(repository), "status", "--porcelain=v1",
                    "--untracked-files=all",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            dirty_paths = [line[3:] for line in status_before]
            self.assertEqual(dirty_paths, ["src/new.py"])
            tracked = subprocess.run(
                ["git", "-C", str(repository), "ls-files", "--error-unmatch", "src/new.py"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(tracked.returncode, 0)
            naive_patch = subprocess.run(
                [
                    "git", "-C", str(repository), "diff", "--binary", "--full-index",
                    "--", "src/new.py",
                ],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(naive_patch, b"")

            admitted_files = reservation["files"]
            all_dirty_are_tracked = tracked.returncode == 0
            can_continue = dirty_paths == admitted_files and all_dirty_are_tracked
            self.assertFalse(can_continue)
            decision = {
                "status": "TYPED_RECOVERY_REQUIRED",
                "action": "NEW_UNIT_OR_MANUAL_RECOVERY",
                "reason": "UNTRACKED_DIRTY_PATH",
                "paths": dirty_paths,
            }
            self.assertEqual(decision["status"], "TYPED_RECOVERY_REQUIRED")

            rows = Store(state_path).list_lane_reservations(include_released=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["lease_id"], "TASK-15-NATIVE-NEW-attempt-00")
            self.assertEqual(rows[0]["status"], "ACTIVE")
            self.assertEqual(rows[0]["revision"], reservation["revision"])
            self.assertFalse(
                any(row["lease_id"].endswith("successor") for row in rows)
            )
            receipt_after, receipt_after_pin = self._read_private_artifact(
                owner_receipt, expected_pin=receipt_pin
            )
            self.assertEqual(receipt_after_pin, receipt_pin)
            self.assertEqual(receipt_after, receipt_before)
            self.assertEqual(untracked.read_bytes(), untracked_content)
            self.assertFalse((temporary_root / "native-new-successor").exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                head,
            )
            status_after = subprocess.run(
                [
                    "git", "-C", str(repository), "status", "--porcelain=v1",
                    "--untracked-files=all",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(status_after, status_before)

    def test_native_cli_repair_refuses_staged_delta_without_release(self):
        native_module = self._native_lane_module()

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            artifacts = temporary_root / "artifacts"
            artifacts.mkdir(mode=0o700)
            artifacts.chmod(0o700)
            repository = temporary_root / "native-staged-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Native Staged"],
                check=True,
            )
            fixture = {
                "src/staged.py": "def value():\n    return 0\n",
                "proof.py": "raise SystemExit(0)\n",
                "PLAN.md": "# Approved plan\n\nModify src/staged.py.\n",
                "PLAN-REVIEW-LOG.json": (
                    '{"receipt_id":"act2-native-staged","reviewer_family":"claude-opus",'
                    '"unit_id":"TASK-15-NATIVE-STAGED","verdict":"APPROVED","version":1}\n'
                ),
                "execution-contract.md": (
                    "Change only src/staged.py and run python3 proof.py. "
                    "Never commit, merge, push, rebase, or checkpoint.\n"
                ),
                "project-overlay.md": "executor_policy=codex-only\n",
            }
            for relative, content in fixture.items():
                destination = repository / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "staged baseline"],
                check=True,
            )
            seed_wip_base(repository)
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            common_raw = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "--git-common-dir"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            common_dir = Path(common_raw)
            if not common_dir.is_absolute():
                common_dir = repository / common_dir
            repo_id = hashlib.sha256(
                canonical_json({"common_dir": str(common_dir.resolve(strict=True))}).encode(
                    "utf-8"
                )
            ).hexdigest()
            state_path = temporary_root / "state" / "state.sqlite3"
            receipt = artifacts / "staged-owner.json"
            request = artifacts / "staged-guard.json"
            guard = [
                "--verdict", "APPROVED", "--weight", "LIGHT",
                "--lane-type", "feature", "--mode", "normal",
                "--worktree", str(repository), "--builder", "Sol 5.6 at ultra",
                "--executor-policy", "codex-only", "--base-sha", head,
                "--plan-file", str(repository / "PLAN.md"),
                "--act2-review-log", str(repository / "PLAN-REVIEW-LOG.json"),
                "--execution-contract", str(repository / "execution-contract.md"),
                "--project-overlay", str(repository / "project-overlay.md"),
                "--oracle-exact", "false", "--proof-command", "python3 proof.py",
                "--proof-scope", "targeted", "--execution-profile", "sprint",
                "--backlog-id", "TASK-15-NATIVE-STAGED",
                "--work-log", str(artifacts / "staged-work.json"),
                "--report", str(artifacts / "staged-report.json"),
                "--predecessor", "none",
                "--instruction", (
                    "Change only src/staged.py, run python3 proof.py, and never commit, "
                    "merge, push, rebase, or checkpoint."
                ),
                "--file", "src/staged.py",
            ]
            self._publish_private_artifact(
                request, (canonical_json(guard) + "\n").encode("utf-8")
            )
            reserve_status, reserve_output, reserve_error = self._invoke_native_lane(
                native_module,
                state_path,
                [
                    "reserve", "--lease-id", "TASK-15-NATIVE-STAGED-attempt-00",
                    "--receipt", str(receipt), "--guard-request", str(request),
                    "--repo-id", repo_id, "--base-sha", head, "--lane", "feature",
                    "--weight", "LIGHT", "--writer", str(repository),
                    "--file", "src/staged.py",
                ],
            )
            self.assertEqual(reserve_status, 0, reserve_error)
            reservation = json.loads(reserve_output)["reservation"]
            receipt_before, receipt_pin = self._read_private_artifact(receipt)
            staged_file = repository / "src" / "staged.py"
            staged_content = b"def value():\n    return 1\n"
            staged_file.write_bytes(staged_content)
            subprocess.run(
                ["git", "-C", str(repository), "add", "--", "src/staged.py"],
                check=True,
            )
            status_before = subprocess.run(
                ["git", "-C", str(repository), "status", "--porcelain=v1"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(status_before, ["M  src/staged.py"])
            cached = subprocess.run(
                ["git", "-C", str(repository), "diff", "--cached", "--quiet", "--exit-code"],
                check=False,
            )
            self.assertEqual(cached.returncode, 1)
            unstaged_patch = subprocess.run(
                [
                    "git", "-C", str(repository), "diff", "--binary", "--full-index",
                    "--", "src/staged.py",
                ],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(unstaged_patch, b"")
            decision = {
                "status": "TYPED_RECOVERY_REQUIRED",
                "reason": "INDEX_DELTA",
            }
            self.assertEqual(decision["reason"], "INDEX_DELTA")
            rows = Store(state_path).list_lane_reservations(include_released=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "ACTIVE")
            self.assertEqual(rows[0]["revision"], reservation["revision"])
            receipt_after, receipt_after_pin = self._read_private_artifact(
                receipt, expected_pin=receipt_pin
            )
            self.assertEqual(receipt_after_pin, receipt_pin)
            self.assertEqual(receipt_after, receipt_before)
            self.assertEqual(staged_file.read_bytes(), staged_content)
            self.assertFalse((temporary_root / "native-staged-successor").exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                head,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "status", "--porcelain=v1"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.splitlines(),
                status_before,
            )

    def test_exact_baseline_is_preserved_as_honest_red_evidence(self):
        scenario = BUILD_SCENARIO_PATH.read_text(encoding="utf-8")
        normalized = " ".join(scenario.split())
        self.assertEqual(scenario.count(BUILD_BASELINE_PROMPT), 2)
        for phrase in (
            "Capture date: 2026-08-26",
            "owner-supplied retained summary",
            "not a verbatim transcript",
            "Baseline verdict: **RED**",
            "admission was conceptual prose rather than an executable reservation",
            "invented profile semantics",
            "Codex proof rerun for both profiles",
            "Post-skill verdict: **GREEN**",
            "authored characterization",
            "not an independent model replay",
            "No repository, provider, browser, command, or tool action occurred",
            "No — natural baseline behavior can define fail-closed admission",
            "provider_pre_terminal",
            "driver-enriched final execution receipt",
        ):
            self.assertIn(phrase, normalized)
        self.assertNotIn(
            "terminal_receipt: forge worker finalizes the separate hash-bound receipt",
            scenario,
        )

    def test_actual_forge_service_build_replay_admits_and_resumes_same_session(self):
        scenario = BUILD_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable offline ForgeService build replay\n\n```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))

        def sha256_file(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            driver_artifacts = temporary_root / "driver-artifacts"
            driver_artifacts.mkdir(mode=0o700)
            driver_artifacts.chmod(0o700)
            repository = temporary_root / "build-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Build Replay"],
                check=True,
            )
            for relative, content in replay["repo_fixture"].items():
                destination = repository / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "build baseline"],
                check=True,
            )
            seed_wip_base(repository)
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            proof_provider = temporary_root / "fake-claude-with-proof.py"
            proof_provider.write_text(
                "#!/usr/bin/env python3\n"
                "import hashlib, json, os, subprocess, sys\n"
                "from pathlib import Path\n"
                f"INNER = {str(FAKE_CLAUDE)!r}\n"
                "if '--version' in sys.argv[1:]:\n"
                "    completed = subprocess.run([INNER, *sys.argv[1:]], check=False)\n"
                "    raise SystemExit(completed.returncode)\n"
                "frame = sys.stdin.buffer.readline()\n"
                "inner = subprocess.run([INNER, *sys.argv[1:]], input=frame, "
                "stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)\n"
                "control_path = Path(os.environ['TMPDIR']) / "
                f"{FAKE_CONTROL_NAME!r}\n"
                "control = json.loads(control_path.read_text(encoding='utf-8'))\n"
                "argv = control['proof_argv']\n"
                "proof = subprocess.run(argv, cwd=Path.cwd(), stdin=subprocess.DEVNULL, "
                "stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)\n"
                "receipt = {'argv': argv, 'exit_code': proof.returncode, "
                "'receipt_id': control['proof_receipt_id'], "
                "'stdout_sha256': hashlib.sha256(proof.stdout).hexdigest(), "
                "'stderr_sha256': hashlib.sha256(proof.stderr).hexdigest()}\n"
                "receipt_bytes = (json.dumps(receipt, sort_keys=True, "
                "separators=(',', ':')) + '\\n').encode('utf-8')\n"
                "Path(control['proof_artifact']).write_bytes(receipt_bytes)\n"
                "proof_hash = hashlib.sha256(receipt_bytes).hexdigest()\n"
                "output = inner.stdout.replace(b'$PROOF_EXIT_CODE', "
                "str(proof.returncode).encode('ascii'))\n"
                "output = output.replace(b'$PROOF_ARTIFACT_SHA256', "
                "proof_hash.encode('ascii'))\n"
                "output = output.replace(b'$PROOF_RECEIPT_ID', "
                "control['proof_receipt_id'].encode('utf-8'))\n"
                "sys.stdout.buffer.write(output)\n"
                "sys.stdout.buffer.flush()\n"
                "sys.stderr.buffer.write(inner.stderr)\n"
                "sys.stderr.buffer.flush()\n"
                "raise SystemExit(inner.returncode)\n",
                encoding="utf-8",
            )
            proof_provider.chmod(0o700)

            state_path = temporary_root / "state" / "state.sqlite3"
            argv_log = temporary_root / "argv.jsonl"
            stdin_log = temporary_root / "stdin.jsonl"
            environment_log = temporary_root / "environment.jsonl"
            control_path = temporary_root / FAKE_CONTROL_NAME

            def write_control(round_index):
                control_path.write_text(
                    json.dumps(
                        {
                            "argv_log": str(argv_log),
                            "stdin_log": str(stdin_log),
                            "environment_log": str(environment_log),
                            "assistant_text": replay["round_report_templates"][round_index],
                            "mutate_files": replay["round_mutations"][round_index],
                            "proof_argv": replay["proof_argv"],
                            "proof_artifact": str(
                                temporary_root / replay["proof_artifacts"][round_index]
                            ),
                            "proof_receipt_id": replay["proof_receipt_ids"][round_index],
                            "parent_tool_use_id": "forge-build-parent",
                            "control_window_seconds": 0.05,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )

            write_control(0)
            environment = {
                "HOME": str(temporary_root),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "LC_CTYPE": "C.UTF-8",
                "TMPDIR": str(temporary_root),
                "SHELL": "/bin/sh",
                "USER": "forge-build-test",
                "FORGE_STATE_PATH": str(state_path),
                "FORGE_LAUNCH_WORKSPACE": str(repository),
                "FORGE_CLAUDE_EXECUTABLE": str(proof_provider),
            }
            service = ForgeService(
                state_path=state_path,
                launch_workspace=repository,
                claude_executable=str(proof_provider),
                bridge_path=ADMISSION_BRIDGE,
                environment=environment,
                _admission_machine_probe=machine_headroom,
            )
            preflight = service.preflight({"cwd": str(repository)}).body
            self.assertEqual(preflight["status"], "ready")
            self.assertFalse(preflight["provider_called"])

            controls = {
                "plan": repository / "PLAN.md",
                "review_log": repository / "PLAN-REVIEW-LOG.json",
                "execution_contract": repository / "execution-contract.md",
                "project_overlay": repository / "project-overlay.md",
            }
            admission = {
                "weight": "LIGHT",
                "proof_scope": "targeted",
                "execution_profile": "sprint",
                "proof_argv": replay["proof_argv"],
                "files": ["src/unit.py"],
                "base_sha": head,
                "verdict": "APPROVED",
                "backlog_ids": ["TASK-15"],
                "controls": {
                    name: {"path": str(path), "sha256": sha256_file(path)}
                    for name, path in controls.items()
                },
                "oracle": {"exact": False},
            }
            work_order = {
                "version": 1,
                "unit_id": "TASK-15",
                "baseline": head,
                "files": admission["files"],
                "proof_argv": admission["proof_argv"],
                "profile": admission["execution_profile"],
                "route": {"family": "claude", "model": "fable", "effort": "max"},
                "report_fields": sorted(self.FINAL_EXECUTION_RECEIPT_FIELDS),
            }
            work_order_sha256 = hashlib.sha256(
                canonical_json(work_order).encode("utf-8")
            ).hexdigest()
            start_arguments = {
                "prompt": replay["start_prompt"],
                "cwd": str(repository),
                "role": "build",
                "model": "fable",
                "effort": "max",
                "idempotency_key": replay["start_idempotency_key"],
                "budget": {
                    "max_seconds": 10,
                    "max_tokens": 4096,
                    "max_turns": 8,
                    "max_cost_usd": "1.00",
                },
                "admission": admission,
            }
            validate_tool_arguments("forge_start", start_arguments)
            started = service.start(start_arguments).body
            job_id = started["job_id"]

            def wait_terminal(revision):
                deadline = time.monotonic() + 10
                last = None
                while time.monotonic() < deadline:
                    last = service.wait(
                        {"job_id": job_id, "revision": revision, "timeout_ms": 1000}
                    ).body
                    revision = last["revision"]
                    if last["status"] in {
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "PAUSED_PROVIDER_ACCESS",
                        "PAUSED_POLICY",
                    }:
                        return last
                self.fail(f"actual ForgeService build did not terminate: {last}")

            def proof_receipt(round_index):
                path = temporary_root / replay["proof_artifacts"][round_index]
                self.assertTrue(path.is_file(), path)
                receipt_bytes = path.read_bytes()
                receipt = json.loads(receipt_bytes)
                self.assertEqual(receipt["argv"], admission["proof_argv"])
                self.assertEqual(
                    receipt["receipt_id"], replay["proof_receipt_ids"][round_index]
                )
                self.assertEqual(
                    hashlib.sha256(receipt_bytes).hexdigest(),
                    replay["proof_artifact_sha256"][round_index],
                )
                return receipt

            def assert_pre_terminal_provider_report(report, expected_status):
                for field in (
                    "report_phase",
                    "unit_id",
                    "requested_route",
                    "observed_route",
                    "status",
                    "implementation_summary",
                    "changed_files",
                    "no_other_paths",
                    "proof_argv",
                    "proof_exit_code",
                    "proof_summary",
                    "proof_artifact_sha256",
                    "proof_receipt_id",
                    "deviations",
                    "risks",
                    "follow_ups",
                    "unresolved_questions",
                    "no_commit_merge_push",
                ):
                    self.assertIn(f"{field}:", report)
                self.assertIn("report_phase: provider_pre_terminal", report)
                self.assertIn(f"status: {expected_status}", report)
                self.assertIn("requested_route: claude/fable/max", report)
                self.assertIn("observed_route: claude/fable/max", report)
                self.assertIn("no_other_paths: true", report)
                self.assertIn("no_commit_merge_push: true", report)
                self.assertNotIn("terminal_receipt:", report)

            def driver_enriched_receipt(
                *, round_index, result, terminal, proof, expected_status
            ):
                attempt_id = terminal["attempt_id"]
                terminal_artifact = result["terminal_receipt"]
                terminal_path = Path(terminal_artifact["path"])
                self.assertTrue(terminal_path.is_file())
                terminal_payload = json.loads(terminal_path.read_text(encoding="utf-8"))
                self.assertEqual(terminal_payload["job_id"], job_id)
                self.assertEqual(terminal_payload["attempt_id"], attempt_id)
                self.assertEqual(terminal_payload["session_id"], terminal["session_id"])
                evidence = service.store.get_attempt_admission(attempt_id)
                report_artifact = result["artifacts"]["report"]
                worker_log_artifact = result["artifacts"]["worker_log"]
                proof_path = temporary_root / replay["proof_artifacts"][round_index]
                final_receipt = {
                    "version": 1,
                    "report_phase": "driver_enriched_final",
                    "unit_id": "TASK-15",
                    "work_order_sha256": work_order_sha256,
                    "admission": {
                        "decision": evidence["result"]["decision"],
                        "job_id": job_id,
                        "attempt_id": attempt_id,
                        "receipt_id": f"forge-admission:{attempt_id}",
                        "evidence_sha256": result["admission_evidence_sha256"],
                    },
                    "requested_route": {
                        "family": "claude", "model": "fable", "effort": "max"
                    },
                    "observed_route": {
                        "family": "claude", "model": "fable", "effort": "max",
                        "session_id": terminal["session_id"],
                    },
                    "status": expected_status,
                    "implementation_summary": (
                        "Changed the admitted file; targeted proof remains red."
                        if expected_status == "FAILED"
                        else "Changed the admitted file and passed the targeted proof."
                    ),
                    "changed_files": ["src/unit.py"],
                    "no_other_paths": True,
                    "proof": {
                        "argv": proof["argv"],
                        "exit_code": proof["exit_code"],
                        "summary": (
                            "targeted proof failed"
                            if proof["exit_code"]
                            else "targeted proof passed"
                        ),
                        "receipt_id": proof["receipt_id"],
                        "artifact_hashes": {
                            "proof_receipt_sha256": sha256_file(proof_path),
                            "stdout_sha256": proof["stdout_sha256"],
                            "stderr_sha256": proof["stderr_sha256"],
                        },
                    },
                    "work_log_artifact": {
                        "path": worker_log_artifact["path"],
                        "sha256": worker_log_artifact["sha256"],
                        "receipt_id": f"forge-worker-log:{attempt_id}",
                    },
                    "report_artifact": {
                        "path": report_artifact["path"],
                        "sha256": report_artifact["sha256"],
                        "receipt_id": f"forge-provider-report:{attempt_id}",
                    },
                    "deviations": [],
                    "risks": (
                        ["bounded proof defect remains"]
                        if expected_status == "FAILED"
                        else []
                    ),
                    "follow_ups": (
                        ["resume the same admitted job for one bounded repair"]
                        if expected_status == "FAILED"
                        else []
                    ),
                    "unresolved_questions": [],
                    "no_commit_merge_push": True,
                    "terminal_receipt": {
                        "kind": "forge_terminal_receipt",
                        "receipt_id": f"forge-terminal:{attempt_id}",
                        "path": terminal_artifact["path"],
                        "sha256": terminal_artifact["sha256"],
                        "job_id": job_id,
                        "attempt_id": attempt_id,
                        "session_id": terminal["session_id"],
                        "status": terminal["status"],
                    },
                }
                final_path = (
                    driver_artifacts / f"claude-round-{round_index + 1}-final.json"
                )
                final_pin = self._publish_private_artifact(
                    final_path,
                    (canonical_json(final_receipt) + "\n").encode("utf-8"),
                )
                final_bytes, read_pin = self._read_private_artifact(
                    final_path, expected_pin=final_pin
                )
                self.assertEqual(read_pin, final_pin)
                self.assertEqual(json.loads(final_bytes), final_receipt)
                self.assertEqual(
                    set(final_receipt), self.FINAL_EXECUTION_RECEIPT_FIELDS
                )
                self.assertEqual(final_receipt["admission"]["job_id"], job_id)
                self.assertEqual(
                    final_receipt["admission"]["attempt_id"], attempt_id
                )
                self.assertEqual(
                    final_receipt["proof"]["artifact_hashes"][
                        "proof_receipt_sha256"
                    ],
                    replay["proof_artifact_sha256"][round_index],
                )
                self.assertEqual(
                    final_receipt["terminal_receipt"]["sha256"],
                    result["artifacts"]["receipt"]["sha256"],
                )
                return final_receipt, final_pin

            terminal_one = wait_terminal(started["revision"])
            self.assertEqual(terminal_one["status"], "SUCCEEDED", terminal_one)
            result_one = service.result(
                {"job_id": job_id, "revision": terminal_one["revision"]}
            ).body
            self.assertEqual(result_one["report"], replay["round_reports"][0])
            assert_pre_terminal_provider_report(result_one["report"], "FAILED")
            proof_one = proof_receipt(0)
            self.assertEqual(proof_one["exit_code"], 1)
            self.assertEqual(
                result_one["artifacts"]["report"]["sha256"],
                hashlib.sha256(result_one["report"].encode("utf-8")).hexdigest(),
            )
            self.assertIn(replay["proof_artifact_sha256"][0], result_one["report"])
            self.assertIn(replay["proof_receipt_ids"][0], result_one["report"])
            attempt_one = terminal_one["attempt_id"]
            session_id = terminal_one["session_id"]
            evidence_one = service.store.get_attempt_admission(attempt_one)
            reservation_one = service.store.get_bridge_lane_reservation(job_id)
            self.assertEqual(evidence_one["result"]["decision"], "ADMIT")
            self.assertEqual(evidence_one["driver"], "codex")
            self.assertEqual(evidence_one["integration_authority"], "codex")
            self.assertEqual(reservation_one["files"], ["src/unit.py"])
            self.assertEqual(
                (repository / "src" / "unit.py").read_text(encoding="utf-8"),
                replay["round_mutations"][0]["src/unit.py"],
            )
            final_one, final_one_pin = driver_enriched_receipt(
                round_index=0,
                result=result_one,
                terminal=terminal_one,
                proof=proof_one,
                expected_status="FAILED",
            )
            self.assertEqual(final_one["status"], "FAILED")
            self.assertEqual(final_one_pin["mode"], 0o600)

            write_control(1)
            dirty_file = repository / "src" / "unit.py"
            resume_arguments = {
                "job_id": job_id,
                "revision": terminal_one["revision"],
                "prompt": replay["resume_prompt"],
                "idempotency_key": replay["resume_idempotency_key"],
                "refreeze": {
                    "baseline_head": head,
                    "dirty_files": [
                        {"path": "src/unit.py", "sha256": sha256_file(dirty_file)}
                    ],
                },
            }
            validate_tool_arguments("forge_resume", resume_arguments)
            resumed = service.resume(resume_arguments).body
            self.assertEqual(resumed["job_id"], job_id)
            self.assertEqual(resumed["resume_session_id"], session_id)
            self.assertNotEqual(resumed["attempt_id"], attempt_one)
            terminal_two = wait_terminal(resumed["revision"])
            self.assertEqual(terminal_two["status"], "SUCCEEDED", terminal_two)
            self.assertEqual(terminal_two["session_id"], session_id)
            result_two = service.result(
                {"job_id": job_id, "revision": terminal_two["revision"]}
            ).body
            self.assertEqual(result_two["report"], replay["round_reports"][1])
            assert_pre_terminal_provider_report(result_two["report"], "DONE")
            proof_two = proof_receipt(1)
            self.assertEqual(proof_two["exit_code"], 0)
            self.assertEqual(
                result_two["artifacts"]["report"]["sha256"],
                hashlib.sha256(result_two["report"].encode("utf-8")).hexdigest(),
            )
            self.assertIn(replay["proof_artifact_sha256"][1], result_two["report"])
            self.assertIn(replay["proof_receipt_ids"][1], result_two["report"])
            evidence_two = service.store.get_attempt_admission(terminal_two["attempt_id"])
            self.assertEqual(evidence_two["result"]["decision"], "ADMIT")
            self.assertEqual(evidence_two["resume"]["parent_attempt_id"], attempt_one)
            self.assertEqual(
                (repository / "src" / "unit.py").read_text(encoding="utf-8"),
                replay["round_mutations"][1]["src/unit.py"],
            )
            final_two, final_two_pin = driver_enriched_receipt(
                round_index=1,
                result=result_two,
                terminal=terminal_two,
                proof=proof_two,
                expected_status="DONE",
            )
            self.assertEqual(final_two["status"], "DONE")
            self.assertEqual(final_two_pin["mode"], 0o600)

            provider_argv = [
                json.loads(line)
                for line in argv_log.read_text(encoding="utf-8").splitlines()
                if "--input-format" in json.loads(line)
            ]
            self.assertEqual(len(provider_argv), 2)
            self.assertEqual(
                provider_argv[0][provider_argv[0].index("--session-id") + 1],
                session_id,
            )
            self.assertEqual(
                provider_argv[1][provider_argv[1].index("--resume") + 1],
                session_id,
            )
            user_frames = [
                json.loads(line)
                for line in stdin_log.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("type") == "user"
            ]
            self.assertEqual(
                [frame["message"]["content"][0]["text"] for frame in user_frames],
                [replay["start_prompt"], replay["resume_prompt"]],
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                head,
            )
            dirty = subprocess.run(
                ["git", "-C", str(repository), "status", "--porcelain=v1"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual([line[3:] for line in dirty], ["src/unit.py"])

    def test_scenario_source_and_response_fingerprints_match_final_bytes(self):
        scenario = BUILD_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable offline ForgeService build replay\n\n```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))
        expected_sources = [
            "scripts/forge_lib/contracts.py",
            "scripts/forge_lib/service.py",
            "scripts/forge_lib/state.py",
            "scripts/forge_lib/worker.py",
            "skills/forge-build/SKILL.md",
            "skills/forge-build/agents/openai.yaml",
        ]
        self.assertEqual(replay["source_paths"], expected_sources)
        digest = hashlib.sha256()
        for relative in expected_sources:
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update((PLUGIN_ROOT / relative).read_bytes() + b"\0")
        self.assertEqual(replay["source_sha256"], digest.hexdigest())

        response_match = re.search(
            r"<!-- POST_BUILD_RESPONSE_BEGIN -->\n(.*?)\n<!-- POST_BUILD_RESPONSE_END -->",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(response_match)
        self.assertEqual(
            replay["post_response_sha256"],
            hashlib.sha256(response_match.group(1).encode("utf-8")).hexdigest(),
        )


class ForgeOrchestrateSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (ORCHESTRATE_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.normalized_skill = " ".join(cls.skill.split())

    @staticmethod
    def _sha256_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _repository_id(worktree: Path) -> str:
        common = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = worktree / common_path
        return hashlib.sha256(
            canonical_json(
                {"common_dir": str(common_path.resolve(strict=True))}
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _write_private_json(path: Path, value) -> None:
        path.write_text(canonical_json(value) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def test_required_files_links_and_trigger_metadata(self):
        required = {"SKILL.md", "agents/openai.yaml"}
        missing = sorted(
            path for path in required if not (ORCHESTRATE_SKILL_ROOT / path).is_file()
        )
        self.assertEqual(missing, [])
        self.assertTrue(ORCHESTRATE_SCENARIO_PATH.is_file(), ORCHESTRATE_SCENARIO_PATH)

        metadata = frontmatter(self.skill)
        self.assertEqual(metadata.get("name"), "forge-orchestrate")
        description = metadata.get("description", "")
        self.assertTrue(description.startswith("Use when "), description)
        self.assertLess(len(description), 500)
        self.assertNotRegex(
            description.lower(),
            r"\b(first|then|finally|decompose|route|admit|review|integrate|commit)\b",
        )

        missing_links = []
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", self.skill):
            target = target.split("#", 1)[0]
            if target and not re.match(r"^[a-z]+://", target):
                resolved = (ORCHESTRATE_SKILL_ROOT / target).resolve()
                if not resolved.exists():
                    missing_links.append(target)
        self.assertEqual(missing_links, [])

    def test_openai_metadata_uses_supported_top_level_policy(self):
        metadata_path = ORCHESTRATE_SKILL_ROOT / "agents" / "openai.yaml"
        self.assertTrue(metadata_path.is_file(), metadata_path)
        self.assertEqual(
            metadata_path.read_text(encoding="utf-8"),
            'interface:\n'
            '  display_name: "Forge Orchestrate"\n'
            '  short_description: "Coordinate approved multi-model build units"\n'
            '  default_prompt: "Use $forge-orchestrate to execute this approved complex plan across isolated Codex and Claude lanes."\n'
            'policy:\n'
            '  allow_implicit_invocation: true\n',
        )

    def test_standalone_act_four_builds_a_dependency_safe_dag(self):
        for phrase in (
            "standalone Act 4",
            "approved complex plan",
            "directed acyclic graph",
            "reject cycles",
            "predecessors",
            "report-consumption receipt",
            "topological order",
            "shared file",
            "add an edge or combine",
            "disjoint exact file sets",
            "successor baseline",
            "unit does not reach the scheduler",
            "feasibility and capability",
            "infeasible dependency keeps its descendants unready",
        ):
            self.assertIn(phrase, self.normalized_skill)

    def test_actual_shared_scheduler_and_isolated_writer_contract_is_explicit(self):
        for phrase in (
            "one isolated worktree per concurrent unit",
            "one writer per worktree",
            "maximum of three active lanes total across Codex and Claude",
            "one machine-wide HEAVY lease",
            "shared `lane_reservations` ledger",
            "atomic admission",
            "fresh scheduler snapshot",
            "`ADMIT`",
            "scripts/forge-native-lane.py",
            "`reserve` before native delegation",
            "`heartbeat` while active",
            "`release` only after subtree quiescence",
            "role=`build` `forge_start`",
            "independent job ID and revision",
            "prose is not admission",
        ):
            self.assertIn(phrase, self.normalized_skill)
        lowered = self.skill.lower()
        self.assertNotRegex(lowered, r"(?m)^\s*(?:\$\s*)?claude\s+--?[a-z]")
        self.assertNotRegex(lowered, r"\b(?:latest|most recent|implicit) session\b")

    def test_profiles_reviews_gate_two_and_integration_are_not_conflated(self):
        for phrase in (
            "Sprint never waits at Gate 2",
            "no per-unit cross-model review",
            "no Codex proof rerun",
            "passing smallest relevant proof",
            "one comprehensive sprint-end gate",
            "Assured plus complex",
            "Gate 2",
            "genuine other-family review receipt",
            "Claude-built unit receives native Codex review",
            "Codex-built unit receives read-only Claude review",
            "same-model review never qualifies",
            "Codex reviews the complete diff",
            "Codex reruns the proof",
            "Only Codex integrates",
            "one unit at a time",
            "topological dependency order",
            "executor never commits, merges, or pushes",
        ):
            self.assertIn(phrase, self.normalized_skill)

    def test_liveness_recovery_and_repairs_are_bounded(self):
        for phrase in (
            "objective or artifact progress",
            "not narration",
            "`forge_status`",
            "bounded `forge_wait`",
            "STALLED",
            "STOPPED",
            "provider or policy pause",
            "never race a live writer",
            "MAX_FIX_ROUNDS",
            "same explicit session",
            "`forge_resume`",
            "exact fix prompt",
            "fresh `ADMIT`",
            "fix-round exhaustion",
            "affected descendants",
            "No model or provider substitution",
        ):
            self.assertIn(phrase, self.normalized_skill)

    def test_exact_baseline_is_preserved_as_honest_red_evidence(self):
        scenario = ORCHESTRATE_SCENARIO_PATH.read_text(encoding="utf-8")
        normalized = " ".join(scenario.split())
        self.assertEqual(scenario.count(ORCHESTRATE_BASELINE_PROMPT), 2)
        for phrase in (
            "Capture date: 2026-08-26",
            "owner-supplied retained summary",
            "not a verbatim transcript",
            "No repository, provider, browser, command, or network action occurred",
            "Baseline verdict: **RED**",
            "atomic conceptual admission",
            "invented Sprint",
            "Gate 2 had no authoritative meaning",
            "No—this baseline can voluntarily enforce shared three-lane/one-heavy/one-writer/disjoint-files behavior",
            "Post-skill verdict: **GREEN**",
            "authored characterization",
            "not an independent model replay",
            "No live Claude/provider or network call was made",
            "Authored expected Act-4 graph",
            "non-runtime characterization",
            "Executable observed shared-scheduler trace",
            "does not claim that the runtime executed",
        ):
            self.assertIn(phrase, normalized)

    def test_authored_expected_graph_is_acyclic_isolated_and_profile_correct(self):
        scenario = ORCHESTRATE_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Authored expected Act-4 graph\n\n.*?```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))
        self.assertEqual(
            replay["evidence_kind"], "authored_non_runtime_characterization"
        )
        self.assertFalse(replay["runtime_executed"])
        units = replay["units"]
        self.assertEqual(len(units), 4)
        by_id = {unit["id"]: unit for unit in units}
        self.assertEqual(len(by_id), 4)
        self.assertEqual(len({unit["worktree"] for unit in units}), 4)
        self.assertTrue(all(unit["max_fix_rounds"] == 2 for unit in units))

        predecessors = {
            unit_id: set(unit["predecessors"]) for unit_id, unit in by_id.items()
        }
        self.assertTrue(
            all(parent in by_id for parents in predecessors.values() for parent in parents)
        )
        resolved = []
        remaining = {unit_id: set(parents) for unit_id, parents in predecessors.items()}
        while remaining:
            ready = sorted(
                unit_id for unit_id, parents in remaining.items() if not parents
            )
            self.assertTrue(ready, "unit graph contains a cycle")
            resolved.extend(ready)
            for unit_id in ready:
                remaining.pop(unit_id)
            for parents in remaining.values():
                parents.difference_update(ready)
        self.assertEqual(set(resolved), set(by_id))

        def reaches(source, target):
            frontier = [target]
            seen = set()
            while frontier:
                current = frontier.pop()
                if current == source:
                    return True
                if current in seen:
                    continue
                seen.add(current)
                frontier.extend(predecessors[current])
            return False

        for left_index, left in enumerate(units):
            for right in units[left_index + 1 :]:
                if set(left["files"]) & set(right["files"]):
                    self.assertTrue(
                        reaches(left["id"], right["id"])
                        or reaches(right["id"], left["id"]),
                        f"overlap is not serialized: {left['id']} {right['id']}",
                    )

        self.assertEqual(set(replay["ready_waves"][0]), {"U1", "U2", "U3"})
        self.assertEqual(replay["ready_waves"][1], ["U4"])
        integrated_before_wave = set()
        for wave in replay["ready_waves"]:
            self.assertLessEqual(
                len(wave), replay["expected_scheduler"]["max_active_lanes"]
            )
            files = []
            worktrees = []
            heavy = 0
            for unit_id in wave:
                unit = by_id[unit_id]
                self.assertTrue(
                    predecessors[unit_id].issubset(integrated_before_wave),
                    f"{unit_id} reached the scheduler before its predecessors integrated",
                )
                files.extend(unit["files"])
                worktrees.append(unit["worktree"])
                heavy += unit["weight"] == "HEAVY"
            self.assertEqual(len(files), len(set(files)))
            self.assertEqual(len(worktrees), len(set(worktrees)))
            self.assertLessEqual(
                heavy, replay["expected_scheduler"]["max_heavy_leases"]
            )
            integrated_before_wave.update(wave)

        admission_preconditions = {
            record["unit_id"]: record
            for record in replay["expected_admission_preconditions"]
        }
        self.assertEqual(set(admission_preconditions), set(by_id))
        for unit_id, record in admission_preconditions.items():
            self.assertEqual(
                set(record["predecessors_integrated_before_admission"]),
                predecessors[unit_id],
            )
            self.assertEqual(
                set(record["receipts_consumed_before_admission"]),
                predecessors[unit_id],
            )
            self.assertEqual(
                set(record["baseline_refrozen_after"]), predecessors[unit_id]
            )

        integration = replay["expected_serial_integration_order"]
        self.assertEqual(set(integration), set(by_id))
        self.assertEqual(len(integration), len(set(integration)))
        integrated = set()
        for unit_id in integration:
            self.assertTrue(predecessors[unit_id].issubset(integrated))
            integrated.add(unit_id)

        sprint = replay["expected_profiles"]["sprint"]
        self.assertFalse(sprint["gate_2"])
        self.assertFalse(sprint["per_unit_cross_model_review"])
        self.assertFalse(sprint["codex_proof_rerun"])
        self.assertEqual(sprint["final_gate_count"], 1)
        assured = replay["expected_profiles"]["assured_complex"]
        self.assertTrue(assured["gate_2"])
        self.assertTrue(assured["codex_complete_diff_and_proof_rerun"])
        for unit in units:
            expected_reviewer = "codex" if unit["family"] == "claude" else "claude"
            self.assertEqual(assured["reviewer_family_by_unit"][unit["id"]], expected_reviewer)
        self.assertEqual(replay["expected_integration_authority"], "codex")
        self.assertTrue(replay["expected_actual_admission_required"])
        self.assertEqual(
            replay["expected_liveness_progress"], "objective_or_artifact"
        )

    def test_production_scheduler_shares_bridge_and_native_reservations(self):
        scenario = ORCHESTRATE_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable observed shared-scheduler trace\n\n.*?```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        observed_trace = json.loads(match.group(1))
        self.assertEqual(
            observed_trace["evidence_kind"],
            "observed_production_components_offline",
        )
        self.assertEqual(
            observed_trace["not_observed"],
            [
                "U1 or U2 integration",
                "U4 admission or successor refreeze",
                "cross-model review receipts",
                "Gate 2",
                "serial Codex integration",
            ],
        )
        expected_observations = observed_trace["observations"]
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            repository = temporary_root / "orchestration-source"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Forge Orchestrate Replay"],
                check=True,
            )
            fixture = {
                "PLAN.md": "# Approved complex plan\n\nExecute ORCH-GRAPH in four units.\n",
                "PLAN-REVIEW-LOG.json": (
                    '{"receipt_id":"act2-orch","reviewer_family":"claude-fable",'
                    '"unit_id":"ORCH-GRAPH","verdict":"APPROVED","version":1}\n'
                ),
                "execution-contract.md": (
                    "Change only the admitted exact file, run python3 proof.py, and "
                    "never commit, merge, push, cherry-pick, rebase, or checkpoint.\n"
                ),
                "project-overlay-claude.md": "executor_policy=claude-only\n",
                "project-overlay-native.md": "executor_policy=codex-only\n",
                "proof.py": "raise SystemExit(0)\n",
                "src/claude.py": "VALUE = 0\n",
                "src/native-heavy.py": "VALUE = 0\n",
                "src/native-light.py": "VALUE = 0\n",
                "src/heavy-collision.py": "VALUE = 0\n",
                "src/writer-collision.py": "VALUE = 0\n",
                "src/fourth.py": "VALUE = 0\n",
            }
            for relative, content in fixture.items():
                destination = repository / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "orchestration baseline"],
                check=True,
            )
            seed_wip_base(repository)
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            worktrees = {}
            for name in (
                "claude",
                "native-heavy",
                "native-light",
                "heavy-collision",
                "file-overlap",
                "fourth",
            ):
                path = temporary_root / f"worktree-{name}"
                subprocess.run(
                    ["git", "-C", str(repository), "worktree", "add", "--detach", str(path), head],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                worktrees[name] = path.resolve(strict=True)

            artifacts = temporary_root / "artifacts"
            artifacts.mkdir(mode=0o700)
            artifacts.chmod(0o700)
            state_path = temporary_root / "state" / "state.sqlite3"
            fake_control = temporary_root / FAKE_CONTROL_NAME
            self._write_private_json(
                fake_control,
                {
                    "assistant_text": "offline orchestration scheduler holder",
                    "control_window_seconds": 5,
                    "argv_log": str(artifacts / "fake-argv.jsonl"),
                    "stdin_log": str(artifacts / "fake-stdin.jsonl"),
                    "environment_log": str(artifacts / "fake-environment.jsonl"),
                },
            )
            environment = {
                "HOME": str(temporary_root),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "LC_CTYPE": "C.UTF-8",
                "TMPDIR": str(temporary_root),
                "SHELL": "/bin/sh",
                "USER": "forge-orchestrate-test",
                "FORGE_STATE_PATH": str(state_path),
                "FORGE_LAUNCH_WORKSPACE": str(repository),
                "FORGE_CLAUDE_EXECUTABLE": str(FAKE_CLAUDE),
            }
            services = {
                name: ForgeService(
                    state_path=state_path,
                    launch_workspace=worktree,
                    claude_executable=str(FAKE_CLAUDE),
                    bridge_path=ADMISSION_BRIDGE,
                    environment={
                        **environment,
                        "FORGE_LAUNCH_WORKSPACE": str(worktree),
                    },
                    _admission_machine_probe=machine_headroom,
                )
                for name, worktree in worktrees.items()
            }
            service = services["claude"]
            self.assertFalse(
                service.preflight({"cwd": str(worktrees["claude"])}).body[
                    "provider_called"
                ]
            )
            repo_id = self._repository_id(repository)
            native_module = ForgeBuildSkillContractTests._native_lane_module()
            native_receipts = {}

            def controls(worktree):
                paths = {
                    "plan": worktree / "PLAN.md",
                    "review_log": worktree / "PLAN-REVIEW-LOG.json",
                    "execution_contract": worktree / "execution-contract.md",
                    "project_overlay": worktree / "project-overlay-claude.md",
                }
                return {
                    name: {"path": str(path), "sha256": self._sha256_file(path)}
                    for name, path in paths.items()
                }

            def admission(worktree, unit_id, relative, weight):
                return {
                    "weight": weight,
                    "proof_scope": "targeted",
                    "execution_profile": "sprint",
                    "proof_argv": ["python3", "proof.py"],
                    "files": [relative],
                    "base_sha": head,
                    "verdict": "APPROVED",
                    "backlog_ids": ["ORCH-GRAPH", unit_id],
                    "controls": controls(worktree),
                    "oracle": {"exact": False},
                }

            def start_bridge(name, unit_id, relative, weight="LIGHT"):
                worktree = worktrees[name]
                return services[name].start(
                    {
                        "prompt": (
                            f"Execute {unit_id} only in {relative}; run python3 proof.py; "
                            "never commit, merge, push, cherry-pick, rebase, or checkpoint."
                        ),
                        "cwd": str(worktree),
                        "role": "build",
                        "model": "fable",
                        "effort": "max",
                        "idempotency_key": f"forge-orchestrate-{unit_id}",
                        "budget": {
                            "max_seconds": 20,
                            "max_tokens": 4096,
                            "max_turns": 8,
                            "max_cost_usd": "1.00",
                        },
                        "admission": admission(worktree, unit_id, relative, weight),
                    }
                ).body

            def native_guard(name, unit_id, relative, weight):
                worktree = worktrees[name]
                request = artifacts / f"{unit_id}-guard.json"
                receipt = artifacts / f"{unit_id}-owner.json"
                guard = [
                    "--verdict", "APPROVED",
                    "--weight", weight,
                    "--lane-type", "feature",
                    "--mode", "normal",
                    "--worktree", str(worktree),
                    "--builder", "Sol 5.6 at ultra",
                    "--executor-policy", "codex-only",
                    "--base-sha", head,
                    "--plan-file", str(worktree / "PLAN.md"),
                    "--act2-review-log", str(worktree / "PLAN-REVIEW-LOG.json"),
                    "--execution-contract", str(worktree / "execution-contract.md"),
                    "--project-overlay", str(worktree / "project-overlay-native.md"),
                    "--oracle-exact", "false",
                    "--proof-command", "python3 proof.py",
                    "--proof-scope", "targeted",
                    "--execution-profile", "sprint",
                    "--backlog-id", "ORCH-GRAPH",
                    "--work-log", str(artifacts / f"{unit_id}-work.jsonl"),
                    "--report", str(artifacts / f"{unit_id}-report.json"),
                    "--predecessor", "none",
                    "--instruction", (
                        f"Execute {unit_id} only in {relative}; run python3 proof.py; "
                        "never commit, merge, push, cherry-pick, rebase, or checkpoint."
                    ),
                    "--file", relative,
                ]
                self._write_private_json(request, guard)
                return worktree, request, receipt

            def reserve_native(name, unit_id, relative, weight):
                worktree, request, receipt = native_guard(
                    name, unit_id, relative, weight
                )
                arguments = [
                    "reserve",
                    "--lease-id", unit_id,
                    "--receipt", str(receipt),
                    "--guard-request", str(request),
                    "--repo-id", repo_id,
                    "--base-sha", head,
                    "--lane", "feature",
                    "--weight", weight,
                    "--writer", str(worktree),
                    "--file", relative,
                ]
                status, output, error = ForgeBuildSkillContractTests._invoke_native_lane(
                    native_module, state_path, arguments
                )
                if status == 0:
                    native_receipts[unit_id] = receipt
                return status, output, error

            bridge_job = None
            actual_observations = {
                "admissions": [],
                "rejections": [],
                "active_snapshot": None,
                "native_heartbeat": None,
                "bridge_terminal": None,
                "native_releases": [],
                "final_active_count": None,
            }

            def record_admission(probe, reservation, idempotent):
                actual_observations["admissions"].append(
                    {
                        "probe": probe,
                        "kind": reservation["kind"],
                        "weight": reservation["weight"],
                        "files": reservation["files"],
                        "status": reservation["status"],
                        "idempotent": idempotent,
                    }
                )

            def record_rejection(probe, error, code, *, plan_unit=True):
                self.assertIn(code, error)
                actual_observations["rejections"].append(
                    {"probe": probe, "code": code, "plan_unit": plan_unit}
                )

            try:
                status, output, error = reserve_native(
                    "native-heavy",
                    "ORCH-U2-NATIVE-HEAVY",
                    "src/native-heavy.py",
                    "HEAVY",
                )
                self.assertEqual(status, 0, error)
                native_heavy_admission = json.loads(output)
                self.assertFalse(native_heavy_admission["idempotent"])
                record_admission(
                    "ORCH-U2-NATIVE-HEAVY",
                    native_heavy_admission["reservation"],
                    native_heavy_admission["idempotent"],
                )

                status, _output, error = reserve_native(
                    "heavy-collision",
                    "ORCH-REJECT-SECOND-HEAVY",
                    "src/heavy-collision.py",
                    "HEAVY",
                )
                self.assertEqual(status, 1)
                record_rejection(
                    "ORCH-REJECT-SECOND-HEAVY", error, "HEAVY_LEASE"
                )

                status, _output, error = reserve_native(
                    "file-overlap",
                    "ORCH-REJECT-OVERLAP",
                    "src/native-heavy.py",
                    "LIGHT",
                )
                self.assertEqual(status, 1)
                record_rejection("ORCH-REJECT-OVERLAP", error, "FILE_OVERLAP")

                status, _output, error = reserve_native(
                    "native-heavy",
                    "ORCH-REJECT-DUPLICATE-WRITER",
                    "src/writer-collision.py",
                    "LIGHT",
                )
                self.assertEqual(status, 1)
                record_rejection(
                    "ORCH-REJECT-DUPLICATE-WRITER",
                    error,
                    "DUPLICATE_WRITER",
                )

                status, output, error = reserve_native(
                    "native-light",
                    "ORCH-U3-NATIVE-LIGHT",
                    "src/native-light.py",
                    "LIGHT",
                )
                self.assertEqual(status, 0, error)
                native_light_admission = json.loads(output)
                self.assertFalse(native_light_admission["idempotent"])
                record_admission(
                    "ORCH-U3-NATIVE-LIGHT",
                    native_light_admission["reservation"],
                    native_light_admission["idempotent"],
                )

                bridge_job = start_bridge(
                    "claude", "ORCH-U1-CLAUDE", "src/claude.py"
                )
                self.assertFalse(bridge_job["idempotent"])
                record_admission(
                    "ORCH-U1-CLAUDE",
                    Store(state_path).get_bridge_lane_reservation(
                        bridge_job["job_id"]
                    ),
                    bridge_job["idempotent"],
                )

                fourth_receipt = artifacts / "ORCH-DIAGNOSTIC-LANE-4-store-owner.json"
                fourth_token = "4" * 64
                self._write_private_json(
                    fourth_receipt,
                    {"lease_id": "ORCH-DIAGNOSTIC-LANE-4", "owner_token": fourth_token},
                )
                fourth_information = fourth_receipt.stat()
                with self.assertRaisesRegex(StateError, "LANE_CAP") as lane_cap:
                    Store(state_path).reserve_native_lane(
                        lease_id="ORCH-DIAGNOSTIC-LANE-4",
                        owner_token=fourth_token,
                        receipt_pin={
                            "path": str(fourth_receipt),
                            "device": fourth_information.st_dev,
                            "inode": fourth_information.st_ino,
                            "owner": fourth_information.st_uid,
                            "mode": stat.S_IMODE(fourth_information.st_mode),
                            "links": fourth_information.st_nlink,
                            "sha256": self._sha256_file(fourth_receipt),
                        },
                        reservation={
                            "repo_id": repo_id,
                            "base_sha": head,
                            "lane": "feature",
                            "weight": "LIGHT",
                            "writer": str(worktrees["fourth"]),
                            "files": ["src/fourth.py"],
                        },
                        ttl_seconds=300,
                        admission={"result": "ADMIT"},
                    )
                record_rejection(
                    "DIAGNOSTIC-LANE-4",
                    str(lane_cap.exception),
                    "LANE_CAP",
                    plan_unit=False,
                )

                active = Store(state_path).list_lane_reservations()
                self.assertEqual(len(active), 3)
                self.assertEqual(
                    sorted(item["kind"] for item in active),
                    ["bridge", "native", "native"],
                )
                self.assertEqual(sum(item["weight"] == "HEAVY" for item in active), 1)
                self.assertEqual(len({item["writer"] for item in active}), 3)
                active_files = [path for item in active for path in item["files"]]
                self.assertEqual(len(active_files), len(set(active_files)))
                actual_observations["active_snapshot"] = {
                    "count": len(active),
                    "kinds": sorted(item["kind"] for item in active),
                    "weights": sorted(item["weight"] for item in active),
                    "writer_count": len({item["writer"] for item in active}),
                    "file_count": len(active_files),
                    "unique_file_count": len(set(active_files)),
                }

                heavy = next(
                    item for item in active if item["lease_id"] == "ORCH-U2-NATIVE-HEAVY"
                )
                status, heartbeat_output, heartbeat_error = (
                    ForgeBuildSkillContractTests._invoke_native_lane(
                        native_module,
                        state_path,
                        [
                            "heartbeat",
                            "--lease-id", heavy["lease_id"],
                            "--receipt", str(native_receipts[heavy["lease_id"]]),
                            "--revision", str(heavy["revision"]),
                        ],
                    )
                )
                self.assertEqual(status, 0, heartbeat_error)
                heartbeat = json.loads(heartbeat_output)
                self.assertEqual(heartbeat["revision"], 1)
                actual_observations["native_heartbeat"] = {
                    "lease_id": heartbeat["lease_id"],
                    "status": heartbeat["status"],
                    "revision": heartbeat["revision"],
                }
            finally:
                store = Store(state_path)
                for row in store.list_lane_reservations(include_released=True):
                    if (
                        row["kind"] == "native"
                        and row["status"] != "RELEASED"
                        and row["lease_id"] in native_receipts
                    ):
                        status, release_output, error = (
                            ForgeBuildSkillContractTests._invoke_native_lane(
                                native_module,
                                state_path,
                                [
                                    "release",
                                    "--lease-id", row["lease_id"],
                                    "--receipt", str(native_receipts[row["lease_id"]]),
                                    "--revision", str(row["revision"]),
                                ],
                            )
                        )
                        self.assertEqual(status, 0, error)
                        released = json.loads(release_output)
                        actual_observations["native_releases"].append(
                            {
                                "lease_id": released["lease_id"],
                                "status": released["status"],
                                "revision": released["revision"],
                            }
                        )
                if bridge_job is not None:
                    deadline = time.monotonic() + 10
                    latest = service.status({"job_id": bridge_job["job_id"]}).body
                    while latest["status"] not in {
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "PAUSED_PROVIDER_ACCESS",
                        "PAUSED_POLICY",
                    } and time.monotonic() < deadline:
                        latest = service.wait(
                            {
                                "job_id": bridge_job["job_id"],
                                "revision": latest["revision"],
                                "timeout_ms": 1000,
                            }
                        ).body
                    event_trace = [
                        {"kind": event.kind, "payload": event.payload}
                        for event in Store(state_path).list_events(
                            bridge_job["job_id"]
                        )
                    ]
                    self.assertEqual(
                        latest["status"],
                        "SUCCEEDED",
                        {"status": latest, "events": event_trace},
                    )
                    actual_observations["bridge_terminal"] = {
                        "status": latest["status"]
                    }

            active_after_release = Store(state_path).list_lane_reservations()
            self.assertEqual(active_after_release, [])
            actual_observations["native_releases"].sort(
                key=lambda item: item["lease_id"]
            )
            actual_observations["final_active_count"] = len(active_after_release)
            self.assertEqual(actual_observations, expected_observations)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                head,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "status", "--porcelain=v1"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout,
                "",
            )

    def test_scenario_source_and_response_fingerprints_match_final_bytes(self):
        scenario = ORCHESTRATE_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable observed shared-scheduler trace\n\n.*?```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))
        expected_sources = [
            "scripts/dispatch-guard.sh",
            "scripts/forge-native-lane.py",
            "scripts/forge_lib/contracts.py",
            "scripts/forge_lib/service.py",
            "scripts/forge_lib/state.py",
            "scripts/forge_lib/worker.py",
            "skills/forge-orchestrate/SKILL.md",
            "skills/forge-orchestrate/agents/openai.yaml",
        ]
        self.assertEqual(replay["source_paths"], expected_sources)
        digest = hashlib.sha256()
        for relative in expected_sources:
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update((PLUGIN_ROOT / relative).read_bytes() + b"\0")
        self.assertEqual(replay["source_sha256"], digest.hexdigest())

        response_match = re.search(
            r"<!-- POST_ORCHESTRATE_RESPONSE_BEGIN -->\n(.*?)\n<!-- POST_ORCHESTRATE_RESPONSE_END -->",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(response_match)
        self.assertEqual(
            replay["post_response_sha256"],
            hashlib.sha256(response_match.group(1).encode("utf-8")).hexdigest(),
        )

class ForgeWithDocsSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (WITH_DOCS_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.normalized_skill = " ".join(cls.skill.split())

    def test_required_files_links_and_trigger_metadata(self):
        required = {
            "SKILL.md",
            "agents/openai.yaml",
            "ADR-FORMAT.md",
            "CONTEXT-FORMAT.md",
        }
        missing = sorted(
            path for path in required if not (WITH_DOCS_SKILL_ROOT / path).is_file()
        )
        self.assertEqual(missing, [])
        self.assertTrue(WITH_DOCS_SCENARIO_PATH.is_file(), WITH_DOCS_SCENARIO_PATH)

        metadata = frontmatter(self.skill)
        self.assertEqual(metadata.get("name"), "forge-with-docs")
        description = metadata.get("description", "")
        self.assertTrue(description.startswith("Use when "), description)
        self.assertLess(len(description), 500)
        self.assertRegex(description.lower(), r"glossary|domain|context|adr")

        missing_links = []
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", self.skill):
            target = target.split("#", 1)[0]
            if target and not re.match(r"^[a-z]+://", target):
                resolved = (WITH_DOCS_SKILL_ROOT / target).resolve()
                if not resolved.exists():
                    missing_links.append(target)
        self.assertEqual(missing_links, [])

    def test_openai_metadata_uses_supported_top_level_policy(self):
        metadata_path = WITH_DOCS_SKILL_ROOT / "agents" / "openai.yaml"
        self.assertTrue(metadata_path.is_file(), metadata_path)
        self.assertEqual(
            metadata_path.read_text(encoding="utf-8"),
            'interface:\n'
            '  display_name: "Forge With Docs"\n'
            '  short_description: "Plan against living domain documentation"\n'
            '  default_prompt: "Use $forge-with-docs to plan this change against the project glossary and ADRs before shared Forge execution."\n'
            'policy:\n'
            '  allow_implicit_invocation: true\n',
        )

    def test_docs_first_act_one_and_question_discipline_are_explicit(self):
        for phrase in (
            "docs-aware Act 1",
            "before the first owner question",
            "CONTEXT.md",
            "CONTEXT-MAP.md",
            "existing ADRs",
            "terminology ledger",
            "inspect code",
            "discoverable local facts",
            "one question at a time",
            "wait for the answer",
            "recommend one answer",
            "concrete legal-domain scenarios",
            "as each decision settles",
            "PLAN.md",
            "PLAN-REVIEW-LOG.md",
        ):
            self.assertIn(phrase, self.normalized_skill)
        self.assertNotIn("consolidated discovery round", self.skill.lower())

    def test_handoff_reuses_shared_acts_without_new_cadence_or_authority(self):
        expected_targets = {
            "../forge/SKILL.md",
            "../forge/ENGINE.md",
            "../forge/ROUTING-POLICY.md",
            "../forge/references/acts-1-2.md",
            "../forge/references/acts-3-4.md",
            "../forge/profiles/sprint.md",
            "../forge/profiles/assured.md",
        }
        linked_targets = {
            target.split("#", 1)[0]
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", self.skill)
        }
        self.assertTrue(expected_targets.issubset(linked_targets))
        for phrase in (
            "hand the locked plan unchanged into shared Acts 2–4",
            "review_controls.domain_docs",
            "canonical sorted unique",
            "worktree-relative",
            "same domain-document path set",
            "current hashes",
            "adds no gate",
            "adds no approval",
            "adds no routing",
            "adds no review",
            "adds no build",
            "adds no proof",
            "adds no commit cadence",
            "Codex remains",
            "integration authority",
        ):
            self.assertIn(phrase, self.normalized_skill)
        self.assertNotRegex(self.skill, r"(?m)^## Act [234]")
        self.assertEqual(set(re.findall(r"`(forge_[a-z]+)`", self.skill)), set())

    def test_templates_keep_domain_language_and_adr_thresholds_narrow(self):
        context = (WITH_DOCS_SKILL_ROOT / "CONTEXT-FORMAT.md").read_text(
            encoding="utf-8"
        )
        adr = (WITH_DOCS_SKILL_ROOT / "ADR-FORMAT.md").read_text(encoding="utf-8")
        for phrase in (
            "CONTEXT-MAP.md",
            "CONTEXT.md",
            "## Language",
            "_Avoid_",
            "One or two sentences",
            "project's legal or business domain",
            "create a root `CONTEXT.md` lazily",
        ):
            self.assertIn(phrase, context)
        for phrase in (
            "docs/adr/",
            "sequential numbering",
            "Hard to reverse",
            "Surprising without context",
            "real trade-off",
            "context, decision, and consequences",
            "Create the directory lazily",
        ):
            self.assertIn(phrase, adr)
        attribution = f"{self.skill}\n{context}\n{adr}"
        self.assertIn("grill-with-docs", attribution)
        self.assertIn("Matt Pocock", attribution)
        self.assertIn("MIT", attribution)

    def test_exact_baseline_is_preserved_as_honest_red_evidence(self):
        scenario = WITH_DOCS_SCENARIO_PATH.read_text(encoding="utf-8")
        normalized = " ".join(scenario.split())
        self.assertEqual(scenario.count(WITH_DOCS_BASELINE_PROMPT), 2)
        self.assertEqual(
            scenario.count(
                "No—natural baseline behavior can prescribe docs-first terminology "
                "control and cadence preservation, but without explicit enforceable "
                "instructions it cannot guarantee either."
            ),
            1,
        )
        for phrase in (
            "Baseline verdict: **RED**",
            "consolidated discovery round",
            "one-question-at-a-time",
            "No repository, file, command, tool, provider, or network action occurred",
            "honest retained summary",
            "not a verbatim response",
            "Post-skill verdict: **GREEN**",
            "authored characterization",
            "not an independent model replay",
        ):
            self.assertIn(phrase, normalized)

    def test_actual_forge_service_docs_aware_handoff_and_resume(self):
        scenario = WITH_DOCS_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable offline ForgeService docs-aware journey\n\n```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))

        def sha256_file(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        def evidence_sha256(value) -> str:
            return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            repository = temporary_root / "legal-domain-repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "user.email",
                    "forge@example.test",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "user.name",
                    "Forge Docs Replay",
                ],
                check=True,
            )
            for relative, content in replay["repo_fixture"].items():
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "domain baseline"],
                check=True,
            )
            seed_wip_base(repository)
            head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            product_path = repository / replay["product_path"]
            product_before = product_path.read_bytes()

            loaded = []
            inspected = []
            ledger = {}
            open_question = None
            docs_written = []
            plan_locked = False
            review_log_initialized = False

            for operation in replay["act1_trace"]:
                event = operation["event"]
                if event == "load":
                    self.assertFalse(open_question)
                    (repository / operation["path"]).read_text(encoding="utf-8")
                    loaded.append(operation["path"])
                elif event == "inspect_code":
                    self.assertFalse(open_question)
                    (repository / operation["path"]).read_text(encoding="utf-8")
                    inspected.append(operation["path"])
                elif event == "ledger_open":
                    self.assertFalse(open_question)
                    ledger[operation["term"]] = dict(operation)
                elif event == "owner_question":
                    self.assertIsNone(open_question, "questions must be one at a time")
                    self.assertEqual(loaded, replay["load_before_questions"])
                    self.assertEqual(inspected, replay["inspect_before_questions"])
                    self.assertEqual(
                        ledger[operation["term"]]["status"],
                        "open",
                    )
                    self.assertTrue(operation["recommendation"])
                    open_question = operation["id"]
                elif event == "owner_answer":
                    self.assertEqual(open_question, operation["id"])
                    open_question = None
                elif event == "ledger_resolve":
                    self.assertIsNone(open_question)
                    self.assertIn(operation["term"], ledger)
                    ledger[operation["term"]]["status"] = "resolved"
                    ledger[operation["term"]]["resolution"] = operation["resolution"]
                elif event == "write_doc":
                    self.assertEqual(
                        ledger[operation["decision"]]["status"],
                        "resolved",
                        "living docs must follow their settled decision",
                    )
                    self.assertFalse(plan_locked)
                    path = repository / operation["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(operation["content"], encoding="utf-8")
                    docs_written.append(operation["path"])
                elif event == "lock_plan":
                    self.assertIsNone(open_question)
                    self.assertTrue(docs_written)
                    self.assertTrue(
                        all(item["status"] == "resolved" for item in ledger.values())
                    )
                    (repository / operation["path"]).write_text(
                        operation["content"], encoding="utf-8"
                    )
                    plan_locked = True
                elif event == "initialize_review_log":
                    self.assertTrue(plan_locked)
                    (repository / operation["path"]).write_text(
                        operation["content"], encoding="utf-8"
                    )
                    review_log_initialized = True
                else:
                    self.fail(f"unknown trace event: {event}")

            self.assertTrue(plan_locked and review_log_initialized)
            self.assertEqual(replay["extra_gates"], [])
            self.assertEqual(replay["extra_approvals"], [])

            plan = repository / "PLAN.md"
            review_log = repository / "PLAN-REVIEW-LOG.md"
            domain_paths = replay["domain_doc_paths"]
            self.assertEqual(domain_paths, sorted(set(domain_paths)))
            self.assertLessEqual(len(domain_paths), 64)

            def domain_declarations():
                return [
                    {
                        "path": relative,
                        "sha256": sha256_file(repository / relative),
                    }
                    for relative in domain_paths
                ]

            def stored_domain_map(pins):
                return {
                    Path(pin["path"]).relative_to(repository).as_posix(): pin["sha256"]
                    for pin in pins
                }

            state_path = temporary_root / "state" / "state.sqlite3"
            argv_log = temporary_root / "argv.jsonl"
            stdin_log = temporary_root / "stdin.jsonl"
            environment_log = temporary_root / "environment.jsonl"
            control_path = temporary_root / FAKE_CONTROL_NAME

            def write_control(report):
                control_path.write_text(
                    json.dumps(
                        {
                            "argv_log": str(argv_log),
                            "stdin_log": str(stdin_log),
                            "environment_log": str(environment_log),
                            "assistant_text": report,
                            "parent_tool_use_id": "forge-with-docs-parent",
                            "control_window_seconds": 0.05,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )

            write_control(replay["round_reports"][0])
            environment = {
                "HOME": str(temporary_root),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "LC_CTYPE": "C.UTF-8",
                "TMPDIR": str(temporary_root),
                "SHELL": "/bin/sh",
                "USER": "forge-with-docs-test",
                "FORGE_STATE_PATH": str(state_path),
                "FORGE_LAUNCH_WORKSPACE": str(repository),
                "FORGE_CLAUDE_EXECUTABLE": str(FAKE_CLAUDE),
            }
            service = ForgeService(
                state_path=state_path,
                launch_workspace=repository,
                claude_executable=str(FAKE_CLAUDE),
                bridge_path=ADMISSION_BRIDGE,
                environment=environment,
                _admission_machine_probe=machine_headroom,
            )
            preflight = service.preflight({"cwd": str(repository)}).body
            self.assertEqual(preflight["status"], "ready")
            self.assertFalse(preflight["provider_called"])

            admission = review_admission(module="tests.test_skill_contracts")
            admission["review_controls"] = {
                "plan": {"path": str(plan), "sha256": sha256_file(plan)},
                "review_log": {
                    "path": str(review_log),
                    "sha256": sha256_file(review_log),
                },
                "domain_docs": domain_declarations(),
            }
            start_arguments = {
                "prompt": replay["start_prompt"],
                "cwd": str(repository),
                "role": "review",
                "model": "fable",
                "effort": "max",
                "idempotency_key": replay["start_idempotency_key"],
                "budget": {
                    "max_seconds": 10,
                    "max_tokens": 4096,
                    "max_turns": 8,
                    "max_cost_usd": "1.00",
                },
                "admission": admission,
            }
            validate_tool_arguments("forge_start", start_arguments)
            started = service.start(start_arguments).body
            job_id = started["job_id"]

            def wait_terminal(revision):
                deadline = time.monotonic() + 10
                last = None
                while time.monotonic() < deadline:
                    last = service.wait(
                        {
                            "job_id": job_id,
                            "revision": revision,
                            "timeout_ms": 1000,
                        }
                    ).body
                    revision = last["revision"]
                    if last["status"] in {
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "PAUSED_PROVIDER_ACCESS",
                        "PAUSED_POLICY",
                    }:
                        return last
                self.fail(f"actual ForgeService job did not terminate: {last}")

            def terminal_receipt(result_body):
                artifact = result_body["terminal_receipt"]
                path = Path(artifact["path"])
                self.assertTrue(path.is_file())
                self.assertEqual(artifact["sha256"], sha256_file(path))
                return json.loads(path.read_text(encoding="utf-8")), artifact

            def append_review_round(
                *, round_number, report, receipt_artifact, disposition
            ):
                prior = review_log.read_bytes()
                entry = (
                    f"\n## Round {round_number}\n\n"
                    f"### Claude critique\n\n```text\n{report}\n```\n\n"
                    f"Terminal receipt SHA-256: `{receipt_artifact['sha256']}`\n\n"
                    f"### Codex disposition\n\n{disposition}\n"
                ).encode("utf-8")
                review_log.write_bytes(prior + entry)
                self.assertEqual(review_log.read_bytes()[: len(prior)], prior)

            terminal_one = wait_terminal(started["revision"])
            self.assertEqual(terminal_one["status"], "SUCCEEDED", terminal_one)
            result_one = service.result(
                {"job_id": job_id, "revision": terminal_one["revision"]}
            ).body
            receipt_one, receipt_artifact_one = terminal_receipt(result_one)
            attempt_one = terminal_one["attempt_id"]
            session_id = terminal_one["session_id"]
            context_one = service.store.get_attempt_context(attempt_one)
            admission_one = service.store.get_attempt_admission(attempt_one)
            for name, path in (("plan", plan), ("review_log", review_log)):
                self.assertEqual(
                    context_one["review_controls"][name],
                    admission_one["review_controls"][name],
                )
                self.assertEqual(
                    Path(context_one["review_controls"][name]["path"]),
                    path,
                )
            start_domain_map = stored_domain_map(
                context_one["review_controls"]["domain_docs"]
            )
            self.assertEqual(list(start_domain_map), domain_paths)
            self.assertEqual(
                admission_one["review_controls"]["domain_docs"],
                context_one["review_controls"]["domain_docs"],
            )
            self.assertEqual(result_one["report"], replay["round_reports"][0])
            self.assertTrue(result_one["report"].endswith("VERDICT: REVISE"))
            self.assertEqual(receipt_one["job_id"], job_id)
            self.assertEqual(receipt_one["session_id"], session_id)

            append_review_round(
                round_number=1,
                report=result_one["report"],
                receipt_artifact=receipt_artifact_one,
                disposition=replay["codex_dispositions"][0],
            )
            for relative, content in replay["round_one_evolution"].items():
                path = repository / relative
                self.assertTrue(
                    relative == "PLAN.md" or relative in domain_paths,
                    relative,
                )
                path.write_text(content, encoding="utf-8")
            write_control(replay["round_reports"][1])

            resume_domain_docs = domain_declarations()
            self.assertEqual(
                [pin["path"] for pin in resume_domain_docs],
                domain_paths,
            )
            refreeze = {
                "baseline_head": head,
                "review_controls": {
                    "plan": {"path": str(plan), "sha256": sha256_file(plan)},
                    "review_log": {
                        "path": str(review_log),
                        "sha256": sha256_file(review_log),
                    },
                    "domain_docs": resume_domain_docs,
                },
            }
            resume_arguments = {
                "job_id": job_id,
                "revision": terminal_one["revision"],
                "prompt": replay["resume_prompt"],
                "idempotency_key": replay["resume_idempotency_key"],
                "refreeze": refreeze,
            }
            validate_tool_arguments("forge_resume", resume_arguments)
            resumed = service.resume(resume_arguments).body
            attempt_two = resumed["attempt_id"]
            self.assertEqual(resumed["job_id"], job_id)
            self.assertNotEqual(attempt_two, attempt_one)
            self.assertEqual(resumed["resumed_from_attempt_id"], attempt_one)
            self.assertEqual(resumed["resume_session_id"], session_id)

            context_two = service.store.get_attempt_context(attempt_two)
            expected_resume_request = {
                "job_id": job_id,
                "revision": terminal_one["revision"],
                "prompt": replay["resume_prompt"],
                "model_override": None,
                "idempotency_key": replay["resume_idempotency_key"],
                "refreeze": refreeze,
            }
            self.assertEqual(
                context_two["request_hash"],
                evidence_sha256(expected_resume_request),
            )
            self.assertEqual(resumed["context_sha256"], context_two["sha256"])
            admission_two = service.store.get_attempt_admission(attempt_two)
            for name in ("plan", "review_log"):
                self.assertEqual(
                    context_two["review_controls"][name]["sha256"],
                    refreeze["review_controls"][name]["sha256"],
                )
            resume_domain_map = stored_domain_map(
                context_two["review_controls"]["domain_docs"]
            )
            self.assertEqual(list(resume_domain_map), domain_paths)
            self.assertEqual(
                admission_two["review_controls"]["domain_docs"],
                context_two["review_controls"]["domain_docs"],
            )
            for relative in replay["evolved_domain_docs"]:
                self.assertNotEqual(
                    start_domain_map[relative],
                    resume_domain_map[relative],
                    relative,
                )
            for relative in set(domain_paths) - set(replay["evolved_domain_docs"]):
                self.assertEqual(
                    start_domain_map[relative],
                    resume_domain_map[relative],
                    relative,
                )

            terminal_two = wait_terminal(resumed["revision"])
            self.assertEqual(terminal_two["status"], "SUCCEEDED", terminal_two)
            result_two = service.result(
                {"job_id": job_id, "revision": terminal_two["revision"]}
            ).body
            receipt_two, receipt_artifact_two = terminal_receipt(result_two)
            self.assertEqual(result_two["report"], replay["round_reports"][1])
            self.assertTrue(result_two["report"].endswith("VERDICT: APPROVED"))
            self.assertEqual(terminal_two["session_id"], session_id)
            self.assertEqual(receipt_two["session_id"], session_id)
            append_review_round(
                round_number=2,
                report=result_two["report"],
                receipt_artifact=receipt_artifact_two,
                disposition=replay["codex_dispositions"][1],
            )

            provider_argv = [
                json.loads(line)
                for line in argv_log.read_text(encoding="utf-8").splitlines()
                if "--input-format" in json.loads(line)
            ]
            self.assertEqual(len(provider_argv), replay["fake_provider_turns"])
            self.assertEqual(
                provider_argv[0][provider_argv[0].index("--session-id") + 1],
                session_id,
            )
            self.assertEqual(
                provider_argv[1][provider_argv[1].index("--resume") + 1],
                session_id,
            )
            user_frames = [
                json.loads(line)
                for line in stdin_log.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("type") == "user"
            ]
            self.assertEqual(
                [frame["message"]["content"][0]["text"] for frame in user_frames],
                [replay["start_prompt"], replay["resume_prompt"]],
            )
            self.assertEqual(
                result_one["usage"]["server_tool_use"],
                {"web_fetch_requests": 0, "web_search_requests": 0},
            )
            self.assertEqual(
                result_two["usage"]["server_tool_use"],
                {"web_fetch_requests": 0, "web_search_requests": 0},
            )
            self.assertEqual(replay["live_provider_calls"], 0)
            child_environments = [
                json.loads(line)
                for line in environment_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(child_environments)
            for child_environment in child_environments:
                self.assertNotIn("ANTHROPIC_API_KEY", child_environment)

            self.assertEqual(product_path.read_bytes(), product_before)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repository), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                head,
            )
            dirty = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(
                sorted(line[3:] for line in dirty),
                sorted(replay["expected_dirty_paths"]),
            )
            self.assertEqual(service.store.list_lane_reservations(), [])

    def test_scenario_source_and_response_fingerprints_match_final_bytes(self):
        scenario = WITH_DOCS_SCENARIO_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"## Executable offline ForgeService docs-aware journey\n\n```json\n(.*?)\n```",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        replay = json.loads(match.group(1))
        expected_sources = [
            "skills/forge-with-docs/SKILL.md",
            "skills/forge-with-docs/agents/openai.yaml",
            "skills/forge-with-docs/ADR-FORMAT.md",
            "skills/forge-with-docs/CONTEXT-FORMAT.md",
        ]
        self.assertEqual(replay["source_paths"], expected_sources)
        digest = hashlib.sha256()
        for relative in expected_sources:
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update((PLUGIN_ROOT / relative).read_bytes() + b"\0")
        self.assertEqual(replay["source_sha256"], digest.hexdigest())

        response_match = re.search(
            r"<!-- POST_WITH_DOCS_RESPONSE_BEGIN -->\n(.*?)\n<!-- POST_WITH_DOCS_RESPONSE_END -->",
            scenario,
            re.DOTALL,
        )
        self.assertIsNotNone(response_match)
        self.assertEqual(
            replay["post_response_sha256"],
            hashlib.sha256(response_match.group(1).encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
