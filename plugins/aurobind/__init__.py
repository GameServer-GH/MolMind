"""AuroBind Catalog 适配器（默认不启用；需靶点序列 + GPU，须主动添加）。"""

from __future__ import annotations

from plugins.aurobind.tools.fitness import predict_pl_fitness, run_enrichment_pass

__all__ = ["predict_pl_fitness", "run_enrichment_pass"]
