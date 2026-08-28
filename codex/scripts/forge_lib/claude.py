"""Bounded, structured adapter for Claude Code's stream-json interface."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import selectors
import subprocess
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from .contracts import EFFORTS, MAX_LINE_BYTES, MAX_PROMPT_BYTES, ROLES
from .processes import (
    ProcessError,
    RetainedProcessGroupScope,
    retain_process_group_scope,
    terminate_retained_process_group,
    wait_for_retained_process_exit,
)
from .security import build_child_environment, redact_sensitive, redact_text


MAX_EVENT_BYTES = MAX_LINE_BYTES
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 5.0
DEFAULT_PREFLIGHT_OUTPUT_LIMIT_BYTES = 64 * 1024
MAX_BUDGET_INPUT_BYTES = 64
MAX_BUDGET_USD = Decimal("1000000")
MAX_BUDGET_FRACTIONAL_PLACES = 6
MAX_BUDGET_DIGITS = 13
MAX_BUDGET_OUTPUT_CHARS = 32
MAX_MODEL_ID_BYTES = 256
MAX_SESSION_ID_BYTES = 256
MAX_REQUEST_ID_BYTES = 256
MAX_MODEL_USAGE_ENTRIES = 16
MAX_USAGE_COUNT = 1_000_000_000
MAX_NUM_TURNS = 1_000_000
MAX_RECEIPT_COST_USD = Decimal("1000000")
MAX_RECEIPT_COST_FRACTIONAL_PLACES = 24
MAX_RECEIPT_COST_DIGITS = 19
MAX_USAGE_DECIMAL = Decimal("1000000000")
MAX_USAGE_DECIMAL_FRACTIONAL_PLACES = 18
MAX_USAGE_DECIMAL_DIGITS = 28
MAX_USAGE_EXTENSION_DEPTH = 6
MAX_USAGE_EXTENSION_CONTAINER_ITEMS = 64
MAX_USAGE_EXTENSION_KEY_BYTES = 128
MAX_USAGE_EXTENSION_STRING_BYTES = 4096
MAX_USAGE_EXTENSION_NODES = 256
MAX_PROVIDER_ERRORS = 64
MAX_PERMISSION_DENIALS = 128
MAX_ASSISTANT_MESSAGE_IDS = 1_000_000
MAX_TOKEN_BUDGET = 10_000_000
MAX_INIT_ARRAY_ENTRIES = 1_024
MAX_INIT_TEXT_BYTES = 4_096
SUPPORTED_CLAUDE_CODE_VERSION = "2.1.248"
SUPPORTED_CLAUDE_CODE_VERSION_OUTPUT = "2.1.248 (Claude Code)"
_PROCESS_TERMINATE_GRACE_SECONDS = 0.2
_MCP_CONFIG = '{"mcpServers":{}}'
_INPUT_MESSAGE_TYPES = frozenset({"user", "control_request"})
_BUDGET_LITERAL = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z"
)
REVIEW_TOOLS = ("Read", "Glob", "Grep", "Agent")
BUILD_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash", "Agent")
_RECOVERY_TOOL_UNIVERSE = frozenset(BUILD_TOOLS)
_MODEL_USAGE_COUNT_FIELDS = frozenset(
    {
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
        "webSearchRequests",
        "contextWindow",
        "maxOutputTokens",
    }
)
_MODEL_USAGE_FIELDS = _MODEL_USAGE_COUNT_FIELDS | {"costUSD"}
_MODEL_USAGE_OPTIONAL_FIELDS = frozenset(
    {"canonicalModel", "provider", "costBasis"}
)
_MODEL_USAGE_COST_BASES = frozenset({"list", "managed", "unknown"})
_USAGE_COUNT_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)
_SERVER_TOOL_COUNT_FIELDS = frozenset(
    {"web_search_requests", "web_fetch_requests"}
)
_CACHE_CREATION_COUNT_FIELDS = frozenset(
    {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}
)
_USAGE_FIELDS = _USAGE_COUNT_FIELDS | {
    "server_tool_use",
    "cache_creation",
    "service_tier",
    "inference_geo",
}
_API_RETRY_ERRORS = frozenset(
    {
        "authentication_failed",
        "oauth_org_not_allowed",
        "billing_error",
        "rate_limit",
        "invalid_request",
        "model_not_found",
        "server_error",
        "unknown",
        "max_output_tokens",
    }
)
_MODEL_FALLBACK_TRIGGERS = frozenset({"model_not_found", "overloaded"})
_RATE_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})
_RATE_LIMIT_TYPES = frozenset(
    {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"}
)
_API_KEY_SOURCES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "apiKeyHelper",
        "/login managed key",
        "none",
        "user",
        "project",
        "org",
        "temporary",
        "oauth",
    }
)
_AUTH_STATUS_RECEIPT_FIELDS = (
    "loggedIn",
    "authMethod",
    "apiProvider",
    "subscriptionType",
)
_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "bypassPermissions", "plan", "dontAsk", "auto"}
)
_INIT_REQUIRED_FIELDS = frozenset(
    {
        "type",
        "subtype",
        "apiKeySource",
        "claude_code_version",
        "cwd",
        "mcp_servers",
        "model",
        "output_style",
        "permissionMode",
        "plugins",
        "session_id",
        "skills",
        "slash_commands",
        "tools",
        "uuid",
    }
)
_INIT_OPTIONAL_FIELDS = frozenset(
    {
        "agents",
        "analytics_disabled",
        "betas",
        "capabilities",
        "effort",
        "fast_mode_disabled_reason",
        "fast_mode_state",
        "mcp_server_errors",
        "memory_paths",
        "messaging_socket_path",
        "product_feedback_disabled",
        "terminal_slash_commands",
    }
)
_FAST_MODE_DISABLED_REASONS = frozenset(
    {
        "free",
        "preference",
        "extra_usage_disabled",
        "network_error",
        "unknown",
        "not_first_party",
        "disabled_by_env",
        "model_not_allowed",
        "sdk_opt_in_required",
        "pending",
    }
)
_FAST_MODE_STATES = frozenset({"off", "cooldown", "on"})
_TASK_NOTIFICATION_STATUSES = frozenset({"completed", "failed", "stopped"})
_TASK_UPDATED_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "killed", "paused"}
)
_MAX_TASK_TIME_MS = 9_007_199_254_740_991
_FULL_MODEL_NAME = re.compile(r"claude-[a-z0-9][a-z0-9._-]*\Z")
SYNTHETIC_API_ERROR_MODEL = "<synthetic>"
_PROVIDER_ERROR_OUTCOMES = {
    "authentication_failed": (
        "PAUSED_PROVIDER_ACCESS",
        "authentication",
        "authentication_failed",
    ),
    "oauth_org_not_allowed": (
        "PAUSED_PROVIDER_ACCESS",
        "oauth_organization",
        "oauth_organization_denied",
    ),
    "billing_error": (
        "PAUSED_PROVIDER_ACCESS",
        "billing",
        "billing_access_required",
    ),
    "rate_limit": (
        "PAUSED_PROVIDER_ACCESS",
        "rate_limit",
        "rate_limit_reached",
    ),
    "invalid_request": ("FAILED", "protocol", "invalid_provider_request"),
    "model_not_found": (
        "PAUSED_PROVIDER_ACCESS",
        "model_not_found",
        "model_not_found",
    ),
    "server_error": ("FAILED", "transport", "provider_server_error"),
    "unknown": ("FAILED", "internal_failure", "unknown_provider_error"),
    "max_output_tokens": (
        "PAUSED_POLICY",
        "token_limit",
        "maximum_output_tokens_reached",
    ),
}


class AdapterError(ValueError):
    """Raised when a Claude command or input message violates the adapter contract."""


class PreflightError(AdapterError):
    """Raised when a bounded, provider-free Claude CLI preflight fails."""

    def __init__(
        self,
        message: str,
        *,
        observed_version: str | None = None,
        provider_access_classification: str | None = None,
        provider_access_reason: str | None = None,
    ):
        self.observed_version = (
            None if observed_version is None else redact_text(observed_version)
        )
        self.provider_access_classification = provider_access_classification
        self.provider_access_reason = provider_access_reason
        super().__init__(message)


class UnsupportedClaudeCodeVersion(PreflightError):
    """Raised when CLI evidence does not match the exact bound event schema."""

    def __init__(self, observed_version: str):
        self.observed_version = redact_text(observed_version)
        super().__init__(
            "unsupported Claude Code version "
            f"{self.observed_version}; install {SUPPORTED_CLAUDE_CODE_VERSION}",
            observed_version=self.observed_version,
        )


class ClaudeProtocolError(AdapterError):
    """Raised when Claude stream output is malformed or exceeds its bound."""


@dataclass(frozen=True, slots=True)
class ProviderPolicyDecision:
    """Typed terminal direction for one provider or Forge policy condition."""

    status: str
    classification: str
    reason: str
    allow_substitution: bool = False


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Immutable inputs needed to construct one bounded Claude invocation."""

    role: str
    model: str
    effort: str
    max_budget_usd: Decimal | int | float | str
    settings_path: str | os.PathLike[str]
    prompt: str
    resume_session_id: str | None = None
    recovery_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Provider-free Claude CLI availability and authentication receipt."""

    executable: str
    version: str
    auth_status: dict[str, Any]

    def redacted_summary(self) -> dict[str, Any]:
        auth_status = {
            name: self.auth_status[name]
            for name in _AUTH_STATUS_RECEIPT_FIELDS
            if name in self.auth_status
        }
        return _redacted_mapping(
            {
                "executable": self.executable,
                "version": self.version,
                "auth_status": auth_status,
            }
        )


@dataclass(frozen=True, slots=True)
class ClaudeEvent:
    """A validated event plus normalized receipt fields and its raw data copy."""

    data: dict[str, Any]
    type: str
    subtype: str | None
    session_id: str | None
    model: str | None
    parent_tool_use_id: str | None
    is_error: bool | None
    model_usage: dict[str, Any] | None
    num_turns: int | None
    total_cost_usd: Decimal | None
    usage: dict[str, Any] | None
    is_api_retry: bool
    assistant_message_id: str | None
    has_effect: bool
    errors: tuple[str, ...]
    permission_denials: tuple[dict[str, Any], ...]
    api_retry_attempt: int | None
    api_retry_max_retries: int | None
    api_retry_delay_ms: int | float | None
    api_retry_error_status: int | None
    api_retry_error: str | None
    fallback_kind: str | None
    fallback_original_model: str | None
    fallback_model: str | None
    rate_limit_info: dict[str, Any] | None
    provider_error: str | None

    @property
    def succeeded(self) -> bool | None:
        """Return result success solely from ``is_error``, never from subtype."""

        if self.type != "result" or self.is_error is None:
            return None
        return not self.is_error

    @property
    def cost_usd(self) -> Decimal | None:
        """Compatibility name for the provider-reported total cost."""

        return self.total_cost_usd

    def redacted_summary(self) -> dict[str, Any]:
        """Return a redacted copy while preserving ``data`` for the worker."""

        if self.type == "system" and self.subtype == "init":
            return _init_receipt_summary(self.data)
        if self.type == "system" and self.subtype in {
            "background_tasks_changed",
            "task_notification",
            "task_progress",
            "task_started",
            "task_updated",
            "thinking_tokens",
        }:
            return _system_lifecycle_receipt_summary(self.data, self.subtype)
        return _redacted_mapping(self.data)


def _redacted_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_sensitive(value)
    if not isinstance(redacted, Mapping):  # pragma: no cover - security helper contract
        raise AdapterError("security redaction did not return an object")
    return dict(redacted)


def _init_receipt_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return identity-safe, allowlisted init evidence for durable receipts."""

    return {
        "type": "system",
        "subtype": "init",
        "apiKeySource": data["apiKeySource"],
        "analytics_disabled": data.get("analytics_disabled"),
        "claude_code_version": data["claude_code_version"],
        "effort": data.get("effort"),
        "fast_mode_disabled": data.get("fast_mode_disabled_reason") is not None,
        "fast_mode_disabled_reason": data.get("fast_mode_disabled_reason"),
        "fast_mode_state": data.get("fast_mode_state"),
        "model": data["model"],
        "permissionMode": data["permissionMode"],
        "product_feedback_disabled": data.get("product_feedback_disabled"),
        "agent_count": len(data.get("agents", [])),
        "capability_count": len(data.get("capabilities", [])),
        "tool_count": len(data["tools"]),
        "mcp_server_count": len(data["mcp_servers"]),
        "mcp_server_error_count": len(data.get("mcp_server_errors", [])),
        "plugin_count": len(data["plugins"]),
        "skill_count": len(data["skills"]),
        "slash_command_count": len(data["slash_commands"]),
        "uuid": data["uuid"],
        "session_id": data["session_id"],
    }


def _system_lifecycle_receipt_summary(
    data: Mapping[str, Any], subtype: str
) -> dict[str, Any]:
    """Return bounded task telemetry without prompts or descriptions."""

    summary: dict[str, Any] = {
        "type": "system",
        "subtype": subtype,
    }
    for name in (
        "task_id",
        "tool_use_id",
        "status",
        "is_backgrounded",
        "spawn_depth",
        "estimated_tokens",
        "estimated_tokens_delta",
        "skip_transcript",
        "ambient",
    ):
        if name in data:
            summary[name] = copy.deepcopy(data[name])
    if subtype in {"task_notification", "task_progress"} and "usage" in data:
        summary["usage"] = copy.deepcopy(data["usage"])
    if subtype == "task_updated":
        patch = data["patch"]
        summary["patch"] = {
            name: copy.deepcopy(patch[name])
            for name in (
                "status",
                "end_time",
                "total_paused_ms",
                "is_backgrounded",
            )
            if name in patch
        }
    if subtype == "background_tasks_changed":
        summary["tasks"] = [
            {"task_id": task["task_id"], "ambient": task.get("ambient", False)}
            for task in data["tasks"]
        ]
    for name in ("uuid", "session_id"):
        if name in data:
            summary[name] = data[name]
    return summary


def _safe_text(value: Any, label: str, *, maximum_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{label} must be non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AdapterError(f"{label} must be valid UTF-8 text") from error
    if len(encoded) > maximum_bytes:
        raise AdapterError(f"{label} exceeds its byte limit")
    if "\x00" in value:
        raise AdapterError(f"{label} contains NUL")
    return value


def _safe_executable(value: str | os.PathLike[str]) -> str:
    try:
        executable = os.fspath(value)
    except TypeError as error:
        raise AdapterError("Claude executable must be a filesystem name") from error
    if not isinstance(executable, str):
        raise AdapterError("Claude executable must be a text filesystem name")
    return _safe_text(executable, "Claude executable")


def _settings_path(value: str | os.PathLike[str]) -> str:
    try:
        path_text = os.fspath(value)
    except TypeError as error:
        raise AdapterError("settings path must be a filesystem path") from error
    if not isinstance(path_text, str):
        raise AdapterError("settings path must be a text filesystem path")
    _safe_text(path_text, "settings path")
    path = Path(path_text)
    if not path.is_absolute() or ".." in path.parts:
        raise AdapterError("settings path must be absolute and traversal-free")
    return path_text


def _budget_input_text(value: Decimal | int | float | str) -> str:
    """Return a bounded numeric spelling without expanding an exponent."""

    if isinstance(value, bool):
        raise AdapterError("maximum USD budget must be a positive decimal")
    if isinstance(value, str):
        representation = value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise AdapterError("maximum USD budget must be finite")
        # Inspect the coefficient before string conversion. A Decimal supplied by
        # another Python component can already contain an enormous digit tuple.
        if len(value.as_tuple().digits) > MAX_BUDGET_INPUT_BYTES:
            raise AdapterError("maximum USD budget input exceeds its digit limit")
        representation = str(value)
    elif isinstance(value, int):
        # Reject large integers before Python must allocate their full decimal form.
        if value <= 0 or value > int(MAX_BUDGET_USD):
            raise AdapterError("maximum USD budget is outside the allowed range")
        representation = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise AdapterError("maximum USD budget must be finite")
        representation = str(value)
    else:
        raise AdapterError("maximum USD budget must be a decimal, number, or string")

    try:
        encoded = representation.encode("ascii")
    except UnicodeEncodeError as error:
        raise AdapterError("maximum USD budget must use ASCII decimal notation") from error
    if not encoded or len(encoded) > MAX_BUDGET_INPUT_BYTES:
        raise AdapterError("maximum USD budget input exceeds its byte limit")
    if _BUDGET_LITERAL.fullmatch(representation) is None:
        raise AdapterError("maximum USD budget has invalid decimal notation")
    return representation


def _normalized_budget_tuple(
    decimal_value: Decimal,
) -> tuple[tuple[int, ...], int]:
    """Trim insignificant zeros without applying Decimal context or formatting."""

    components = decimal_value.as_tuple()
    if not isinstance(components.exponent, int):  # finite values always use int
        raise AdapterError("maximum USD budget must be finite")
    if len(components.digits) > MAX_BUDGET_INPUT_BYTES:
        raise AdapterError("maximum USD budget input exceeds its digit limit")
    end = len(components.digits)
    while end > 1 and components.digits[end - 1] == 0:
        end -= 1
    digits = components.digits[:end]
    exponent = components.exponent + len(components.digits) - end
    return digits, exponent


def _render_budget(digits: tuple[int, ...], exponent: int) -> str:
    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        rendered = coefficient + "0" * exponent
    else:
        decimal_position = len(coefficient) + exponent
        if decimal_position > 0:
            rendered = (
                coefficient[:decimal_position]
                + "."
                + coefficient[decimal_position:]
            )
        else:
            rendered = "0." + "0" * (-decimal_position) + coefficient
    if len(rendered) > MAX_BUDGET_OUTPUT_CHARS:
        raise AdapterError("normalized maximum USD budget exceeds its output limit")
    return rendered


def _budget_text(value: Decimal | int | float | str) -> str:
    representation = _budget_input_text(value)
    try:
        decimal_value = Decimal(representation)
    except InvalidOperation as error:
        raise AdapterError("maximum USD budget must be a positive decimal") from error
    if (
        not decimal_value.is_finite()
        or decimal_value <= 0
        or decimal_value > MAX_BUDGET_USD
    ):
        raise AdapterError("maximum USD budget is outside the allowed range")

    digits, exponent = _normalized_budget_tuple(decimal_value)
    if len(digits) > MAX_BUDGET_DIGITS:
        raise AdapterError("maximum USD budget exceeds its normalized digit limit")
    if max(-exponent, 0) > MAX_BUDGET_FRACTIONAL_PLACES:
        raise AdapterError("maximum USD budget supports at most six fractional places")
    # At this point value, scale, coefficient, and exponent are all bounded, so
    # fixed-point rendering cannot allocate attacker-controlled output.
    return _render_budget(digits, exponent)


def normalize_budget_usd(value: Decimal | int | float | str) -> str:
    """Return the exact bounded fixed-point spelling accepted by Claude CLI."""

    return _budget_text(value)


def normalize_model_identifier(value: Any) -> str:
    """Validate one explicit model argument without interpreting aliases."""

    model = _safe_text(value, "model", maximum_bytes=MAX_MODEL_ID_BYTES)
    if model.startswith("-") or any(character.isspace() for character in model):
        raise AdapterError("model must be an explicit CLI model identifier")
    if redact_text(model) != model:
        raise AdapterError("model must not contain credential-shaped material")
    return model


def _validate_prompt(prompt: Any) -> str:
    if not isinstance(prompt, str) or not prompt:
        raise AdapterError("prompt must be non-empty text")
    try:
        encoded = prompt.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AdapterError("prompt must be valid UTF-8 text") from error
    if len(encoded) > MAX_PROMPT_BYTES:
        raise AdapterError("prompt exceeds the Forge byte limit")
    if "\x00" in prompt:
        raise AdapterError("prompt contains NUL")
    return prompt


def _role_tools(spec: JobSpec) -> tuple[str, ...]:
    if spec.role == "review":
        if spec.recovery_tools:
            raise AdapterError("review cannot request recovery tools")
        return REVIEW_TOOLS
    if spec.role == "build":
        if spec.recovery_tools:
            raise AdapterError("build cannot request recovery tools")
        return BUILD_TOOLS
    if not isinstance(spec.recovery_tools, tuple) or not spec.recovery_tools:
        raise AdapterError("recovery requires an explicit minimum tool tuple")
    seen: set[str] = set()
    for tool in spec.recovery_tools:
        if tool not in _RECOVERY_TOOL_UNIVERSE:
            raise AdapterError(f"unsupported recovery tool: {tool!r}")
        if tool in seen:
            raise AdapterError(f"duplicate recovery tool: {tool}")
        seen.add(tool)
    return spec.recovery_tools


def _session_arguments(spec: JobSpec) -> list[str]:
    if spec.resume_session_id is not None:
        recorded = _safe_text(
            spec.resume_session_id,
            "recorded session id",
            maximum_bytes=256,
        )
        if recorded.casefold() in {"latest", "continue"} or recorded.startswith("-"):
            raise AdapterError("resume requires an explicit recorded session id")
        return ["--resume", recorded]
    generated = str(uuid.uuid4())
    try:
        parsed = uuid.UUID(generated)
    except (ValueError, AttributeError) as error:  # pragma: no cover - uuid4 contract
        raise AdapterError("could not generate an explicit Claude session id") from error
    if parsed.version != 4:
        raise AdapterError("generated Claude session id is not a UUID4")
    return ["--session-id", generated]


def build_command(
    spec: JobSpec,
    *,
    executable: str | os.PathLike[str] = "claude",
) -> list[str]:
    """Build the exact Claude stream-json argv without prompt interpolation."""

    if not isinstance(spec, JobSpec):
        raise AdapterError("build_command requires a JobSpec")
    if spec.role not in ROLES:
        raise AdapterError(f"unsupported Claude role: {spec.role!r}")
    model = normalize_model_identifier(spec.model)
    if spec.effort not in EFFORTS:
        raise AdapterError(f"unsupported Claude effort: {spec.effort!r}")
    _validate_prompt(spec.prompt)
    tools = ",".join(_role_tools(spec))
    command = [
        _safe_executable(executable),
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        *_session_arguments(spec),
        "--model",
        model,
        "--effort",
        spec.effort,
        "--max-budget-usd",
        _budget_text(spec.max_budget_usd),
        "--settings",
        _settings_path(spec.settings_path),
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        _MCP_CONFIG,
        "--disable-slash-commands",
        "--no-chrome",
        "--permission-mode",
        "dontAsk",
        "--tools",
        tools,
        "--allowedTools",
        tools,
    ]
    return command


def build_user_message(prompt: str) -> dict[str, Any]:
    """Return the frozen coordinator-to-Claude stdin user message."""

    prompt = _validate_prompt(prompt)
    return {
        "type": "user",
        "session_id": "",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        },
        "parent_tool_use_id": None,
        "origin": {"kind": "coordinator"},
    }


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise AdapterError(f"{label} must contain exactly {sorted(expected)}")


def _validate_user_message(message: Mapping[str, Any]) -> None:
    _require_exact_keys(
        message,
        {"type", "session_id", "message", "parent_tool_use_id", "origin"},
        "Claude user message",
    )
    if (
        message.get("type") != "user"
        or message.get("session_id") != ""
        or message.get("parent_tool_use_id") is not None
        or message.get("origin") != {"kind": "coordinator"}
    ):
        raise AdapterError("Claude user message metadata is not canonical")
    inner = message.get("message")
    if not isinstance(inner, Mapping):
        raise AdapterError("Claude user message.message must be an object")
    _require_exact_keys(inner, {"role", "content"}, "Claude user message.message")
    if inner.get("role") != "user":
        raise AdapterError("Claude user message role must be user")
    content = inner.get("content")
    if not isinstance(content, list) or len(content) != 1:
        raise AdapterError("Claude user message must contain exactly one text block")
    block = content[0]
    if not isinstance(block, Mapping):
        raise AdapterError("Claude user message text block must be an object")
    _require_exact_keys(block, {"type", "text"}, "Claude user message text block")
    if block.get("type") != "text":
        raise AdapterError("Claude user message content must be text")
    _validate_prompt(block.get("text"))


def _validate_interrupt_message(message: Mapping[str, Any]) -> None:
    _require_exact_keys(
        message,
        {"type", "request_id", "request"},
        "Claude interrupt message",
    )
    if message.get("type") != "control_request":
        raise AdapterError("Claude control message must be a control_request")
    request_id = _safe_text(
        message.get("request_id"),
        "interrupt request id",
        maximum_bytes=MAX_REQUEST_ID_BYTES,
    )
    if any(character.isspace() for character in request_id):
        raise AdapterError("interrupt request id must not contain whitespace")
    request = message.get("request")
    if not isinstance(request, Mapping) or dict(request) != {"subtype": "interrupt"}:
        raise AdapterError("only the exact interrupt control request is allowed")


def encode_stream_message(message: Mapping[str, Any]) -> bytes:
    """Encode one exact canonical user or interrupt object as JSONL."""

    if not isinstance(message, Mapping):
        raise AdapterError("Claude stdin message must be an object")
    message_type = message.get("type")
    if not isinstance(message_type, str) or message_type not in _INPUT_MESSAGE_TYPES:
        raise AdapterError("Claude stdin message has an unsupported type")
    if message_type == "user":
        _validate_user_message(message)
    else:
        _validate_interrupt_message(message)
    try:
        encoded = json.dumps(
            dict(message),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise AdapterError("Claude stdin message is not bounded JSON") from error
    if len(encoded) > MAX_EVENT_BYTES:
        raise AdapterError("Claude stdin message exceeds the stream byte limit")
    return encoded + b"\n"


def encode_user_message(prompt: str) -> bytes:
    return encode_stream_message(build_user_message(prompt))


def _terminate_process(
    process: subprocess.Popen[bytes],
    scope: RetainedProcessGroupScope,
) -> None:
    terminate_retained_process_group(
        process,
        scope,
        grace_seconds=_PROCESS_TERMINATE_GRACE_SECONDS,
    )


def _run_preflight_command(
    arguments: list[str],
    *,
    environment: Mapping[str, str] | None,
    timeout_seconds: float,
    output_limit_bytes: int,
) -> tuple[bytes, bytes]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise PreflightError("preflight timeout must be positive and finite")
    if (
        isinstance(output_limit_bytes, bool)
        or not isinstance(output_limit_bytes, int)
        or output_limit_bytes <= 0
    ):
        raise PreflightError("preflight output limit must be a positive integer")
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=None if environment is None else dict(environment),
            start_new_session=True,
            shell=False,
        )
    except OSError as error:
        raise PreflightError(f"could not start Claude preflight: {error}") from error
    try:
        process_scope = retain_process_group_scope(process)
    except ProcessError as error:
        # start_new_session has already bound this unreaped child to its own
        # non-reusable group. Kill before reaping if richer OS identity evidence
        # cannot be captured; never let an untracked preflight continue.
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise PreflightError(
            "Claude preflight process-group identity could not be captured"
        ) from error
    assert process.stdout is not None
    assert process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    deadline = time.monotonic() + float(timeout_seconds)
    total_bytes = 0
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreflightError("Claude preflight timed out")
            ready = selector.select(remaining)
            if not ready:
                raise PreflightError("Claude preflight timed out")
            for key, _ in ready:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), min(65_536, output_limit_bytes + 1))
                except OSError as error:
                    raise PreflightError("could not read Claude preflight output") from error
                if not chunk:
                    selector.unregister(stream)
                    continue
                total_bytes += len(chunk)
                if total_bytes > output_limit_bytes:
                    raise PreflightError("Claude preflight output exceeded its byte limit")
                streams[stream].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PreflightError("Claude preflight timed out")
        try:
            leader_exited = wait_for_retained_process_exit(
                process,
                process_scope,
                remaining,
            )
        except ProcessError as error:
            raise PreflightError(
                "Claude preflight leader exit could not be observed"
            ) from error
        if not leader_exited:
            raise PreflightError("Claude preflight timed out")
    except BaseException:
        try:
            _terminate_process(process, process_scope)
        except ProcessError as cleanup_error:
            raise PreflightError(
                "Claude preflight process-group cleanup failed"
            ) from cleanup_error
        raise
    else:
        try:
            return_code = terminate_retained_process_group(
                process,
                process_scope,
                grace_seconds=_PROCESS_TERMINATE_GRACE_SECONDS,
            )
        except ProcessError as cleanup_error:
            raise PreflightError(
                "Claude preflight process-group cleanup failed"
            ) from cleanup_error
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    stdout = bytes(streams[process.stdout])
    stderr = bytes(streams[process.stderr])
    if return_code != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        detail = redact_text(detail) or f"exit {return_code}"
        raise PreflightError(f"Claude preflight failed: {detail}")
    return stdout, stderr


def _preflight_environment(
    parent_environment: Mapping[str, str] | None,
    allowed_auth_names: Iterable[str],
) -> dict[str, str]:
    source = os.environ if parent_environment is None else parent_environment
    try:
        return build_child_environment(
            source,
            allowed_auth_names=allowed_auth_names,
        )
    except (TypeError, ValueError) as error:
        raise PreflightError("Claude preflight environment policy is invalid") from error


def _decode_text(payload: bytes, label: str) -> str:
    try:
        text = payload.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise PreflightError(f"{label} was not valid UTF-8") from error
    if not text or "\x00" in text:
        raise PreflightError(f"{label} was empty or invalid")
    return text


def get_version(
    executable: str | os.PathLike[str] = "claude",
    *,
    environment: Mapping[str, str] | None = None,
    allowed_auth_names: Iterable[str] = (),
    timeout_seconds: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    output_limit_bytes: int = DEFAULT_PREFLIGHT_OUTPUT_LIMIT_BYTES,
) -> str:
    """Run only ``claude --version`` with bounded time and capture."""

    executable_text = _safe_executable(executable)
    stdout, _ = _run_preflight_command(
        [executable_text, "--version"],
        environment=_preflight_environment(environment, allowed_auth_names),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
    )
    return _decode_text(stdout, "Claude version output")


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClaudeProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ClaudeProtocolError(f"non-finite JSON number: {value}")


class _JsonFloat(float):
    """Float-compatible decoded value retaining its bounded source spelling."""

    def __new__(cls, value: str | float) -> "_JsonFloat":
        instance = super().__new__(cls, value)
        instance.lexeme = value.lexeme if isinstance(value, _JsonFloat) else str(value)
        return instance


def _parse_json_float(value: str) -> _JsonFloat:
    if len(value.encode("ascii")) > 64:
        raise ClaudeProtocolError("JSON number exceeds its representation limit")
    try:
        exact = Decimal(value)
        decoded = _JsonFloat(value)
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise ClaudeProtocolError("JSON number is invalid") from error
    if not exact.is_finite() or not math.isfinite(decoded):
        raise ClaudeProtocolError("JSON number is not finite")
    if exact != 0 and decoded == 0:
        raise ClaudeProtocolError("JSON number underflows bounded float storage")
    return decoded


def _parse_json_integer(value: str) -> int:
    if len(value) > 32:
        raise ClaudeProtocolError("JSON integer exceeds its representation limit")
    try:
        return int(value)
    except ValueError as error:  # pragma: no cover - json scanner contract
        raise ClaudeProtocolError("JSON integer is invalid") from error


def _validate_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ClaudeProtocolError("non-finite JSON number")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item)


def _load_json_object(
    payload: bytes | str,
    *,
    maximum_bytes: int,
    label: str,
) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            encoded = payload.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ClaudeProtocolError(f"{label} is not valid UTF-8") from error
    elif isinstance(payload, bytes):
        encoded = payload
    else:
        raise ClaudeProtocolError(f"{label} must be bytes or text")
    if not encoded or len(encoded) > maximum_bytes:
        raise ClaudeProtocolError(f"{label} is empty or exceeds its byte limit")
    try:
        text = encoded.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
            parse_int=_parse_json_integer,
        )
        _validate_finite_json(value)
    except ClaudeProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ClaudeProtocolError(f"{label} is malformed JSON") from error
    if not isinstance(value, dict):
        raise ClaudeProtocolError(f"{label} must be a JSON object")
    return value


def get_auth_status(
    executable: str | os.PathLike[str] = "claude",
    *,
    environment: Mapping[str, str] | None = None,
    allowed_auth_names: Iterable[str] = (),
    timeout_seconds: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    output_limit_bytes: int = DEFAULT_PREFLIGHT_OUTPUT_LIMIT_BYTES,
) -> dict[str, Any]:
    """Run only ``claude auth status --json`` with bounded time and capture."""

    executable_text = _safe_executable(executable)
    stdout, _ = _run_preflight_command(
        [executable_text, "auth", "status", "--json"],
        environment=_preflight_environment(environment, allowed_auth_names),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
    )
    try:
        status = _load_json_object(
            stdout,
            maximum_bytes=output_limit_bytes,
            label="Claude auth status",
        )
    except ClaudeProtocolError as error:
        raise PreflightError(str(error)) from error
    if status.get("loggedIn") is not True:
        raise PreflightError("Claude auth status reports not logged in")
    return status


def _auth_preflight_access_outcome(
    error: PreflightError,
) -> tuple[str, str] | None:
    """Classify only deterministic access markers from the auth-status stage."""

    text = str(error).casefold()
    if "oauth authentication is currently not allowed for this organization" in text:
        return "oauth_organization", "oauth_organization_denied"
    if any(
        marker in text
        for marker in (
            "authentication failed",
            "unauthorized",
            "invalid api key",
            "token revoked",
            "not logged in",
            "login required",
        )
    ):
        return "authentication", "authentication_failed"
    if any(
        marker in text
        for marker in ("credit balance", "credits exhausted", "billing", "payment required")
    ):
        return "billing", "billing_error"
    if any(marker in text for marker in ("rate limit", "quota exceeded")):
        return "rate_limit", "rate_limit_reached"
    return None


def preflight(
    executable: str | os.PathLike[str] = "claude",
    *,
    environment: Mapping[str, str] | None = None,
    allowed_auth_names: Iterable[str] = (),
    timeout_seconds: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    output_limit_bytes: int = DEFAULT_PREFLIGHT_OUTPUT_LIMIT_BYTES,
) -> PreflightResult:
    """Return provider-free Claude CLI version and auth status evidence."""

    executable_text = _safe_executable(executable)
    allowed_auth = tuple(allowed_auth_names)
    version = get_version(
        executable_text,
        environment=environment,
        allowed_auth_names=allowed_auth,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
    )
    if not is_supported_claude_code_version(version):
        raise UnsupportedClaudeCodeVersion(version)
    try:
        auth_status = get_auth_status(
            executable_text,
            environment=environment,
            allowed_auth_names=allowed_auth,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )
    except PreflightError as error:
        access_outcome = _auth_preflight_access_outcome(error)
        raise PreflightError(
            "Claude auth-status preflight failed",
            observed_version=version,
            provider_access_classification=(
                None if access_outcome is None else access_outcome[0]
            ),
            provider_access_reason=(
                None if access_outcome is None else access_outcome[1]
            ),
        ) from error
    return PreflightResult(
        executable=executable_text,
        version=version,
        auth_status=auth_status,
    )


def is_supported_claude_code_version(value: Any) -> bool:
    """Return whether provider-free version evidence matches the bound parser."""

    return value == SUPPORTED_CLAUDE_CODE_VERSION_OUTPUT


def _protocol_string(value: Any, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise ClaudeProtocolError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ClaudeProtocolError(f"{label} must be valid UTF-8") from error
    if len(encoded) > maximum_bytes or "\x00" in value:
        raise ClaudeProtocolError(f"{label} exceeds its text bound")
    return value


def _protocol_text(value: Any, label: str, maximum_bytes: int) -> str:
    text = _protocol_string(value, label, maximum_bytes)
    if not text:
        raise ClaudeProtocolError(f"{label} must be non-empty text")
    return text


def _protocol_identifier(value: Any, label: str, maximum_bytes: int) -> str:
    identifier = _protocol_text(value, label, maximum_bytes)
    if any(character.isspace() for character in identifier):
        raise ClaudeProtocolError(f"{label} must not contain whitespace")
    return identifier


def _protocol_model_identifier(value: Any, label: str) -> str:
    identifier = _protocol_identifier(value, label, MAX_MODEL_ID_BYTES)
    if redact_text(identifier) != identifier:
        raise ClaudeProtocolError("provider model identity is credential-shaped")
    return identifier


def _optional_string(
    data: Mapping[str, Any],
    name: str,
    *,
    maximum_bytes: int = 256,
) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    return _protocol_text(value, f"Claude event {name}", maximum_bytes)


def _required_session(data: Mapping[str, Any], label: str) -> str:
    return _protocol_identifier(
        data.get("session_id"),
        f"{label}.session_id",
        MAX_SESSION_ID_BYTES,
    )


def _required_bounded_text_array(
    data: Mapping[str, Any], name: str, *, label: str
) -> tuple[str, ...]:
    if name not in data:
        raise ClaudeProtocolError(f"{label}.{name} is required")
    value = data[name]
    if not isinstance(value, list) or len(value) > MAX_INIT_ARRAY_ENTRIES:
        raise ClaudeProtocolError(f"{label}.{name} must be a bounded text array")
    return tuple(
        _protocol_text(item, f"{label}.{name}[{index}]", MAX_INIT_TEXT_BYTES)
        for index, item in enumerate(value)
    )


def _require_exact_fields(
    data: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> None:
    missing = required - set(data)
    if missing:
        raise ClaudeProtocolError(
            f"{label} is missing required field: {sorted(missing)[0]}"
        )
    unexpected = set(data) - required - optional
    if unexpected:
        raise ClaudeProtocolError(f"{label} has an unexpected field")


def _required_boolean(data: Mapping[str, Any], name: str, *, label: str) -> bool:
    value = data.get(name)
    if not isinstance(value, bool):
        raise ClaudeProtocolError(f"{label}.{name} must be a boolean")
    return value


def _validate_bounded_text_mapping(value: Any, *, label: str) -> None:
    if not isinstance(value, dict) or len(value) > MAX_INIT_ARRAY_ENTRIES:
        raise ClaudeProtocolError(f"{label} must be a bounded text object")
    for index, (key, item) in enumerate(value.items()):
        _protocol_text(key, f"{label} key[{index}]", 256)
        _protocol_text(item, f"{label} value[{index}]", MAX_INIT_TEXT_BYTES)


def _validate_mcp_server_errors(value: Any) -> None:
    if not isinstance(value, list) or len(value) > MAX_INIT_ARRAY_ENTRIES:
        raise ClaudeProtocolError(
            "system/init.mcp_server_errors must be a bounded array"
        )
    required = frozenset({"name", "type", "message"})
    for index, error in enumerate(value):
        if not isinstance(error, dict):
            raise ClaudeProtocolError("system/init MCP server error is malformed")
        _require_exact_fields(
            error,
            required=required,
            label=f"system/init.mcp_server_errors[{index}]",
        )
        _protocol_text(
            error["name"], f"system/init.mcp_server_errors[{index}].name", 256
        )
        _protocol_text(
            error["type"], f"system/init.mcp_server_errors[{index}].type", 256
        )
        _protocol_text(
            error["message"],
            f"system/init.mcp_server_errors[{index}].message",
            MAX_INIT_TEXT_BYTES,
        )


def _validate_init_event(data: Mapping[str, Any]) -> None:
    """Validate the installed Claude Code 2.1.248 init trust anchor."""

    _require_exact_fields(
        data,
        required=_INIT_REQUIRED_FIELDS,
        optional=_INIT_OPTIONAL_FIELDS,
        label="system/init",
    )
    api_key_source = _protocol_text(
        data["apiKeySource"],
        "system/init.apiKeySource",
        64,
    )
    if api_key_source not in _API_KEY_SOURCES:
        raise ClaudeProtocolError("system/init.apiKeySource is invalid")
    version = _protocol_text(
        data["claude_code_version"],
        "system/init.claude_code_version",
        64,
    )
    if version != SUPPORTED_CLAUDE_CODE_VERSION:
        raise ClaudeProtocolError("system/init Claude Code version is unsupported")
    _protocol_text(data["cwd"], "system/init.cwd", MAX_INIT_TEXT_BYTES)
    for name in (
        "agents",
        "betas",
        "capabilities",
        "terminal_slash_commands",
    ):
        if name in data:
            _required_bounded_text_array(data, name, label="system/init")
    if "analytics_disabled" in data:
        _required_boolean(data, "analytics_disabled", label="system/init")
    if "fast_mode_disabled_reason" in data:
        disabled_reason = data["fast_mode_disabled_reason"]
        if disabled_reason is not None:
            disabled_reason = _protocol_text(
                disabled_reason,
                "system/init.fast_mode_disabled_reason",
                MAX_INIT_TEXT_BYTES,
            )
            if disabled_reason not in _FAST_MODE_DISABLED_REASONS:
                raise ClaudeProtocolError(
                    "system/init.fast_mode_disabled_reason is invalid"
                )
    if "fast_mode_state" in data:
        fast_mode_state = _protocol_text(
            data["fast_mode_state"], "system/init.fast_mode_state", 256
        )
        if fast_mode_state not in _FAST_MODE_STATES:
            raise ClaudeProtocolError("system/init.fast_mode_state is invalid")
    if "effort" in data:
        effort = data["effort"]
        if effort is not None:
            effort = _protocol_text(effort, "system/init.effort", 64)
            if effort not in EFFORTS:
                raise ClaudeProtocolError("system/init.effort is invalid")
    if "memory_paths" in data:
        _validate_bounded_text_mapping(
            data["memory_paths"], label="system/init.memory_paths"
        )
    if "messaging_socket_path" in data:
        _protocol_text(
            data["messaging_socket_path"],
            "system/init.messaging_socket_path",
            MAX_INIT_TEXT_BYTES,
        )
    _required_bounded_text_array(data, "tools", label="system/init")
    _required_bounded_text_array(data, "slash_commands", label="system/init")
    _required_bounded_text_array(data, "skills", label="system/init")
    _protocol_text(data["output_style"], "system/init.output_style", 256)
    permission_mode = _protocol_text(
        data["permissionMode"], "system/init.permissionMode", 64
    )
    if permission_mode not in _PERMISSION_MODES:
        raise ClaudeProtocolError("system/init.permissionMode is invalid")
    if "product_feedback_disabled" in data:
        _required_boolean(data, "product_feedback_disabled", label="system/init")

    mcp_servers = data["mcp_servers"]
    if not isinstance(mcp_servers, list) or len(mcp_servers) > MAX_INIT_ARRAY_ENTRIES:
        raise ClaudeProtocolError("system/init.mcp_servers must be a bounded array")
    for index, server in enumerate(mcp_servers):
        if not isinstance(server, dict) or not {"name", "status"} <= set(server):
            raise ClaudeProtocolError("system/init MCP server is malformed")
        _protocol_text(
            server["name"], f"system/init.mcp_servers[{index}].name", 256
        )
        _protocol_text(
            server["status"], f"system/init.mcp_servers[{index}].status", 256
        )

    plugins = data["plugins"]
    if not isinstance(plugins, list) or len(plugins) > MAX_INIT_ARRAY_ENTRIES:
        raise ClaudeProtocolError("system/init.plugins must be a bounded array")
    for index, plugin in enumerate(plugins):
        if not isinstance(plugin, dict) or not {"name", "path"} <= set(plugin):
            raise ClaudeProtocolError("system/init plugin is malformed")
        _protocol_text(plugin["name"], f"system/init.plugins[{index}].name", 256)
        _protocol_text(
            plugin["path"],
            f"system/init.plugins[{index}].path",
            MAX_INIT_TEXT_BYTES,
        )
        for field in ("source", "version"):
            if field in plugin:
                _protocol_text(
                    plugin[field], f"system/init.plugins[{index}].{field}", 256
                )
    if "mcp_server_errors" in data:
        _validate_mcp_server_errors(data["mcp_server_errors"])


def _validate_task_notification(data: Mapping[str, Any]) -> None:
    required = frozenset(
        {
            "type",
            "subtype",
            "task_id",
            "status",
            "output_file",
            "summary",
            "uuid",
            "session_id",
        }
    )
    optional = frozenset(
        {"tool_use_id", "usage", "skip_transcript", "ambient"}
    )
    _require_exact_fields(
        data,
        required=required,
        optional=optional,
        label="system/task_notification",
    )
    _protocol_identifier(
        data["task_id"], "system/task_notification.task_id", MAX_REQUEST_ID_BYTES
    )
    if "tool_use_id" in data:
        _protocol_identifier(
            data["tool_use_id"],
            "system/task_notification.tool_use_id",
            MAX_REQUEST_ID_BYTES,
        )
    status = _protocol_text(data["status"], "system/task_notification.status", 64)
    if status not in _TASK_NOTIFICATION_STATUSES:
        raise ClaudeProtocolError("system/task_notification.status is invalid")
    _protocol_string(
        data["output_file"],
        "system/task_notification.output_file",
        MAX_EVENT_BYTES,
    )
    _protocol_string(
        data["summary"], "system/task_notification.summary", MAX_EVENT_BYTES
    )
    if "usage" in data:
        _validate_lifecycle_usage(
            data["usage"], label="system/task_notification.usage"
        )
    for name in ("skip_transcript", "ambient"):
        if name in data and not isinstance(data[name], bool):
            raise ClaudeProtocolError(
                f"system/task_notification.{name} must be a boolean"
            )
    _protocol_identifier(
        data["uuid"], "system/task_notification.uuid", MAX_REQUEST_ID_BYTES
    )
    _required_session(data, "system/task_notification")


def _validate_lifecycle_usage(value: Any, *, label: str) -> None:
    required = frozenset({"total_tokens", "tool_uses", "duration_ms"})
    usage = value
    if not isinstance(usage, dict):
        raise ClaudeProtocolError(f"{label} must be an object")
    _require_exact_fields(
        usage,
        required=required,
        label=label,
    )
    for name in required:
        _bounded_count(usage[name], f"{label}.{name}")


def _validate_task_started(data: Mapping[str, Any]) -> None:
    required = frozenset(
        {
            "type",
            "subtype",
            "description",
            "session_id",
            "task_id",
            "uuid",
        }
    )
    optional = frozenset(
        {
            "tool_use_id",
            "subagent_type",
            "is_backgrounded",
            "spawn_depth",
            "task_type",
            "workflow_name",
            "prompt",
            "skip_transcript",
            "ambient",
        }
    )
    _require_exact_fields(
        data,
        required=required,
        optional=optional,
        label="system/task_started",
    )
    _protocol_identifier(
        data["task_id"], "system/task_started.task_id", MAX_REQUEST_ID_BYTES
    )
    if "tool_use_id" in data:
        _protocol_identifier(
            data["tool_use_id"],
            "system/task_started.tool_use_id",
            MAX_REQUEST_ID_BYTES,
        )
    for name in ("task_type", "subagent_type", "workflow_name"):
        if name in data:
            _protocol_string(data[name], f"system/task_started.{name}", 256)
    _protocol_string(
        data["description"], "system/task_started.description", MAX_EVENT_BYTES
    )
    if "prompt" in data:
        _protocol_string(
            data["prompt"], "system/task_started.prompt", MAX_EVENT_BYTES
        )
    for name in ("is_backgrounded", "skip_transcript", "ambient"):
        if name in data and not isinstance(data[name], bool):
            raise ClaudeProtocolError(f"system/task_started.{name} must be a boolean")
    if "spawn_depth" in data:
        _bounded_count(data["spawn_depth"], "system/task_started.spawn_depth", 128)
    _protocol_identifier(
        data["uuid"], "system/task_started.uuid", MAX_REQUEST_ID_BYTES
    )
    _required_session(data, "system/task_started")


def _validate_task_progress(data: Mapping[str, Any]) -> None:
    required = frozenset(
        {
            "type",
            "subtype",
            "task_id",
            "description",
            "usage",
            "uuid",
            "session_id",
        }
    )
    optional = frozenset(
        {"tool_use_id", "subagent_type", "last_tool_name", "summary"}
    )
    _require_exact_fields(
        data,
        required=required,
        optional=optional,
        label="system/task_progress",
    )
    _protocol_identifier(
        data["task_id"], "system/task_progress.task_id", MAX_REQUEST_ID_BYTES
    )
    if "tool_use_id" in data:
        _protocol_identifier(
            data["tool_use_id"],
            "system/task_progress.tool_use_id",
            MAX_REQUEST_ID_BYTES,
        )
    _protocol_string(
        data["description"], "system/task_progress.description", MAX_EVENT_BYTES
    )
    for name in ("subagent_type", "last_tool_name"):
        if name in data:
            _protocol_string(data[name], f"system/task_progress.{name}", 256)
    if "summary" in data:
        _protocol_string(
            data["summary"], "system/task_progress.summary", MAX_EVENT_BYTES
        )
    _validate_lifecycle_usage(data["usage"], label="system/task_progress.usage")
    _protocol_identifier(
        data["uuid"], "system/task_progress.uuid", MAX_REQUEST_ID_BYTES
    )
    _required_session(data, "system/task_progress")


def _validate_task_updated(data: Mapping[str, Any]) -> None:
    required = frozenset({"type", "subtype", "task_id", "patch", "uuid", "session_id"})
    _require_exact_fields(data, required=required, label="system/task_updated")
    _protocol_identifier(
        data["task_id"], "system/task_updated.task_id", MAX_REQUEST_ID_BYTES
    )
    patch = data["patch"]
    allowed_patch = frozenset(
        {
            "status",
            "description",
            "end_time",
            "total_paused_ms",
            "error",
            "is_backgrounded",
        }
    )
    if not isinstance(patch, dict):
        raise ClaudeProtocolError("system/task_updated.patch must be an object")
    unexpected = set(patch) - allowed_patch
    if unexpected:
        raise ClaudeProtocolError(
            "system/task_updated.patch has an unexpected field"
        )
    if "status" in patch:
        status = _protocol_text(
            patch["status"], "system/task_updated.patch.status", 64
        )
        if status not in _TASK_UPDATED_STATUSES:
            raise ClaudeProtocolError("system/task_updated.patch.status is invalid")
    for name in ("description", "error"):
        if name in patch:
            _protocol_string(
                patch[name], f"system/task_updated.patch.{name}", MAX_EVENT_BYTES
            )
    for name in ("end_time", "total_paused_ms"):
        if name in patch:
            _optional_bounded_integer(
                patch[name],
                f"system/task_updated.patch.{name}",
                minimum=0,
                maximum=_MAX_TASK_TIME_MS,
            )
    if "is_backgrounded" in patch and not isinstance(
        patch["is_backgrounded"], bool
    ):
        raise ClaudeProtocolError(
            "system/task_updated.patch.is_backgrounded must be a boolean"
        )
    _protocol_identifier(
        data["uuid"], "system/task_updated.uuid", MAX_REQUEST_ID_BYTES
    )
    _required_session(data, "system/task_updated")


def _validate_background_tasks_changed(data: Mapping[str, Any]) -> None:
    required = frozenset({"type", "subtype", "tasks", "uuid", "session_id"})
    _require_exact_fields(
        data, required=required, label="system/background_tasks_changed"
    )
    tasks = data["tasks"]
    if not isinstance(tasks, list) or len(tasks) > MAX_INIT_ARRAY_ENTRIES:
        raise ClaudeProtocolError(
            "system/background_tasks_changed.tasks must be a bounded array"
        )
    task_fields = frozenset({"description", "task_id", "task_type"})
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ClaudeProtocolError(
                "system/background_tasks_changed task is malformed"
            )
        label = f"system/background_tasks_changed.tasks[{index}]"
        _require_exact_fields(
            task,
            required=task_fields,
            optional=frozenset({"ambient"}),
            label=label,
        )
        _protocol_identifier(
            task["task_id"], f"{label}.task_id", MAX_REQUEST_ID_BYTES
        )
        _protocol_string(task["task_type"], f"{label}.task_type", 256)
        _protocol_string(
            task["description"], f"{label}.description", MAX_EVENT_BYTES
        )
        if "ambient" in task and not isinstance(task["ambient"], bool):
            raise ClaudeProtocolError(f"{label}.ambient must be a boolean")
    _protocol_identifier(
        data["uuid"], "system/background_tasks_changed.uuid", MAX_REQUEST_ID_BYTES
    )
    _required_session(data, "system/background_tasks_changed")


def _validate_thinking_tokens(data: Mapping[str, Any]) -> None:
    required = frozenset(
        {
            "type",
            "subtype",
            "estimated_tokens",
            "estimated_tokens_delta",
            "uuid",
            "session_id",
        }
    )
    _require_exact_fields(data, required=required, label="system/thinking_tokens")
    _bounded_count(
        data["estimated_tokens"], "system/thinking_tokens.estimated_tokens"
    )
    _bounded_count(
        data["estimated_tokens_delta"],
        "system/thinking_tokens.estimated_tokens_delta",
    )
    _protocol_identifier(
        data["uuid"], "system/thinking_tokens.uuid", MAX_REQUEST_ID_BYTES
    )
    _required_session(data, "system/thinking_tokens")


def _bounded_count(value: Any, label: str, maximum: int = MAX_USAGE_COUNT) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        raise ClaudeProtocolError(f"{label} must be a bounded non-negative integer")
    return value


def _optional_num_turns(data: Mapping[str, Any]) -> int | None:
    value = data.get("num_turns")
    if value is None:
        return None
    return _bounded_count(value, "result.num_turns", MAX_NUM_TURNS)


def _receipt_cost(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClaudeProtocolError(f"{label} must be a JSON number")
    lexeme = value.lexeme if isinstance(value, _JsonFloat) else str(value)
    if len(lexeme.encode("ascii")) > 64:
        raise ClaudeProtocolError(f"{label} exceeds its representation bound")
    try:
        cost = Decimal(lexeme)
    except InvalidOperation as error:
        raise ClaudeProtocolError(f"{label} is not a valid decimal") from error
    if not cost.is_finite() or cost < 0 or cost > MAX_RECEIPT_COST_USD:
        raise ClaudeProtocolError(f"{label} is outside its allowed range")
    components = cost.as_tuple()
    end = len(components.digits)
    while end > 1 and components.digits[end - 1] == 0:
        end -= 1
    exponent = int(components.exponent) + len(components.digits) - end
    if (
        max(-exponent, 0) > MAX_RECEIPT_COST_FRACTIONAL_PLACES
        or end > MAX_RECEIPT_COST_DIGITS
    ):
        raise ClaudeProtocolError(f"{label} exceeds its precision bound")
    return cost


def _optional_cost(data: Mapping[str, Any]) -> Decimal | None:
    value = data.get("total_cost_usd", data.get("cost_usd"))
    return None if value is None else _receipt_cost(value, "result cost")


def _validate_model_usage(
    value: Any,
    *,
    allow_empty: bool = False,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > MAX_MODEL_USAGE_ENTRIES:
        raise ClaudeProtocolError("result.modelUsage must contain bounded model entries")
    if not value:
        return {}
    for model_id, entry in value.items():
        _protocol_model_identifier(model_id, "modelUsage model id")
        if not isinstance(entry, dict):
            raise ClaudeProtocolError("modelUsage entry must be an object")
        entry_fields = set(entry)
        if (
            not _MODEL_USAGE_FIELDS <= entry_fields
            or entry_fields - _MODEL_USAGE_FIELDS - _MODEL_USAGE_OPTIONAL_FIELDS
        ):
            raise ClaudeProtocolError(
                "modelUsage entry must contain the exact 2.1.248 fields"
            )
        for field in _MODEL_USAGE_COUNT_FIELDS:
            _bounded_count(entry[field], f"modelUsage.{model_id}.{field}")
        _receipt_cost(entry["costUSD"], f"modelUsage.{model_id}.costUSD")
        if "canonicalModel" in entry:
            _protocol_model_identifier(
                entry["canonicalModel"],
                f"modelUsage.{model_id}.canonicalModel",
            )
        if "provider" in entry:
            _protocol_text(
                entry["provider"],
                f"modelUsage.{model_id}.provider",
                256,
            )
        if "costBasis" in entry:
            cost_basis = _protocol_text(
                entry["costBasis"],
                f"modelUsage.{model_id}.costBasis",
                64,
            )
            if cost_basis not in _MODEL_USAGE_COST_BASES:
                raise ClaudeProtocolError(
                    f"modelUsage.{model_id}.costBasis is invalid"
                )
    return copy.deepcopy(value)


def _validate_count_object(value: Any, fields: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ClaudeProtocolError(f"{label} must be an object")
    for field in fields & set(value):
        _bounded_count(value[field], f"{label}.{field}")


def _usage_decimal(value: Any, label: str, *, allow_negative: bool) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClaudeProtocolError(f"{label} must be a JSON number")
    lexeme = value.lexeme if isinstance(value, _JsonFloat) else str(value)
    if len(lexeme.encode("ascii")) > 64:
        raise ClaudeProtocolError(f"{label} exceeds its representation bound")
    try:
        number = Decimal(lexeme)
    except InvalidOperation as error:
        raise ClaudeProtocolError(f"{label} is not a valid decimal") from error
    lower_bound = -MAX_USAGE_DECIMAL if allow_negative else Decimal(0)
    if (
        not number.is_finite()
        or number < lower_bound
        or number > MAX_USAGE_DECIMAL
    ):
        raise ClaudeProtocolError(f"{label} is outside its allowed range")
    components = number.as_tuple()
    end = len(components.digits)
    while end > 1 and components.digits[end - 1] == 0:
        end -= 1
    exponent = int(components.exponent) + len(components.digits) - end
    if (
        max(-exponent, 0) > MAX_USAGE_DECIMAL_FRACTIONAL_PLACES
        or end > MAX_USAGE_DECIMAL_DIGITS
    ):
        raise ClaudeProtocolError(f"{label} exceeds its precision bound")
    return number


def _bounded_extension_text(value: Any, label: str, maximum_bytes: int) -> None:
    if not isinstance(value, str):
        raise ClaudeProtocolError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ClaudeProtocolError(f"{label} must be valid UTF-8") from error
    if len(encoded) > maximum_bytes or "\x00" in value:
        raise ClaudeProtocolError(f"{label} exceeds its text bound")


def _validate_usage_extension(
    value: Any,
    label: str,
    *,
    depth: int,
    nodes: list[int],
) -> None:
    nodes[0] += 1
    if nodes[0] > MAX_USAGE_EXTENSION_NODES:
        raise ClaudeProtocolError("usage extensions exceed their node bound")
    if depth > MAX_USAGE_EXTENSION_DEPTH:
        raise ClaudeProtocolError(f"{label} exceeds its nesting bound")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        _bounded_extension_text(value, label, MAX_USAGE_EXTENSION_STRING_BYTES)
        return
    if isinstance(value, int):
        if abs(value) > MAX_USAGE_COUNT:
            raise ClaudeProtocolError(f"{label} exceeds its numeric bound")
        return
    if isinstance(value, float):
        _usage_decimal(value, label, allow_negative=True)
        return
    if isinstance(value, (dict, list)):
        if len(value) > MAX_USAGE_EXTENSION_CONTAINER_ITEMS:
            raise ClaudeProtocolError(f"{label} exceeds its container bound")
        if isinstance(value, dict):
            for key, item in value.items():
                _bounded_extension_text(
                    key,
                    f"{label} key",
                    MAX_USAGE_EXTENSION_KEY_BYTES,
                )
                _validate_usage_extension(
                    item,
                    f"{label}.{key}",
                    depth=depth + 1,
                    nodes=nodes,
                )
        else:
            for index, item in enumerate(value):
                _validate_usage_extension(
                    item,
                    f"{label}[{index}]",
                    depth=depth + 1,
                    nodes=nodes,
                )
        return
    raise ClaudeProtocolError(f"{label} is not safe JSON")


def _validate_usage_extensions(
    value: Mapping[str, Any],
    known_fields: frozenset[str],
    label: str,
    nodes: list[int],
) -> None:
    if len(value) > MAX_USAGE_EXTENSION_CONTAINER_ITEMS:
        raise ClaudeProtocolError(f"{label} exceeds its container bound")
    for field in set(value) - known_fields:
        _bounded_extension_text(
            field,
            f"{label} extension key",
            MAX_USAGE_EXTENSION_KEY_BYTES,
        )
        _validate_usage_extension(
            value[field],
            f"{label}.{field}",
            depth=0,
            nodes=nodes,
        )


def _validate_usage(value: Any, label: str = "usage") -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ClaudeProtocolError(f"{label} must be an object")
    if {"input_tokens", "output_tokens"} - set(value):
        raise ClaudeProtocolError(f"{label} has missing fields")
    extension_nodes = [0]
    _validate_usage_extensions(value, _USAGE_FIELDS, label, extension_nodes)
    for field in _USAGE_COUNT_FIELDS & set(value):
        _bounded_count(value[field], f"{label}.{field}")
    if "server_tool_use" in value:
        server_tool_use = value["server_tool_use"]
        _validate_count_object(server_tool_use, _SERVER_TOOL_COUNT_FIELDS, f"{label}.server_tool_use")
        _validate_usage_extensions(
            server_tool_use,
            _SERVER_TOOL_COUNT_FIELDS,
            f"{label}.server_tool_use",
            extension_nodes,
        )
    if "cache_creation" in value:
        cache_creation = value["cache_creation"]
        _validate_count_object(cache_creation, _CACHE_CREATION_COUNT_FIELDS, f"{label}.cache_creation")
        _validate_usage_extensions(
            cache_creation,
            _CACHE_CREATION_COUNT_FIELDS,
            f"{label}.cache_creation",
            extension_nodes,
        )
    if "service_tier" in value:
        _protocol_text(value["service_tier"], f"{label}.service_tier", 128)
    if "inference_geo" in value:
        _bounded_extension_text(
            value["inference_geo"],
            f"{label}.inference_geo",
            128,
        )
    if "speed" in value:
        _protocol_text(value["speed"], f"{label}.speed", 128)
    return copy.deepcopy(value)


def _optional_bounded_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ClaudeProtocolError(f"{label} must be a bounded integer")
    return value


def _exact_json_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClaudeProtocolError(f"{label} must be a JSON number")
    lexeme = value.lexeme if isinstance(value, _JsonFloat) else str(value)
    if len(lexeme.encode("ascii")) > 64:
        raise ClaudeProtocolError(f"{label} exceeds its representation bound")
    try:
        number = Decimal(lexeme)
    except InvalidOperation as error:
        raise ClaudeProtocolError(f"{label} is not a valid decimal") from error
    if not number.is_finite():
        raise ClaudeProtocolError(f"{label} must be finite")
    return number


def _bounded_json_number(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int | float:
    number = _exact_json_decimal(value, label)
    if not Decimal(minimum) <= number <= Decimal(maximum):
        raise ClaudeProtocolError(f"{label} must be a bounded number")
    return value


def _result_errors(data: Mapping[str, Any], event_type: str) -> tuple[str, ...]:
    if event_type != "result":
        return ()
    if "error" in data:
        raise ClaudeProtocolError(
            "result.error is not part of the Claude Code 2.1.248 result schema"
        )
    subtype = data.get("subtype")
    if subtype == "success":
        if "errors" in data:
            raise ClaudeProtocolError("success result must not contain errors")
        return ()
    if "errors" not in data:
        raise ClaudeProtocolError("error result must contain errors")
    value = data["errors"]
    if not isinstance(value, (list, tuple)) or len(value) > MAX_PROVIDER_ERRORS:
        raise ClaudeProtocolError("result.errors must be a bounded string array")
    return tuple(
        _protocol_text(item, f"result.errors[{index}]", 4096)
        for index, item in enumerate(value)
    )


def _permission_denials(
    data: Mapping[str, Any], event_type: str
) -> tuple[dict[str, Any], ...]:
    if event_type != "result":
        return ()
    if "permission_denials" not in data:
        raise ClaudeProtocolError("result.permission_denials is required")
    value = data["permission_denials"]
    if not isinstance(value, (list, tuple)) or len(value) > MAX_PERMISSION_DENIALS:
        raise ClaudeProtocolError(
            "result.permission_denials must be a bounded object array"
        )
    result: list[dict[str, Any]] = []
    for index, denial in enumerate(value):
        if not isinstance(denial, dict) or set(denial) != {
            "tool_name",
            "tool_use_id",
            "tool_input",
        }:
            raise ClaudeProtocolError(
                f"result.permission_denials[{index}] has invalid fields"
            )
        _protocol_text(
            denial["tool_name"],
            f"result.permission_denials[{index}].tool_name",
            256,
        )
        _protocol_identifier(
            denial["tool_use_id"],
            f"result.permission_denials[{index}].tool_use_id",
            256,
        )
        if not isinstance(denial["tool_input"], dict):
            raise ClaudeProtocolError(
                f"result.permission_denials[{index}].tool_input must be an object"
            )
        result.append(copy.deepcopy(denial))
    return tuple(result)


def _validate_result_union(data: Mapping[str, Any], event_type: str) -> None:
    if event_type != "result":
        return
    subtype = data.get("subtype")
    error_subtypes = {
        "error_during_execution",
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
    }
    if subtype not in {"success", *error_subtypes}:
        raise ClaudeProtocolError("result.subtype is not a 2.1.248 result subtype")
    required = {
        "duration_ms",
        "duration_api_ms",
        "is_error",
        "num_turns",
        "stop_reason",
        "total_cost_usd",
        "usage",
        "modelUsage",
        "permission_denials",
        "uuid",
        "session_id",
    }
    missing = required - set(data)
    if missing:
        raise ClaudeProtocolError(
            f"result is missing required field: {sorted(missing)[0]}"
        )
    _bounded_count(data["duration_ms"], "result.duration_ms")
    _bounded_count(data["duration_api_ms"], "result.duration_api_ms")
    stop_reason = data["stop_reason"]
    if stop_reason is not None:
        _protocol_text(stop_reason, "result.stop_reason", 256)
    if subtype == "success":
        _protocol_text(data.get("result"), "result.result", MAX_EVENT_BYTES)
        if "errors" in data:
            raise ClaudeProtocolError("success result must not contain errors")
        api_status = data.get("api_error_status")
        if api_status is not None:
            _optional_bounded_integer(
                api_status,
                "result.api_error_status",
                minimum=100,
                maximum=599,
            )
    else:
        if "result" in data:
            raise ClaudeProtocolError("error result must not contain result text")
        if data.get("is_error") is not True:
            raise ClaudeProtocolError("error result must set is_error")
        if "errors" not in data:
            raise ClaudeProtocolError("error result must contain errors")


def _assistant_fields(
    data: Mapping[str, Any],
    event_type: str,
) -> tuple[str | None, bool, bool, str | None]:
    """Return message id, effect truth, synthetic truth, and provider error."""

    if event_type != "assistant":
        return (
            None,
            event_type in {"tool_progress", "tool_use_summary"},
            False,
            None,
        )
    message = data.get("message")
    if not isinstance(message, dict):  # checked again at the call site for clarity
        raise ClaudeProtocolError("assistant.message must be an object")
    message_id = message.get("id")
    if message_id is not None:
        message_id = _protocol_identifier(
            message_id,
            "assistant.message.id",
            MAX_REQUEST_ID_BYTES,
        )
    synthetic_hint = data.get("is_api_error_message", False)
    if not isinstance(synthetic_hint, bool):
        raise ClaudeProtocolError("assistant.is_api_error_message must be a boolean")
    provider_error = data.get("error")
    if provider_error is not None and provider_error not in _API_RETRY_ERRORS:
        raise ClaudeProtocolError("assistant.error is not a 2.1.248 provider error")
    parsed_model = _protocol_model_identifier(
        message.get("model"), "assistant.message.model"
    )
    synthetic = parsed_model == SYNTHETIC_API_ERROR_MODEL
    if synthetic != (provider_error is not None):
        raise ClaudeProtocolError(
            "assistant synthetic model and canonical provider error must agree"
        )
    status = data.get("api_error_status")
    if status is not None:
        _optional_bounded_integer(
            status,
            "assistant.api_error_status",
            minimum=100,
            maximum=599,
        )
    if provider_error is not None:
        return message_id, False, True, provider_error
    content = message.get("content")
    if content is not None and not isinstance(content, list):
        raise ClaudeProtocolError("assistant.message.content must be an array")
    usage = message.get("usage")
    output_tokens = 0
    if isinstance(usage, dict) and "output_tokens" in usage:
        output_tokens = _bounded_count(
            usage["output_tokens"], "assistant.message.usage.output_tokens"
        )
    return message_id, bool(content) or output_tokens > 0, False, None


def _api_retry_fields(
    data: Mapping[str, Any], event_type: str, subtype: str | None
) -> tuple[int | None, int | None, int | float | None, int | None, str | None]:
    if event_type != "system" or subtype != "api_retry":
        return None, None, None, None, None
    if "error_status" not in data:
        raise ClaudeProtocolError("system/api_retry.error_status is required")
    attempt = _optional_bounded_integer(
        data.get("attempt"), "system/api_retry.attempt", minimum=1, maximum=1_000_000
    )
    max_retries = _optional_bounded_integer(
        data.get("max_retries"),
        "system/api_retry.max_retries",
        minimum=0,
        maximum=1_000_000,
    )
    delay = _bounded_json_number(
        data.get("retry_delay_ms"),
        "system/api_retry.retry_delay_ms",
        minimum=0,
        maximum=86_400_000,
    )
    status = data.get("error_status")
    if status is not None:
        status = _optional_bounded_integer(
            status,
            "system/api_retry.error_status",
            minimum=100,
            maximum=599,
        )
    error = data.get("error")
    if not isinstance(error, str) or error not in _API_RETRY_ERRORS:
        raise ClaudeProtocolError("system/api_retry.error is not a 2.1.248 error name")
    return attempt, max_retries, delay, status, error


def _fallback_fields(
    data: Mapping[str, Any], event_type: str, subtype: str | None
) -> tuple[str | None, str | None, str | None]:
    if event_type != "system" or subtype not in {
        "model_fallback",
        "model_refusal_fallback",
    }:
        return None, None, None
    if subtype == "model_fallback" and data.get("trigger") not in _MODEL_FALLBACK_TRIGGERS:
        raise ClaudeProtocolError("system/model_fallback.trigger is invalid")
    if subtype == "model_refusal_fallback":
        _protocol_text(data.get("trigger"), "model refusal trigger", 128)
        if data.get("direction") != "retry":
            raise ClaudeProtocolError("model refusal fallback direction is invalid")
        _protocol_identifier(
            data.get("request_id"), "model refusal request id", MAX_REQUEST_ID_BYTES
        )
    original = _protocol_model_identifier(
        data.get("original_model"), "fallback original model"
    )
    fallback = _protocol_model_identifier(data.get("fallback_model"), "fallback model")
    _protocol_text(data.get("content"), "fallback content", 4096)
    return subtype, original, fallback


def _rate_limit_info(data: Mapping[str, Any], event_type: str) -> dict[str, Any] | None:
    if event_type != "rate_limit_event":
        return None
    value = data.get("rate_limit_info")
    if not isinstance(value, dict):
        raise ClaudeProtocolError("rate_limit_event.rate_limit_info must be an object")
    if value.get("status") not in _RATE_LIMIT_STATUSES:
        raise ClaudeProtocolError("rate_limit_event status is invalid")
    rate_limit_type = value.get("rateLimitType")
    if rate_limit_type is not None and rate_limit_type not in _RATE_LIMIT_TYPES:
        raise ClaudeProtocolError("rate_limit_event type is invalid")
    utilization = value.get("utilization")
    if utilization is not None:
        exact_utilization = _exact_json_decimal(
            utilization, "rate_limit_event utilization"
        )
        if not Decimal(0) <= exact_utilization <= Decimal(1):
            raise ClaudeProtocolError("rate_limit_event utilization is invalid")
    return copy.deepcopy(value)


def _provider_classification(
    event: ClaudeEvent,
) -> tuple[str, str, str] | None:
    text = "\n".join(event.errors).casefold()
    if event.permission_denials:
        return "permission_denial", "PAUSED_POLICY", "permission_denied"
    if "oauth authentication is currently not allowed for this organization" in text:
        return "oauth_organization", "PAUSED_PROVIDER_ACCESS", "oauth_organization_denied"
    if any(
        marker in text
        for marker in (
            "authentication",
            "unauthorized",
            "invalid api key",
            "token revoked",
            "x-api-key",
        )
    ):
        return "authentication", "PAUSED_PROVIDER_ACCESS", "authentication_failed"
    if any(
        marker in text
        for marker in ("credit balance", "credits exhausted", "billing", "payment required")
    ):
        return "billing", "PAUSED_PROVIDER_ACCESS", "billing_access_required"
    if any(marker in text for marker in ("rate limit", "usage limit", "too many requests")):
        return "rate_limit", "PAUSED_PROVIDER_ACCESS", "rate_limit_reached"
    if "model" in text and any(
        marker in text
        for marker in ("not found", "does not exist", "retired", "unavailable")
    ):
        return "model_not_found", "PAUSED_PROVIDER_ACCESS", "model_not_found"
    if "sandbox" in text and any(
        marker in text for marker in ("unavailable", "not available", "required")
    ):
        return "sandbox_unavailable", "PAUSED_POLICY", "sandbox_unavailable"
    if "permission denied" in text:
        return "permission_denial", "PAUSED_POLICY", "permission_denied"
    if any(
        marker in text
        for marker in (
            "connection reset",
            "connection refused",
            "timed out",
            "timeout",
            "network",
            "transport",
            "socket",
            "dns",
        )
    ):
        return "transport", "FAILED", "transport_failure"
    if any(marker in text for marker in ("malformed provider response", "protocol")):
        return "protocol", "FAILED", "protocol_failure"
    if any(marker in text for marker in ("request cancelled", "request canceled", "aborted")):
        return "cancellation", "FAILED", "provider_cancellation"
    if event.is_error is True:
        return "internal_failure", "FAILED", "internal_failure"
    return None


class ProviderPolicyTracker:
    """One-attempt policy state derived only from canonical Claude events."""

    def __init__(
        self,
        *,
        model_requested: str,
        max_tokens: int,
        max_turns: int,
        max_cost_usd: Decimal,
        effort_requested: str | None = None,
    ):
        try:
            self.model_requested = _protocol_model_identifier(
                model_requested, "requested model"
            )
        except ClaudeProtocolError as error:
            raise AdapterError(str(error)) from error
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= MAX_TOKEN_BUDGET
        ):
            raise AdapterError("maximum tokens is outside its bound")
        if (
            isinstance(max_turns, bool)
            or not isinstance(max_turns, int)
            or not 1 <= max_turns <= MAX_ASSISTANT_MESSAGE_IDS
        ):
            raise AdapterError("maximum turns is outside its bound")
        if not isinstance(max_cost_usd, Decimal):
            try:
                max_cost_usd = Decimal(str(max_cost_usd))
            except (InvalidOperation, ValueError) as error:
                raise AdapterError("maximum USD cost is invalid") from error
        if (
            not max_cost_usd.is_finite()
            or max_cost_usd <= 0
            or max_cost_usd > MAX_BUDGET_USD
        ):
            raise AdapterError("maximum USD cost is outside its bound")
        self.max_tokens = max_tokens
        self.max_turns = max_turns
        self.max_cost_usd = max_cost_usd
        if effort_requested is not None and effort_requested not in EFFORTS:
            raise AdapterError("requested effort is unsupported")
        self.effort_requested = effort_requested
        self.model_cli_accepted: str | None = None
        self._init_seen = False
        self._models_observed: list[str] = []
        self._assistant_message_ids: list[str] = []
        self.retry_count = 0
        self.has_effect = False

    @property
    def models_observed(self) -> tuple[str, ...]:
        return tuple(self._models_observed)

    @property
    def assistant_message_ids(self) -> tuple[str, ...]:
        return tuple(self._assistant_message_ids)

    @staticmethod
    def _decision(status: str, classification: str, reason: str) -> ProviderPolicyDecision:
        return ProviderPolicyDecision(
            status=status,
            classification=classification,
            reason=reason,
            allow_substitution=False,
        )

    def observe(self, event: ClaudeEvent) -> ProviderPolicyDecision | None:
        if not isinstance(event, ClaudeEvent):
            raise AdapterError("provider policy requires a ClaudeEvent")
        if event.provider_error is not None:
            # Claude 2.1.248 can represent an API failure as a synthetic
            # assistant message. It carries no model/tool/token effect, so this
            # one canonical provider-access path is safe to classify even when
            # init could not be emitted.
            outcome = _PROVIDER_ERROR_OUTCOMES[event.provider_error]
            return self._decision(*outcome)
        if event.type == "system" and event.subtype == "init":
            if self._init_seen:
                return self._decision("FAILED", "protocol", "duplicate_init")
            self._init_seen = True
            self.model_cli_accepted = event.model
            applied_effort = event.data.get("effort")
            if (
                self.effort_requested is not None
                and applied_effort is not None
                and applied_effort != self.effort_requested
            ):
                return self._decision(
                    "PAUSED_POLICY",
                    "effort_mismatch",
                    "requested_effort_not_applied",
                )
            if (
                _FULL_MODEL_NAME.fullmatch(self.model_requested)
                and event.model != self.model_requested
            ):
                return self._decision(
                    "PAUSED_POLICY", "model_mismatch", "full_name_model_mismatch"
                )
            return None
        preinit_kind = None
        if event.type in {"assistant", "result", "rate_limit_event"}:
            preinit_kind = event.type
        elif event.type == "system" and event.subtype in {
            "api_retry",
            "model_fallback",
            "model_refusal_fallback",
        }:
            preinit_kind = event.subtype
        elif event.has_effect:
            preinit_kind = event.type
        if not self._init_seen and preinit_kind is not None:
            return self._decision(
                "FAILED", "protocol", f"{preinit_kind}_before_init"
            )
        if event.fallback_kind is not None:
            return self._decision("PAUSED_POLICY", "model_fallback", event.fallback_kind)
        if event.is_api_retry:
            if self.has_effect:
                return self._decision(
                    "PAUSED_POLICY", "transport", "api_retry_after_effect"
                )
            if event.api_retry_error not in {"server_error", "unknown"}:
                return self._decision(*_PROVIDER_ERROR_OUTCOMES[event.api_retry_error])
            if self.retry_count >= 1 or event.api_retry_attempt != 1:
                return self._decision(
                    "PAUSED_POLICY", "transport", "api_retry_limit_exceeded"
                )
            self.retry_count += 1
            return None
        if event.type == "assistant":
            if event.has_effect:
                self.has_effect = True
            if event.assistant_message_id is None:
                return self._decision(
                    "PAUSED_POLICY", "protocol", "assistant_message_id_missing"
                )
            if event.assistant_message_id not in self._assistant_message_ids:
                self._assistant_message_ids.append(event.assistant_message_id)
                if len(self._assistant_message_ids) > self.max_turns:
                    return self._decision(
                        "PAUSED_POLICY", "turn_limit", "assistant_turn_limit_exceeded"
                    )
            if event.model is not None and event.model not in self._models_observed:
                self._models_observed.append(event.model)
            if len(self._models_observed) > 1:
                return self._decision(
                    "PAUSED_POLICY", "model_mismatch", "multiple_models_observed"
                )
            if (
                _FULL_MODEL_NAME.fullmatch(self.model_requested)
                and event.model != self.model_requested
            ):
                return self._decision(
                    "PAUSED_POLICY", "model_mismatch", "full_name_model_mismatch"
                )
            return None
        if event.rate_limit_info is not None:
            utilization = event.rate_limit_info.get("utilization")
            if event.rate_limit_info["status"] == "rejected" or (
                utilization is not None
                and _exact_json_decimal(
                    utilization, "rate_limit_event utilization"
                )
                >= Decimal("0.95")
            ):
                return self._decision(
                    "PAUSED_PROVIDER_ACCESS",
                    "rate_limit",
                    "remaining_allowance_at_or_below_5_percent",
                )
            return None
        if event.type == "result":
            if event.model_usage and self._models_observed:
                primary_model = self._models_observed[0]
                primary_accounted = any(
                    model == primary_model
                    or entry.get("canonicalModel") == primary_model
                    for model, entry in event.model_usage.items()
                    if model != SYNTHETIC_API_ERROR_MODEL
                )
                if not primary_accounted:
                    return self._decision(
                        "PAUSED_POLICY",
                        "model_mismatch",
                        "primary_model_usage_missing",
                    )
            if event.subtype == "error_max_budget_usd":
                return self._decision("PAUSED_POLICY", "cost_limit", "usd_limit_reached")
            if event.subtype == "error_max_turns":
                return self._decision(
                    "PAUSED_POLICY", "turn_limit", "assistant_turn_limit_exceeded"
                )
            classification = _provider_classification(event)
            if classification is not None:
                return self._decision(classification[1], classification[0], classification[2])
            assert event.usage is not None  # exact result union validated at parse time
            cumulative_tokens = sum(
                event.usage.get(field, 0)
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )
            if cumulative_tokens >= self.max_tokens:
                return self._decision(
                    "PAUSED_POLICY", "token_limit", "token_limit_reached"
                )
            if (
                event.total_cost_usd is not None
                and event.total_cost_usd >= self.max_cost_usd
            ):
                return self._decision("PAUSED_POLICY", "cost_limit", "usd_limit_reached")
        if event.has_effect:
            self.has_effect = True
        return None

    def model_receipt(self) -> dict[str, Any]:
        verification = "unverified"
        if len(self._models_observed) == 1:
            if _FULL_MODEL_NAME.fullmatch(self.model_requested):
                if (
                    self.model_cli_accepted == self.model_requested
                    and self._models_observed[0] == self.model_requested
                ):
                    verification = "full_name_exact"
            elif self.model_cli_accepted is not None:
                verification = "alias_observed"
        return {
            "model_requested": self.model_requested,
            "model_cli_accepted": self.model_cli_accepted,
            "models_observed": list(self._models_observed),
            "verification_level": verification,
        }


def parse_event(payload: bytes | str) -> ClaudeEvent:
    """Parse one bounded Claude output JSONL event and fail closed on malformation."""

    data = _load_json_object(
        payload,
        maximum_bytes=MAX_EVENT_BYTES,
        label="Claude event",
    )
    event_type = data.get("type")
    event_type = _protocol_text(event_type, "Claude event type", 64)
    subtype = _optional_string(data, "subtype", maximum_bytes=128)
    session_id = _optional_string(data, "session_id")
    if "usage" in data and not (
        event_type == "result"
        or (
            event_type == "system"
            and subtype in {"task_notification", "task_progress"}
        )
    ):
        raise ClaudeProtocolError(
            "top-level usage is not valid for this Claude event"
        )
    if event_type == "assistant" and "parent_tool_use_id" not in data:
        raise ClaudeProtocolError("assistant.parent_tool_use_id is required")
    parent_tool_use_id = _optional_string(data, "parent_tool_use_id")
    model: str | None = None
    is_error: bool | None = None
    (
        assistant_message_id,
        has_effect,
        synthetic_api_error,
        provider_error,
    ) = _assistant_fields(data, event_type)

    policy_relevant = (
        event_type in {"assistant", "result", "rate_limit_event"}
        or (
            event_type == "system"
            and subtype
            in {
                "init",
                "api_retry",
                "model_fallback",
                "model_refusal_fallback",
            }
        )
    )
    if policy_relevant:
        _protocol_identifier(
            data.get("uuid"), "Claude event uuid", MAX_REQUEST_ID_BYTES
        )
        session_id = _required_session(data, f"{event_type}/{subtype or ''}")

    if event_type == "system" and subtype == "init":
        _validate_init_event(data)
        session_id = _required_session(data, "system/init")
        model = _protocol_model_identifier(data.get("model"), "system/init.model")
    elif event_type == "system" and subtype == "task_notification":
        _validate_task_notification(data)
        session_id = _required_session(data, "system/task_notification")
    elif event_type == "system" and subtype == "task_progress":
        _validate_task_progress(data)
        session_id = _required_session(data, "system/task_progress")
    elif event_type == "system" and subtype == "task_started":
        _validate_task_started(data)
        session_id = _required_session(data, "system/task_started")
    elif event_type == "system" and subtype == "task_updated":
        _validate_task_updated(data)
        session_id = _required_session(data, "system/task_updated")
    elif event_type == "system" and subtype == "background_tasks_changed":
        _validate_background_tasks_changed(data)
        session_id = _required_session(data, "system/background_tasks_changed")
    elif event_type == "system" and subtype == "thinking_tokens":
        _validate_thinking_tokens(data)
        session_id = _required_session(data, "system/thinking_tokens")
    elif event_type == "assistant":
        session_id = _required_session(data, "assistant")
        message = data.get("message")
        if not isinstance(message, dict):
            raise ClaudeProtocolError("assistant.message must be an object")
        parsed_model = _protocol_model_identifier(
            message.get("model"), "assistant.message.model"
        )
        model = None if synthetic_api_error else parsed_model
        if "usage" in message:
            _validate_usage(message["usage"], "assistant.message.usage")
    elif event_type == "result":
        session_id = _required_session(data, "result")
        error_value = data.get("is_error")
        if not isinstance(error_value, bool):
            raise ClaudeProtocolError("result.is_error must be a boolean")
        is_error = error_value

    _validate_result_union(data, event_type)
    errors = _result_errors(data, event_type)
    permission_denials = _permission_denials(data, event_type)
    (
        api_retry_attempt,
        api_retry_max_retries,
        api_retry_delay_ms,
        api_retry_error_status,
        api_retry_error,
    ) = _api_retry_fields(data, event_type, subtype)
    (
        fallback_kind,
        fallback_original_model,
        fallback_model,
    ) = _fallback_fields(data, event_type, subtype)
    rate_limit_info = _rate_limit_info(data, event_type)
    num_turns = _optional_num_turns(data)
    model_usage_value = data.get("modelUsage")
    if event_type == "result" and "modelUsage" not in data:
        raise ClaudeProtocolError("result.modelUsage is required")
    model_usage = _validate_model_usage(model_usage_value, allow_empty=True)
    if event_type == "result" and "usage" not in data:
        raise ClaudeProtocolError("result.usage is required")
    usage = (
        _validate_usage(data.get("usage"), "result.usage")
        if event_type == "result"
        else None
    )
    total_cost_usd = _optional_cost(data)
    if event_type == "result" and (
        num_turns is None
        or total_cost_usd is None
        or model_usage is None
        or usage is None
    ):
        raise ClaudeProtocolError(
            "result budget fields must use their non-null 2.1.248 types"
        )
    return ClaudeEvent(
        data=copy.deepcopy(data),
        type=event_type,
        subtype=subtype,
        session_id=session_id,
        model=model,
        parent_tool_use_id=parent_tool_use_id,
        is_error=is_error,
        model_usage=model_usage,
        num_turns=num_turns,
        total_cost_usd=total_cost_usd,
        usage=usage,
        is_api_retry=event_type == "system" and subtype == "api_retry",
        assistant_message_id=assistant_message_id,
        has_effect=has_effect,
        errors=errors,
        permission_denials=permission_denials,
        api_retry_attempt=api_retry_attempt,
        api_retry_max_retries=api_retry_max_retries,
        api_retry_delay_ms=api_retry_delay_ms,
        api_retry_error_status=api_retry_error_status,
        api_retry_error=api_retry_error,
        fallback_kind=fallback_kind,
        fallback_original_model=fallback_original_model,
        fallback_model=fallback_model,
        rate_limit_info=rate_limit_info,
        provider_error=provider_error,
    )


def iter_events(
    stream: BinaryIO | TextIO,
    *,
    expected_session_id: str | None = None,
) -> Iterator[ClaudeEvent]:
    """Yield validated events without ever reading an unbounded JSONL line."""

    expected = None
    if expected_session_id is not None:
        expected = _protocol_identifier(
            expected_session_id,
            "expected session id",
            MAX_SESSION_ID_BYTES,
        )
    while True:
        try:
            line = stream.readline(MAX_EVENT_BYTES + 1)
        except (OSError, ValueError, TypeError) as error:
            raise ClaudeProtocolError("could not read Claude event stream") from error
        if line in (b"", ""):
            return
        if not line.endswith(b"\n" if isinstance(line, bytes) else "\n"):
            raise ClaudeProtocolError("Claude event frame is not newline terminated")
        if len(line.encode("utf-8") if isinstance(line, str) else line) > MAX_EVENT_BYTES:
            raise ClaudeProtocolError("Claude event frame exceeds its byte limit")
        event = parse_event(line)
        if expected is not None and event.session_id is not None and event.session_id != expected:
            raise ClaudeProtocolError("Claude event session_id does not match the expected session")
        yield event


__all__ = [
    "AdapterError",
    "BUILD_TOOLS",
    "ClaudeEvent",
    "ClaudeProtocolError",
    "DEFAULT_PREFLIGHT_OUTPUT_LIMIT_BYTES",
    "DEFAULT_PREFLIGHT_TIMEOUT_SECONDS",
    "JobSpec",
    "MAX_EVENT_BYTES",
    "MAX_BUDGET_DIGITS",
    "MAX_BUDGET_FRACTIONAL_PLACES",
    "MAX_BUDGET_INPUT_BYTES",
    "MAX_BUDGET_OUTPUT_CHARS",
    "MAX_BUDGET_USD",
    "MAX_MODEL_ID_BYTES",
    "MAX_MODEL_USAGE_ENTRIES",
    "MAX_NUM_TURNS",
    "MAX_RECEIPT_COST_DIGITS",
    "MAX_RECEIPT_COST_FRACTIONAL_PLACES",
    "MAX_RECEIPT_COST_USD",
    "MAX_REQUEST_ID_BYTES",
    "MAX_SESSION_ID_BYTES",
    "MAX_USAGE_COUNT",
    "PreflightError",
    "PreflightResult",
    "ProviderPolicyDecision",
    "ProviderPolicyTracker",
    "REVIEW_TOOLS",
    "SYNTHETIC_API_ERROR_MODEL",
    "UnsupportedClaudeCodeVersion",
    "build_command",
    "build_user_message",
    "encode_stream_message",
    "encode_user_message",
    "get_auth_status",
    "get_version",
    "is_supported_claude_code_version",
    "iter_events",
    "normalize_budget_usd",
    "normalize_model_identifier",
    "parse_event",
    "preflight",
]
