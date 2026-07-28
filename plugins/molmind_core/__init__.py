"""molmind-core Plugin：Tools/Skills 包装现有 services（R2 再搬迁 scientific）。"""

from plugins.molmind_core.tools.scientific import run_score_and_rank, timed_call

__all__ = ["run_score_and_rank", "timed_call"]
