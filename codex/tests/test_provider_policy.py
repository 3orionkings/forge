import copy
import hashlib
import json
import os
import select
import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tests" / "fixtures" / "forge_bridge_admission.py"
FAKE_CLAUDE = ROOT / "tests" / "fixtures" / "fake_claude.py"
FAKE_CONTROL_NAME = "forge-fake-claude-control.json"
sys.path.insert(0, str(ROOT / "scripts"))

from forge_lib import claude, contracts, service, worker  # noqa: E402
from tests.admission_fixture import (
    machine_headroom,
    review_admission,
    seed_reconciled_base,
)


SESSION_ID = "12345678-1234-4678-9234-567812345678"
UUID = "87654321-4321-4678-9234-567812345678"
STANDARD_USAGE = {
    "input_tokens": 7,
    "output_tokens": 3,
    "cache_read_input_tokens": 1,
    "cache_creation_input_tokens": 2,
    "server_tool_use": {
        "web_search_requests": 0,
        "web_fetch_requests": 0,
    },
    "cache_creation": {
        "ephemeral_5m_input_tokens": 2,
        "ephemeral_1h_input_tokens": 0,
    },
    "service_tier": "standard",
}
PROVIDER_ERROR_ENUM = (
    "authentication_failed",
    "oauth_org_not_allowed",
    "billing_error",
    "rate_limit",
    "invalid_request",
    "model_not_found",
    "server_error",
    "unknown",
    "max_output_tokens",
)


def model_usage_entry(*, input_tokens=7, output_tokens=3, cost_usd=0.25):
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadInputTokens": 1,
        "cacheCreationInputTokens": 2,
        "webSearchRequests": 0,
        "costUSD": cost_usd,
        "contextWindow": 200_000,
        "maxOutputTokens": 32_000,
    }


def policy_type():
    value = getattr(claude, "ProviderPolicyTracker", None)
    if value is None:
        raise AssertionError("ProviderPolicyTracker is not implemented")
    return value


def canonical_result(
    *,
    errors=(),
    permission_denials=(),
    subtype=None,
    is_error=None,
    total_cost_usd=0.25,
    num_turns=1,
):
    if is_error is None:
        is_error = bool(errors or permission_denials)
    if subtype is None:
        subtype = "error_during_execution" if is_error else "success"
    payload = {
        "type": "result",
        "subtype": subtype,
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": is_error,
        "num_turns": num_turns,
        "stop_reason": None,
        "total_cost_usd": total_cost_usd,
        "usage": copy.deepcopy(STANDARD_USAGE),
        "modelUsage": {
            "claude-opus-4-6": model_usage_entry(cost_usd=total_cost_usd)
        },
        "permission_denials": list(permission_denials),
        "uuid": UUID,
        "session_id": SESSION_ID,
    }
    if subtype == "success":
        payload["result"] = "bounded report"
    else:
        payload["errors"] = list(errors)
    return payload


def canonical_init(model="claude-opus-4-6"):
    return {
        "type": "system",
        "subtype": "init",
        "apiKeySource": "none",
        "agents": ["claude", "Explore", "general-purpose", "Plan"],
        "analytics_disabled": False,
        "capabilities": ["interrupt_receipt_v1", "msg_lifecycle_v1"],
        "claude_code_version": "2.1.248",
        "cwd": str(ROOT),
        "fast_mode_disabled_reason": "sdk_opt_in_required",
        "fast_mode_state": "off",
        "memory_paths": {"auto": "/bounded/claude/memory"},
        "messaging_socket_path": "/tmp/cc-socks/bounded.sock",
        "tools": ["Read", "Glob", "Grep", "Agent"],
        "mcp_servers": [],
        "model": model,
        "permissionMode": "dontAsk",
        "product_feedback_disabled": False,
        "slash_commands": [],
        "output_style": "default",
        "skills": [],
        "plugins": [],
        "uuid": UUID,
        "session_id": SESSION_ID,
    }


def canonical_assistant(
    message_id,
    *,
    model="claude-opus-4-6",
    output_tokens=1,
    content=None,
    provider_error=None,
):
    if content is None:
        content = [{"type": "text", "text": "effect"}]
    payload = {
        "type": "assistant",
        "message": {
            "id": message_id,
            "container": None,
            "model": model,
            "role": "assistant",
            "stop_details": None,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "type": "message",
            "usage": {
                "input_tokens": 0,
                "output_tokens": output_tokens,
            },
            "content": content,
            "context_management": None,
        },
        "parent_tool_use_id": None,
        "uuid": UUID,
        "session_id": SESSION_ID,
    }
    if provider_error is not None:
        payload["message"]["model"] = "<synthetic>"
        payload["error"] = provider_error
        payload["api_error_status"] = 500
    return payload


def canonical_retry(*, attempt=1, error="unknown", error_status=None):
    return {
        "type": "system",
        "subtype": "api_retry",
        "attempt": attempt,
        "max_retries": 10,
        "retry_delay_ms": 5,
        "error_status": error_status,
        "error": error,
        "uuid": UUID,
        "session_id": SESSION_ID,
    }


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PublicBudgetContractTests(unittest.TestCase):
    def start_arguments(self, budget):
        return {
            "prompt": "Review the frozen plan.",
            "budget": budget,
            "admission": review_admission(module="tests.test_provider_policy"),
        }

    def test_budget_exposes_distinct_token_turn_and_usd_limits(self):
        budget = {
            "max_seconds": 60,
            "max_tokens": 4096,
            "max_turns": 12,
            "max_cost_usd": "1.250000",
        }

        validated = contracts.validate_tool_arguments(
            "forge_start", self.start_arguments(budget)
        )

        self.assertEqual(validated["budget"], budget)
        self.assertNotEqual(
            validated["budget"]["max_tokens"],
            validated["budget"]["max_turns"],
        )

    def test_published_budget_schema_uses_only_standard_json_schema_types(self):
        cost_schema = contracts.TOOL_SCHEMAS["forge_start"]["properties"][
            "budget"
        ]["properties"]["max_cost_usd"]

        self.assertEqual(cost_schema["type"], "string")
        self.assertIn("pattern", cost_schema)
        self.assertNotIn("maxFractionalPlaces", cost_schema)
        json.dumps(contracts.TOOL_SCHEMAS, allow_nan=False)

    def test_provider_authoritative_cost_evidence_requires_ascii_decimal_text(self):
        with self.assertRaisesRegex(
            service.StateError,
            "provider-authoritative cost is invalid",
        ):
            service._validate_accounting_evidence(
                {
                    "usage": copy.deepcopy(STANDARD_USAGE),
                    "total_cost_usd": "１２",
                    "usage_provenance": "provider_authoritative",
                    "cost_provenance": "provider_authoritative",
                }
            )

    def test_budget_accepts_exact_bounds_and_six_place_precision(self):
        cases = (
            {
                "max_seconds": 1,
                "max_tokens": 1,
                "max_turns": 1,
                "max_cost_usd": "0.000001",
            },
            {
                "max_seconds": 86_400,
                "max_tokens": 10_000_000,
                "max_turns": 1_000_000,
                "max_cost_usd": "1000000",
            },
        )
        for budget in cases:
            with self.subTest(budget=budget):
                contracts.validate_tool_arguments(
                    "forge_start", self.start_arguments(budget)
                )

    def test_budget_rejects_missing_overloaded_or_imprecise_fields(self):
        valid = {
            "max_seconds": 60,
            "max_tokens": 4096,
            "max_turns": 12,
            "max_cost_usd": "1.25",
        }
        invalid = (
            {key: value for key, value in valid.items() if key != "max_turns"},
            {key: value for key, value in valid.items() if key != "max_cost_usd"},
            {**valid, "max_turns": 0},
            {**valid, "max_turns": 1_000_001},
            {**valid, "max_cost_usd": "0"},
            {**valid, "max_cost_usd": "0.0000001"},
            {**valid, "max_cost_usd": "1000000.000001"},
            {**valid, "max_cost_usd": 1.25},
            {**valid, "max_cost_usd": True},
        )
        for budget in invalid:
            with self.subTest(budget=budget), self.assertRaises(
                contracts.ContractError
            ):
                contracts.validate_tool_arguments(
                    "forge_start", self.start_arguments(budget)
                )

    def test_resume_contract_has_one_explicit_model_override(self):
        accepted = contracts.validate_tool_arguments(
            "forge_resume",
            {
                "job_id": "job-1",
                "revision": 7,
                "model_override": "claude-opus-4-6",
            },
        )
        self.assertEqual(accepted["model_override"], "claude-opus-4-6")
        with self.assertRaises(contracts.ContractError):
            contracts.validate_tool_arguments(
                "forge_resume",
                {
                    "job_id": "job-1",
                    "revision": 7,
                    "fallback_model": "claude-sonnet-4-6",
                },
            )


class ClaudeCodeSchemaTests(unittest.TestCase):
    def test_model_identities_reject_secret_control_and_oversized_values_at_every_parser_ingress(self):
        secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456"
        invalid = (secret, "fable\nsmuggled", "m" * (claude.MAX_MODEL_ID_BYTES + 1))
        for value in invalid:
            with self.subTest(ingress="requested", value=value[:16]), self.assertRaises(
                claude.AdapterError
            ):
                claude.normalize_model_identifier(value)

        for ingress in ("init", "assistant", "modelUsage"):
            for value in invalid:
                if ingress == "init":
                    payload = canonical_init(value)
                elif ingress == "assistant":
                    payload = canonical_assistant("msg_invalid_model", model=value)
                else:
                    payload = canonical_result()
                    entry = payload["modelUsage"].pop("claude-opus-4-6")
                    payload["modelUsage"][value] = entry
                with self.subTest(ingress=ingress, value=value[:16]), self.assertRaises(
                    claude.ClaudeProtocolError
                ):
                    claude.parse_event(json.dumps(payload))

    def test_required_nullable_fields_distinguish_absent_from_explicit_null(self):
        assistant = canonical_assistant("msg_nullable")
        retry = canonical_retry(error_status=None)

        self.assertIsNone(
            claude.parse_event(json.dumps(assistant)).parent_tool_use_id
        )
        self.assertIsNone(
            claude.parse_event(json.dumps(retry)).api_retry_error_status
        )

        for payload, field in (
            (assistant, "parent_tool_use_id"),
            (retry, "error_status"),
        ):
            malformed = copy.deepcopy(payload)
            malformed.pop(field)
            with self.subTest(type=payload["type"], field=field), self.assertRaises(
                claude.ClaudeProtocolError
            ):
                claude.parse_event(json.dumps(malformed))

    def test_system_init_requires_the_exact_bounded_2_1_248_trust_anchor(self):
        required = (
            "apiKeySource",
            "claude_code_version",
            "cwd",
            "tools",
            "mcp_servers",
            "model",
            "permissionMode",
            "slash_commands",
            "output_style",
            "skills",
            "plugins",
            "uuid",
            "session_id",
        )
        for field in required:
            malformed = canonical_init()
            malformed.pop(field)
            with self.subTest(missing=field), self.assertRaises(
                claude.ClaudeProtocolError
            ):
                claude.parse_event(json.dumps(malformed))

        malformed_values = (
            {"apiKeySource": 7},
            {"claude_code_version": "2.1.154"},
            {"cwd": ""},
            {"tools": "Read"},
            {"tools": [""]},
            {"mcp_servers": {}},
            {"mcp_servers": [{"name": "local"}]},
            {"permissionMode": 7},
            {"slash_commands": {}},
            {"output_style": ""},
            {"skills": {}},
            {"plugins": {}},
            {"plugins": [{"name": "plugin-only"}]},
        )
        for replacement in malformed_values:
            malformed = canonical_init()
            malformed.update(replacement)
            with self.subTest(replacement=replacement), self.assertRaises(
                claude.ClaudeProtocolError
            ):
                claude.parse_event(json.dumps(malformed))

        valid = canonical_init()
        valid["mcp_servers"] = [{"name": "local", "status": "connected"}]
        valid["plugins"] = [
            {"name": "local-plugin", "path": "/bounded/plugin", "source": "local"}
        ]
        self.assertEqual(
            claude.parse_event(json.dumps(valid)).model,
            "claude-opus-4-6",
        )

    def test_error_result_consumes_canonical_errors_and_permission_denials(self):
        denial = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_1",
            "tool_input": {"command": "git status"},
        }
        event = claude.parse_event(
            json.dumps(
                canonical_result(
                    errors=("Permission denied",), permission_denials=(denial,)
                )
            )
        )

        self.assertEqual(event.errors, ("Permission denied",))
        self.assertEqual(event.permission_denials, (denial,))

    def test_noncanonical_synthetic_error_object_is_rejected(self):
        payload = canonical_result(errors=("Authentication failed",))
        payload.pop("errors")
        payload["error"] = {
            "type": "authentication_error",
            "message": "Authentication failed",
        }
        with self.assertRaises(claude.ClaudeProtocolError):
            claude.parse_event(json.dumps(payload))

    def test_api_retry_uses_exact_2_1_248_fields(self):
        event = claude.parse_event(json.dumps(canonical_retry()))

        self.assertTrue(event.is_api_retry)
        self.assertEqual(event.api_retry_attempt, 1)
        self.assertEqual(event.api_retry_max_retries, 10)
        self.assertEqual(event.api_retry_delay_ms, 5)
        self.assertIsNone(event.api_retry_error_status)
        self.assertEqual(event.api_retry_error, "unknown")

    def test_assistant_provider_error_enum_is_exact_and_synthetic(self):
        for provider_error in PROVIDER_ERROR_ENUM:
            with self.subTest(provider_error=provider_error):
                event = claude.parse_event(
                    json.dumps(
                        canonical_assistant(
                            f"msg_{provider_error}", provider_error=provider_error
                        )
                    )
                )
                self.assertEqual(event.provider_error, provider_error)
                self.assertIsNone(event.model)
                self.assertFalse(event.has_effect)

        invalid = canonical_assistant("msg_bad", provider_error="server_error")
        invalid["error"] = "transport_error"
        with self.assertRaises(claude.ClaudeProtocolError):
            claude.parse_event(json.dumps(invalid))

    def test_api_retry_rejects_noncanonical_status_and_error_names(self):
        invalid = (
            canonical_retry(attempt=0),
            canonical_retry(error_status="Connection reset by peer"),
            canonical_retry(error="transport_error"),
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(
                claude.ClaudeProtocolError
            ):
                claude.parse_event(json.dumps(payload))

    def test_fallback_and_refusal_events_are_explicit_policy_events(self):
        fallback = {
            "type": "system",
            "subtype": "model_fallback",
            "trigger": "model_not_found",
            "original_model": "claude-opus-4-6",
            "fallback_model": "claude-sonnet-4-6",
            "content": "Switched models",
            "uuid": UUID,
            "session_id": SESSION_ID,
        }
        refusal = {
            "type": "system",
            "subtype": "model_refusal_fallback",
            "trigger": "refusal",
            "direction": "retry",
            "original_model": "claude-opus-4-6",
            "fallback_model": "claude-sonnet-4-6",
            "request_id": "req_1",
            "content": "Retried with another model",
            "uuid": UUID,
            "session_id": SESSION_ID,
        }

        fallback_event = claude.parse_event(json.dumps(fallback))
        refusal_event = claude.parse_event(json.dumps(refusal))

        self.assertEqual(fallback_event.fallback_kind, "model_fallback")
        self.assertEqual(refusal_event.fallback_kind, "model_refusal_fallback")
        self.assertEqual(fallback_event.fallback_model, "claude-sonnet-4-6")
        self.assertEqual(refusal_event.fallback_model, "claude-sonnet-4-6")

    def test_synthetic_model_requires_canonical_error_and_real_model_is_never_discarded(self):
        event = claude.parse_event(
            json.dumps(
                canonical_assistant(
                    "msg_error",
                    provider_error="server_error",
                )
            )
        )

        self.assertIsNone(event.model)
        self.assertEqual(event.assistant_message_id, "msg_error")
        self.assertFalse(event.has_effect)

        sentinel_without_error = canonical_assistant(
            "msg_false_synthetic", model="<synthetic>"
        )
        with self.assertRaises(claude.ClaudeProtocolError):
            claude.parse_event(json.dumps(sentinel_without_error))

        real_model = canonical_assistant("msg_real", model="claude-opus-4-6")
        real_model["is_api_error_message"] = True
        parsed_real = claude.parse_event(json.dumps(real_model))
        self.assertEqual(parsed_real.model, "claude-opus-4-6")
        self.assertTrue(parsed_real.has_effect)

    def test_result_exact_union_and_required_common_fields(self):
        api_failure_result = canonical_result(is_error=True, subtype="success")
        api_failure_result["api_error_status"] = 500
        event = claude.parse_event(json.dumps(api_failure_result))
        self.assertTrue(event.is_error)
        self.assertEqual(event.errors, ())

        malformed = []
        missing_permission_denials = canonical_result()
        missing_permission_denials.pop("permission_denials")
        malformed.append(missing_permission_denials)
        success_with_errors = canonical_result()
        success_with_errors["errors"] = ["not in success union"]
        malformed.append(success_with_errors)
        error_without_errors = canonical_result(errors=("Internal provider failure",))
        error_without_errors.pop("errors")
        malformed.append(error_without_errors)
        error_with_result = canonical_result(errors=("Internal provider failure",))
        error_with_result["result"] = "not in error union"
        malformed.append(error_with_result)
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(
                claude.ClaudeProtocolError
            ):
                claude.parse_event(json.dumps(payload))

    def test_result_budget_fields_are_required_and_non_null(self):
        for field in ("num_turns", "total_cost_usd", "modelUsage", "usage"):
            for replacement in (None, "wrong-type"):
                malformed = canonical_result()
                malformed[field] = replacement
                with self.subTest(field=field, replacement=replacement), self.assertRaises(
                    claude.ClaudeProtocolError
                ):
                    claude.parse_event(json.dumps(malformed))

    def test_model_usage_empty_is_allowed_and_nonempty_entries_are_exact_eight_fields(self):
        empty = canonical_result(num_turns=3)
        empty["modelUsage"] = {}
        self.assertEqual(claude.parse_event(json.dumps(empty)).model_usage, {})

        for field in tuple(model_usage_entry()):
            payload = canonical_result()
            del payload["modelUsage"]["claude-opus-4-6"][field]
            with self.subTest(missing=field), self.assertRaises(
                claude.ClaudeProtocolError
            ):
                claude.parse_event(json.dumps(payload))

        payload = canonical_result()
        payload["modelUsage"]["claude-opus-4-6"]["webFetchRequests"] = 0
        with self.assertRaises(claude.ClaudeProtocolError):
            claude.parse_event(json.dumps(payload))

    def test_policy_relevant_events_require_uuid_and_session_identity(self):
        payloads = (
            canonical_init(),
            canonical_assistant("msg_identity"),
            canonical_retry(),
            canonical_result(),
            {
                "type": "system",
                "subtype": "model_fallback",
                "trigger": "model_not_found",
                "original_model": "claude-opus-4-6",
                "fallback_model": "claude-sonnet-4-6",
                "content": "fallback",
                "uuid": UUID,
                "session_id": SESSION_ID,
            },
        )
        for payload in payloads:
            for missing in ("uuid", "session_id"):
                malformed = copy.deepcopy(payload)
                malformed.pop(missing)
                with self.subTest(type=payload["type"], missing=missing), self.assertRaises(
                    claude.ClaudeProtocolError
                ):
                    claude.parse_event(json.dumps(malformed))


class ProviderClassificationMatrixTests(unittest.TestCase):
    MATRIX = (
        ("authentication", "authentication_failed", "PAUSED_PROVIDER_ACCESS"),
        ("oauth_organization", "oauth_org_not_allowed", "PAUSED_PROVIDER_ACCESS"),
        ("billing", "billing_error", "PAUSED_PROVIDER_ACCESS"),
        ("rate_limit", "rate_limit", "PAUSED_PROVIDER_ACCESS"),
        ("protocol", "invalid_request", "FAILED"),
        ("model_not_found", "model_not_found", "PAUSED_PROVIDER_ACCESS"),
        ("transport", "server_error", "FAILED"),
        ("internal_failure", "unknown", "FAILED"),
        ("token_limit", "max_output_tokens", "PAUSED_POLICY"),
    )

    def test_canonical_assistant_error_enum_has_typed_outcomes(self):
        tracker_class = policy_type()
        for classification, provider_error, expected_status in self.MATRIX:
            with self.subTest(classification=classification):
                tracker = tracker_class(
                    model_requested="fable",
                    max_tokens=10_000_000,
                    max_turns=10,
                    max_cost_usd=Decimal("1"),
                )
                decision = tracker.observe(
                    claude.parse_event(
                        json.dumps(
                            canonical_assistant(
                                f"msg_{provider_error}",
                                provider_error=provider_error,
                            )
                        )
                    )
                )
                self.assertIsNotNone(decision)
                self.assertEqual(decision.classification, classification)
                self.assertEqual(decision.status, expected_status)
                self.assertTrue(decision.reason)
                self.assertFalse(decision.allow_substitution)

    def test_non_api_provider_conditions_complete_required_matrix(self):
        denial = {
            "tool_name": "Bash",
            "tool_use_id": "toolu_1",
            "tool_input": {"command": "git status"},
        }
        cases = (
            (
                "permission_denial",
                canonical_result(
                    errors=("Permission denied",), permission_denials=(denial,)
                ),
                "PAUSED_POLICY",
            ),
            (
                "sandbox_unavailable",
                canonical_result(errors=("Sandbox required but unavailable",)),
                "PAUSED_POLICY",
            ),
            (
                "cancellation",
                canonical_result(errors=("Request cancelled",)),
                "FAILED",
            ),
        )
        for classification, payload, expected_status in cases:
            with self.subTest(classification=classification):
                tracker = policy_type()(
                    model_requested="fable",
                    max_tokens=10_000_000,
                    max_turns=10,
                    max_cost_usd=Decimal("1"),
                )
                self.assertIsNone(
                    tracker.observe(
                        claude.parse_event(json.dumps(canonical_init("fable")))
                    )
                )
                decision = tracker.observe(claude.parse_event(json.dumps(payload)))
                self.assertEqual(decision.classification, classification)
                self.assertEqual(decision.status, expected_status)


class ProviderPolicyTrackerTests(unittest.TestCase):
    def tracker(self, *, initialized=True, **overrides):
        values = {
            "model_requested": "fable",
            "max_tokens": 10_000_000,
            "max_turns": 2,
            "max_cost_usd": Decimal("1"),
        }
        values.update(overrides)
        tracker = policy_type()(**values)
        if initialized:
            self.assertIsNone(
                tracker.observe(
                    claude.parse_event(
                        json.dumps(canonical_init(values["model_requested"]))
                    )
                )
            )
        return tracker

    def test_one_pre_effect_transport_retry_is_allowed(self):
        tracker = self.tracker()

        self.assertIsNone(
            tracker.observe(claude.parse_event(json.dumps(canonical_retry())))
        )
        self.assertEqual(tracker.retry_count, 1)

    def test_reported_init_effort_must_match_requested_effort_when_present(self):
        tracker = self.tracker(initialized=False, effort_requested="max")
        init = canonical_init("fable")
        init["effort"] = "high"

        decision = tracker.observe(claude.parse_event(json.dumps(init)))

        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "effort_mismatch")
        self.assertEqual(decision.reason, "requested_effort_not_applied")

        for reported in (None, "max"):
            matching = self.tracker(initialized=False, effort_requested="max")
            init = canonical_init("fable")
            if reported is not None:
                init["effort"] = reported
            with self.subTest(reported=reported):
                self.assertIsNone(
                    matching.observe(claude.parse_event(json.dumps(init)))
                )

    def test_second_transport_retry_pauses(self):
        tracker = self.tracker()
        self.assertIsNone(
            tracker.observe(claude.parse_event(json.dumps(canonical_retry())))
        )

        decision = tracker.observe(
            claude.parse_event(json.dumps(canonical_retry(attempt=2)))
        )

        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "transport")
        self.assertEqual(decision.reason, "api_retry_limit_exceeded")

    def test_retry_after_assistant_or_tool_effect_pauses(self):
        effects = (
            canonical_assistant("msg_text"),
            canonical_assistant(
                "msg_tool",
                output_tokens=0,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/x"},
                    }
                ],
            ),
        )
        for effect in effects:
            with self.subTest(effect=effect["message"]["id"]):
                tracker = self.tracker()
                self.assertIsNone(
                    tracker.observe(claude.parse_event(json.dumps(effect)))
                )
                decision = tracker.observe(
                    claude.parse_event(json.dumps(canonical_retry()))
                )
                self.assertEqual(decision.status, "PAUSED_POLICY")
                self.assertEqual(decision.reason, "api_retry_after_effect")

    def test_distinct_assistant_ids_not_provider_num_turns_enforce_turn_limit(self):
        tracker = self.tracker(max_turns=2)
        first = claude.parse_event(json.dumps(canonical_assistant("msg_1")))
        duplicate = claude.parse_event(json.dumps(canonical_assistant("msg_1")))
        second = claude.parse_event(json.dumps(canonical_assistant("msg_2")))
        third = claude.parse_event(json.dumps(canonical_assistant("msg_3")))

        self.assertIsNone(tracker.observe(first))
        self.assertIsNone(tracker.observe(duplicate))
        self.assertIsNone(tracker.observe(second))
        decision = tracker.observe(third)

        self.assertEqual(tracker.assistant_message_ids, ("msg_1", "msg_2", "msg_3"))
        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "turn_limit")

    def test_ordered_observed_models_and_cli_accepted_model_are_separate(self):
        tracker = self.tracker(initialized=False)
        self.assertIsNone(
            tracker.observe(claude.parse_event(json.dumps(canonical_init("fable"))))
        )
        self.assertIsNone(
            tracker.observe(
                claude.parse_event(
                    json.dumps(canonical_assistant("msg_1", model="claude-opus-4-6"))
                )
            )
        )

        receipt = tracker.model_receipt()

        self.assertEqual(receipt["model_requested"], "fable")
        self.assertEqual(receipt["model_cli_accepted"], "fable")
        self.assertEqual(receipt["models_observed"], ["claude-opus-4-6"])
        self.assertEqual(receipt["verification_level"], "alias_observed")
        self.assertNotEqual(receipt["model_requested"], receipt["models_observed"][0])

    def test_alias_verification_requires_cli_init_evidence(self):
        tracker = self.tracker(initialized=False)
        decision = tracker.observe(
            claude.parse_event(
                json.dumps(canonical_assistant("msg_1", model="claude-opus-4-6"))
            )
        )

        self.assertEqual(decision.reason, "assistant_before_init")
        self.assertEqual(tracker.model_receipt()["verification_level"], "unverified")

    def test_provider_stream_requires_one_ordered_init_except_synthetic_api_error(self):
        real_assistant = self.tracker(initialized=False)
        decision = real_assistant.observe(
            claude.parse_event(json.dumps(canonical_assistant("msg_before_init")))
        )
        self.assertEqual(decision.status, "FAILED")
        self.assertEqual(decision.classification, "protocol")
        self.assertEqual(decision.reason, "assistant_before_init")
        self.assertEqual(real_assistant.models_observed, ())
        self.assertEqual(real_assistant.assistant_message_ids, ())
        self.assertFalse(real_assistant.has_effect)

        result_before_init = self.tracker(initialized=False)
        decision = result_before_init.observe(
            claude.parse_event(json.dumps(canonical_result()))
        )
        self.assertEqual(decision.status, "FAILED")
        self.assertEqual(decision.classification, "protocol")
        self.assertEqual(decision.reason, "result_before_init")
        self.assertEqual(result_before_init.models_observed, ())

        duplicate = self.tracker(initialized=False)
        self.assertIsNone(duplicate.observe(claude.parse_event(json.dumps(canonical_init("fable")))))
        decision = duplicate.observe(
            claude.parse_event(json.dumps(canonical_init("fable")))
        )
        self.assertEqual(decision.status, "FAILED")
        self.assertEqual(decision.classification, "protocol")
        self.assertEqual(decision.reason, "duplicate_init")

        synthetic = self.tracker(initialized=False)
        decision = synthetic.observe(
            claude.parse_event(
                json.dumps(
                    canonical_assistant(
                        "msg_synthetic_before_init",
                        provider_error="authentication_failed",
                    )
                )
            )
        )
        self.assertEqual(decision.status, "PAUSED_PROVIDER_ACCESS")
        self.assertEqual(decision.classification, "authentication")

    def test_effect_bearing_tool_events_before_init_fail_the_ordered_stream(self):
        for event_type in ("tool_progress", "tool_use_summary"):
            with self.subTest(event_type=event_type):
                tracker = self.tracker(initialized=False)
                event = claude.parse_event(json.dumps({"type": event_type}))
                self.assertTrue(event.has_effect)

                decision = tracker.observe(event)

                self.assertEqual(decision.status, "FAILED")
                self.assertEqual(decision.classification, "protocol")
                self.assertEqual(decision.reason, f"{event_type}_before_init")
                self.assertFalse(tracker.has_effect)

    def test_multiple_observed_models_pause_in_original_order(self):
        tracker = self.tracker(max_turns=10)
        self.assertIsNone(
            tracker.observe(
                claude.parse_event(
                    json.dumps(canonical_assistant("msg_1", model="claude-opus-4-6"))
                )
            )
        )

        decision = tracker.observe(
            claude.parse_event(
                json.dumps(canonical_assistant("msg_2", model="claude-sonnet-4-6"))
            )
        )

        self.assertEqual(tracker.models_observed, ("claude-opus-4-6", "claude-sonnet-4-6"))
        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "model_mismatch")

    def test_empty_assistant_still_counts_its_id_and_model_truth(self):
        tracker = self.tracker(max_turns=10)
        empty = canonical_assistant(
            "msg_empty",
            model="claude-opus-4-6",
            content=[],
            output_tokens=0,
        )

        self.assertIsNone(tracker.observe(claude.parse_event(json.dumps(empty))))
        self.assertFalse(tracker.has_effect)
        self.assertEqual(tracker.assistant_message_ids, ("msg_empty",))
        self.assertEqual(tracker.models_observed, ("claude-opus-4-6",))
        decision = tracker.observe(
            claude.parse_event(
                json.dumps(
                    canonical_assistant(
                        "msg_effect",
                        model="claude-sonnet-4-6",
                    )
                )
            )
        )

        self.assertEqual(
            tracker.assistant_message_ids,
            ("msg_empty", "msg_effect"),
        )
        self.assertEqual(
            tracker.models_observed,
            ("claude-opus-4-6", "claude-sonnet-4-6"),
        )
        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "model_mismatch")
        self.assertEqual(decision.reason, "multiple_models_observed")

    def test_result_model_usage_allows_auxiliary_models_when_primary_is_present(self):
        tracker = self.tracker(max_turns=10)
        self.assertIsNone(
            tracker.observe(
                claude.parse_event(
                    json.dumps(canonical_assistant("msg_1", model="claude-opus-4-6"))
                )
            )
        )
        payload = canonical_result()
        payload["modelUsage"] = {
            "claude-opus-4-6": model_usage_entry(cost_usd=0.2),
            "<synthetic>": model_usage_entry(
                input_tokens=0, output_tokens=0, cost_usd=0
            ),
            "claude-sonnet-4-6": model_usage_entry(
                input_tokens=1, output_tokens=1, cost_usd=0.05
            ),
        }

        decision = tracker.observe(claude.parse_event(json.dumps(payload)))

        self.assertEqual(tracker.models_observed, ("claude-opus-4-6",))
        self.assertIsNone(decision)

    def test_result_model_usage_must_include_the_observed_primary_model(self):
        tracker = self.tracker(max_turns=10)
        self.assertIsNone(
            tracker.observe(
                claude.parse_event(
                    json.dumps(canonical_assistant("msg_1", model="claude-opus-4-6"))
                )
            )
        )
        payload = canonical_result()
        payload["modelUsage"] = {
            "claude-haiku-4-5-20251001": model_usage_entry(
                input_tokens=1, output_tokens=1, cost_usd=0.001
            )
        }

        decision = tracker.observe(claude.parse_event(json.dumps(payload)))

        self.assertEqual(tracker.models_observed, ("claude-opus-4-6",))
        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "model_mismatch")
        self.assertEqual(decision.reason, "primary_model_usage_missing")

    def test_full_name_request_mismatch_pauses_but_exact_match_verifies(self):
        mismatch = self.tracker(model_requested="claude-opus-4-6")
        decision = mismatch.observe(
            claude.parse_event(
                json.dumps(canonical_assistant("msg_1", model="claude-sonnet-4-6"))
            )
        )
        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.reason, "full_name_model_mismatch")

        exact = self.tracker(model_requested="claude-opus-4-6")
        self.assertIsNone(
            exact.observe(
                claude.parse_event(
                    json.dumps(canonical_assistant("msg_1", model="claude-opus-4-6"))
                )
            )
        )
        self.assertEqual(
            exact.model_receipt()["verification_level"], "full_name_exact"
        )

    def test_model_fallback_and_refusal_fail_closed(self):
        payloads = (
            {
                "type": "system",
                "subtype": "model_fallback",
                "trigger": "overloaded",
                "original_model": "claude-opus-4-6",
                "fallback_model": "claude-sonnet-4-6",
                "content": "Switched models",
                "uuid": UUID,
                "session_id": SESSION_ID,
            },
            {
                "type": "system",
                "subtype": "model_refusal_fallback",
                "trigger": "refusal",
                "direction": "retry",
                "original_model": "claude-opus-4-6",
                "fallback_model": "claude-sonnet-4-6",
                "request_id": "req_1",
                "content": "Retried with another model",
                "uuid": UUID,
                "session_id": SESSION_ID,
            },
        )
        for payload in payloads:
            with self.subTest(subtype=payload["subtype"]):
                decision = self.tracker().observe(
                    claude.parse_event(json.dumps(payload))
                )
                self.assertEqual(decision.status, "PAUSED_POLICY")
                self.assertEqual(decision.classification, "model_fallback")
                self.assertFalse(decision.allow_substitution)

    def test_reported_cost_at_or_above_limit_pauses(self):
        below = self.tracker(max_cost_usd=Decimal("1"))
        self.assertIsNone(
            below.observe(
                claude.parse_event(
                    json.dumps(canonical_result(total_cost_usd=0.999999))
                )
            )
        )

        at_limit = self.tracker(max_cost_usd=Decimal("1"))
        decision = at_limit.observe(
            claude.parse_event(json.dumps(canonical_result(total_cost_usd=1.0)))
        )
        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "cost_limit")

    def test_final_cumulative_usage_enforces_tokens_without_event_double_counting(self):
        at_limit = policy_type()(
            model_requested="fable",
            max_tokens=13,
            max_turns=10,
            max_cost_usd=Decimal("1"),
        )
        self.assertIsNone(
            at_limit.observe(claude.parse_event(json.dumps(canonical_init("fable"))))
        )
        self.assertIsNone(
            at_limit.observe(
                claude.parse_event(json.dumps(canonical_assistant("msg_tokens")))
            )
        )
        decision = at_limit.observe(
            claude.parse_event(json.dumps(canonical_result()))
        )
        self.assertEqual(decision.status, "PAUSED_POLICY")
        self.assertEqual(decision.classification, "token_limit")
        self.assertEqual(decision.reason, "token_limit_reached")

        below_limit = policy_type()(
            model_requested="fable",
            max_tokens=14,
            max_turns=10,
            max_cost_usd=Decimal("1"),
        )
        self.assertIsNone(
            below_limit.observe(
                claude.parse_event(json.dumps(canonical_init("fable")))
            )
        )
        self.assertIsNone(
            below_limit.observe(
                claude.parse_event(json.dumps(canonical_assistant("msg_tokens")))
            )
        )
        self.assertIsNone(
            below_limit.observe(claude.parse_event(json.dumps(canonical_result())))
        )

    def test_canonical_limit_result_subtypes_are_not_shadowed_by_generic_errors(self):
        cases = (
            ("error_max_turns", "turn_limit", "assistant_turn_limit_exceeded"),
            ("error_max_budget_usd", "cost_limit", "usd_limit_reached"),
        )
        for subtype, classification, reason in cases:
            with self.subTest(subtype=subtype):
                tracker = self.tracker(max_turns=10, max_cost_usd=Decimal("10"))
                decision = tracker.observe(
                    claude.parse_event(
                        json.dumps(
                            canonical_result(
                                subtype=subtype,
                                is_error=True,
                                errors=("Provider limit reached",),
                                total_cost_usd=0.01,
                            )
                        )
                    )
                )
                self.assertEqual(decision.status, "PAUSED_POLICY")
                self.assertEqual(decision.classification, classification)
                self.assertEqual(decision.reason, reason)

    def test_remaining_allowance_at_or_below_five_percent_pauses(self):
        def event(utilization):
            return claude.parse_event(
                json.dumps(
                    {
                        "type": "rate_limit_event",
                        "rate_limit_info": {
                            "status": "allowed_warning",
                            "rateLimitType": "five_hour",
                            "utilization": utilization,
                        },
                        "uuid": UUID,
                        "session_id": SESSION_ID,
                    }
                )
            )

        self.assertIsNone(self.tracker().observe(event(0.949999)))
        for utilization in (0.95, 1.0):
            with self.subTest(utilization=utilization):
                decision = self.tracker().observe(event(utilization))
                self.assertEqual(decision.status, "PAUSED_PROVIDER_ACCESS")
                self.assertEqual(decision.classification, "rate_limit")
                self.assertEqual(decision.reason, "remaining_allowance_at_or_below_5_percent")

    def test_rate_limit_threshold_compares_the_exact_json_decimal_lexeme(self):
        def event(lexeme):
            payload = {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed_warning",
                    "rateLimitType": "five_hour",
                    "utilization": "__EXACT_UTILIZATION__",
                },
                "uuid": UUID,
                "session_id": SESSION_ID,
            }
            wire = json.dumps(payload).replace(
                '"__EXACT_UTILIZATION__"', lexeme
            )
            return claude.parse_event(wire)

        self.assertIsNone(
            self.tracker().observe(event("0.949999999999999999"))
        )
        decision = self.tracker().observe(event("0.950000000000000000"))
        self.assertEqual(decision.status, "PAUSED_PROVIDER_ACCESS")
        self.assertEqual(decision.classification, "rate_limit")

    def test_fallback_model_flag_remains_forbidden(self):
        settings = ROOT / "tests" / "fixtures" / "provider-policy-settings.json"
        command = claude.build_command(
            claude.JobSpec(
                role="review",
                model="fable",
                effort="max",
                max_budget_usd=Decimal("1"),
                settings_path=settings.resolve(),
                prompt="Review the frozen plan.",
            )
        )
        self.assertNotIn("--fallback-model", command)


class BridgeHarness:
    def __init__(self, environment):
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(BRIDGE)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        self._next_id = 1

    def tool(self, name, arguments, timeout=5):
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError(f"no bridge response for {name}")
        line = self.process.stdout.readline()
        if not line:
            assert self.process.stderr is not None
            stderr = self.process.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"bridge exited during {name}: {stderr}")
        response = json.loads(line)
        if "error" in response:
            raise AssertionError(response)
        return response["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class ProviderPolicyIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_path = Path(temporary.name).resolve(strict=True)
        self.repo = self.make_repo("repo")
        self.state_path = self.temp_path / "state" / "state.sqlite3"
        self.argv_log = self.temp_path / "argv.jsonl"
        self.control_path = self.temp_path / FAKE_CONTROL_NAME
        self.environment = {
            "HOME": str(self.temp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LC_CTYPE": "C.UTF-8",
            "TMPDIR": str(self.temp_path),
            "SHELL": "/bin/sh",
            "USER": "forge-test",
            "FORGE_STATE_PATH": str(self.state_path),
            "FORGE_LAUNCH_WORKSPACE": str(self.repo),
            "FORGE_CLAUDE_EXECUTABLE": str(FAKE_CLAUDE),
        }
        self.bridges = []
        self.addCleanup(self.close_bridges)
        self.write_control(control_window_seconds=0)

    def make_repo(self, name):
        repository = self.temp_path / name
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "forge@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Forge Test"],
            check=True,
        )
        (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "baseline"],
            check=True,
        )
        seed_reconciled_base(repository)
        return repository

    def write_control(self, **updates):
        current = {}
        if self.control_path.exists():
            current = json.loads(self.control_path.read_text(encoding="utf-8"))
        current.update(
            {
                "argv_log": str(self.argv_log),
                "assistant_text": "bounded fake report",
                **updates,
            }
        )
        self.control_path.write_text(
            json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def product_admission(self, *, files=("tracked.txt",), preserved_paths=None):
        fixtures = {
            "Plan.md": "approved provider-output plan\n",
            "act2-review.json": json.dumps(
                {
                    "version": 1,
                    "unit_id": "TASK-11-OUTPUT",
                    "reviewer_family": "claude-opus",
                    "verdict": "APPROVED",
                    "receipt_id": "act2-task-11-output",
                },
                sort_keys=True,
            )
            + "\n",
            "execution-contract.md": (
                "Claude may change only declared product files and must not change "
                "Git or Codex-owned controls.\n"
            ),
            "project-overlay.md": "executor_policy=claude-only\n",
        }
        for name, content in fixtures.items():
            path = self.repo / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), "add", "--", *fixtures],
            check=True,
        )
        if subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), "diff", "--cached", "--quiet"],
            check=False,
        ).returncode:
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(self.repo),
                    "commit",
                    "-qm",
                    "provider output controls",
                ],
                check=True,
            )
        head = subprocess.run(
            ["/usr/bin/git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(self.repo),
                "update-ref",
                "refs/remotes/origin/launch/reconciled-base",
                head,
            ],
            check=True,
        )
        controls = {
            "plan": self.repo / "Plan.md",
            "review_log": self.repo / "act2-review.json",
            "execution_contract": self.repo / "execution-contract.md",
            "project_overlay": self.repo / "project-overlay.md",
        }
        admission = {
            "weight": "LIGHT",
            "proof_scope": "targeted",
            "execution_profile": "sprint",
            "proof_argv": [
                "python3",
                "-m",
                "unittest",
                "tests.test_provider_policy",
            ],
            "files": list(files),
            "base_sha": head,
            "verdict": "APPROVED",
            "backlog_ids": ["TASK-11-OUTPUT"],
            "controls": {
                name: {"path": str(path), "sha256": sha256(path)}
                for name, path in controls.items()
            },
            "oracle": {"exact": False},
        }
        if preserved_paths is not None:
            admission["preserved_paths"] = list(preserved_paths)
        return admission

    def start_product(self, bridge, *, key, admission=None):
        declaration = self.product_admission() if admission is None else admission
        response = bridge.tool(
            "forge_start",
            self.start_arguments(
                role="build",
                idempotency_key=key,
                admission=declaration,
            ),
        )
        self.assertFalse(response["isError"], response)
        return response["structuredContent"]["job_id"]

    def boundary_event(self, job_id):
        from forge_lib.state import Store

        events = Store(self.state_path).list_events(job_id)
        return next(
            (event for event in events if event.kind == "OUTPUT_BOUNDARY_VIOLATION"),
            None,
        )

    def test_product_provider_may_change_one_declared_file(self):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={"tracked.txt": "declared provider output\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="declared-provider-output")
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        self.assertEqual(
            (self.repo / "tracked.txt").read_text(encoding="utf-8"),
            "declared provider output\n",
        )
        self.assertIsNone(self.boundary_event(job_id))

    def test_product_provider_may_create_one_declared_nested_file(self):
        (self.repo / "src").mkdir()
        admission = self.product_admission(files=("src/new.py",))
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={"src/new.py": "declared nested output\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(
            bridge,
            key="declared-nested-provider-output",
            admission=admission,
        )
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        self.assertEqual(
            (self.repo / "src" / "new.py").read_text(encoding="utf-8"),
            "declared nested output\n",
        )
        self.assertIsNone(self.boundary_event(job_id))

    def test_existing_declared_external_hardlink_is_rejected_before_provider(self):
        admission = self.product_admission()
        external = self.temp_path / "external-authority.txt"
        external.write_text("baseline\n", encoding="utf-8")
        tracked = self.repo / "tracked.txt"
        tracked.unlink()
        os.link(external, tracked, follow_symlinks=False)
        self.assertEqual(tracked.stat().st_nlink, 2)
        bridge = self.bridge()

        response = bridge.tool(
            "forge_start",
            self.start_arguments(
                role="build",
                idempotency_key="declared-external-hardlink",
                admission=admission,
            ),
        )

        self.assertTrue(response["isError"], response)
        self.assertEqual(external.read_text(encoding="utf-8"), "baseline\n")
        from forge_lib.state import Store

        with Store(self.state_path).connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                0,
            )
        provider_commands = []
        if self.argv_log.exists():
            provider_commands = [
                json.loads(line)
                for line in self.argv_log.read_text(encoding="utf-8").splitlines()
                if "-p" in json.loads(line)
            ]
        self.assertEqual(provider_commands, [])

    def test_provider_created_external_hardlink_fails_output_boundary(self):
        external = self.temp_path / "external-provider-authority.txt"
        external.write_text("baseline\n", encoding="utf-8")
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            hardlink_files={"tracked.txt": str(external)},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="provider-created-hardlink")
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertEqual(external.read_text(encoding="utf-8"), "baseline\n")
        self.assertEqual((self.repo / "tracked.txt").stat().st_nlink, 2)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertEqual(
            event.payload["violations"]["declared_path_types"],
            ["tracked.txt"],
        )

    def test_undeclared_provider_output_fails_closed_without_reverting_user_bytes(self):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={"undeclared.txt": "provider-owned bytes\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="undeclared-provider-output")
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertEqual(
            (self.repo / "undeclared.txt").read_text(encoding="utf-8"),
            "provider-owned bytes\n",
        )
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertEqual(event.payload["classification"], "provider_output_boundary")
        self.assertEqual(event.payload["violations"]["undeclared_paths"], ["undeclared.txt"])
        body, receipt = self.result_receipt(bridge, job_id)
        self.assertEqual(body["status"], "FAILED")
        self.assertEqual(receipt["terminal_status"], "FAILED")
        self.assertNotEqual(receipt["terminal_status"], "SUCCEEDED")

    def test_generated_tool_cache_churn_is_allowed_at_exact_roots(self):
        (self.repo / "node_modules").mkdir()
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            make_directories=[
                ".next",
                ".next/dev",
                ".next/dev/cache",
                ".next/dev/cache/webpack",
                "node_modules/.vite",
                "node_modules/.vite/vitest",
                "node_modules/.vite/vitest/results",
            ],
            mutate_files={
                ".next/dev/cache/webpack/index.pack": "generated cache\n",
                ".next/dev/trace": "generated trace\n",
                "node_modules/.vite/vitest/results/metadata.json": "{}\n",
            },
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="exact-generated-cache-output")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        self.assertIsNone(self.boundary_event(job_id))

    def test_generated_tool_cache_near_misses_and_dependency_edits_are_rejected(self):
        for directory in ("src", "node_modules"):
            (self.repo / directory).mkdir()
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            make_directories=[
                ".next-source",
                ".next-source/cache",
                "src/.next",
                "node_modules/.vite-source",
                "node_modules/.vite-source/cache",
                "node_modules/real-package",
            ],
            mutate_files={
                ".next-source/cache/payload": "not an exact cache root\n",
                "src/.next/payload": "nested source cache\n",
                "node_modules/.vite-source/cache/payload": "near miss\n",
                "node_modules/real-package/index.js": "dependency mutation\n",
            },
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="generated-cache-near-miss-output")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        undeclared = event.payload["violations"]["undeclared_paths"]
        self.assertIn(".next-source/cache/payload", undeclared)
        self.assertIn("src/.next/payload", undeclared)
        self.assertIn("node_modules/.vite-source/cache/payload", undeclared)
        self.assertIn("node_modules/real-package/index.js", undeclared)

    def test_generated_cache_root_requires_exact_opaque_directory_identity(self):
        opaque = ("opaque_directory", 1, 2, os.geteuid(), 0o755)
        unsafe = {
            "ordinary-directory": ("directory", 1, 2, os.geteuid(), 0o755, 2),
            "regular": ("regular", 1, 3, os.geteuid(), 0o644, 1, 0, 5, 6, "0" * 64),
            "symlink": ("symlink", 1, 4, os.geteuid(), 0o777, 1, 3, 5, 6, "1" * 64),
            "special": ("other", 1, 5, os.geteuid(), 0o10644, 1, 0, 5, 6),
        }

        self.assertEqual(
            worker._final_generated_cache_roots({".next": opaque}),
            frozenset({".next"}),
        )
        for label, fact in unsafe.items():
            with self.subTest(label=label):
                self.assertEqual(
                    worker._final_generated_cache_roots({".next": fact}),
                    frozenset(),
                )
        replaced = ("opaque_directory", 1, 9, os.geteuid(), 0o755)
        self.assertFalse(
            worker._allowed_generated_cache_change(
                ".next", opaque, replaced, frozenset({".next"})
            )
        )
        self.assertEqual(
            worker._final_generated_cache_roots(
                {".next": ("opaque_directory", 1, 2, os.geteuid(), 0o775)}
            ),
            frozenset(),
        )
        self.assertEqual(
            worker._final_generated_cache_roots(
                {".next": ("opaque_directory", 1, 2, os.geteuid() + 1, 0o755)}
            ),
            frozenset(),
        )

    def test_opaque_cache_snapshot_revalidates_root_before_skipping_descendants(self):
        snapshot_root = self.temp_path / "opaque-cache-race"
        cache = snapshot_root / ".next"
        cache.mkdir(parents=True)
        original_fact = worker._output_path_fact_at
        raced = False

        def replace_after_fact(directory_fd, name, information, *, deadline):
            nonlocal raced
            fact = original_fact(
                directory_fd,
                name,
                information,
                deadline=deadline,
            )
            if name == ".next" and not raced:
                raced = True
                cache.rename(snapshot_root / ".next-replaced")
                cache.mkdir()
            return fact

        with mock.patch.object(
            worker,
            "_output_path_fact_at",
            side_effect=replace_after_fact,
        ), self.assertRaisesRegex(worker.WorkerError, "changed while opened"):
            worker._snapshot_output_tree_once(
                snapshot_root,
                opaque_directories=frozenset({".next"}),
                deadline=time.monotonic() + 5,
            )
        self.assertTrue(raced)

    def test_generated_cache_snapshot_is_opaque_only_at_exact_roots(self):
        snapshot_root = self.temp_path / "opaque-cache-snapshot"
        (snapshot_root / ".next" / "dev" / "cache").mkdir(parents=True)
        (snapshot_root / ".next" / "dev" / "cache" / "payload").write_text(
            "cache\n", encoding="utf-8"
        )
        (snapshot_root / "node_modules" / ".vite" / "vitest").mkdir(parents=True)
        (snapshot_root / "node_modules" / ".vite" / "vitest" / "payload").write_text(
            "cache\n", encoding="utf-8"
        )
        package = snapshot_root / "node_modules" / "real-package"
        package.mkdir()
        (package / "index.js").write_text("source dependency\n", encoding="utf-8")

        facts = worker._stable_output_tree(
            snapshot_root,
            opaque_directories=frozenset(worker._GENERATED_CACHE_ROOTS),
        )

        self.assertEqual(facts[".next"][0], "opaque_directory")
        self.assertEqual(facts["node_modules/.vite"][0], "opaque_directory")
        self.assertFalse(any(path.startswith(".next/") for path in facts))
        self.assertFalse(
            any(path.startswith("node_modules/.vite/") for path in facts)
        )
        self.assertIn("node_modules/real-package/index.js", facts)

    def test_existing_generated_cache_content_may_be_overwritten(self):
        (self.repo / ".gitignore").write_text(".next/\n", encoding="utf-8")
        cache = self.repo / ".next" / "dev" / "cache"
        cache.mkdir(parents=True)
        artifact = cache / "existing.pack"
        artifact.write_text("old cache\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", ".gitignore"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "ignore cache"],
            check=True,
        )
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={".next/dev/cache/existing.pack": "new cache\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="existing-generated-cache-output")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        self.assertEqual(artifact.read_text(encoding="utf-8"), "new cache\n")
        self.assertIsNone(self.boundary_event(job_id))

    def test_tracked_file_inside_cache_root_remains_inside_output_boundary(self):
        cache = self.repo / ".next"
        cache.mkdir()
        tracked_cache = cache / "tracked.txt"
        tracked_cache.write_text("tracked baseline\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "-f", ".next/tracked.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "tracked cache input"],
            check=True,
        )
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={".next/tracked.txt": "undeclared tracked mutation\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="tracked-cache-output-boundary")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertIn(
            ".next/tracked.txt",
            event.payload["violations"]["undeclared_paths"],
        )

    def test_admitted_product_file_cannot_be_inside_generated_cache(self):
        (self.repo / ".next").mkdir()
        admission = self.product_admission(files=(".next/product-output.txt",))
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={".next/product-output.txt": "must not run\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(
            bridge,
            key="declared-generated-cache-output",
            admission=admission,
        )
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertFalse((self.repo / ".next" / "product-output.txt").exists())

    def test_empty_claude_runtime_markers_are_allowed_at_any_parent(self):
        (self.repo / "controls").mkdir()
        (self.repo / "controls" / "unit").mkdir()
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            make_directories=[
                ".claude",
                ".claude/.cc-writes",
                "controls/unit/.claude",
                "controls/unit/.claude/.cc-writes",
            ],
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="empty-claude-runtime-markers")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        self.assertIsNone(self.boundary_event(job_id))

    def test_empty_marker_is_allowed_beside_unchanged_claude_content(self):
        claude_root = self.repo / ".claude"
        claude_root.mkdir()
        (claude_root / "settings.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", ".claude/settings.json"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "claude settings"],
            check=True,
        )
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            make_directories=[".claude/.cc-writes"],
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="empty-marker-beside-claude-content")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)
        self.assertIsNone(self.boundary_event(job_id))

    def test_claude_runtime_marker_payloads_are_rejected(self):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            make_directories=[".claude", ".claude/.cc-writes"],
            mutate_files={
                ".claude/settings.json": "not runtime-marker state\n",
                ".claude/.cc-writes/payload.txt": "not empty\n",
            },
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="claude-runtime-marker-payloads")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        undeclared = event.payload["violations"]["undeclared_paths"]
        self.assertIn(".claude/settings.json", undeclared)
        self.assertIn(".claude/.cc-writes/payload.txt", undeclared)

    def test_claude_runtime_marker_nested_directory_is_rejected(self):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            make_directories=[
                ".claude",
                ".claude/.cc-writes",
                ".claude/.cc-writes/nested",
            ],
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="claude-runtime-marker-nested-dir")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertIn(
            ".claude/.cc-writes/nested",
            event.payload["violations"]["undeclared_paths"],
        )

    def test_claude_runtime_marker_requires_safe_directory_identity(self):
        safe = ("directory", 1, 2, os.geteuid(), 0o755, 2)
        unsafe_roots = (
            ("directory", 1, 2, os.geteuid(), 0o775, 2),
            ("directory", 1, 2, os.geteuid() + 1, 0o755, 2),
            ("symlink", 1, 2, os.geteuid(), 0o777, 1, 0, 1, 1, "0" * 64),
            ("regular", 1, 2, os.geteuid(), 0o644, 1, 0, 1, 1, "0" * 64),
        )
        for fact in unsafe_roots:
            with self.subTest(fact_type=fact[0], owner=fact[3], mode=fact[4]):
                self.assertEqual(
                    worker._final_empty_claude_marker_roots(
                        {".claude": fact, ".claude/.cc-writes": safe}
                    ),
                    frozenset(),
                )
        replaced = ("directory", 1, 9, os.geteuid(), 0o755, 2)
        self.assertFalse(
            worker._allowed_empty_claude_marker_change(
                ".claude",
                safe,
                replaced,
                frozenset({".claude"}),
            )
        )

    def test_claude_runtime_marker_near_miss_is_rejected(self):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            make_directories=[".claude", ".claude/.cc-write"],
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="claude-runtime-marker-near-miss")
        terminal = self.wait_terminal_state(job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertIn(
            ".claude/.cc-write",
            event.payload["violations"]["undeclared_paths"],
        )

    def test_codex_control_mutation_is_typed_and_preserved_on_failure(self):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={"Plan.md": "provider tampered control\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="control-provider-output")
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertEqual(
            (self.repo / "Plan.md").read_text(encoding="utf-8"),
            "provider tampered control\n",
        )
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertEqual(event.payload["violations"]["control_roles"], ["plan"])

    def assert_git_mutation_fails(self, *, name, files, operations, expected_fact):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files=files,
            mutate_git=operations,
        )
        bridge = self.bridge()
        job_id = self.start_product(bridge, key=f"git-{name}-provider-output")
        terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(terminal["status"], "FAILED", terminal)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertIn(expected_fact, event.payload["violations"]["git_facts"])

    def test_git_index_mutation_fails_before_success(self):
        self.assert_git_mutation_fails(
            name="index",
            files={"tracked.txt": "staged provider output\n"},
            operations=[{"operation": "add", "paths": ["tracked.txt"]}],
            expected_fact="index",
        )

    def test_git_head_mutation_fails_before_success(self):
        self.assert_git_mutation_fails(
            name="head",
            files={"tracked.txt": "committed provider output\n"},
            operations=[
                {"operation": "add", "paths": ["tracked.txt"]},
                {"operation": "commit"},
            ],
            expected_fact="head",
        )

    def test_git_refs_mutation_fails_before_success(self):
        self.assert_git_mutation_fails(
            name="refs",
            files={},
            operations=[
                {
                    "operation": "update_ref",
                    "ref": "refs/heads/provider-output",
                    "target": "HEAD",
                }
            ],
            expected_fact="refs",
        )

    def test_raw_git_metadata_mutation_fails_and_is_not_reverted(self):
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={".git/provider-output-metadata": "hostile metadata\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="raw-git-metadata-provider-output")
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertEqual(
            (self.repo / ".git" / "provider-output-metadata").read_text(
                encoding="utf-8"
            ),
            "hostile metadata\n",
        )
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertIn("metadata", event.payload["violations"]["git_facts"])

    def test_provider_access_failure_cannot_hide_an_undeclared_mutation(self):
        self.write_control(
            provider_error="billing",
            control_window_seconds=0,
            mutate_files={"undeclared-on-error.txt": "preserved boundary breach\n"},
        )
        bridge = self.bridge()

        job_id = self.start_product(bridge, key="provider-error-output-boundary")
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        event = self.boundary_event(job_id)
        self.assertIsNotNone(event)
        self.assertEqual(
            event.payload["violations"]["undeclared_paths"],
            ["undeclared-on-error.txt"],
        )

    def test_recovery_is_refused_before_preserved_file_or_provider_effect(self):
        admission = self.product_admission(
            files=("tracked.txt",),
            preserved_paths=("tracked.txt",),
        )
        (self.repo / "tracked.txt").write_text(
            "preserved recovery input\n",
            encoding="utf-8",
        )
        self.write_control(
            provider_error=None,
            control_window_seconds=0,
            mutate_files={"tracked.txt": "recovered provider output\n"},
        )
        bridge = self.bridge()
        response = bridge.tool(
            "forge_start",
            self.start_arguments(
                role="recovery",
                idempotency_key="recovery-provider-output",
                admission=admission,
            ),
        )
        self.assertTrue(response["isError"], response)
        self.assertEqual(
            response["structuredContent"]["message"],
            "Forge service initialization failed.",
        )
        self.assertEqual(
            (self.repo / "tracked.txt").read_text(encoding="utf-8"),
            "preserved recovery input\n",
        )
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.argv_log.exists())

    def bridge(self, *, launch_workspace=None):
        environment = dict(self.environment)
        if launch_workspace is not None:
            environment["FORGE_LAUNCH_WORKSPACE"] = str(launch_workspace)
        bridge = BridgeHarness(environment)
        self.bridges.append(bridge)
        return bridge

    def close_bridges(self):
        for bridge in reversed(self.bridges):
            bridge.close()

    def budget(self, **overrides):
        value = {
            "max_seconds": 5,
            "max_tokens": 4096,
            "max_turns": 8,
            "max_cost_usd": "1",
        }
        value.update(overrides)
        return value

    def start_arguments(self, **overrides):
        value = {
            "prompt": "Review the frozen plan.",
            "cwd": str(self.repo),
            "role": "review",
            "model": "fable",
            "effort": "max",
            "idempotency_key": f"policy-{time.monotonic_ns()}",
            "budget": self.budget(),
            "admission": review_admission(module="tests.test_provider_policy"),
        }
        value.update(overrides)
        return value

    def wait_terminal(self, bridge, job_id, timeout=10):
        deadline = time.monotonic() + timeout
        revision = None
        last = None
        terminal = {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "PAUSED_PROVIDER_ACCESS",
            "PAUSED_POLICY",
        }
        while time.monotonic() < deadline:
            arguments = {"job_id": job_id, "timeout_ms": 250}
            if revision is not None:
                arguments["revision"] = revision
            response = bridge.tool("forge_wait", arguments, timeout=3)
            self.assertFalse(response["isError"], response)
            last = response["structuredContent"]
            revision = last["revision"]
            if last["status"] in terminal:
                return last
        self.fail(f"job did not reach a durable terminal state: {last}")

    def wait_terminal_state(self, job_id, timeout=10):
        """Poll durable state without racing workspace revalidation during mutations."""

        from forge_lib.processes import process_instance_is_live
        from forge_lib.state import Store

        store = Store(self.state_path)
        deadline = time.monotonic() + timeout
        terminal = {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "PAUSED_PROVIDER_ACCESS",
            "PAUSED_POLICY",
        }
        while time.monotonic() < deadline:
            job = store.get_job(job_id)
            if job.status in terminal:
                attempt = store.get_attempt(job.current_attempt_id)
                if (
                    attempt.worker_pid is None
                    or attempt.worker_identity is None
                    or not process_instance_is_live(
                        attempt.worker_pid,
                        attempt.worker_identity,
                    )
                ):
                    return {
                        "status": job.status,
                        "job_id": job.id,
                        "revision": job.revision,
                    }
            time.sleep(0.01)
        self.fail(f"job did not reach a durable terminal state: {job.status}")

    def result_receipt(self, bridge, job_id):
        response = bridge.tool("forge_result", {"job_id": job_id})
        self.assertFalse(response["isError"], response)
        body = response["structuredContent"]
        receipt_path = Path(body["artifacts"]["receipt"]["path"])
        return body, json.loads(receipt_path.read_text(encoding="utf-8"))

    def assert_provider_quiescent(self, provider_pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(provider_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        self.fail(f"provider process {provider_pid} survived terminalization")

    def attempt(self, job_id):
        from forge_lib.state import Store

        store = Store(self.state_path)
        job = store.get_job(job_id)
        return store, store.get_attempt(job.current_attempt_id)

    def test_access_and_policy_failures_have_typed_durable_receipts_and_model_truth(self):
        cases = (
            ("authentication", "PAUSED_PROVIDER_ACCESS", "authentication", [], "unverified"),
            ("oauth_organization", "PAUSED_PROVIDER_ACCESS", "oauth_organization", [], "unverified"),
            ("billing", "PAUSED_PROVIDER_ACCESS", "billing", [], "unverified"),
            ("rate_limit", "PAUSED_PROVIDER_ACCESS", "rate_limit", [], "unverified"),
            ("model_unavailable", "PAUSED_PROVIDER_ACCESS", "model_not_found", [], "unverified"),
            ("permission", "PAUSED_POLICY", "permission_denial", ["fable"], "alias_observed"),
            ("sandbox_unavailable", "PAUSED_POLICY", "sandbox_unavailable", ["fable"], "alias_observed"),
        )
        bridge = self.bridge()
        for provider_error, status, classification, models, verification in cases:
            with self.subTest(provider_error=provider_error):
                self.write_control(provider_error=provider_error, control_window_seconds=0)
                started = bridge.tool(
                    "forge_start",
                    self.start_arguments(
                        idempotency_key=f"typed-{provider_error}"
                    ),
                )
                self.assertFalse(started["isError"], started)
                job_id = started["structuredContent"]["job_id"]
                terminal = self.wait_terminal(bridge, job_id)
                self.assertEqual(terminal["status"], status, terminal)
                self.assertEqual(terminal["pause"]["classification"], classification)
                status_body = bridge.tool("forge_status", {"job_id": job_id})[
                    "structuredContent"
                ]
                self.assertEqual(status_body["pause"], terminal["pause"])
                store, attempt = self.attempt(job_id)
                self.assertIsNotNone(attempt.provider_pgid)
                body, receipt = self.result_receipt(bridge, job_id)
                self.assertEqual(body["status"], status)
                self.assertEqual(body["pause"], terminal["pause"])
                self.assertEqual(receipt["terminal_status"], status)
                self.assertEqual(receipt["pause"]["classification"], classification)
                self.assertTrue(receipt["pause"]["reason"])
                self.assertEqual(receipt["model_requested"], "fable")
                self.assertEqual(receipt["model_cli_accepted"], "fable")
                self.assertEqual(receipt["models_observed"], models)
                self.assertEqual(receipt["verification_level"], verification)
                self.assert_provider_quiescent(attempt.provider_pgid)
                events = store.list_events(job_id)
                pauses = [event for event in events if event.kind == "PROVIDER_PAUSED"]
                self.assertEqual(pauses[-1].payload["classification"], classification)

    def test_canonical_transport_protocol_cancellation_and_internal_fail_closed(self):
        cases = (
            ("transient_transport", "transport"),
            ("protocol", "protocol"),
            ("cancellation", "cancellation"),
            ("internal", "internal_failure"),
        )
        bridge = self.bridge()
        for provider_error, classification in cases:
            with self.subTest(provider_error=provider_error):
                self.write_control(provider_error=provider_error, control_window_seconds=0)
                started = bridge.tool(
                    "forge_start",
                    self.start_arguments(idempotency_key=f"failed-{provider_error}"),
                )
                job_id = started["structuredContent"]["job_id"]
                terminal = self.wait_terminal(bridge, job_id)
                self.assertEqual(terminal["status"], "FAILED", terminal)
                body, receipt = self.result_receipt(bridge, job_id)
                self.assertEqual(body["failure"]["classification"], classification)
                self.assertEqual(
                    receipt["error"]["classification"], classification
                )
                _store, attempt = self.attempt(job_id)
                self.assert_provider_quiescent(attempt.provider_pgid)

    def test_worker_enforces_wall_turn_and_usd_limits(self):
        cases = (
            (
                "wall",
                {"control_window_seconds": 5},
                self.budget(max_seconds=3),
                "wall_limit",
            ),
            (
                "turn",
                {"extra_assistant_events": 2, "control_window_seconds": 0},
                self.budget(max_turns=1),
                "turn_limit",
            ),
            (
                "token",
                {"control_window_seconds": 0},
                self.budget(max_tokens=13),
                "token_limit",
            ),
            (
                "usd",
                {"total_cost_usd": 0.25, "control_window_seconds": 0},
                self.budget(max_cost_usd="0.25"),
                "cost_limit",
            ),
        )
        bridge = self.bridge()
        for name, control, budget, classification in cases:
            with self.subTest(limit=name):
                fake_control = {
                    "provider_error": None,
                    "extra_assistant_events": 0,
                    "total_cost_usd": 0.0123,
                }
                fake_control.update(control)
                self.write_control(**fake_control)
                started = bridge.tool(
                    "forge_start",
                    self.start_arguments(
                        idempotency_key=f"limit-{name}", budget=budget
                    ),
                )
                self.assertFalse(started["isError"], started)
                job_id = started["structuredContent"]["job_id"]
                terminal = self.wait_terminal(bridge, job_id)
                self.assertEqual(terminal["status"], "PAUSED_POLICY", terminal)
                _body, receipt = self.result_receipt(bridge, job_id)
                self.assertEqual(receipt["pause"]["classification"], classification)
                self.assertEqual(receipt["budget"], budget)
                _store, attempt = self.attempt(job_id)
                self.assert_provider_quiescent(attempt.provider_pgid)
        commands = [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
            if "--max-budget-usd" in json.loads(line)
        ]
        usd_command = commands[-1]
        self.assertEqual(
            usd_command[usd_command.index("--max-budget-usd") + 1], "0.25"
        )
        self.assertNotIn("--fallback-model", usd_command)

    def test_wall_deadline_includes_preflight_before_any_provider_spawn(self):
        self.write_control(provider_error=None, delay_seconds=2)
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(
                idempotency_key="preflight-wall",
                budget=self.budget(max_seconds=3),
            ),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "PAUSED_POLICY", terminal)
        self.assertEqual(terminal["pause"]["classification"], "wall_limit")
        _store, attempt = self.attempt(job_id)
        self.assertIsNone(attempt.provider_pgid)
        commands = [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertFalse(any("-p" in command for command in commands))

    def test_resumed_attempt_preflight_wall_limit_terminalizes_without_redispatch(self):
        self.write_control(provider_error="billing", control_window_seconds=0)
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(
                idempotency_key="resume-preflight-wall",
                budget=self.budget(max_seconds=3),
            ),
        )
        job_id = started["structuredContent"]["job_id"]
        first = self.wait_terminal(bridge, job_id)
        self.assertEqual(first["status"], "PAUSED_PROVIDER_ACCESS", first)

        self.write_control(
            provider_error=None,
            delay_seconds=2,
            control_window_seconds=0,
        )
        resumed = bridge.tool(
            "forge_resume",
            {"job_id": job_id, "revision": first["revision"]},
        )
        self.assertFalse(resumed["isError"], resumed)
        second = self.wait_terminal(bridge, job_id, timeout=6)

        self.assertEqual(second["status"], "PAUSED_POLICY", second)
        self.assertEqual(second["pause"]["classification"], "wall_limit")
        from forge_lib.state import Store

        attempts = Store(self.state_path).list_attempts(job_id)
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(attempts[1].provider_pgid)
        provider_commands = [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
            if "-p" in json.loads(line)
        ]
        self.assertEqual(len(provider_commands), 1)

    def test_rate_limit_remaining_allowance_threshold_pauses_end_to_end(self):
        self.write_control(
            provider_error=None,
            rate_limit_utilization=0.95,
            rate_limit_status="allowed_warning",
            control_window_seconds=2,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="rate-threshold"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "PAUSED_PROVIDER_ACCESS", terminal)
        self.assertEqual(terminal["pause"]["classification"], "rate_limit")
        _body, receipt = self.result_receipt(bridge, job_id)
        self.assertEqual(
            receipt["pause"]["reason"],
            "remaining_allowance_at_or_below_5_percent",
        )
        _store, attempt = self.attempt(job_id)
        self.assert_provider_quiescent(attempt.provider_pgid)

    def test_resume_model_override_is_revision_owned_persisted_and_explicit(self):
        self.write_control(provider_error="billing", control_window_seconds=0)
        owner = self.bridge()
        started = owner.tool(
            "forge_start",
            self.start_arguments(
                idempotency_key="resume-override",
                budget=self.budget(max_seconds=10),
            ),
        )
        job_id = started["structuredContent"]["job_id"]
        first = self.wait_terminal(owner, job_id)
        self.assertEqual(first["status"], "PAUSED_PROVIDER_ACCESS", first)
        from forge_lib.state import Store

        store = Store(self.state_path)
        self.assertEqual(len(store.list_attempts(job_id)), 1)
        stale = owner.tool(
            "forge_resume",
            {
                "job_id": job_id,
                "revision": first["revision"] - 1,
                "model_override": "claude-opus-4-6",
            },
        )
        self.assertTrue(stale["isError"], stale)
        self.assertEqual(len(store.list_attempts(job_id)), 1)

        invalid_override = owner.tool(
            "forge_resume",
            {
                "job_id": job_id,
                "revision": first["revision"],
                "model_override": "--fallback-model",
            },
        )
        self.assertTrue(invalid_override["isError"], invalid_override)
        self.assertEqual(len(Store(self.state_path).list_attempts(job_id)), 1)

        self.write_control(provider_error=None, control_window_seconds=4)
        resumed = owner.tool(
            "forge_resume",
            {
                "job_id": job_id,
                "revision": first["revision"],
                "model_override": "claude-opus-4-6",
            },
        )
        self.assertFalse(resumed["isError"], resumed)
        pending = None
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            pending = owner.tool("forge_status", {"job_id": job_id})[
                "structuredContent"
            ]
            if (
                pending["status"] == "RUNNING"
                and pending["progress"]["last_kind"] == "CLAUDE_EVENT"
            ):
                break
            time.sleep(0.01)
        self.assertEqual(pending["status"], "RUNNING", pending)
        duplicate = owner.tool(
            "forge_resume",
            {
                "job_id": job_id,
                # An exact retry uses the terminal parent's accepted revision,
                # even though provider events have advanced the live child.
                "revision": first["revision"],
                "model_override": "claude-opus-4-6",
            },
        )
        self.assertFalse(duplicate["isError"], duplicate)
        self.assertTrue(duplicate["structuredContent"]["idempotent"])
        self.assertEqual(len(Store(self.state_path).list_attempts(job_id)), 2)
        conflict = owner.tool(
            "forge_resume",
            {
                "job_id": job_id,
                "revision": first["revision"],
                "model_override": "claude-sonnet-4-6",
            },
        )
        self.assertTrue(conflict["isError"], conflict)
        self.assertEqual(len(Store(self.state_path).list_attempts(job_id)), 2)
        stale_pending = owner.tool(
            "forge_resume",
            {
                "job_id": job_id,
                "revision": first["revision"] - 1,
                "model_override": "claude-opus-4-6",
            },
        )
        self.assertTrue(stale_pending["isError"], stale_pending)
        self.assertEqual(len(Store(self.state_path).list_attempts(job_id)), 2)
        second = self.wait_terminal(owner, job_id)
        self.assertEqual(second["status"], "SUCCEEDED", second)
        attempts = Store(self.state_path).list_attempts(job_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1].model_override, "claude-opus-4-6")
        immutable_job = Store(self.state_path).get_job(job_id)
        self.assertEqual(immutable_job.payload["model"], "fable")
        _body, receipt = self.result_receipt(owner, job_id)
        self.assertEqual(receipt["model_requested"], "claude-opus-4-6")
        self.assertEqual(receipt["model_cli_accepted"], "claude-opus-4-6")
        self.assertEqual(receipt["models_observed"], ["claude-opus-4-6"])
        self.assertEqual(receipt["verification_level"], "full_name_exact")
        commands = [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
            if "-p" in json.loads(line)
        ]
        resumed_command = commands[-1]
        self.assertEqual(
            resumed_command[resumed_command.index("--model") + 1],
            "claude-opus-4-6",
        )
        self.assertNotIn("--fallback-model", resumed_command)

        other_repo = self.make_repo("other")
        outsider = self.bridge(launch_workspace=other_repo)
        denied = outsider.tool(
            "forge_resume",
            {
                "job_id": job_id,
                "revision": second["revision"],
                "model_override": "claude-sonnet-4-6",
            },
        )
        self.assertTrue(denied["isError"], denied)
        self.assertEqual(len(Store(self.state_path).list_attempts(job_id)), 2)

        self.write_control(provider_error="billing", control_window_seconds=0)
        original = owner.tool(
            "forge_start",
            self.start_arguments(idempotency_key="resume-original-model"),
        )
        original_job_id = original["structuredContent"]["job_id"]
        original_pause = self.wait_terminal(owner, original_job_id)
        self.assertEqual(original_pause["status"], "PAUSED_PROVIDER_ACCESS")
        self.write_control(provider_error=None, control_window_seconds=0)
        omitted = owner.tool(
            "forge_resume",
            {
                "job_id": original_job_id,
                "revision": original_pause["revision"],
            },
        )
        self.assertFalse(omitted["isError"], omitted)
        original_done = self.wait_terminal(owner, original_job_id)
        self.assertEqual(original_done["status"], "SUCCEEDED", original_done)
        original_attempts = Store(self.state_path).list_attempts(original_job_id)
        self.assertIsNone(original_attempts[1].model_override)
        _original_body, original_receipt = self.result_receipt(
            owner, original_job_id
        )
        self.assertEqual(original_receipt["model_requested"], "fable")

    def test_api_retry_after_effect_terminalizes_typed_pause_and_quiesces(self):
        self.write_control(
            provider_error=None,
            api_retry_after_assistant=True,
            api_retry_attempts=1,
            control_window_seconds=2,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="retry-after-effect"),
        )
        self.assertFalse(started["isError"], started)
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(terminal["status"], "PAUSED_POLICY", terminal)
        _body, receipt = self.result_receipt(bridge, job_id)
        self.assertEqual(receipt["pause"]["classification"], "transport")
        self.assertEqual(receipt["pause"]["reason"], "api_retry_after_effect")
        _store, attempt = self.attempt(job_id)
        self.assert_provider_quiescent(attempt.provider_pgid)

    def test_assistant_before_init_has_durable_protocol_outcome(self):
        self.write_control(
            provider_error=None,
            omit_init=True,
            control_window_seconds=0,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="unverified-model-outcome"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertEqual(terminal["failure"]["classification"], "protocol")
        body, receipt = self.result_receipt(bridge, job_id)
        self.assertEqual(body["failure"], terminal["failure"])
        self.assertEqual(receipt["error"]["classification"], "protocol")
        store, attempt = self.attempt(job_id)
        outcomes = [
            event for event in store.list_events(job_id)
            if event.kind == "PROVIDER_FAILED"
        ]
        self.assertEqual(outcomes[-1].payload["reason"], "assistant_before_init")
        self.assert_provider_quiescent(attempt.provider_pgid)

    def test_wire_protocol_failure_has_durable_classified_outcome(self):
        self.write_control(
            provider_error=None,
            malformed_event=True,
            control_window_seconds=0,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="protocol-outcome"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertEqual(terminal["failure"]["classification"], "protocol")
        body, receipt = self.result_receipt(bridge, job_id)
        self.assertEqual(body["failure"], terminal["failure"])
        self.assertEqual(receipt["error"]["classification"], "protocol")
        store, attempt = self.attempt(job_id)
        outcomes = [
            event for event in store.list_events(job_id)
            if event.kind == "PROVIDER_FAILED"
        ]
        self.assertEqual(outcomes[-1].payload["reason"], "protocol_failure")
        self.assert_provider_quiescent(attempt.provider_pgid)

    def test_process_exit_failures_have_durable_classified_outcomes(self):
        cases = (
            (
                "nonzero",
                {"stream_exit": 7, "abrupt_eof_before_result": False},
                "transport",
                "provider_process_exit_nonzero",
            ),
            (
                "abrupt-eof",
                {"stream_exit": 0, "abrupt_eof_before_result": True},
                "internal_failure",
                "terminal_result_missing",
            ),
        )
        bridge = self.bridge()
        for name, controls, classification, reason in cases:
            with self.subTest(exit=name):
                self.write_control(
                    provider_error=None,
                    control_window_seconds=0,
                    **controls,
                )
                started = bridge.tool(
                    "forge_start",
                    self.start_arguments(idempotency_key=f"process-exit-{name}"),
                )
                job_id = started["structuredContent"]["job_id"]
                terminal = self.wait_terminal(bridge, job_id)
                self.assertEqual(terminal["status"], "FAILED", terminal)
                self.assertEqual(
                    terminal["failure"],
                    {"classification": classification, "reason": reason},
                )
                body, receipt = self.result_receipt(bridge, job_id)
                self.assertEqual(body["failure"], terminal["failure"])
                self.assertEqual(receipt["error"]["classification"], classification)
                store, attempt = self.attempt(job_id)
                outcomes = [
                    event for event in store.list_events(job_id)
                    if event.kind == "PROVIDER_FAILED"
                ]
                self.assertEqual(outcomes[-1].payload["reason"], reason)
                self.assert_provider_quiescent(attempt.provider_pgid)

    def test_null_reported_cost_cannot_bypass_the_worker_budget(self):
        self.write_control(
            provider_error=None,
            null_total_cost_usd=True,
            control_window_seconds=0,
        )
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(
                idempotency_key="null-cost-budget-bypass",
                budget=self.budget(max_cost_usd="0.000001"),
            ),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "FAILED", terminal)
        self.assertEqual(terminal["failure"]["classification"], "protocol")
        body, receipt = self.result_receipt(bridge, job_id)
        self.assertEqual(body["failure"], terminal["failure"])
        self.assertEqual(receipt["error"]["classification"], "protocol")
        _store, attempt = self.attempt(job_id)
        self.assert_provider_quiescent(attempt.provider_pgid)

    def test_unsupported_cli_version_pauses_before_provider_launch_with_evidence(self):
        observed_version = "2.1.154 (Claude Code)"
        self.write_control(
            provider_error=None,
            version_override=observed_version,
            control_window_seconds=0,
        )
        with self.assertRaises(claude.UnsupportedClaudeCodeVersion) as rejected:
            claude.preflight(
                str(FAKE_CLAUDE),
                environment=self.environment,
                timeout_seconds=2,
            )
        self.assertEqual(rejected.exception.observed_version, observed_version)
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="unsupported-cli-version"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)

        self.assertEqual(terminal["status"], "PAUSED_POLICY", terminal)
        self.assertEqual(terminal["pause"]["classification"], "provider_version")
        self.assertEqual(terminal["provider_version_observed"], observed_version)
        self.assertIn("2.1.248", terminal["pause"]["reason"])
        body, receipt = self.result_receipt(bridge, job_id)
        self.assertEqual(body["provider_version_observed"], observed_version)
        self.assertEqual(receipt["provider_version_observed"], observed_version)
        argv = [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn(["--version"], argv)
        self.assertFalse(
            any("--output-format" in invocation for invocation in argv),
            argv,
        )
        _store, attempt = self.attempt(job_id)
        self.assertIsNone(attempt.provider_pgid)

    def test_preflight_auth_command_failures_terminalize_with_typed_durable_outcomes(self):
        bridge = self.bridge()
        cases = (
            (
                "recognized-access",
                "authentication failed: invalid API key",
                "PAUSED_PROVIDER_ACCESS",
                "authentication",
                "authentication_failed",
                "pause",
                "PROVIDER_PAUSED",
            ),
            (
                "unrecognized",
                "local preflight helper exploded",
                "FAILED",
                "preflight_or_internal",
                "provider_preflight_failed",
                "failure",
                "PROVIDER_FAILED",
            ),
        )
        for (
            name,
            detail,
            expected_status,
            classification,
            reason,
            outcome_field,
            outcome_kind,
        ) in cases:
            with self.subTest(case=name):
                before_argv = (
                    len(self.argv_log.read_text(encoding="utf-8").splitlines())
                    if self.argv_log.exists()
                    else 0
                )
                self.write_control(
                    auth_exit=7,
                    auth_json=detail,
                    provider_error=None,
                    control_window_seconds=0,
                )
                started = bridge.tool(
                    "forge_start",
                    self.start_arguments(idempotency_key=f"preflight-{name}"),
                )
                job_id = started["structuredContent"]["job_id"]
                terminal = self.wait_terminal(bridge, job_id)

                self.assertEqual(terminal["status"], expected_status, terminal)
                self.assertEqual(
                    terminal[outcome_field],
                    {"classification": classification, "reason": reason},
                )
                self.assertEqual(
                    terminal["provider_version_observed"],
                    claude.SUPPORTED_CLAUDE_CODE_VERSION_OUTPUT,
                )
                body, receipt = self.result_receipt(bridge, job_id)
                self.assertEqual(body[outcome_field], terminal[outcome_field])
                receipt_outcome = (
                    receipt["pause"] if outcome_field == "pause" else receipt["error"]
                )
                self.assertEqual(receipt_outcome["classification"], classification)
                self.assertEqual(receipt_outcome["reason"], reason)
                self.assertEqual(
                    receipt["provider_version_observed"],
                    claude.SUPPORTED_CLAUDE_CODE_VERSION_OUTPUT,
                )
                store, attempt = self.attempt(job_id)
                self.assertIsNone(attempt.provider_pgid)
                outcomes = [
                    event
                    for event in store.list_events(job_id)
                    if event.kind == outcome_kind
                ]
                self.assertEqual(outcomes[-1].payload["classification"], classification)
                evidence = json.dumps(
                    {
                        "status": terminal,
                        "result": body,
                        "receipt": receipt,
                        "events": [event.payload for event in store.list_events(job_id)],
                    },
                    sort_keys=True,
                )
                self.assertNotIn(detail, evidence)
                invocations = [
                    json.loads(line)
                    for line in self.argv_log.read_text(encoding="utf-8").splitlines()[
                        before_argv:
                    ]
                ]
                self.assertEqual(
                    invocations,
                    [["--version"], ["auth", "status", "--json"]],
                )
                self.assertFalse(
                    any("--output-format" in invocation for invocation in invocations)
                )

    def test_accounting_values_have_explicit_provider_or_unavailable_provenance(self):
        bridge = self.bridge()
        cases = (
            (
                "success",
                {"provider_error": None, "total_cost_usd": 0.125},
                "SUCCEEDED",
                "provider_authoritative",
            ),
            (
                "permission-result",
                {"provider_error": "permission", "total_cost_usd": 0.25},
                "PAUSED_POLICY",
                "provider_authoritative",
            ),
            (
                "pre-result-access",
                {"provider_error": "billing"},
                "PAUSED_PROVIDER_ACCESS",
                "unavailable",
            ),
        )
        for name, controls, expected_status, provenance in cases:
            with self.subTest(case=name):
                self.write_control(control_window_seconds=0, **controls)
                started = bridge.tool(
                    "forge_start",
                    self.start_arguments(idempotency_key=f"provenance-{name}"),
                )
                job_id = started["structuredContent"]["job_id"]
                terminal = self.wait_terminal(bridge, job_id)
                self.assertEqual(terminal["status"], expected_status, terminal)
                body, receipt = self.result_receipt(bridge, job_id)
                for published in (terminal, body, receipt):
                    self.assertEqual(published["usage_provenance"], provenance)
                    self.assertEqual(published["cost_provenance"], provenance)
                    if provenance == "provider_authoritative":
                        self.assertIsInstance(published["usage"], dict)
                        self.assertIsInstance(published["total_cost_usd"], str)
                    else:
                        self.assertIsNone(published["usage"])
                        self.assertIsNone(published["total_cost_usd"])

    def test_secret_shaped_model_ids_never_enter_argv_state_or_terminal_evidence(self):
        canary = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456"
        bridge = self.bridge()

        rejected_start = bridge.tool(
            "forge_start",
            self.start_arguments(
                idempotency_key="secret-requested-model",
                model=canary,
            ),
        )
        self.assertTrue(rejected_start["isError"], rejected_start)
        self.assertNotIn(canary, json.dumps(rejected_start, sort_keys=True))

        self.write_control(provider_error="billing", control_window_seconds=0)
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="secret-resume-override"),
        )
        paused = self.wait_terminal(bridge, started["structuredContent"]["job_id"])
        store, _attempt = self.attempt(paused["job_id"])
        before_attempts = len(store.list_attempts(paused["job_id"]))
        rejected_resume = bridge.tool(
            "forge_resume",
            {
                "job_id": paused["job_id"],
                "revision": paused["revision"],
                "model_override": canary,
            },
        )
        self.assertTrue(rejected_resume["isError"], rejected_resume)
        self.assertEqual(len(store.list_attempts(paused["job_id"])), before_attempts)
        self.assertNotIn(canary, json.dumps(rejected_resume, sort_keys=True))

        cases = (
            ("init", {"cli_accepted_model": canary, "observed_model": "fable"}),
            ("assistant", {"cli_accepted_model": "fable", "observed_model": canary}),
            (
                "model-usage",
                {
                    "cli_accepted_model": "fable",
                    "observed_model": "fable",
                    "model_usage_models": [canary],
                },
            ),
        )
        for name, controls in cases:
            with self.subTest(ingress=name):
                self.write_control(
                    provider_error=None,
                    control_window_seconds=0,
                    **controls,
                )
                started = bridge.tool(
                    "forge_start",
                    self.start_arguments(idempotency_key=f"secret-provider-{name}"),
                )
                job_id = started["structuredContent"]["job_id"]
                terminal = self.wait_terminal(bridge, job_id)
                self.assertEqual(terminal["status"], "FAILED", terminal)
                self.assertEqual(terminal["failure"]["classification"], "protocol")
                body, receipt = self.result_receipt(bridge, job_id)
                store, attempt = self.attempt(job_id)
                published = json.dumps(
                    {
                        "terminal": terminal,
                        "body": body,
                        "receipt": receipt,
                        "events": [event.payload for event in store.list_events(job_id)],
                    },
                    sort_keys=True,
                )
                self.assertNotIn(canary, published)
                for artifact in (
                    "events.jsonl",
                    "stderr.log",
                    "report.txt",
                    "receipt.json",
                    "worker.log",
                ):
                    with store.open_attempt_artifact(job_id, attempt.id, artifact) as (
                        descriptor,
                        _path,
                    ):
                        self.assertNotIn(canary.encode("utf-8"), os.read(descriptor, 2 * 1024 * 1024))
        self.assertNotIn(canary.encode("utf-8"), self.state_path.read_bytes())
        self.assertNotIn(canary, self.argv_log.read_text(encoding="utf-8"))

    def test_authentic_v8_terminal_upgrade_remains_readable_but_not_resumable(self):
        from forge_lib.service import ForgeService
        from forge_lib.state import StateError, Store, canonical_json

        self.write_control(provider_error=None, control_window_seconds=0)
        bridge = self.bridge()
        started = bridge.tool(
            "forge_start",
            self.start_arguments(idempotency_key="authentic-v8-terminal"),
        )
        job_id = started["structuredContent"]["job_id"]
        terminal = self.wait_terminal(bridge, job_id)
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)

        store = Store(self.state_path)
        job = store.get_job(job_id)
        attempt = store.get_attempt(job.current_attempt_id)
        receipt_path = store.attempt_artifact_path(
            job_id, attempt.id, "receipt.json"
        )
        current_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        legacy_payload = copy.deepcopy(job.payload)
        legacy_payload.pop("admission_evidence")
        legacy_payload.pop("request_hash")
        legacy_payload["budget"] = {
            "max_seconds": job.payload["budget"]["max_seconds"],
            "max_tokens": job.payload["budget"]["max_tokens"],
        }
        legacy_payload["max_budget_usd"] = job.payload["budget"]["max_cost_usd"]
        legacy_payload_json = canonical_json(legacy_payload)
        legacy_request_hash = hashlib.sha256(
            legacy_payload_json.encode("utf-8")
        ).hexdigest()

        with store.connection() as connection:
            connection.execute(
                "DELETE FROM events WHERE job_id = ? AND kind IN "
                "('PROVIDER_PREFLIGHT', 'PROVIDER_ACCOUNTING')",
                (job_id,),
            )
            rows = connection.execute(
                "SELECT id, sequence, kind, payload_json FROM events "
                "WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
            previous_hash = None
            for row in rows:
                envelope = canonical_json(
                    {
                        "kind": row["kind"],
                        "payload": json.loads(row["payload_json"]),
                        "previous_hash": previous_hash,
                        "sequence": row["sequence"],
                    }
                )
                event_hash = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
                connection.execute(
                    "UPDATE events SET previous_hash = ?, event_hash = ? "
                    "WHERE id = ?",
                    (previous_hash, event_hash, row["id"]),
                )
                previous_hash = event_hash
            event_bytes = connection.execute(
                "SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) "
                "FROM events WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE attempts SET event_count = ?, event_bytes = ? WHERE id = ?",
                (len(rows), event_bytes, attempt.id),
            )
            connection.execute(
                "UPDATE jobs SET payload_json = ?, request_hash = ? WHERE id = ?",
                (legacy_payload_json, legacy_request_hash, job_id),
            )

        legacy_receipt = {
            key: current_receipt[key]
            for key in (
                "job_id",
                "attempt_id",
                "repo_id",
                "worktree",
                "baseline_head",
                "status_fingerprint",
                "requested_model",
                "observed_model",
                "requested_effort",
                "session_id",
                "parent_tool_use_ids",
                "report_hash",
                "events_hash",
                "stderr_hash",
                "worker_log_hash",
                "terminal_status",
                "num_turns",
                "total_cost_usd",
                "usage",
            )
        }
        legacy_receipt["event_hash"] = previous_hash
        legacy_receipt_bytes = (
            canonical_json(legacy_receipt) + "\n"
        ).encode("utf-8")
        receipt_path.write_bytes(legacy_receipt_bytes)
        legacy_receipt_hash = hashlib.sha256(legacy_receipt_bytes).hexdigest()

        with store.connection() as connection:
            connection.execute(
                "UPDATE attempts SET terminal_receipt_hash = ? WHERE id = ?",
                (legacy_receipt_hash, attempt.id),
            )
            for column in (
                "model_override",
                "model_cli_accepted",
                "models_observed_json",
                "verification_level",
                "provider_identity",
            ):
                connection.execute(f"ALTER TABLE attempts DROP COLUMN {column}")
            connection.execute(
                "UPDATE schema_version SET version = 8 WHERE singleton = 1"
            )

        preserved_payload = legacy_payload_json
        preserved_request_hash = legacy_request_hash
        preserved_receipt = receipt_path.read_bytes()
        preserved_receipt_hash = legacy_receipt_hash

        service = ForgeService(
            state_path=self.state_path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
        )
        status = service.status({"job_id": job_id}).body
        result = service.result({"job_id": job_id}).body
        history = service.list_jobs({}).body["jobs"]

        self.assertEqual(status["status"], "SUCCEEDED")
        self.assertNotIn("model_cli_accepted", status)
        self.assertNotIn("usage_provenance", status)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["report"], "bounded fake report")
        self.assertNotIn("usage_provenance", result)
        self.assertEqual(history[0]["job_id"], job_id)
        self.assertEqual(len(service.store.list_attempts(job_id)), 1)
        self.assertGreater(len(service.store.list_events(job_id)), 0)

        with service.store.connection() as connection:
            stored = connection.execute(
                "SELECT payload_json, request_hash FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            stored_receipt_hash = connection.execute(
                "SELECT terminal_receipt_hash FROM attempts WHERE id = ?",
                (attempt.id,),
            ).fetchone()[0]
        self.assertEqual(stored["payload_json"], preserved_payload)
        self.assertEqual(stored["request_hash"], preserved_request_hash)
        self.assertEqual(receipt_path.read_bytes(), preserved_receipt)
        self.assertEqual(stored_receipt_hash, preserved_receipt_hash)

        before_attempts = service.store.list_attempts(job_id)
        with self.assertRaisesRegex(StateError, "legacy v8"):
            service.resume(
                {
                    "job_id": job_id,
                    "revision": status["revision"],
                    "model_override": "claude-opus-4-6",
                }
            )
        self.assertEqual(service.store.list_attempts(job_id), before_attempts)

    def test_authentic_v9_terminal_upgrade_is_immutable_readable_and_not_resumable(self):
        from forge_lib.service import ForgeService
        from forge_lib.state import StateError, Store, canonical_json

        self.write_control(provider_error=None, control_window_seconds=0)
        service = ForgeService(
            state_path=self.state_path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
            _admission_machine_probe=machine_headroom,
        )
        started = service.start(
            self.start_arguments(idempotency_key="authentic-v9-terminal")
        )
        self.assertFalse(started.is_error, started)
        job_id = started.body["job_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            terminal = service.status({"job_id": job_id}).body
            if terminal["status"] in {
                "SUCCEEDED",
                "FAILED",
                "PAUSED_POLICY",
                "PAUSED_PROVIDER_ACCESS",
            }:
                break
            time.sleep(0.02)
        self.assertEqual(terminal["status"], "SUCCEEDED", terminal)

        store = Store(self.state_path)
        job = store.get_job(job_id)
        attempt = store.get_attempt(job.current_attempt_id)
        receipt_path = store.attempt_artifact_path(job_id, attempt.id, "receipt.json")
        current_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        legacy_payload = copy.deepcopy(job.payload)
        legacy_payload.pop("admission_evidence")
        legacy_payload.pop("request_hash")
        legacy_payload_json = canonical_json(legacy_payload)
        legacy_request_hash = hashlib.sha256(
            legacy_payload_json.encode("utf-8")
        ).hexdigest()
        legacy_receipt = copy.deepcopy(current_receipt)
        legacy_receipt.pop("admission_evidence_sha256")
        legacy_receipt_bytes = (canonical_json(legacy_receipt) + "\n").encode("utf-8")
        legacy_receipt_hash = hashlib.sha256(legacy_receipt_bytes).hexdigest()
        receipt_path.write_bytes(legacy_receipt_bytes)

        with store.connection() as connection:
            connection.execute(
                "UPDATE jobs SET payload_json = ?, request_hash = ? WHERE id = ?",
                (legacy_payload_json, legacy_request_hash, job_id),
            )
            connection.execute(
                "UPDATE attempts SET terminal_receipt_hash = ? WHERE id = ?",
                (legacy_receipt_hash, attempt.id),
            )
            connection.execute("DROP TABLE attempt_admissions")
            connection.execute("ALTER TABLE attempts DROP COLUMN provider_identity")
            connection.execute(
                "UPDATE schema_version SET version = 9 WHERE singleton = 1"
            )

        preserved_payload = legacy_payload_json
        preserved_receipt = receipt_path.read_bytes()
        service = ForgeService(
            state_path=self.state_path,
            launch_workspace=self.repo,
            claude_executable=str(FAKE_CLAUDE),
            bridge_path=BRIDGE,
            environment=self.environment,
        )
        status = service.status({"job_id": job_id}).body
        result = service.result({"job_id": job_id}).body
        history = service.list_jobs({}).body["jobs"]

        self.assertEqual(status["status"], "SUCCEEDED")
        self.assertNotIn("admission_evidence_sha256", status)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["report"], "bounded fake report")
        self.assertNotIn("admission_evidence_sha256", result)
        self.assertEqual(history[0]["job_id"], job_id)
        self.assertEqual(len(service.store.list_attempts(job_id)), 1)
        with service.store.connection() as connection:
            stored = connection.execute(
                "SELECT payload_json, request_hash FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            admission_count = connection.execute(
                "SELECT COUNT(*) FROM attempt_admissions WHERE attempt_id = ?",
                (attempt.id,),
            ).fetchone()[0]
        self.assertEqual(stored["payload_json"], preserved_payload)
        self.assertEqual(stored["request_hash"], legacy_request_hash)
        self.assertEqual(receipt_path.read_bytes(), preserved_receipt)
        self.assertEqual(admission_count, 0)

        before_attempts = service.store.list_attempts(job_id)
        with self.assertRaisesRegex(StateError, "legacy v9"):
            service.resume(
                {
                    "job_id": job_id,
                    "revision": status["revision"],
                    "model_override": "claude-opus-4-6",
                }
            )
        self.assertEqual(service.store.list_attempts(job_id), before_attempts)

    def test_prequeued_cancel_owns_ready_provider_policy_arbitration(self):
        from forge_lib import processes, worker as worker_module
        from forge_lib.state import Store

        store = Store(self.temp_path / "cancel-policy-race" / "state.sqlite3")
        job = store.create_job(
            repo_id="cancel-policy-race-repo",
            idempotency_key="cancel-policy-race",
            request_hash="hash",
            payload={},
        )
        claim = store.claim_dispatch(job.id, owner_pid=os.getpid())
        assert claim is not None
        store.record_worker_pid(job.id, os.getpid(), _dispatch_claim=claim)
        authority = store.claim_worker_authority(job.id, claim.token)
        assert authority is not None
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="PREFLIGHT",
        )

        ready_frames = (
            json.dumps(canonical_init("fable"), separators=(",", ":"))
            + "\n"
            + json.dumps(
                canonical_assistant(
                    "msg_ready_billing",
                    provider_error="billing_error",
                ),
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        child_code = "\n".join(
            (
                "import json, os, sys, time",
                f"os.write(1, {ready_frames!r})",
                "request = json.loads(sys.stdin.buffer.readline())",
                "ack = {'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':{}}}",
                "print(json.dumps(ack, separators=(',', ':')), flush=True)",
                "time.sleep(30)",
            )
        )
        provider = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        self.addCleanup(
            lambda: provider.kill() if provider.poll() is None else None
        )
        assert provider.stdout is not None
        ready, _, _ = select.select([provider.stdout], [], [], 2)
        self.assertTrue(ready, "provider policy output was not ready before cancellation")

        store.record_provider_identity(
            job.id,
            session_id=SESSION_ID,
            provider_pgid=provider.pid,
        )
        current = store.get_job(job.id)
        store.transition(
            job.id,
            expected_revision=current.revision,
            new_status="RUNNING",
        )
        manifest = self.temp_path / "cancel-policy-race-manifest.jsonl"
        manifest.write_text("", encoding="utf-8")
        manifest.chmod(0o400)
        containment = processes._test_cancellation_containment(manifest)
        admission_evidence = processes.cancellation_admission_preflight(
            containment,
            expected_process_group_id=provider.pid,
        )
        self.assertTrue(admission_evidence["supported"], admission_evidence)
        running = store.get_job(job.id)
        attempt = store.get_attempt(running.current_attempt_id)
        admission = store.request_cancellation(
            job.id,
            expected_revision=running.revision,
            live_worker_identity=attempt.worker_identity,
            cancellation_manifest_pin=admission_evidence["_test_manifest_pin"],
        )
        self.assertEqual(admission.command.status, "QUEUED")

        attempt_id = running.current_attempt_id
        store.create_attempt_artifact(job.id, attempt_id, "events.jsonl")
        store.create_attempt_artifact(job.id, attempt_id, "stderr.log")
        tracker = claude.ProviderPolicyTracker(
            model_requested="fable",
            max_tokens=4096,
            max_turns=8,
            max_cost_usd=Decimal("1"),
        )
        try:
            with store.open_attempt_artifact(
                job.id,
                attempt_id,
                "events.jsonl",
                writable=True,
            ) as (events_descriptor, _events_path), store.open_attempt_artifact(
                job.id,
                attempt_id,
                "stderr.log",
                writable=True,
            ) as (stderr_descriptor, _stderr_path):
                _result, _model, _parents, cancellation = worker_module._collect_provider(
                    provider,
                    store=store,
                    job_id=job.id,
                    session_id=SESSION_ID,
                    dispatch_token=claim.token,
                    worker_pid=os.getpid(),
                    worker_authority=authority,
                    events_descriptor=events_descriptor,
                    stderr_descriptor=stderr_descriptor,
                    deadline=time.monotonic() + 5,
                    policy_tracker=tracker,
                    include_cancellation=True,
                    cancellation_containment=containment,
                )
        finally:
            if provider.poll() is None:
                provider.kill()
            provider.wait(timeout=2)
            for stream in (provider.stdin, provider.stdout, provider.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        self.assertIsNotNone(cancellation)
        self.assertEqual(cancellation["request_id"], admission.command.payload["request_id"])
        self.assertEqual(store.get_job(job.id).status, "INTERRUPTING")
        self.assertEqual(store.list_commands(job.id)[0].status, "ACKNOWLEDGED")
        outcome_kinds = {
            event.kind for event in store.list_events(job.id)
            if event.kind in {"PROVIDER_PAUSED", "PROVIDER_FAILED"}
        }
        self.assertEqual(outcome_kinds, set())
        self.assert_provider_quiescent(provider.pid)


if __name__ == "__main__":
    unittest.main()
