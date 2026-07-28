"""自然语言意图解析（规则优先，不改榜）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from plugins.molmind_core.scientific.pipeline.runner import TOP_N_MAX, TOP_N_MIN


@dataclass(frozen=True)
class MentionRef:
    kind: str  # plugin | skill | tool
    id: str
    raw: str


@dataclass(frozen=True)
class AgentIntent:
    want_csv: bool
    want_pdf: bool
    top_n: int
    raw_text: str
    reason: str
    skill_ids: tuple[str, ...]
    #: False = 纯对话，不调用筛选 / 导出工具
    wants_tools: bool = True
    #: 输入中通过 / 或 @ 点选的插件 / 技能 / 工具
    mentions: tuple[MentionRef, ...] = ()
    #: introduce | invoke | ""
    mention_action: str = ""
    #: 用户原文里写的 Top N（可能超出规范上限）
    requested_top_n: int | None = None
    #: requested_top_n > top_n_max，需先反问再跑工具
    top_n_over_limit: bool = False
    top_n_max: int = TOP_N_MAX
    top_n_min: int = TOP_N_MIN
    #: 独立证据查询（R0，只读；不得触发提名/写榜）。
    query_evidence: bool = False
    evidence_molecule_id: str | None = None
    evidence_inchikey: str | None = None
    evidence_cas: str | None = None
    evidence_smiles: str | None = None
    evidence_providers: tuple[str, ...] = ()
    evidence_query_types: tuple[str, ...] = ()
    evidence_allow_live: bool = False
    evidence_force_refresh: bool = False
    evidence_total_timeout_sec: float = 45.0


_TOP_RE = re.compile(
    r"(?:top[\s\-_]*)(\d{1,3})\s*(?:个|名)?|"
    r"(?:提名|清单|候选|导出|生成).{0,12}?(\d{1,3})\s*(?:个|名)?|"
    r"(\d{1,3})\s*(?:个|名)?\s*(?:提名|清单|候选)",
    re.I,
)

# 例：@skill:masld_nominate  /tool:score_and_rank
_MENTION_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))[@/](plugin|skill|tool):([A-Za-z0-9][\w.\-]*)",
    re.I,
)

_INCHIKEY_RE = re.compile(r"\b([A-Z]{14}-[A-Z]{10}-[A-Z])\b", re.I)
_CAS_RE = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
_EVIDENCE_MENTION_IDS = frozenset({"query_evidence", "masld_explain"})
_PROVIDER_NAMES = (
    "chembl",
    "pubchem",
    "bindingdb",
    "opentargets",
    "epa",
    "toxcast",
    "dilirank",
)
_QUERY_TYPE_NAMES = (
    "lipid",
    "tox",
    "novelty",
    "pathway",
    "annotation",
    "query_audit",
)


def _first_group(patterns: tuple[str, ...], text: str, *, flags: int = 0) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            value = str(match.group(1) or "").strip().strip("\"'")
            if value:
                return value
    return None


def _csv_param(text: str, names: tuple[str, ...]) -> tuple[str, ...]:
    joined = "|".join(re.escape(name) for name in names)
    match = re.search(
        rf"(?:^|\s)(?:{joined})\s*[:=：]\s*([^\s;；]+)",
        text,
        re.I,
    )
    if not match:
        return ()
    values: list[str] = []
    for item in re.split(r"[,，|/]", match.group(1)):
        value = item.strip().lower()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _looks_like_evidence_query(text: str) -> bool:
    low = (text or "").lower()
    if not any(term in low for term in ("证据", "evidence")):
        return False
    return any(
        term in low
        for term in (
            "查询",
            "查找",
            "检索",
            "查看",
            "看看",
            "寻找",
            "补证",
            "证据卡",
            "证据详情",
            "有哪些证据",
            "有什么证据",
            "query evidence",
            "find evidence",
            "evidence for",
        )
    )


def _extract_evidence_args(text: str) -> dict[str, object]:
    raw = (text or "").strip()
    low = raw.lower()

    inchikey = _first_group(
        (
            r"(?:inchikey|inchi[_\s-]?key)\s*[:=：]\s*([A-Z]{14}-[A-Z]{10}-[A-Z])",
        ),
        raw,
        flags=re.I,
    )
    if not inchikey:
        match = _INCHIKEY_RE.search(raw)
        inchikey = match.group(1) if match else None
    if inchikey:
        inchikey = inchikey.upper()

    cas = _first_group(
        (r"(?:cas(?:[_\s-]?(?:rn|号))?)\s*[:=：]\s*(\d{2,7}-\d{2}-\d)",),
        raw,
        flags=re.I,
    )
    if not cas:
        match = _CAS_RE.search(raw)
        cas = match.group(1) if match else None

    smiles = _first_group(
        (
            r"(?:standardized[_\s-]?)?smiles\s*[:=：]\s*\"([^\"]+)\"",
            r"(?:standardized[_\s-]?)?smiles\s*[:=：]\s*'([^']+)'",
            r"(?:standardized[_\s-]?)?smiles\s*[:=：]\s*([^\s,，;；]+)",
        ),
        raw,
        flags=re.I,
    )

    molecule_id = _first_group(
        (
            r"(?:molecule[_\s-]?id|mol[_\s-]?id|分子\s*id|候选\s*id)\s*[:=：]\s*([A-Za-z0-9][\w.\-]*)",
            r"(?:候选|分子)\s*[:=：]?\s*([A-Za-z0-9][\w.\-]*)",
            (
                r"(?:[@/](?:tool:query_evidence|skill:masld_explain))\s+"
                r"(?:(?:试用|调用|查询|检索)\s+)?([A-Za-z0-9][\w.\-]*)"
            ),
        ),
        raw,
        flags=re.I,
    )
    if molecule_id and molecule_id.lower() in {
        "allow_live",
        "force_refresh",
        "providers",
        "query_types",
        "true",
        "false",
    }:
        molecule_id = None

    providers = list(_csv_param(raw, ("providers", "provider", "来源")))
    if not providers:
        for provider in _PROVIDER_NAMES:
            if re.search(rf"\b{re.escape(provider)}\b", low) and provider not in providers:
                providers.append(provider)

    query_types = list(
        _csv_param(raw, ("query_types", "query_type", "types", "type", "查询类型"))
    )
    if not query_types:
        for query_type in _QUERY_TYPE_NAMES:
            if re.search(rf"\b{re.escape(query_type)}\b", low):
                query_types.append(query_type)
        zh_types = (
            ("降脂", "lipid"),
            ("毒性", "tox"),
            ("新颖", "novelty"),
            ("通路", "pathway"),
            ("机制", "pathway"),
            ("注释", "annotation"),
            ("审计", "query_audit"),
        )
        for marker, query_type in zh_types:
            if marker in raw and query_type not in query_types:
                query_types.append(query_type)

    live_explicitly_disabled = bool(
        re.search(
            r"\ballow[_\s-]?live\s*[:=：]\s*(?:false|0|no|off)\b",
            low,
        )
        or re.search(
            r"(?:不要|别|禁止|不准|无需|不用|关闭)\s*(?:开启\s*)?"
            r"(?:联网|live|allow[_\s-]?live)",
            low,
        )
    )
    live_explicitly_enabled = bool(
        re.search(
            r"\ballow[_\s-]?live\s*[:=：]\s*(?:true|1|yes|on)\b",
            low,
        )
        or re.search(r"开启\s*(?:联网|live)(?:查询|检索|补证(?:据)?)", low)
    )
    # Conflicting or negated instructions fail closed to offline.
    allow_live = live_explicitly_enabled and not live_explicitly_disabled
    force_refresh = bool(
        re.search(
            r"\bforce[_\s-]?refresh\s*[:=：]\s*(?:true|1|yes|on)\b",
            low,
        )
        or any(marker in raw for marker in ("强制刷新", "忽略缓存重查", "跳过缓存重查"))
    )
    timeout_match = re.search(
        r"\b(?:total[_\s-]?timeout(?:[_\s-]?sec)?|deadline)\s*[:=：]\s*(\d+(?:\.\d+)?)",
        low,
    )
    total_timeout_sec = 45.0
    if timeout_match:
        total_timeout_sec = min(300.0, max(0.1, float(timeout_match.group(1))))

    return {
        "evidence_molecule_id": molecule_id,
        "evidence_inchikey": inchikey,
        "evidence_cas": cas,
        "evidence_smiles": smiles,
        "evidence_providers": tuple(providers),
        "evidence_query_types": tuple(query_types),
        "evidence_allow_live": allow_live,
        "evidence_force_refresh": force_refresh,
        "evidence_total_timeout_sec": total_timeout_sec,
    }


def _extract_top_n(
    text: str,
    default: int = 10,
    *,
    top_n_min: int = TOP_N_MIN,
    top_n_max: int = TOP_N_MAX,
) -> tuple[int, int | None, bool]:
    """Return (clamped_top_n, requested_or_None, over_limit)."""
    lo = int(top_n_min)
    hi = int(top_n_max)
    if lo > hi:
        lo, hi = TOP_N_MIN, TOP_N_MAX
    m = _TOP_RE.search(text or "")
    if not m:
        return max(lo, min(hi, default)), None, False
    for g in m.groups():
        if g:
            n = int(g)
            over = n > hi
            clamped = max(lo, min(hi, n))
            return clamped, n, over
    return max(lo, min(hi, default)), None, False


def extract_mentions(text: str) -> tuple[MentionRef, ...]:
    seen: set[tuple[str, str]] = set()
    out: list[MentionRef] = []
    for m in _MENTION_RE.finditer(text or ""):
        kind = m.group(1).lower()
        mid = m.group(2)
        key = (kind, mid)
        if key in seen:
            continue
        seen.add(key)
        out.append(MentionRef(kind=kind, id=mid, raw=m.group(0)))
    return tuple(out)


def _mention_action(_text: str, mentions: tuple[MentionRef, ...]) -> str:
    """Structural default only.

    introduce vs invoke is refined by the runtime LLM classifier — do not
    hardcode confirmation / intent verb tables here. Safe default: introduce
    (never auto-run tools from a bare mention).
    """
    if not mentions:
        return ""
    return "introduce"


def parse_intent(
    text: str,
    *,
    default_top_n: int = 10,
    top_n_min: int = TOP_N_MIN,
    top_n_max: int = TOP_N_MAX,
) -> AgentIntent:
    raw = (text or "").strip()
    low = raw.lower()
    top_n, requested_top_n, top_n_over_limit = _extract_top_n(
        raw,
        default=default_top_n,
        top_n_min=top_n_min,
        top_n_max=top_n_max,
    )
    mentions = extract_mentions(raw)
    mention_action = _mention_action(raw, mentions)
    evidence_args = _extract_evidence_args(raw)
    evidence_mention = any(m.id in _EVIDENCE_MENTION_IDS for m in mentions)
    wants_evidence_query = evidence_mention or _looks_like_evidence_query(raw)

    # 有点选 mention 时优先走 mention 分支（单独介绍 / 试用，不联动整条流水线）
    if mentions and mention_action:
        return AgentIntent(
            want_csv=False,
            want_pdf=False,
            top_n=top_n,
            raw_text=raw,
            reason=(
                f"点选 {', '.join(m.raw for m in mentions)}，"
                + ("试用调用" if mention_action == "invoke" else "介绍说明")
            ),
            skill_ids=(),
            wants_tools=False,
            mentions=mentions,
            mention_action=mention_action,
            requested_top_n=requested_top_n,
            top_n_over_limit=False,
            top_n_max=int(top_n_max),
            top_n_min=int(top_n_min),
            query_evidence=wants_evidence_query,
            **evidence_args,
        )

    # 证据查询是只读 R0 工具，必须先于「候选/清单」提名词面判断，
    # 避免「查询候选 T001 的证据」误触发 score_and_rank。
    if wants_evidence_query:
        return AgentIntent(
            want_csv=False,
            want_pdf=False,
            top_n=top_n,
            raw_text=raw,
            reason="查询候选分子的可审计证据（不改主榜）",
            skill_ids=("masld_explain",),
            wants_tools=True,
            mentions=(),
            mention_action="",
            requested_top_n=requested_top_n,
            top_n_over_limit=False,
            top_n_max=int(top_n_max),
            top_n_min=int(top_n_min),
            query_evidence=True,
            **evidence_args,
        )

    # Skill-surface tokens (deliverable / skill vocabulary), not dialog confirmations.
    # Limits & confirmations live in plugin/skill YAML + runtime LLM classifiers.
    mentions_csv = any(
        k in low for k in ("csv", "清单", "提名", "候选", "筛选", "top", "短名单")
    )
    mentions_pdf = any(k in low for k in ("pdf", "机制", "假说", "验证方案", "报告"))
    # Explicit deliverable markers → tools even without soft verbs like「帮我」
    strong_product = any(k in low for k in ("csv", "pdf", "top", "清单", "sdf"))
    soft_request = any(
        k in low
        for k in ("生成", "帮我", "导出", "跑", "开始", "做一份", "出一份", "给我", "来一份")
    )

    product = mentions_csv or mentions_pdf
    if not product or (not soft_request and not strong_product):
        return AgentIntent(
            want_csv=False,
            want_pdf=False,
            top_n=top_n,
            raw_text=raw,
            reason="一般对话，暂不调用筛选工具",
            skill_ids=(),
            wants_tools=False,
            mentions=mentions,
            mention_action="",
            requested_top_n=requested_top_n,
            top_n_over_limit=False,
            top_n_max=int(top_n_max),
            top_n_min=int(top_n_min),
        )

    want_csv = mentions_csv or (soft_request and not mentions_pdf) or (
        soft_request and mentions_pdf
    )
    only_pdf = mentions_pdf and not mentions_csv and not any(
        k in low for k in ("清单", "提名", "候选", "csv", "top")
    )
    if only_pdf:
        want_csv = False
        want_pdf = True
    else:
        want_pdf = mentions_pdf
        if soft_request and not mentions_csv and not mentions_pdf:
            want_csv = True

    skills: list[str] = []
    if want_csv:
        skills.append("masld_nominate")
    if want_pdf:
        skills.append("masld_mechanism")

    shown_n = requested_top_n if (requested_top_n and top_n_over_limit) else top_n
    parts = []
    if want_csv:
        parts.append(f"Top{shown_n} 候选 CSV")
    if want_pdf:
        parts.append("机制与验证方案 PDF")
    reason = "需要：" + " + ".join(parts)
    if top_n_over_limit and requested_top_n is not None:
        reason += f"（超出上限 {top_n_max}，需确认）"

    return AgentIntent(
        want_csv=want_csv,
        want_pdf=want_pdf,
        top_n=top_n,
        raw_text=raw,
        reason=reason,
        skill_ids=tuple(skills),
        wants_tools=True,
        mentions=mentions,
        mention_action="",
        requested_top_n=requested_top_n,
        top_n_over_limit=bool(top_n_over_limit),
        top_n_max=int(top_n_max),
        top_n_min=int(top_n_min),
    )
