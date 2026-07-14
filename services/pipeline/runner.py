"""端到端筛选流水线（阶段 3：离线无证据可出 CSV）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from packages.goldset import load_goldset
from packages.models import CriticAction, MoleculeRecord, RunDiagnostics, ScoreRecord
from services.critic import run_evidence_bound_llm_critic, rule_critic
from services.evidence_facade.bundle import EvidenceBundle
from services.evidence_facade.facade import EvidenceFacade
from services.hard_filter import apply_hard_filters
from services.ingest import parse_sdf_detailed, quiet_rdkit
from services.pipeline.config_loader import AppConfig, load_config
from services.pipeline.diagnostics import compute_diagnostics
from services.pipeline.export import CSV_COLUMNS, export_nomination_csv, rows_from_top, to_csv_text
from services.pipeline.run_log import LogSink, RunLogCollector
from services.ranker import apply_scaffold_diversity, score_molecule

# 竞赛 Top10 为主；短名单 live=40 / Critic top_k=30，上限 50 避免过重计算
TOP_N_MIN = 1
TOP_N_MAX = 50


@dataclass
class PipelineResult:
    top_molecules: list[ScoreRecord]
    input_count: int
    filtered_out: int
    eligible_count: int
    requested_top_n: int
    config: AppConfig
    diagnostics: RunDiagnostics
    critic_actions: list[CriticAction] = field(default_factory=list)
    source_filename: str = ""
    note: str | None = None
    raw_count: int = 0
    parse_skipped: int = 0
    inchikey_missing: int = 0
    logs: list[dict] = field(default_factory=list)

    @property
    def output_count(self) -> int:
        return len(self.top_molecules)

    def to_row_dicts(self) -> list[dict[str, str | int | float]]:
        return rows_from_top(
            self.top_molecules,
            mode=self.config.mode,
            config_hash=self.config.config_hash,
            degraded_channels=self.config.degraded_channels,
        )

    def to_csv_text(self) -> str:
        return to_csv_text(
            self.top_molecules,
            mode=self.config.mode,
            config_hash=self.config.config_hash,
            degraded_channels=self.config.degraded_channels,
        )


def _score_all(
    records: list[MoleculeRecord],
    config: AppConfig,
    gold,
    facade: EvidenceFacade,
    *,
    allow_live: bool,
) -> tuple[list[ScoreRecord], int]:
    scored: list[ScoreRecord] = []
    evidence_hits = 0
    for record in records:
        evidence = facade.query(
            inchikey=record.inchikey,
            cas=record.cas,
            smiles=record.smiles,
            allow_live=allow_live,
        )
        if evidence.lipid or evidence.tox:
            evidence_hits += 1
        scored.append(score_molecule(record, config, gold, evidence))
    return scored, evidence_hits


def screen_sdf(
    input_path: Path,
    *,
    cfg: AppConfig | None = None,
    top_n: int | None = None,
    source_filename: str = "",
    log_sink: LogSink | None = None,
) -> PipelineResult:
    config = cfg or load_config(mode="offline")
    gold = load_goldset()
    facade = EvidenceFacade(config)
    log = RunLogCollector(sink=log_sink)

    with quiet_rdkit():
        return _screen_sdf_inner(
            input_path,
            config=config,
            gold=gold,
            facade=facade,
            top_n=top_n,
            source_filename=source_filename,
            log=log,
        )


def _screen_sdf_inner(
    input_path: Path,
    *,
    config: AppConfig,
    gold,
    facade: EvidenceFacade,
    top_n: int | None,
    source_filename: str,
    log: RunLogCollector,
) -> PipelineResult:
    mode_label = {"auto": "Quality-Max", "online": "Online", "offline": "Offline"}.get(
        config.mode, config.mode
    )
    use_snapshot = bool(config.evidence.get("use_snapshot", True))
    snap_zh = "开启" if use_snapshot else "关闭"
    snap_en = "enabled" if use_snapshot else "disabled"
    log.emit(
        "INFO",
        (
            f"启动筛选：运行模式={mode_label}（{config.mode}），"
            f"使用快照={snap_zh}，config_hash={config.config_hash}，"
            f"输入文件={source_filename or input_path.name}"
        ),
        (
            f"Screen started: mode={mode_label} ({config.mode}), "
            f"use_snapshot={snap_en}, config_hash={config.config_hash}, "
            f"input_file={source_filename or input_path.name}"
        ),
        progress=5,
    )

    log.emit(
        "INFO",
        f"开始解析 SDF 文件：{source_filename or input_path.name}",
        f"Start parsing SDF file: {source_filename or input_path.name}",
        progress=12,
    )
    parsed = parse_sdf_detailed(input_path)
    records = parsed.records
    if not records:
        log.emit(
            "ERROR",
            f"解析失败：未从文件 {input_path.name} 解析到任何有效分子",
            f"Parse failed: no valid molecules found in file {input_path.name}",
            progress=100,
        )
        raise ValueError(f"未从 {input_path} 解析到有效分子")

    log.emit(
        "INFO",
        (
            f"解析完成：原始记录={parsed.raw_count}，有效分子={len(records)}，"
            f"跳过无效结构={parsed.skipped}，缺少 InChIKey={parsed.inchikey_missing}"
        ),
        (
            f"Parse complete: raw_records={parsed.raw_count}, valid_molecules={len(records)}, "
            f"skipped_invalid_structures={parsed.skipped}, missing_inchikey={parsed.inchikey_missing}"
        ),
        progress=20,
    )

    log.emit(
        "INFO",
        "开始硬过滤：应用物理化学性质与结构规则筛除不合格分子",
        "Start hard filtering: apply physicochemical and structural rules to remove ineligible molecules",
        progress=28,
    )
    passed: list[MoleculeRecord] = []
    filtered_out = 0
    for record in records:
        decision = apply_hard_filters(record, config)
        if decision.passed:
            passed.append(record)
        else:
            filtered_out += 1

    log.emit(
        "INFO",
        f"硬过滤完成：保留分子={len(passed)}，剔除分子={filtered_out}",
        f"Hard filter complete: kept_molecules={len(passed)}, removed_molecules={filtered_out}",
        progress=35,
    )

    snap_path_zh = "读取本地证据快照" if use_snapshot else "不读取本地证据快照"
    snap_path_en = "reading local evidence snapshots" if use_snapshot else "skipping local evidence snapshots"
    log.emit(
        "INFO",
        f"开始本地证据打分：{snap_path_zh}，并融合规则打分与可选机器学习头",
        f"Start local evidence scoring: {snap_path_en}, fuse rule scores and optional ML heads",
        progress=42,
    )
    scored, _ = _score_all(passed, config, gold, facade, allow_live=False)
    eligible = [m for m in scored if not m.gated_out]
    eligible.sort(key=lambda m: (-m.final_score, m.molecule_id))
    gated = len(scored) - len(eligible)
    log.emit(
        "INFO",
        f"门控完成：门控后候选={len(eligible)}，门控剔除={gated}，已按 final_score 降序排序",
        f"Gating complete: eligible_candidates={len(eligible)}, gated_out={gated}, sorted by final_score descending",
        progress=55,
    )

    deep_m = int(config.evidence.get("deep_query_top_m", 40))
    evidence_hits = 0
    if config.allow_live_evidence and deep_m > 0 and eligible:
        timeout_sec = float(config.evidence.get("http_timeout_sec", 4.0))
        adapters = config.evidence.get("adapters") or ["chembl_lipid_v1", "pubchem_tox_v1"]
        adapter_names = ", ".join(str(a) for a in adapters)
        shortlist_n = min(deep_m, len(eligible))
        log.emit(
            "INFO",
            (
                f"开始短名单 live 补洞：对门控后按分数靠前的 {shortlist_n} 个候选"
                f"（Top-M={deep_m}，模式={config.mode}）逐个请求外网证据。"
                f"当前启用适配器={adapter_names}（ChEMBL 降脂证据 / PubChem 毒性证据），"
                f"单次 HTTP 超时={timeout_sec}s；若候选已在本地快照命中则跳过 live。"
                f"该阶段依赖外网往返，耗时随短名单长度与网络状况增加；"
                f"连续失败达到熔断阈值后会自动降级，停止继续发起 live 请求。"
            ),
            (
                f"Start live evidence fill for shortlist: query external evidence for the top "
                f"{shortlist_n} gated candidates (Top-M={deep_m}, mode={config.mode}) one by one. "
                f"Active adapters={adapter_names} (ChEMBL lipid / PubChem tox), "
                f"per-request HTTP timeout={timeout_sec}s; candidates already present in the local "
                f"snapshot are skipped when snapshot-prefer mode is on. "
                f"Runtime grows with shortlist size and network latency; after repeated failures "
                f"the circuit breaker opens and further live requests are skipped with degradation."
            ),
            progress=62,
        )
        by_id = {r.molecule_id: r for r in passed}
        deep_ids = {m.molecule_id for m in eligible[:deep_m]}
        rescored: list[ScoreRecord] = []
        for mol in eligible:
            if mol.molecule_id not in deep_ids:
                rescored.append(mol)
                continue
            record = by_id[mol.molecule_id]
            evidence = facade.query(
                inchikey=record.inchikey,
                cas=record.cas,
                smiles=record.smiles,
                allow_live=True,
            )
            if evidence.lipid or evidence.tox:
                evidence_hits += 1
            rescored.append(score_molecule(record, config, gold, evidence))
        eligible = [m for m in rescored if not m.gated_out]
        eligible.sort(key=lambda m: (-m.final_score, m.molecule_id))
        log.emit(
            "INFO",
            f"Live 补洞完成：外网证据命中数={evidence_hits}，当前门控后候选={len(eligible)}",
            f"Live fill complete: external_evidence_hits={evidence_hits}, eligible_candidates={len(eligible)}",
            progress=72,
        )
    else:
        evidence_hits = sum(1 for m in eligible if m.conf_e > 0)
        log.emit(
            "INFO",
            (
                f"跳过 live 补洞：模式={config.mode}，allow_live_evidence={config.allow_live_evidence}，"
                f"deep_query_top_m={deep_m}；本地证据命中数（conf_e>0）={evidence_hits}"
            ),
            (
                f"Skipping live fill: mode={config.mode}, allow_live_evidence={config.allow_live_evidence}, "
                f"deep_query_top_m={deep_m}; local_evidence_hits (conf_e>0)={evidence_hits}"
            ),
            progress=72,
        )

    facade.finalize_degraded_flags(any_hit=evidence_hits > 0)
    if config.degraded_channels:
        channels = " | ".join(config.degraded_channels)
        log.emit(
            "WARN",
            f"证据通道已降级：{channels}",
            f"Evidence channels degraded: {channels}",
            progress=75,
        )

    n = top_n or config.top_n
    top_k_n = max(n, config.top_k_for_critic)
    diversity = config.diversity
    max_per_scaffold = int(diversity.get("max_per_scaffold", 2))
    redundancy_lambda = float(diversity.get("redundancy_lambda", 0.05))
    log.emit(
        "INFO",
        (
            f"开始骨架多样性筛选：目标 Top-K={top_k_n}，"
            f"每骨架上限={max_per_scaffold}，冗余惩罚 lambda={redundancy_lambda}"
        ),
        (
            f"Start scaffold diversity selection: target Top-K={top_k_n}, "
            f"max_per_scaffold={max_per_scaffold}, redundancy_lambda={redundancy_lambda}"
        ),
        progress=80,
    )
    diversified = apply_scaffold_diversity(
        eligible,
        top_n=top_k_n,
        max_per_scaffold=max_per_scaffold,
        redundancy_lambda=redundancy_lambda,
    )
    log.emit(
        "INFO",
        f"骨架多样性筛选完成：多样性后候选数={len(diversified)}",
        f"Scaffold diversity complete: diversified_candidates={len(diversified)}",
        progress=85,
    )

    log.emit(
        "INFO",
        f"开始规则 Critic：目标输出 Top N={n}，对多样性候选做金标准校准与风险剔除",
        f"Start rule Critic: target output Top N={n}, apply gold-set calibration and risk removals",
        progress=90,
    )
    top, actions = rule_critic(diversified, eligible, config, gold, top_n=n)
    if actions:
        log.emit(
            "INFO",
            f"规则 Critic 完成：执行动作数={len(actions)}，当前短名单大小={len(top)}",
            f"Rule Critic complete: action_count={len(actions)}, shortlist_size={len(top)}",
            progress=92,
        )

    log.emit(
        "INFO",
        "开始证据约束 LLM Critic：仅在启用且具备本轮证据约束时调整短名单",
        "Start evidence-bound LLM Critic: adjust shortlist only when enabled and run evidence is available",
        progress=94,
    )
    top, llm_actions = run_evidence_bound_llm_critic(top, config)
    actions.extend(llm_actions)
    if llm_actions:
        log.emit(
            "INFO",
            f"LLM Critic 完成：执行动作数={len(llm_actions)}，当前短名单大小={len(top)}",
            f"LLM Critic complete: action_count={len(llm_actions)}, shortlist_size={len(top)}",
            progress=96,
        )
    else:
        log.emit(
            "INFO",
            "LLM Critic 未改动短名单：功能关闭，或本轮无可应用动作",
            "LLM Critic made no changes: feature disabled, or no applicable actions in this run",
            progress=96,
        )

    diag = compute_diagnostics(
        cfg=config,
        gold=gold,
        input_count=len(records),
        filtered_out=filtered_out,
        scored=scored,
        eligible=eligible,
        top=top,
        evidence_hit_count=evidence_hits,
        raw_count=parsed.raw_count,
        parse_skipped=parsed.skipped,
        inchikey_missing=parsed.inchikey_missing,
    )

    note_parts: list[str] = []
    if len(top) < n:
        note_parts.append(f"合格候选仅 {len(top)} 个，少于请求的 Top {n}。")
    if parsed.skipped:
        note_parts.append(
            f"解析跳过 {parsed.skipped}/{parsed.raw_count} 条无效结构（价态/kekulize 等）。"
        )
    note = " ".join(note_parts) or None

    q_pass = diag.quality_pass
    quality_zh = "PASS" if q_pass else "FAIL" if q_pass is False else "N/A"
    quality_en = quality_zh
    log.emit(
        "SUCCESS" if q_pass is not False else "WARN",
        (
            f"筛选完成：输出 Top={len(top)}，请求 Top={n}，质量门禁={quality_zh}，"
            f"模式={mode_label}（{config.mode}），使用快照={snap_zh}，config_hash={config.config_hash}"
        ),
        (
            f"Screen complete: output_top={len(top)}, requested_top={n}, quality_gate={quality_en}, "
            f"mode={mode_label} ({config.mode}), use_snapshot={snap_en}, config_hash={config.config_hash}"
        ),
        progress=100,
    )

    return PipelineResult(
        top_molecules=top,
        input_count=len(records),
        filtered_out=filtered_out,
        eligible_count=len(eligible),
        requested_top_n=n,
        config=config,
        diagnostics=diag,
        critic_actions=actions,
        source_filename=source_filename or input_path.name,
        note=note,
        raw_count=parsed.raw_count,
        parse_skipped=parsed.skipped,
        inchikey_missing=parsed.inchikey_missing,
        logs=log.as_dicts(),
    )


def run_pipeline(
    input_path: Path,
    output_path: Path,
    *,
    mode: str = "offline",
    top_n: int | None = None,
    write_mechanism: bool = True,
) -> PipelineResult:
    cfg = load_config(mode=mode)
    result = screen_sdf(input_path, cfg=cfg, top_n=top_n, source_filename=input_path.name)
    export_nomination_csv(
        result.top_molecules,
        output_path,
        mode=cfg.mode,
        config_hash=cfg.config_hash,
        degraded_channels=cfg.degraded_channels,
        requested_top_n=result.requested_top_n,
    )
    if write_mechanism and result.top_molecules:
        from services.mechanism import render_mechanism_markdown

        mech_path = Path(output_path).with_suffix(".mechanism.md")
        # 机制 LLM 仅润色 Markdown；绝不回写分数或重排 Top 10
        render_mechanism_markdown(
            result.top_molecules,
            mech_path,
            llm_cfg=result.config.llm,
            mark_degraded=result.config.mark_degraded,
        )
    return result


__all__ = [
    "CSV_COLUMNS",
    "EvidenceBundle",
    "PipelineResult",
    "TOP_N_MAX",
    "TOP_N_MIN",
    "run_pipeline",
    "screen_sdf",
]
