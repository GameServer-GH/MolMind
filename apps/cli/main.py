"""MolMind CLI：SDF → Top 10 CSV（Quality-Max 默认 auto）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.goldset import load_goldset
from services.eval_harness import run_goldset_harness
from services.evidence_facade.bake import (
    bake_from_sdf,
    bake_frozen_top10,
    bake_submission_evidence,
)
from services.pipeline import load_config, run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MolMind: SDF → Top 10 CSV")
    parser.add_argument("--input", "-i", help="输入 .sdf 路径")
    parser.add_argument(
        "--output",
        "-o",
        default="output/nomination_top10.csv",
        help="输出 CSV 路径",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "online", "offline"],
        default="auto",
        help="auto=Quality-Max（默认）；offline/online 仅调试",
    )
    parser.add_argument("--top", type=int, default=None, help="Top N（默认读配置）")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--eval-goldset", action="store_true", help="仅跑 GoldSet 回归")
    parser.add_argument(
        "--bake-evidence",
        action="store_true",
        help="有网时把短名单的 ChEMBL/PubChem 证据烘焙到 data/evidence_snapshot/",
    )
    parser.add_argument(
        "--bake-top-m",
        type=int,
        default=None,
        help="bake-evidence 短名单大小（默认读配置 bake_top_m）",
    )
    parser.add_argument(
        "--bake-force",
        action="store_true",
        help="即使 snapshot 已有该 InChIKey 也重新拉取（写后自动压缩覆盖旧行）",
    )
    parser.add_argument(
        "--bake-frozen-top10",
        action="store_true",
        help="仅烘焙已冻结的 Top 10 实体，不重新筛选候选",
    )
    parser.add_argument(
        "--bake-submission",
        action="store_true",
        help="一次烘焙冻结 Top 10 与 Top-M 候选窗口，供日常 auto 完全复用",
    )
    parser.add_argument(
        "--bake-output",
        default=None,
        help="证据 JSONL 输出路径（默认 data/evidence_snapshot/baked_evidence_v2.jsonl）",
    )
    parser.add_argument(
        "--compact-snapshot",
        action="store_true",
        help="压缩 data/evidence_snapshot/*.jsonl：同 InChIKey+adapter+query_type 保留最后一条",
    )
    parser.add_argument("--no-mechanism", action="store_true")
    args = parser.parse_args(argv)

    if args.eval_goldset:
        cfg = load_config(mode=args.mode, seed=args.seed)
        gold = load_goldset()
        result = run_goldset_harness(cfg, gold)
        print(
            json.dumps(
                {"passed": result.passed, "messages": result.messages},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.passed else 2

    if args.compact_snapshot:
        from services.evidence_facade.snapshot_compact import compact_all_snapshots

        stats = compact_all_snapshots(backup=True)
        print(json.dumps([s.__dict__ for s in stats], ensure_ascii=False, indent=2))
        return 0

    if args.bake_evidence:
        if not args.input and not args.bake_frozen_top10:
            parser.error("--bake-evidence 需要 --input")
        output_path = Path(args.bake_output) if args.bake_output else None
        if args.bake_submission:
            if not args.input:
                parser.error("--bake-submission 需要 --input")
            stats = bake_submission_evidence(
                Path(args.input),
                top_m=args.bake_top_m,
                output_path=output_path,
                skip_cached=not args.bake_force,
            )
        elif args.bake_frozen_top10:
            stats = bake_frozen_top10(
                output_path=output_path,
                skip_cached=not args.bake_force,
            )
        else:
            stats = bake_from_sdf(
                Path(args.input),
                top_m=args.bake_top_m,
                output_path=output_path,
                skip_cached=not args.bake_force,
            )
        print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
        return 0

    if not args.input:
        parser.error("--input 必填（或使用 --eval-goldset / --bake-evidence）")

    try:
        result = run_pipeline(
            Path(args.input),
            Path(args.output),
            mode=args.mode,
            top_n=args.top,
            write_mechanism=not args.no_mechanism,
        )
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(f"mode={result.config.mode} config_hash={result.config.config_hash}")
    print(f"SDF 记录: {result.raw_count}（解析跳过 {result.parse_skipped}）")
    print(f"有效分子: {result.input_count}")
    print(f"硬过滤剔除: {result.filtered_out}")
    print(f"门控后候选: {result.eligible_count}")
    print(f"输出 Top {result.output_count} → {args.output}")
    print(
        f"diagnostics: std_tox={result.diagnostics.std_tox} "
        f"scaffolds={result.diagnostics.scaffold_diversity_top10} "
        f"engineering_pass={result.diagnostics.engineering_pass} "
        f"scientific_validation={result.diagnostics.scientific_validation_status}"
    )
    resource_counts = result.hepg2_ffa_resources.get("resource_counts", {})
    print(
        "hepg2_ffa_resources: "
        f"total={resource_counts.get('total', 0)} "
        f"mechanistic_context={resource_counts.get('mechanistic_context', 0)} "
        f"assay_qc={resource_counts.get('assay_qc', 0)} "
        f"dual_endpoint_training_eligible="
        f"{resource_counts.get('candidate_dual_endpoint_training_eligible', 0)} "
        "ranking_effect=none"
    )
    if result.config.degraded_channels:
        print(f"degraded_channels: {', '.join(result.config.degraded_channels)}")
    if result.note:
        print(f"提示: {result.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
