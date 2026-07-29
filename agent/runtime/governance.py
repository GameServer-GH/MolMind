"""Uniform ToolSpec enforcement for every governed runtime tool call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from agent.runtime.planning import session_capabilities
from agent.runtime.scheduler import RunController, ScheduledCall, canonical_args_hash


@dataclass(frozen=True)
class GovernanceDecision:
    allowed: bool
    code: str
    message: str
    args_hash: str
    call: ScheduledCall | None = None
    approval_scope: str = ""


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_json_schema(
    value: Any,
    schema: dict[str, Any] | None,
    *,
    path: str = "$",
) -> list[str]:
    """Validate the small JSON-Schema subset used by MolMind ToolSpec files."""
    rule = dict(schema or {})
    if not rule:
        return []
    errors: list[str] = []
    expected = rule.get("type")
    expected_types = [expected] if isinstance(expected, str) else list(expected or [])
    if expected_types and not any(_json_type_matches(value, item) for item in expected_types):
        return [f"{path}: expected {'|'.join(expected_types)}"]

    if "enum" in rule and value not in rule.get("enum", []):
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if rule.get("minimum") is not None and value < rule["minimum"]:
            errors.append(f"{path}: below minimum {rule['minimum']}")
        if rule.get("maximum") is not None and value > rule["maximum"]:
            errors.append(f"{path}: above maximum {rule['maximum']}")
    if isinstance(value, str):
        if rule.get("minLength") is not None and len(value) < int(rule["minLength"]):
            errors.append(f"{path}: shorter than minLength")
        if rule.get("maxLength") is not None and len(value) > int(rule["maxLength"]):
            errors.append(f"{path}: longer than maxLength")
    if isinstance(value, list):
        item_schema = rule.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    validate_json_schema(item, item_schema, path=f"{path}[{index}]")
                )
    if isinstance(value, dict):
        required = [str(item) for item in rule.get("required") or []]
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name}: required")
        properties = rule.get("properties") or {}
        if isinstance(properties, dict):
            for name, item in value.items():
                child_schema = properties.get(name)
                if isinstance(child_schema, dict):
                    errors.extend(
                        validate_json_schema(item, child_schema, path=f"{path}.{name}")
                    )
                elif rule.get("additionalProperties") is False:
                    errors.append(f"{path}.{name}: additional property not allowed")
    return errors


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def approval_scope(tool: Any, args: dict[str, Any], policy: dict[str, Any]) -> str:
    hitl = {str(value) for value in policy.get("hitl_required") or []}
    if bool(getattr(tool, "confirmation_required", False)) or tool.tool_id in hitl:
        return tool.tool_id
    if bool(args.get("allow_live")) and "allow_live" in hitl:
        return "allow_live"
    return ""


def grant_approval(
    session: Any,
    *,
    tool_id: str,
    args: dict[str, Any],
    scope: str,
    ttl_sec: int = 600,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    record = {
        "approval_id": uuid.uuid4().hex,
        "session_id": str(session.session_id),
        "tool_id": str(tool_id),
        "scope": str(scope or tool_id),
        "args_hash": canonical_args_hash(args),
        "decision": "approved",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(seconds=max(30, min(int(ttl_sec), 3600)))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "used_at": None,
    }
    grants = list(getattr(session, "approval_grants", None) or [])
    grants.append(record)
    session.approval_grants = grants[-24:]
    return record


def _consume_matching_approval(
    session: Any,
    *,
    tool_id: str,
    args_hash: str,
    scope: str,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    for record in reversed(list(getattr(session, "approval_grants", None) or [])):
        if not isinstance(record, dict):
            continue
        if record.get("decision") != "approved" or record.get("used_at"):
            continue
        if str(record.get("tool_id") or "") != tool_id:
            continue
        if str(record.get("scope") or "") != scope:
            continue
        if str(record.get("args_hash") or "") != args_hash:
            continue
        expires_at = _parse_time(record.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        record["used_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return record
    return None


class ToolGovernance:
    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def authorize(
        self,
        *,
        session: Any,
        tool_id: str,
        args: dict[str, Any],
        controller: RunController,
        task_id: str = "",
        confirmed_scopes: set[str] | None = None,
    ) -> GovernanceDecision:
        normalized_args = dict(args or {})
        args_hash = canonical_args_hash(normalized_args)
        tool = self.registry.tools.get(tool_id)
        if tool is None:
            return GovernanceDecision(
                False,
                "unknown_tool",
                f"工具未注册：{tool_id}",
                args_hash,
            )
        plugin = self.registry.plugins.get(tool.plugin_id)
        available = bool(plugin and plugin.builtin) or tool.plugin_id in set(
            getattr(session, "installed_catalog", None) or []
        )
        if not available:
            return GovernanceDecision(
                False,
                "plugin_not_enabled",
                f"工具所属插件未启用：{tool.plugin_id}",
                args_hash,
            )
        profile = self.registry.get_profile(session.profile_id)
        schema_errors = validate_json_schema(normalized_args, tool.input_schema)
        if schema_errors:
            return GovernanceDecision(
                False,
                "invalid_args",
                "; ".join(schema_errors[:8]),
                args_hash,
            )
        missing = [
            requirement
            for requirement in tool.requires
            if requirement not in session_capabilities(session)
        ]
        if missing:
            return GovernanceDecision(
                False,
                "missing_precondition",
                f"缺少前置条件：{','.join(missing)}",
                args_hash,
            )
        if "top_n" in normalized_args:
            try:
                top_n = int(normalized_args["top_n"])
            except (TypeError, ValueError):
                return GovernanceDecision(
                    False,
                    "invalid_args",
                    "top_n 必须是整数",
                    args_hash,
                )
            minimum = tool.limits.get("top_n_min")
            maximum = tool.limits.get("top_n_max")
            if minimum is not None and top_n < int(minimum):
                return GovernanceDecision(
                    False,
                    "limit_violation",
                    f"top_n 不能小于 {minimum}",
                    args_hash,
                )
            if maximum is not None and top_n > int(maximum):
                return GovernanceDecision(
                    False,
                    "limit_violation",
                    f"top_n 不能大于 {maximum}",
                    args_hash,
                )

        required_scope = approval_scope(tool, normalized_args, profile.policy)
        allowed, reason = controller.can_start(
            tool_id=tool_id,
            args_hash=args_hash,
            allow_retry=bool(tool.idempotent),
        )
        post_deadline_finalizer = (
            not allowed
            and reason == "max_wall_time_exceeded"
            and tool_id == "export_nomination"
            and getattr(session, "last_result", None) is not None
            and bool(tool.idempotent)
            and not bool(tool.writes_selection)
        )
        if not allowed:
            if post_deadline_finalizer:
                call = controller.start_post_deadline_finalizer(
                    tool_id=tool_id,
                    args_hash=args_hash,
                    task_id=task_id,
                    timeout_sec=tool.timeout_sec,
                    writes_selection=tool.writes_selection,
                )
                return GovernanceDecision(
                    True,
                    "post_deadline_finalizer",
                    "筛选已完成；允许进行一次本地 CSV 收尾导出。",
                    args_hash,
                    call=call,
                    approval_scope=required_scope,
                )
            return GovernanceDecision(
                False,
                reason,
                f"运行预算已停止本次调用：{reason}",
                args_hash,
                approval_scope=required_scope,
            )
        if required_scope:
            explicit = required_scope in set(confirmed_scopes or set())
            # Explicit per-turn consent is valid for allow_live only. R2/write
            # operations always require a separately stored exact approval.
            if not (required_scope == "allow_live" and explicit):
                approval = _consume_matching_approval(
                    session,
                    tool_id=tool_id,
                    args_hash=args_hash,
                    scope=required_scope,
                )
                if approval is None:
                    return GovernanceDecision(
                        False,
                        "approval_required",
                        (
                            f"工具 {tool_id} 需要针对当前参数的确认"
                            if required_scope == tool_id
                            else "显式联网需要本轮确认"
                        ),
                        args_hash,
                        approval_scope=required_scope,
                    )

        call = controller.start_call(
            tool_id=tool_id,
            args_hash=args_hash,
            task_id=task_id,
            timeout_sec=tool.timeout_sec,
            writes_selection=tool.writes_selection,
            allow_retry=bool(tool.idempotent),
        )
        return GovernanceDecision(
            True,
            "allowed",
            "工具调用已通过治理检查",
            args_hash,
            call=call,
            approval_scope=required_scope,
        )
