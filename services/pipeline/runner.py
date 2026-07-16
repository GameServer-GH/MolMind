"""端到端筛选流水线（阶段 3：离线无证据可出 CSV）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from packages.goldset import load_goldset
from packages.models import (
    CriticAction,
    MoleculeAssessment,
    MoleculeRecord,
    RunDiagnostics,
    ScoreRecord,
    ScreeningAuditRecord,
    format_selection_reason,
)
from services.critic import run_evidence_bound_llm_critic, rule_critic, summarize_critic_actions
from services.evidence_facade.bundle import EvidenceBundle
from services.evidence_facade.facade import EvidenceFacade
from services.evidence_facade.mechanism_graph import (
    MechanismGraph,
    build_mechanism_graphs,
    load_mechanism_context,
)
from services.evidence_facade.hepg2_ffa_resources import resource_registry_runtime_payload
from services.eligibility import evaluate_candidate_eligibility, policy_from_config
from services.hard_filter import apply_hard_filters
from services.ingest import (
    ParseProgress,
    estimate_sdf_record_count,
    feature_cache_path,
    load_feature_cache,
    parse_sdf_detailed,
    quiet_rdkit,
    save_feature_cache,
    sha256_file,
)
from services.pipeline.config_loader import ROOT, AppConfig, load_config
from services.pipeline.diagnostics import compute_diagnostics
from services.pipeline.export import (
    CSV_COLUMNS,
    export_critic_audit_csv,
    export_candidate_scores_jsonl,
    export_citations_jsonl,
    export_evidence_ledger_jsonl,
    export_nomination_csv,
    export_rank_robustness_json,
    export_screening_audit_csv,
    export_selection_audit_jsonl,
    rows_from_top,
    to_csv_text,
)
from services.pipeline.run_log import LogSink, RunLogCollector
from services.pipeline.run_identity import deterministic_run_id, selection_sha256
from services.ranker import analyze_rank_robustness, apply_scaffold_diversity, score_molecule

# Top10 为主；短名单 live=40 / Critic top_k=30，上限 50 避免过重计算
TOP_N_MIN = 1
TOP_N_MAX = 50
# 解析阶段进度条占用 [12, 20)；完成后 runner 再推到 20
_PARSE_PROGRESS_START = 12
_PARSE_PROGRESS_SPAN = 7


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
    review_required_count: int = 0
    screening_audit: list[ScreeningAuditRecord] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    rank_robustness: list[dict[str, object]] = field(default_factory=list)
    manifest_path: str = ""
    input_sha256: str = ""
    selection_sha256: str = ""
    run_id: str = ""
    mechanism_graphs: list[MechanismGraph] = field(default_factory=list)
    hepg2_ffa_resources: dict[str, object] = field(default_factory=dict)
    scored_molecules: list[ScoreRecord] = field(default_factory=list)
    selection_audit: list[dict[str, object]] = field(default_factory=list)

    @property
    def output_count(self) -> int:
        return len(self.top_molecules)

    def to_row_dicts(self) -> list[dict[str, str | int | float]]:
        return rows_from_top(
            self.top_molecules,
            mode=self.config.mode,
            config_hash=self.config.config_hash,
            degraded_channels=self.config.degraded_channels,
            run_id=self.run_id,
            input_sha256=self.input_sha256,
            selection_hash=self.selection_sha256,
        )

    def to_csv_text(self) -> str:
        return to_csv_text(
            self.top_molecules,
            mode=self.config.mode,
            config_hash=self.config.config_hash,
            degraded_channels=self.config.degraded_channels,
            run_id=self.run_id,
            input_sha256=self.input_sha256,
            selection_hash=self.selection_sha256,
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
    requested_n = top_n or config.top_n
    if requested_n < TOP_N_MIN or requested_n > TOP_N_MAX:
        raise ValueError(f"top_n 须在 {TOP_N_MIN}–{TOP_N_MAX} 之间")
    gold = load_goldset()
    task_model = config.model_manifest.get("task_specific_dual_endpoint") or {}
    if str(task_model.get("status") or "unavailable") != "available":
        config.mark_degraded("hepg2_ffa_dual_endpoint_model_unavailable")
    facade = EvidenceFacade(config)
    log = RunLogCollector(sink=log_sink)

    with quiet_rdkit():
        return _screen_sdf_inner(
            input_path,
            config=config,
            gold=gold,
            facade=facade,
            top_n=requested_n,
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

    display_name = source_filename or input_path.name
    estimated_records = estimate_sdf_record_count(input_path)
    estimate_zh = (
        f"预估约 {estimated_records} 条记录"
        if estimated_records > 0
        else "记录数暂未知（将按流式推进）"
    )
    estimate_en = (
        f"about {estimated_records} records estimated"
        if estimated_records > 0
        else "record count unknown (streaming progress)"
    )
    log.emit(
        "INFO",
        f"开始解析 SDF 文件：{display_name}（{estimate_zh}）",
        f"Start parsing SDF file: {display_name} ({estimate_en})",
        progress=_PARSE_PROGRESS_START,
    )
    log.emit(
        "INFO",
        (
            "解析耗时主要来自逐分子化学计算：每条记录都要做确定性标准化"
            "（Cleanup / 取主体片段 / 去电荷 / 互变异构规范化），"
            "再算 Morgan 指纹、InChIKey 与理化描述符；"
            "当前为单线程、无特征缓存，万级库通常需要数分钟，进度会持续更新。"
        ),
        (
            "Parse time is dominated by per-molecule chemistry: each record runs "
            "deterministic standardization "
            "(Cleanup / parent fragment / uncharge / canonical tautomer), "
            "then Morgan fingerprint, InChIKey and descriptors. "
            "The path is single-threaded with no feature cache, so large libraries "
            "often take several minutes; progress updates continue throughout."
        ),
        progress=_PARSE_PROGRESS_START,
    )

    parse_started = time.monotonic()
    last_emit_at = parse_started
    last_emit_processed = 0
    progress_step = (
        max(100, estimated_records // 20) if estimated_records > 0 else 250
    )

    def _on_parse_progress(snap: ParseProgress) -> None:
        nonlocal last_emit_at, last_emit_processed
        if snap.processed <= 0:
            return
        now = time.monotonic()
        finished = (
            snap.estimated_total > 0 and snap.processed >= snap.estimated_total
        )
        due = (
            finished
            or snap.processed - last_emit_processed >= progress_step
            or now - last_emit_at >= 5.0
        )
        if not due:
            return
        last_emit_at = now
        last_emit_processed = snap.processed
        elapsed = max(0.0, now - parse_started)
        if snap.estimated_total > 0:
            ratio = min(1.0, snap.processed / snap.estimated_total)
            pct = _PARSE_PROGRESS_START + int(_PARSE_PROGRESS_SPAN * ratio)
            rate = (snap.processed / elapsed) if elapsed > 0.5 else 0.0
            eta_zh = ""
            eta_en = ""
            if rate > 0 and snap.processed < snap.estimated_total:
                remain = (snap.estimated_total - snap.processed) / rate
                eta_zh = f"，预计剩余约 {remain:.0f} 秒"
                eta_en = f", ETA ~{remain:.0f}s"
            log.emit(
                "INFO",
                (
                    f"解析进行中：{snap.processed}/{snap.estimated_total} "
                    f"（有效 {snap.valid}，跳过 {snap.skipped}，"
                    f"已用时 {elapsed:.0f} 秒{eta_zh}）"
                    " — 标准化/指纹计算中"
                ),
                (
                    f"Parsing in progress: {snap.processed}/{snap.estimated_total} "
                    f"(valid={snap.valid}, skipped={snap.skipped}, "
                    f"elapsed {elapsed:.0f}s{eta_en}) "
                    "— standardization/fingerprint compute"
                ),
                progress=pct,
            )
            return
        pct = min(
            _PARSE_PROGRESS_START + _PARSE_PROGRESS_SPAN - 1,
            _PARSE_PROGRESS_START + snap.processed // max(progress_step, 1),
        )
        log.emit(
            "INFO",
            (
                f"解析进行中：已处理 {snap.processed} 条"
                f"（有效 {snap.valid}，跳过 {snap.skipped}，已用时 {elapsed:.0f} 秒）"
                " — 标准化/指纹计算中"
            ),
            (
                f"Parsing in progress: processed {snap.processed} "
                f"(valid={snap.valid}, skipped={snap.skipped}, "
                f"elapsed {elapsed:.0f}s) — standardization/fingerprint compute"
            ),
            progress=pct,
        )

    cache_cfg = config.feature_cache
    cache_enabled = bool(cache_cfg.get("enabled", False))
    cache_schema = str(cache_cfg.get("schema_version") or "ingest-features-v1")
    cache_dir = Path(str(cache_cfg.get("directory") or ".molmind_cache/features"))
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir
    cache_path = feature_cache_path(
        input_path,
        cache_dir=cache_dir,
        schema_version=cache_schema,
    )
    parsed = load_feature_cache(cache_path) if cache_enabled else None
    if parsed is not None:
        log.emit(
            "INFO",
            f"命中特征缓存：{cache_path.name}；跳过重复标准化、描述符和指纹计算",
            f"Feature cache hit: {cache_path.name}; skipped repeated standardization, descriptors and fingerprints",
            progress=19,
        )
    else:
        parsed = parse_sdf_detailed(
            input_path,
            on_progress=_on_parse_progress,
            estimated_total=estimated_records,
        )
        if cache_enabled:
            try:
                save_feature_cache(
                    cache_path,
                    parsed,
                    metadata={
                        "input_sha256": sha256_file(input_path),
                        "schema_version": cache_schema,
                    },
                )
                log.emit(
                    "INFO",
                    f"已写入特征缓存：{cache_path.name}",
                    f"Feature cache written: {cache_path.name}",
                    progress=19,
                )
            except OSError as exc:
                config.mark_degraded("feature_cache_write_failed")
                log.emit(
                    "WARN",
                    f"特征缓存写入失败，继续本次运行：{exc}",
                    f"Feature cache write failed; continuing this run: {exc}",
                    progress=19,
                )
    records = parsed.records
    if not records:
        log.emit(
            "ERROR",
            f"解析失败：未从文件 {input_path.name} 解析到任何有效分子",
            f"Parse failed: no valid molecules found in file {input_path.name}",
            progress=100,
        )
        raise ValueError(f"未从 {input_path} 解析到有效分子")

    parse_elapsed = max(0.0, time.monotonic() - parse_started)
    log.emit(
        "INFO",
        (
            f"解析完成：原始记录={parsed.raw_count}，有效分子={len(records)}，"
            f"跳过无效结构={parsed.skipped}，缺少 InChIKey={parsed.inchikey_missing}，"
            f"耗时 {parse_elapsed:.1f} 秒"
        ),
        (
            f"Parse complete: raw_records={parsed.raw_count}, valid_molecules={len(records)}, "
            f"skipped_invalid_structures={parsed.skipped}, "
            f"missing_inchikey={parsed.inchikey_missing}, "
            f"elapsed {parse_elapsed:.1f}s"
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
    review_required_count = 0
    screening_audit = [
        ScreeningAuditRecord(
            molecule_id=issue.molecule_id,
            source_index=issue.source_index,
            status=issue.status,
            reason_codes=(issue.reason_code,),
            reason=issue.reason,
        )
        for issue in parsed.issues
    ]
    for record in records:
        decision = apply_hard_filters(record, config)
        screening_audit.append(
            ScreeningAuditRecord(
                molecule_id=record.molecule_id,
                source_index=record.source_index,
                status=decision.status,
                reason_codes=tuple(decision.reason_codes),
                reason=decision.reason,
                alert_hits=tuple(
                    f"{hit.rule_id}:{hit.classification}" for hit in decision.alert_hits
                ),
            )
        )
        if decision.passed:
            passed.append(record)
            if decision.status == "review_required":
                review_required_count += 1
        else:
            filtered_out += 1

    log.emit(
        "INFO",
        (
            f"初筛完成：进入评分={len(passed)}，硬剔除={filtered_out}，"
            f"需复核但保留={review_required_count}"
        ),
        (
            f"Screening complete: advanced_to_scoring={len(passed)}, hard_rejected={filtered_out}, "
            f"review_required_but_retained={review_required_count}"
        ),
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
    review_ids = {
        item.molecule_id
        for item in screening_audit
        if item.status == "review_required"
    }
    for molecule in scored:
        if molecule.molecule_id in review_ids:
            molecule.eligibility_status = "review_required"
            molecule.gated_out = True
            molecule.gate_reason = "screening_review_required"
    eligible = [m for m in scored if not m.gated_out]
    eligible.sort(key=lambda m: (-m.final_score, m.molecule_id))
    gated = len(scored) - len(eligible)
    log.emit(
        "INFO",
        f"门控完成：门控后候选={len(eligible)}，门控剔除={gated}，已按 final_score 降序排序",
        f"Gating complete: eligible_candidates={len(eligible)}, gated_out={gated}, sorted by final_score descending",
        progress=55,
    )

    # OriGene 风格 Top-M：身份冲突检查 → 证据补洞 → 统一 eligibility 重跑。
    # 窗口沿用 deep_query_top_m（默认 40），不硬改 50/100 以免冲击 freeze/SLA。
    deep_m = int(config.evidence.get("deep_query_top_m", 40))
    evidence_hits = 0
    identity_review_count = 0
    if deep_m > 0 and eligible:
        shortlist_n = min(deep_m, len(eligible))
        by_id = {r.molecule_id: r for r in passed}
        deep_ids = {m.molecule_id for m in eligible[:deep_m]}
        allow_live = bool(config.allow_live_evidence)
        timeout_sec = float(config.evidence.get("http_timeout_sec", 4.0))
        adapters = config.evidence.get("adapters") or ["chembl_lipid_v1", "pubchem_tox_v1"]
        adapter_names = ", ".join(str(a) for a in adapters)
        log.emit(
            "INFO",
            (
                f"开始 Top-M 证据编排（OriGene 风格）：窗口={shortlist_n} "
                f"（deep_query_top_m={deep_m}）；步骤=身份冲突检查→"
                f"{'live' if allow_live else 'snapshot'}补洞→统一资格重跑；"
                f"模式={config.mode}；适配器={adapter_names}；HTTP超时={timeout_sec}s"
            ),
            (
                f"Start Top-M evidence orchestration: window={shortlist_n} "
                f"(deep_query_top_m={deep_m}); steps=identity_check→"
                f"{'live' if allow_live else 'snapshot'}_fill→re-eligibility; "
                f"mode={config.mode}; adapters={adapter_names}; http_timeout={timeout_sec}s"
            ),
            progress=62,
        )
        rescored: list[ScoreRecord] = []
        for mol in eligible:
            if mol.molecule_id not in deep_ids:
                rescored.append(mol)
                continue
            record = by_id[mol.molecule_id]
            # Step 1–2: re-query (live only when allow_live_evidence); identity status
            # is re-derived inside score_molecule from query_audit / PubChem multi-CID.
            evidence = facade.query(
                inchikey=record.inchikey,
                cas=record.cas,
                smiles=record.smiles,
                allow_live=allow_live,
            )
            if evidence.has_identity_review_required:
                identity_review_count += 1
            if evidence.lipid or evidence.tox:
                evidence_hits += 1
            # Step 3: unified eligibility via score_molecule
            rescored.append(score_molecule(record, config, gold, evidence))
        eligible = [m for m in rescored if not m.gated_out]
        by_scored = {m.molecule_id: m for m in scored}
        by_scored.update({m.molecule_id: m for m in rescored})
        scored = [by_scored.get(m.molecule_id, m) for m in scored]
        eligible.sort(key=lambda m: (-m.final_score, m.molecule_id))
        log.emit(
            "INFO",
            (
                f"Top-M 编排完成：证据命中={evidence_hits}，"
                f"身份待审={identity_review_count}，门控后候选={len(eligible)}，"
                f"live={'on' if allow_live else 'off'}"
            ),
            (
                f"Top-M orchestration complete: evidence_hits={evidence_hits}, "
                f"identity_review={identity_review_count}, eligible={len(eligible)}, "
                f"live={'on' if allow_live else 'off'}"
            ),
            progress=72,
        )
    else:
        evidence_hits = sum(1 for m in eligible if m.conf_e > 0)
        log.emit(
            "INFO",
            (
                f"跳过 Top-M 编排：deep_query_top_m={deep_m} 或无合格候选；"
                f"本地证据命中数（conf_e>0）={evidence_hits}"
            ),
            (
                f"Skipping Top-M orchestration: deep_query_top_m={deep_m} or no eligible; "
                f"local_evidence_hits (conf_e>0)={evidence_hits}"
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
    rank_robustness = analyze_rank_robustness(eligible, config, top_n=n)
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
        max_pairwise_similarity=float(diversity.get("max_pairwise_tanimoto", 1.0)),
        similarity_cluster_threshold=float(
            diversity.get("similarity_cluster_threshold", 1.0)
        ),
        max_per_similarity_cluster=int(diversity.get("max_per_similarity_cluster", 999999)),
        mmr_lambda=float(diversity.get("mmr_lambda", 1.0)),
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
        drop_hist = summarize_critic_actions(actions)
        hist_zh = ", ".join(f"{k}={v}" for k, v in drop_hist.items() if v and k != "keep")
        hist_keep = drop_hist.get("keep", 0)
        log.emit(
            "INFO",
            (
                f"规则 Critic 完成：执行动作数={len(actions)}，keep={hist_keep}，"
                f"当前短名单大小={len(top)}"
                + (f"；drop 直方图：{hist_zh}" if hist_zh else "")
            ),
            (
                f"Rule Critic complete: action_count={len(actions)}, keep={hist_keep}, "
                f"shortlist_size={len(top)}"
                + (f"; drop_histogram={hist_zh}" if hist_zh else "")
            ),
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

    # 最终交付前再次执行唯一资格接口；Critic/LLM 无权重新引入不合格候选。
    policy = policy_from_config(config.gates)
    final_top: list[ScoreRecord] = []
    for mol in top:
        decision = evaluate_candidate_eligibility(
            MoleculeAssessment(
                molecule_id=mol.molecule_id,
                lipid_score=mol.lipid_score,
                toxicity_score=mol.tox_risk,
                toxicity_confidence=mol.toxicity_confidence,
                toxicity_evidence_coverage=mol.toxicity_evidence_coverage,
                safety_clearance_confidence=mol.safety_clearance_confidence,
            ),
            policy,
        )
        mol.eligibility_status = decision.status
        mol.eligibility_reasons = decision.reasons
        mol.gated_out = not decision.is_eligible
        mol.gate_reason = "" if decision.is_eligible else "; ".join(decision.reasons)
        if decision.is_eligible:
            final_top.append(mol)
        else:
            actions.append(
                CriticAction(
                    action="drop",
                    molecule_id=mol.molecule_id,
                    reason=f"final_eligibility_gate: {mol.gate_reason}",
                    original_status="critic_shortlist",
                    checks_performed=("final_eligibility_gate",),
                    score_before=mol.final_score,
                    score_after=0.0,
                    eligibility_before="eligible",
                    eligibility_after=decision.status,
                    final_decision="not_selected",
                )
            )
    top = final_top

    selected_ids = {m.molecule_id for m in top}
    drop_reasons = {
        action.molecule_id: action.reason
        for action in actions
        if action.action in {"drop", "replace"} and action.molecule_id not in selected_ids
    }
    selection_audit: list[dict[str, object]] = []
    for mol in top:
        mol.selection_factors = dict(mol.selection_factors or {})
        mol.selection_factors["eligibility"] = mol.eligibility_status
        mol.selection_factors["score"] = f"{mol.final_score:.4f}"
        mol.selection_factors.setdefault("combo_adjustment", "selected")
        mol.selection_reason = format_selection_reason(mol.selection_factors)
        selection_audit.append(
            {
                "molecule_id": mol.molecule_id,
                "outcome": "selected",
                "selection_factors": dict(mol.selection_factors),
                "selection_reason": mol.selection_reason,
                "final_score": mol.final_score,
                "eligibility_status": mol.eligibility_status,
            }
        )
    # 落榜审计：短名单（多样性后）未进入最终 Top-N 的候选
    shortlist_ids = {m.molecule_id for m in diversified}
    for mol in diversified:
        if mol.molecule_id in selected_ids:
            continue
        factors = dict(mol.selection_factors or {})
        factors["eligibility"] = mol.eligibility_status
        factors["score"] = f"{mol.final_score:.4f}"
        if mol.molecule_id in drop_reasons:
            factors["combo_adjustment"] = drop_reasons[mol.molecule_id]
        elif mol.gated_out:
            factors["combo_adjustment"] = mol.gate_reason or "gated_out"
        else:
            factors["combo_adjustment"] = "not_selected_below_top_n"
        reason = format_selection_reason(factors)
        mol.selection_factors = factors
        mol.selection_reason = reason
        selection_audit.append(
            {
                "molecule_id": mol.molecule_id,
                "outcome": "not_selected",
                "selection_factors": factors,
                "selection_reason": reason,
                "final_score": mol.final_score,
                "eligibility_status": mol.eligibility_status,
            }
        )
    # 资格失败但曾进入 eligible 池前列的候选也记一笔（便于对照）
    for mol in eligible[: max(n, deep_m)]:
        if mol.molecule_id in selected_ids or mol.molecule_id in shortlist_ids:
            continue
        factors = dict(mol.selection_factors or {})
        factors["eligibility"] = mol.eligibility_status
        factors["score"] = f"{mol.final_score:.4f}"
        factors["combo_adjustment"] = factors.get("combo_adjustment") or "dropped_before_diversity"
        selection_audit.append(
            {
                "molecule_id": mol.molecule_id,
                "outcome": "not_selected",
                "selection_factors": factors,
                "selection_reason": format_selection_reason(factors),
                "final_score": mol.final_score,
                "eligibility_status": mol.eligibility_status,
            }
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
    if diag.notes:
        log.emit(
            "INFO" if q_pass else "WARN",
            f"诊断备注：{' | '.join(diag.notes)}",
            f"Diagnostics notes: {' | '.join(diag.notes)}",
            progress=98,
        )
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

    input_hash = sha256_file(input_path)
    selection_hash = selection_sha256(top)
    run_id = deterministic_run_id(
        input_sha256=input_hash,
        config_hash=config.config_hash,
        selection_hash=selection_hash,
    )
    mechanism_context, mechanism_context_sha256 = load_mechanism_context()
    mechanism_graphs = build_mechanism_graphs(
        top,
        context=mechanism_context,
        context_sha256=mechanism_context_sha256,
    )
    hepg2_ffa_resources = resource_registry_runtime_payload()

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
        review_required_count=review_required_count,
        screening_audit=screening_audit,
        logs=log.as_dicts(),
        rank_robustness=rank_robustness,
        input_sha256=input_hash,
        selection_sha256=selection_hash,
        run_id=run_id,
        mechanism_graphs=mechanism_graphs,
        hepg2_ffa_resources=hepg2_ffa_resources,
        scored_molecules=scored,
        selection_audit=selection_audit,
    )


def run_pipeline(
    input_path: Path,
    output_path: Path,
    *,
    mode: str = "offline",
    top_n: int | None = None,
    write_mechanism: bool = True,
) -> PipelineResult:
    started_at = datetime.now(timezone.utc)
    started_clock = time.monotonic()
    cfg = load_config(mode=mode)
    result = screen_sdf(input_path, cfg=cfg, top_n=top_n, source_filename=input_path.name)
    export_nomination_csv(
        result.top_molecules,
        output_path,
        mode=cfg.mode,
        config_hash=cfg.config_hash,
        degraded_channels=cfg.degraded_channels,
        requested_top_n=result.requested_top_n,
        run_id=result.run_id,
        input_sha256=result.input_sha256,
        selection_hash=result.selection_sha256,
    )
    audit_path = Path(output_path).with_suffix(".screening_audit.csv")
    export_screening_audit_csv(result.screening_audit, audit_path)
    critic_path = Path(output_path).with_suffix(".critic_audit.csv")
    export_critic_audit_csv(result.critic_actions, critic_path)
    robustness_path = Path(output_path).with_suffix(".rank_robustness.json")
    export_rank_robustness_json(result.rank_robustness, robustness_path)
    candidate_scores_path = Path(output_path).with_suffix(".candidate_scores.jsonl")
    export_candidate_scores_jsonl(result.scored_molecules, candidate_scores_path)
    evidence_ledger_path = Path(output_path).with_suffix(".evidence_ledger.jsonl")
    export_evidence_ledger_jsonl(result.scored_molecules, evidence_ledger_path)
    citations_path = Path(output_path).with_suffix(".citations.jsonl")
    export_citations_jsonl(result.scored_molecules, citations_path)
    selection_audit_path = Path(output_path).with_suffix(".selection_audit.jsonl")
    export_selection_audit_jsonl(result.selection_audit, selection_audit_path)
    graph_path = Path(output_path).with_suffix(".mechanism_graph.json")
    from services.pipeline.export import export_mechanism_graph_json

    export_mechanism_graph_json(result.mechanism_graphs, graph_path)
    resource_path = Path(output_path).with_suffix(".hepg2_ffa_resources.json")
    from services.pipeline.export import export_hepg2_ffa_resources_json

    export_hepg2_ffa_resources_json(result.hepg2_ffa_resources, resource_path)
    if write_mechanism and result.top_molecules:
        from services.mechanism import render_mechanism_markdown

        mech_path = Path(output_path).with_suffix(".mechanism.md")
        # 机制 LLM 仅润色 Markdown；绝不回写分数或重排 Top 10
        render_mechanism_markdown(
            result.top_molecules,
            mech_path,
            llm_cfg=result.config.llm,
            mark_degraded=result.config.mark_degraded,
            assumptions=result.config.assumptions,
            run_context={
                "run_id": result.run_id,
                "input_sha256": result.input_sha256,
                "config_hash": result.config.config_hash,
                "selection_sha256": result.selection_sha256,
            },
            mechanism_graphs=result.mechanism_graphs,
        )
    from services.pipeline.manifest import write_run_manifest

    manifest_path = write_run_manifest(
        input_path=input_path,
        output_path=Path(output_path),
        cfg=cfg,
        started_at=started_at,
        runtime_seconds=time.monotonic() - started_clock,
        run_id=result.run_id,
        selection_hash=result.selection_sha256,
        top_molecules=result.top_molecules,
    )
    result.manifest_path = str(manifest_path)
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
