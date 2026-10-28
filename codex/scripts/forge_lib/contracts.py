"""Strict, stdlib-only contracts for the Forge MCP surface."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_LINE_BYTES = 1_048_576
MAX_PROMPT_BYTES = 524_288
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROLES = ("review", "build", "recovery")
RECOVERY_DISABLED_MESSAGE = (
    "Automated Forge recovery is disabled in this build; existing work and state "
    "were left unchanged. Use a manual handoff."
)
WORKSPACE_CONTEXT_FIELD = "_forge_workspace"
MAX_COST_USD = Decimal("1000000")
MAX_COST_USD_FRACTIONAL_PLACES = 6
_DECIMAL_PATTERN = r"^[+]?(?:[0-9]+(?:\.[0-9]{0,6})?|\.[0-9]{1,6})$"
_DECIMAL_LITERAL = re.compile(_DECIMAL_PATTERN)


class ContractError(ValueError):
    """Raised when an RPC argument does not satisfy a Forge contract."""


def _string_schema(max_length: int = 256) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _object_schema(properties: dict[str, Any], required: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _array_schema(items: dict[str, Any], *, maximum: int = 256) -> dict[str, Any]:
    return {"type": "array", "items": items, "maxItems": maximum}


_BUDGET_SCHEMA = _object_schema(
    {
        "max_seconds": {"type": "integer", "minimum": 1, "maximum": 86_400},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
        "max_turns": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        "max_cost_usd": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": _DECIMAL_PATTERN,
        },
    },
    required=("max_seconds", "max_tokens", "max_turns", "max_cost_usd"),
)

_HASH_SCHEMA = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
    "pattern": r"^[0-9a-f]{64}$",
}
_PINNED_ARTIFACT_SCHEMA = _object_schema(
    {"path": _string_schema(4096), "sha256": _HASH_SCHEMA},
    required=("path", "sha256"),
)
_REVIEW_CONTROLS_SCHEMA = _object_schema(
    {
        "plan": _PINNED_ARTIFACT_SCHEMA,
        "review_log": _PINNED_ARTIFACT_SCHEMA,
        "domain_docs": _array_schema(_PINNED_ARTIFACT_SCHEMA, maximum=64),
    },
    required=("plan", "review_log"),
)
_DIRTY_FILE_SCHEMA = _object_schema(
    {"path": _string_schema(4096), "sha256": _HASH_SCHEMA},
    required=("path", "sha256"),
)
_REFREEZE_SCHEMA = _object_schema(
    {
        "baseline_head": _string_schema(64),
        "dirty_files": _array_schema(_DIRTY_FILE_SCHEMA, maximum=256),
        "review_controls": _REVIEW_CONTROLS_SCHEMA,
    },
    required=("baseline_head",),
)
_EXPANSION_SCHEMA = _object_schema(
    {
        "lease_id": _string_schema(256),
        "lease_revision": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2_147_483_647,
        },
        "prior_files": _array_schema(_string_schema(4096), maximum=256),
        "files": _array_schema(_string_schema(4096), maximum=256),
    },
    required=("lease_id", "lease_revision", "prior_files", "files"),
)
_CONTROLS_SCHEMA = _object_schema(
    {
        "plan": _PINNED_ARTIFACT_SCHEMA,
        "review_log": _PINNED_ARTIFACT_SCHEMA,
        "execution_contract": _PINNED_ARTIFACT_SCHEMA,
        "project_overlay": _PINNED_ARTIFACT_SCHEMA,
        "engine_contract": _PINNED_ARTIFACT_SCHEMA,
        "routing_policy": _PINNED_ARTIFACT_SCHEMA,
        "liveness_fixture": _PINNED_ARTIFACT_SCHEMA,
        "watchdog_fixture": _PINNED_ARTIFACT_SCHEMA,
        "watchdog_tests_fixture": _PINNED_ARTIFACT_SCHEMA,
        "freeze_manifest": _PINNED_ARTIFACT_SCHEMA,
    },
    required=("plan", "review_log", "execution_contract", "project_overlay"),
)
_ORACLE_SCHEMA = _object_schema(
    {
        "exact": {"type": "boolean"},
        "capability_receipt": _PINNED_ARTIFACT_SCHEMA,
    },
    required=("exact",),
)
_PREDECESSOR_REVIEW_SCHEMA = _object_schema(
    {
        "artifact": _PINNED_ARTIFACT_SCHEMA,
        "reviewer_family": _string_schema(128),
        "thread": _string_schema(256),
        "verdict": {"type": "string", "enum": ["APPROVED", "REVISE"]},
        "receipt_id": _string_schema(256),
    },
    required=("artifact", "reviewer_family", "thread", "verdict", "receipt_id"),
)
_PREDECESSOR_SCHEMA = _object_schema(
    {
        "unit_id": _string_schema(256),
        "builder_family": _string_schema(128),
        "report": _PINNED_ARTIFACT_SCHEMA,
        "report_receipt_id": _string_schema(256),
        "consumption_receipt": _PINNED_ARTIFACT_SCHEMA,
        "review": _PREDECESSOR_REVIEW_SCHEMA,
    },
    required=(
        "unit_id",
        "builder_family",
        "report",
        "report_receipt_id",
        "consumption_receipt",
    ),
)
_ADMISSION_SCHEMA = _object_schema(
    {
        "weight": {"type": "string", "enum": ["LIGHT", "HEAVY"]},
        "proof_scope": {"type": "string", "enum": ["targeted", "broad"]},
        "execution_profile": {"type": "string", "enum": ["sprint", "assured"]},
        "proof_argv": _array_schema(_string_schema(4096), maximum=128),
        "files": _array_schema(_string_schema(4096), maximum=256),
        "base_sha": _string_schema(64),
        "verdict": {"type": "string", "enum": ["APPROVED", "REVISE"]},
        "backlog_ids": _array_schema(_string_schema(256), maximum=128),
        "controls": _CONTROLS_SCHEMA,
        "review_controls": _REVIEW_CONTROLS_SCHEMA,
        "oracle": _ORACLE_SCHEMA,
        "predecessor": _PREDECESSOR_SCHEMA,
        "preserved_paths": _array_schema(_string_schema(4096), maximum=256),
    },
    required=("weight", "proof_scope", "execution_profile", "proof_argv", "files"),
)

_JOB_PROPERTIES = {
    "job_id": {"type": "string", "minLength": 1, "maxLength": 256},
    "revision": {"type": "integer", "minimum": 0, "maximum": 2_147_483_647},
}

_WORKSPACE_CONTEXT_SCHEMA = _object_schema(
    {
        "session_id": _string_schema(256),
        "token": {
            "type": "string",
            "minLength": 43,
            "maxLength": 43,
            "pattern": r"^[A-Za-z0-9_-]{43}$",
        },
    },
    required=("session_id", "token"),
)

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "forge_preflight": _object_schema({"cwd": _string_schema(4096)}),
    "forge_start": _object_schema(
        {
            "prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_BYTES},
            "cwd": _string_schema(4096),
            "role": {"type": "string", "enum": list(ROLES)},
            "model": _string_schema(256),
            "effort": {"type": "string", "enum": list(EFFORTS)},
            "idempotency_key": _string_schema(256),
            "budget": _BUDGET_SCHEMA,
            "admission": _ADMISSION_SCHEMA,
        },
        required=("prompt", "admission"),
    ),
    "forge_status": _object_schema(_JOB_PROPERTIES, required=("job_id",)),
    "forge_wait": _object_schema(
        {
            **_JOB_PROPERTIES,
            "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 86_400_000},
        },
        required=("job_id",),
    ),
    "forge_result": _object_schema(_JOB_PROPERTIES, required=("job_id",)),
    "forge_resume": _object_schema(
        {
            **_JOB_PROPERTIES,
            "model_override": _string_schema(256),
            "prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_BYTES},
            "idempotency_key": _string_schema(256),
            "refreeze": _REFREEZE_SCHEMA,
        },
        required=("job_id", "revision"),
    ),
    "forge_steer": _object_schema(
        {
            **_JOB_PROPERTIES,
            "prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_BYTES},
            "admission_expansion": _EXPANSION_SCHEMA,
        },
        required=("job_id", "revision", "prompt"),
    ),
    "forge_cancel": _object_schema(_JOB_PROPERTIES, required=("job_id", "revision")),
    "forge_list": _object_schema(
        {
            "status": {"type": "string", "enum": ["queued", "running", "completed", "failed", "cancelled"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    ),
}

# Codex supplies this reserved capability through the plugin's trusted
# PreToolUse hook. It stays optional in the published schemas so the model can
# form an ordinary tool call before the hook rewrites its input; the installed
# bridge requires and validates it before touching durable state.
for _tool_schema in TOOL_SCHEMAS.values():
    _tool_schema["properties"][WORKSPACE_CONTEXT_FIELD] = _WORKSPACE_CONTEXT_SCHEMA


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ContractError(f"{path} must be an object")
        properties = schema.get("properties", {})
        if not schema.get("additionalProperties", True):
            unknown = set(value) - set(properties)
            if unknown:
                raise ContractError(f"{path} has unknown field: {sorted(unknown)[0]}")
        missing = [name for name in schema.get("required", ()) if name not in value]
        if missing:
            raise ContractError(f"{path} is missing required field: {missing[0]}")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise ContractError(f"{path} must be an array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ContractError(f"{path} has too many items")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
        return
    if expected == "boolean":
        if not isinstance(value, bool):
            raise ContractError(f"{path} must be a boolean")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise ContractError(f"{path} must be a string")
        if "\x00" in value:
            raise ContractError(f"{path} contains a NUL byte")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContractError(f"{path} is not valid UTF-8") from error
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ContractError(f"{path} is too short")
        if "maxLength" in schema and len(encoded) > schema["maxLength"]:
            raise ContractError(f"{path} is too long")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ContractError(f"{path} has invalid notation")
        if path.endswith(".budget.max_cost_usd"):
            try:
                number = Decimal(value)
            except InvalidOperation as error:
                raise ContractError(f"{path} is not a decimal") from error
            if not number.is_finite() or number <= 0:
                raise ContractError(f"{path} is below minimum")
            if number > MAX_COST_USD:
                raise ContractError(f"{path} is above maximum")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ContractError(f"{path} must be an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError(f"{path} is above maximum")
    else:
        raise ContractError(f"unsupported contract type at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path} has an invalid value")


def validate_object(value: Any, schema: dict[str, Any], path: str = "object") -> dict[str, Any]:
    """Validate a strict object schema and return the original object."""
    _validate(value, schema, path)
    return value


def validate_tool_arguments(name: str, arguments: Any) -> dict[str, Any]:
    """Validate and return a tool argument object, rejecting unknown fields."""
    if name not in TOOL_SCHEMAS:
        raise ContractError(f"unknown tool: {name}")
    return validate_object(arguments, TOOL_SCHEMAS[name], "arguments")
