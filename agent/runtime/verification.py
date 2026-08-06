"""Bind user-visible execution claims to durable runtime evidence."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class ClaimViolation:
    code: str
    message: str


_COMPLETION_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:完成|筛出|筛选|冻结|生成|导出)(?!的)|"
    r"(?:筛选|任务|计算)(?:已经)?完成",
    re.I,
)
_EVIDENCE_COMPLETION_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:完成|查到|找到|返回).{0,12}(?:证据查询|本地证据|证据)|"
    r"(?:证据查询)(?:已经)?(?:完成|返回)",
    re.I,
)
_RUNNING_CLAIM_RE = re.compile(r"(?:正在|已经启动|已启动).{0,12}(?:筛选|运行|计算|生成)", re.I)
_ARTIFACT_CLAIM_RE = re.compile(r"(?:已|已经)(?:导出|生成).{0,12}(?:CSV|PDF|附件|文件)", re.I)
_LOCAL_CAPABILITY_DENIAL_RE = re.compile(
    r"(?:当前|这个|本).{0,24}(?:对话)?(?:环境|系统).{0,48}"
    r"(?:不具备|不具有|没有|无法).{0,48}(?:筛选|排序|计算能力)",
    re.I,
)
# A TopN value is ambiguous on its own: it can name a ranked molecule
# ("T19959 是 Top1"), a future request ("上传后生成 Top10"), or the actual
# size of a completed frozen shortlist.  Only the latter is an execution claim
# that can be compared with the stored frozen result.
_TOP_COMPLETION_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:完成|生成|导出|筛选|筛出|冻结)(?:了)?[^。！？\n]{0,24}?Top\s*(\d{1,3})|"
    r"(?:筛选|任务|计算)(?:已经)?完成[^。！？\n]{0,24}?Top\s*(\d{1,3})|"
    r"(?:本(?:轮|次)|当前)(?:实际)?(?:已经|已)?冻结(?:了)?[^。！？\n]{0,24}?Top\s*(\d{1,3})",
    re.I,
)


def _has_running_step(session: Any) -> bool:
    active = getattr(session, "active_plan", None)
    if not isinstance(active, dict):
        return False
    return any(
        isinstance(step, dict) and step.get("status") == "running"
        for step in active.get("steps") or []
    )


def _frozen_count(session: Any) -> int:
    result = getattr(session, "last_result", None)
    return len(getattr(result, "top_molecules", None) or []) if result is not None else 0


def _has_successful_tool_memory(session: Any, tool_id: str) -> bool:
    memory = [
        item
        for item in (getattr(session, "working_memory", None) or [])
        if isinstance(item, dict)
    ]
    latest_turn_id = str(memory[-1].get("turn_id") or "") if memory else ""
    for iteration in reversed(memory):
        if not isinstance(iteration, dict):
            continue
        if latest_turn_id and str(iteration.get("turn_id") or "") != latest_turn_id:
            break
        for call in iteration.get("tool_calls") or []:
            if (
                isinstance(call, dict)
                and call.get("tool") == tool_id
                and call.get("status") == "succeeded"
            ):
                return True
    return False


def _has_any_successful_tool_memory(session: Any) -> bool:
    memory = [
        item
        for item in (getattr(session, "working_memory", None) or [])
        if isinstance(item, dict)
    ]
    latest_turn_id = str(memory[-1].get("turn_id") or "") if memory else ""
    for iteration in reversed(memory):
        if latest_turn_id and str(iteration.get("turn_id") or "") != latest_turn_id:
            break
        if any(
            isinstance(call, dict) and call.get("status") == "succeeded"
            for call in iteration.get("tool_calls") or []
        ):
            return True
    return False


def verify_assistant_claims(session: Any, text: str) -> list[ClaimViolation]:
    """Return unsupported user-visible execution claims.

    Reasoning and recommendations remain unrestricted. Only statements that
    assert a real execution state or artifact are checked.
    """
    body = str(text or "")
    violations: list[ClaimViolation] = []
    frozen_count = _frozen_count(session)
    artifacts = getattr(session, "artifacts", None) or {}

    if getattr(session, "sdf_bytes", None) and _LOCAL_CAPABILITY_DENIAL_RE.search(body):
        violations.append(
            ClaimViolation(
                "local_screening_capability_denial",
                "会话已绑定 SDF，但回复否认本地 score_and_rank 筛选能力",
            )
        )
    evidence_completion_supported = (
        bool(_EVIDENCE_COMPLETION_CLAIM_RE.search(body))
        and _has_successful_tool_memory(session, "query_evidence")
    )
    if (
        _COMPLETION_CLAIM_RE.search(body)
        and frozen_count <= 0
        and not artifacts
        and not evidence_completion_supported
        and not _has_any_successful_tool_memory(session)
    ):
        violations.append(
            ClaimViolation("completion_without_evidence", "没有成功工具结果或冻结结果支持完成声明")
        )
    if _RUNNING_CLAIM_RE.search(body) and not _has_running_step(session):
        violations.append(
            ClaimViolation("running_without_evidence", "没有处于 running 状态的计划步骤")
        )
    if _ARTIFACT_CLAIM_RE.search(body) and not artifacts:
        violations.append(
            ClaimViolation("artifact_without_evidence", "会话中没有对应 artifact")
        )
    for raw_match in _TOP_COMPLETION_CLAIM_RE.findall(body):
        raw_n = next((value for value in raw_match if value), "")
        claimed = int(raw_n)
        if frozen_count != claimed:
            violations.append(
                ClaimViolation(
                    "topn_mismatch",
                    f"声明 Top{claimed}，实际冻结数量为 {frozen_count}",
                )
            )
            break
    return violations


def evidence_correction(session: Any, violations: list[ClaimViolation]) -> str:
    if any(v.code == "local_screening_capability_denial" for v in violations):
        filename = str(getattr(session, "sdf_filename", "") or "已绑定的 SDF 附件")
        return (
            f"当前会话已绑定「{filename}」，本地 score_and_rank 工具可以执行实际筛选。"
            "刚才的回复错误否认了该能力，已被系统拦截；当前尚无完成的冻结结果。"
            "你可以直接请求生成候选 CSV，系统会按会话默认 TopN 或你指定的 TopN 执行。"
        )
    frozen_count = _frozen_count(session)
    run_id = str(getattr(session, "last_run_id", "") or "").strip()
    if frozen_count:
        state = f"当前可核验的冻结结果为 Top{frozen_count}"
        if run_id:
            state += f"（run_id: {run_id}）"
        state += "。"
    else:
        state = "当前会话没有可核验的冻结筛选结果。"
    details = "；".join(v.message for v in violations)
    return (
        f"{state}刚才拟生成的回复包含缺少工具证据的执行声明，已被系统拦截"
        f"（{details}）。我可以继续澄清需求，或在条件满足后实际调用工具。"
    )
