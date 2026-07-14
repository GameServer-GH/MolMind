"""services.pipeline — 编排与配置导出。"""

from services.pipeline.config_loader import AppConfig, ConfigLoadError, load_config
from services.pipeline.export import CSV_COLUMNS, export_nomination_csv, to_csv_text


def __getattr__(name: str):
    if name in ("PipelineResult", "run_pipeline", "screen_sdf"):
        from services.pipeline import runner as _runner

        return getattr(_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AppConfig",
    "CSV_COLUMNS",
    "ConfigLoadError",
    "PipelineResult",
    "export_nomination_csv",
    "load_config",
    "run_pipeline",
    "screen_sdf",
    "to_csv_text",
]
