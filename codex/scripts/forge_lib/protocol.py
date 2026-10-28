"""MCP JSON-RPC 2.0 stdio server for Forge."""

from __future__ import annotations

import json
import math
import sys
from typing import Any, BinaryIO

from .contracts import ContractError, MAX_LINE_BYTES, TOOL_SCHEMAS, validate_tool_arguments

SERVER_NAME = "forge"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2024-11-05")


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id: Any, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _tool_descriptions() -> list[dict[str, Any]]:
    descriptions = {
        "forge_preflight": "Validate Forge runtime admission prerequisites.",
        "forge_start": "Start a bounded Forge job.",
        "forge_status": "Read the status of a Forge job.",
        "forge_wait": "Wait for a Forge job state transition.",
        "forge_result": "Read a Forge job result.",
        "forge_resume": "Resume a Forge job at an expected revision.",
        "forge_steer": "Steer a Forge job at an expected revision.",
        "forge_cancel": "Cancel a Forge job at an expected revision.",
        "forge_list": "List Forge jobs.",
    }
    return [{"name": name, "description": descriptions[name], "inputSchema": schema} for name, schema in TOOL_SCHEMAS.items()]


def _is_valid_id(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value)
    return False


def _is_notification_shape(request: Any) -> bool:
    """Identify a notification before validating its params."""
    return (
        isinstance(request, dict)
        and request.get("jsonrpc") == "2.0"
        and isinstance(request.get("method"), str)
        and "id" not in request
    )


def _validate_request(request: Any) -> tuple[Any, bool]:
    if not isinstance(request, dict):
        raise ValueError("Request must be an object")
    request_id = request.get("id")
    if "id" in request and not _is_valid_id(request_id):
        raise ValueError("Request id must be a string, number, or null")
    if request.get("jsonrpc") != "2.0":
        raise ValueError("jsonrpc must be '2.0'")
    if not isinstance(request.get("method"), str):
        raise ValueError("method must be a string")
    if "params" in request and not isinstance(request["params"], (dict, list)):
        raise TypeError("params must be an object or array")
    return request_id, "id" not in request


def _read_line(stream: BinaryIO) -> bytes | None:
    """Read one line while bounding memory and drain a rejected line."""
    first = stream.readline(MAX_LINE_BYTES + 1)
    if not first:
        return None
    if len(first) <= MAX_LINE_BYTES and first.endswith(b"\n"):
        return first[:-1].rstrip(b"\r")
    if len(first) <= MAX_LINE_BYTES and not first.endswith(b"\n"):
        tail = stream.readline(8192)
        if tail == b"":
            return first
        if tail == b"\n":
            return first.rstrip(b"\r")
        first = tail
    while first and not first.endswith(b"\n"):
        first = stream.readline(8192)
    return b"__FORGE_OVERSIZE_LINE__"


def _dispatch(request: dict[str, Any], request_id: Any, notification: bool = False) -> dict[str, Any] | None:
    method = request["method"]
    params = request.get("params", {})
    if method == "notifications/initialized":
        return None if notification else _error(request_id, -32601, "Method not found")
    if method == "initialize":
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params")
        requested = params.get("protocolVersion", SUPPORTED_PROTOCOLS[0])
        protocol_version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _result(request_id, {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params")
        return _result(request_id, {})
    if method == "tools/list":
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params")
        return _result(request_id, {"tools": _tool_descriptions()})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params")
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_SCHEMAS:
            return _error(request_id, -32602, "Invalid params", "Unknown tool")
        arguments = params.get("arguments", {})
        try:
            validate_tool_arguments(name, arguments)
        except ContractError as exc:
            return _error(request_id, -32602, "Invalid params", str(exc))
        try:
            from .service import get_request_service

            service, sanitized_arguments = get_request_service(
                arguments,
                tool_name=name,
            )
            execution = service.execute(name, sanitized_arguments)
        except Exception:  # fail closed without exposing environment or state details
            body = {
                "status": "error",
                "tool": name,
                "message": "Forge service initialization failed.",
            }
            message = body["message"]
            is_error = True
        else:
            body = execution.body
            message = execution.message
            is_error = execution.is_error
        return _result(request_id, {
            "content": [{"type": "text", "text": message}],
            "structuredContent": body,
            "isError": is_error,
        })
    return _error(request_id, -32601, "Method not found")


def serve_stdio(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    """Serve newline-delimited JSON-RPC requests until clean EOF."""
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    for raw_line in iter(lambda: _read_line(input_stream), None):
        request_id: Any = None
        notification = False
        try:
            if raw_line == b"__FORGE_OVERSIZE_LINE__":
                response = _error(None, -32600, "Invalid Request: line exceeds maximum size")
            else:
                try:
                    request = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response = _error(None, -32700, "Parse error")
                else:
                    # A missing id identifies a notification before validating
                    # method/params, so malformed notifications stay silent.
                    notification = _is_notification_shape(request)
                    try:
                        request_id, notification = _validate_request(request)
                    except TypeError:
                        response = _error(request.get("id") if isinstance(request, dict) else None, -32602, "Invalid params")
                    except ValueError:
                        candidate_id = request.get("id") if isinstance(request, dict) else None
                        response = _error(candidate_id if _is_valid_id(candidate_id) else None, -32600, "Invalid Request")
                    else:
                        response = _dispatch(request, request_id, notification)
        except Exception as exc:  # pragma: no cover - safety net for a long-lived bridge
            print(f"forge bridge internal error: {exc}", file=sys.stderr)
            response = _error(request_id, -32603, "Internal error")
        if response is not None and not notification:
            output_stream.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
            output_stream.flush()
