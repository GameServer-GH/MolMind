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
    #: 导出同一冻结运行中的 reserve CSV。
    want_reserve: bool = False
    #: 生成包含主榜、候补、manifest 与 trace 的竞赛交卷包。
    want_bundle: bool = False
    #: False = 纯对话，不调用筛选 / 导出工具
    wants_tools: bool = True
    #: 输入中通过 / 或 @ 点选的插件 / 技能 / 工具
    mentions: tuple[MentionRef, ...] = ()
    #: introduce | invoke | ""
    mention_action: str = ""
    #: 同一输入中除点选指令外仍需回答的普通对话子任务。
    #: 点选项不能再把这部分文本短路掉。
    companion_text: str = ""
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
    #: 追问上一轮候选为何处于某个排名；只解释，不重跑或导出。
    explain_ranking: bool = False
    ranking_molecule_id: str | None = None
    #: 用户明确点名的多个冻结排名，例如“解释 Top4 和 Top5”。
    #: 空值表示未点名或只点名一个排名，沿用原有单分子/TopN 概览语义。
    ranking_positions: tuple[int, ...] = ()
    #: “排名 Top5 的分子”中的 Top5 指向单个排名对象，不是 Top5 概览。
    ranking_position_subject: bool = False
    #: Structured dialog act: user is requesting Core screening/export execution
    #: (as opposed to chat, explain, or supplementary SCP evidence).
    execution_requested: bool = False
    #: Drop any matching frozen result and run a fresh score_and_rank.
    #: Set by the loop execution-gate classifier (or structural rescreen cues),
    #: not by domain keyword walls in the router.
    force_rescreen: bool = False


_TOP_RE = re.compile(
    r"(?:top[\s\-_]*)(\d{1,3})\s*(?:个|名)?|"
    r"(?:提名|清单|候选|导出|生成).{0,12}?(\d{1,3})\s*(?:个|名)?|"
    r"(\d{1,3})\s*(?:个|名)?\s*(?:提名|清单|候选)",
    re.I,
)

_RANKING_CONTEXT_RE = re.compile(
    r"top[\s\-_]*\d+|排名|排第|第\s*\d+\s*(?:名|位)?|前\s*\d+\s*名?|第一名|榜首|首位|入选",
    re.I,
)
_RANK_POSITION_RE = re.compile(
    r"(?:(?<![A-Za-z])top[\s\-_]*|第\s*)(\d{1,3})(?:\s*(?:名|位))?",
    re.I,
)
_FRONT_RANK_SPAN_RE = re.compile(r"前\s*(\d{1,3})\s*名?", re.I)
_RANKING_COMPARE_RE = re.compile(
    r"哪个|哪一个|更适合|更推荐|优先推进|怎么选|选哪个",
    re.I,
)
_RANK_POSITION_SUBJECT_RE = re.compile(
    r"(?:"
    r"(?:排名\s*)?(?:(?<![A-Za-z])top[\s\-_]*|第\s*)\d{1,3}"
    r"(?:\s*(?:名|位))?\s*(?:的)?\s*(?:分子|候选|化合物)"
    r"|"
    r"(?:(?<![A-Za-z])top[\s\-_]*|第\s*)\d{1,3}(?:\s*(?:名|位))?"
    r".{0,12}?(?:没(?:有)?进榜|未进榜|没(?:有)?入选|未入选|没上榜|未上榜|落选)"
    r")",
    re.I,
)
_RANKING_EXPLANATION_RE = re.compile(
    r"为什么|为何|为啥|凭什么|原因|理由|依据|解释|说明|介绍|讲讲|聊聊|"
    r"怎么(?:会|是|排|选|成为|成了|来的)|如何(?:排|选|得出)|"
    r"\bwhy\b|\bhow\s+come\b|\bexplain\b",
    re.I,
)
_EXPLICIT_RUN_RE = re.compile(
    r"生成|导出|筛选|重跑|重新跑|开始跑|跑一下|制作|做一份|出一份|"
    r"\bgenerate\b|\bexport\b|\brerun\b|\brun\b",
    re.I,
)
_FORCE_RESCREEN_RE = re.compile(
    r"重新筛选|重跑|重新跑|忽略(?:上述|之前)?(?:条件|偏好|配置)|另起|从头(?:筛|跑)",
    re.I,
)
_QUESTION_END_RE = re.compile(r"(?:吗|呢|么|？|\?)\s*$", re.I)
_MOLECULE_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*)"
    r"(?![A-Za-z0-9_.-])",
    re.I,
)

# 例：@skill:masld_nominate  /tool:score_and_rank
_MENTION_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))[@/](plugin|skill|tool):([A-Za-z0-9][\w.\-]*)",
    re.I,
)
_COMPOUND_SPLIT_RE = re.compile(
    r"[，,；;。！？!?]+|"
    r"(?:并且|同时|另外|顺便|然后|以及|并)"
    r"(?=(?:请|帮|告诉|解释|回答|说明|介绍|总结|什么|为什么|为何|如何|"
    r"能否|是否|我|这|该))",
    re.I,
)
_MENTION_CONTROL_RE = re.compile(
    r"^(?:(?:我(?:是)?(?:要|想)?|请|帮我|麻烦|现在|先|再)\s*)*"
    r"(?:(?:试用|调用|运行|执行|使用|介绍|查看|看看|查询|检索)\s*)+"
    r"(?:(?:一下|这个|该|工具|插件|技能|功能)\s*)*",
    re.I,
)
_TOOL_ARG_TOKEN_RE = re.compile(
    r"^(?:"
    r"[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*|"
    r"[A-Za-z_][\w.-]*\s*=\s*[^\s]+|"
    r"\d+(?:\.\d+)?"
    r")\s*",
    re.I,
)
_CONTROL_ONLY_RE = re.compile(
    r"^(?:(?:我(?:是)?(?:要|想)?|请|帮我|麻烦|现在|先|再)\s*)*"
    r"(?:(?:试用|调用|运行|执行|使用|介绍|查看|看看|查询|检索)\s*)*"
    r"(?:(?:一下|这个|该|工具|插件|技能|功能)\s*)*$",
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
    # Evidence is a molecule-level Core operation only when an identity or an
    # explicit evidence-card request is present. Domain vocabulary belongs to
    # plugin capabilities; it must not decide whether a literature question
    # is routed to Core here.
    if not re.search(
        r"(?:inchikey|cas|smiles|molecule[_\s-]?id|候选\s*分子|分子\s*证据|证据\s*卡)"
        r"|(?:候选|分子)\s*[:=：]?\s*[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*",
        low,
    ):
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


def extract_companion_text(text: str) -> str:
    """Extract non-mention work from a turn that explicitly selects a capability.

    Mention syntax is a routing hint, not an instruction to discard the rest
    of the user's sentence.  Tool identifiers and common argument tokens are
    removed conservatively; meaningful clauses remain available to the
    conversational branch of the turn.
    """
    raw = (text or "").strip()
    if not raw or not _MENTION_RE.search(raw):
        return ""
    residual = _MENTION_RE.sub(" ", raw)
    clauses: list[str] = []
    for raw_clause in _COMPOUND_SPLIT_RE.split(residual):
        clause = str(raw_clause or "").strip(" \t\r\n、:：-")
        if not clause:
            continue
        clause = _MENTION_CONTROL_RE.sub("", clause).strip(" \t\r\n、:：-")
        # Explicit invocation arguments belong to the mention branch.  Strip
        # only leading, structurally obvious tokens so normal prose survives.
        previous = None
        while clause and clause != previous:
            previous = clause
            clause = _TOOL_ARG_TOKEN_RE.sub("", clause).strip()
        clause = re.sub(r"^(?:并且|同时|另外|顺便|然后|以及|并)\s*", "", clause)
        if not clause or _CONTROL_ONLY_RE.fullmatch(clause):
            continue
        clauses.append(clause)
    return "；".join(dict.fromkeys(clauses))


def _mention_action(_text: str, mentions: tuple[MentionRef, ...]) -> str:
    """Structural default only.

    introduce vs invoke is refined by the runtime LLM classifier — do not
    hardcode confirmation / intent verb tables here. Safe default: introduce
    (never auto-run tools from a bare mention).
    """
    if not mentions:
        return ""
    return "introduce"


def ranking_question_fallback(text: str) -> tuple[bool, str | None]:
    """Conservative offline fallback for a question about an existing rank.

    ``top1`` is ambiguous: in “生成 top1” it is an output size, while in
    “为啥 top1 是 T19959” it is the subject of a question. Online routing must
    use the Loop LLM classifiers; this helper is only for LLM-down paths and
    for extracting molecule ids after an explain act is chosen.
    """
    raw = (text or "").strip()
    if not _RANKING_CONTEXT_RE.search(raw):
        return False, None

    asks_for_explanation = bool(_RANKING_EXPLANATION_RE.search(raw))
    asks_compare = bool(_RANKING_COMPARE_RE.search(raw))
    asks_a_question = bool(_QUESTION_END_RE.search(raw))
    explicitly_runs = bool(_EXPLICIT_RUN_RE.search(raw))
    if (
        not asks_for_explanation
        and not asks_compare
        and (not asks_a_question or explicitly_runs)
    ):
        return False, None
    if (asks_for_explanation or asks_compare) and explicitly_runs:
        return False, None

    molecule_id = None
    for match in _MOLECULE_ID_RE.finditer(raw):
        candidate = match.group(1)
        if not candidate.lower().startswith("top"):
            molecule_id = candidate
            break
    return True, molecule_id


def extract_ranking_positions(text: str) -> tuple[int, ...]:
    """Return explicitly named frozen ranks in their original order.

    ``Top4 和 Top5`` is a pairwise explanation request, rather than shorthand
    for the first four rows.  ``前 5 名`` expands to ranks 1..N.  Keep the
    positional list separate from ``requested_top_n`` so an execution-size
    parser cannot collapse it to the first number it encounters.
    """
    raw = text or ""
    out: list[int] = []
    span = _FRONT_RANK_SPAN_RE.search(raw)
    if span:
        end = int(span.group(1))
        if TOP_N_MIN <= end <= TOP_N_MAX:
            out.extend(range(1, end + 1))
    for match in _RANK_POSITION_RE.finditer(raw):
        rank = int(match.group(1))
        if TOP_N_MIN <= rank <= TOP_N_MAX and rank not in out:
            out.append(rank)
    return tuple(out)


def ranking_position_subject_fallback(text: str) -> bool:
    """Whether a named rank refers to one molecule rather than a TopN set."""
    return bool(_RANK_POSITION_SUBJECT_RE.search(text or ""))


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
    companion_text = extract_companion_text(raw)
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
            companion_text=companion_text,
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

    # Skill-surface tokens: keep concrete deliverable / artifact names.
    # Soft domain nouns (筛选/候选/机制/假说/报告) are not enough alone — they
    # collide with inventory/meta questions. Force-rescreen is an exception.
    mentions_csv = any(
        k in low for k in ("csv", "清单", "top", "短名单", "提名")
    ) or bool(_FORCE_RESCREEN_RE.search(raw))
    mentions_pdf = any(k in low for k in ("pdf", "验证方案"))
    mentions_reserve = any(
        k in low
        for k in (
            "候补",
            "备用",
            "reserve",
            "nomination_reserve",
            "replacement",
            "顺延",
        )
    )
    mentions_bundle = any(
        k in low
        for k in (
            "交卷包",
            "提交包",
            "结果包",
            "候选包",
            "bundle",
            "submission bundle",
            "submission_bundle",
            "export_submission_bundle",
        )
    )
    # Explicit deliverable markers → tools even without soft verbs like「帮我」
    strong_product = any(
        k in low
        for k in (
            "csv",
            "pdf",
            "top",
            "清单",
            "sdf",
            "候补",
            "reserve",
            "交卷包",
            "候选包",
            "bundle",
        )
    )
    soft_request = any(
        k in low
        for k in ("生成", "帮我", "导出", "跑", "开始", "做一份", "出一份", "给我", "来一份")
    )
    # Soft verbs alone must not open the tool lane; they only refine deliverable
    # type after a strong product / explicit-run surface is present.
    execution_requested = bool(
        strong_product or _EXPLICIT_RUN_RE.search(raw)
    )

    # Ranking follow-ups may look like chat or tool surface. Do not decide
    # explain_ranking here — Loop LLM (or offline ranking_question_fallback)
    # owns the dialog act. Still mark wants_tools so Loop enters classification
    # even when the utterance lacks csv/top product tokens (e.g. 「前 5 名」).
    is_ranking_followup, ranking_molecule_id = ranking_question_fallback(raw)
    if is_ranking_followup:
        return AgentIntent(
            want_csv=False,
            want_pdf=False,
            top_n=top_n,
            raw_text=raw,
            reason="ranking_followup_candidate",
            skill_ids=(),
            wants_tools=True,
            mentions=mentions,
            mention_action="",
            requested_top_n=requested_top_n,
            top_n_over_limit=False,
            top_n_max=int(top_n_max),
            top_n_min=int(top_n_min),
            explain_ranking=False,
            ranking_molecule_id=ranking_molecule_id,
            ranking_positions=extract_ranking_positions(raw),
            ranking_position_subject=ranking_position_subject_fallback(raw),
            execution_requested=False,
        )

    product = mentions_csv or mentions_pdf or mentions_reserve or mentions_bundle
    if not product or not execution_requested:
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
            explain_ranking=False,
            ranking_molecule_id=None,
            ranking_positions=(),
            ranking_position_subject=False,
            execution_requested=False,
        )

    want_reserve = mentions_reserve or mentions_bundle
    want_bundle = mentions_bundle
    primary_markers = (
        "top",
        "主榜",
        "primary",
        "nomination_top",
        "候选",
        "提名清单",
    )
    mentions_primary_csv = mentions_csv and (
        not mentions_reserve or any(marker in low for marker in primary_markers)
    )
    want_csv = mentions_primary_csv or want_bundle or (soft_request and not mentions_pdf and not want_reserve) or (
        soft_request and mentions_pdf
    )
    # execution_requested with screening vocabulary but no soft verb still means
    # a Core deliverable (e.g. 「按默认配置重新筛选」).
    if execution_requested and mentions_csv and not mentions_pdf and not want_reserve and not want_bundle:
        want_csv = True
    only_pdf = mentions_pdf and not mentions_csv and not want_reserve and not want_bundle and not any(
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
    if want_bundle:
        skills.append("masld_export_bundle")

    shown_n = requested_top_n if (requested_top_n and top_n_over_limit) else top_n
    parts = []
    if want_csv:
        parts.append(f"Top{shown_n} 候选 CSV")
    if want_pdf:
        parts.append("机制与验证方案 PDF")
    if want_reserve:
        parts.append("候补名单 CSV")
    if want_bundle:
        parts.append("竞赛提交包")
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
        want_reserve=want_reserve,
        want_bundle=want_bundle,
        wants_tools=True,
        mentions=mentions,
        mention_action="",
        requested_top_n=requested_top_n,
        top_n_over_limit=bool(top_n_over_limit),
        top_n_max=int(top_n_max),
        top_n_min=int(top_n_min),
        execution_requested=True,
        force_rescreen=bool(_FORCE_RESCREEN_RE.search(raw)),
    )
