"""services.pipeline — 编排与配置导出。"""

from services.pipeline.config_loader import AppConfig, ConfigLoadError, load_config
from services.pipeline.export import (
    CSV_COLUMNS,
    SCREENING_AUDIT_COLUMNS,
    export_critic_audit_csv,
    export_hepg2_ffa_resources_json,
    export_nomination_csv,
    export_screening_audit_csv,
    export_rank_robustness_json,
    to_csv_text,
)


def __getattr__(name: str):
    if name in ("PipelineResult", "run_pipeline", "screen_sdf"):
        from services.pipeline import runner as _runner

        return getattr(_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AppConfig",
    "CSV_COLUMNS",
    "SCREENING_AUDIT_COLUMNS",
    "ConfigLoadError",
    "PipelineResult",
    "export_nomination_csv",
    "export_critic_audit_csv",
    "export_hepg2_ffa_resources_json",
    "export_screening_audit_csv",
    "export_rank_robustness_json",
    "load_config",
    "run_pipeline",
    "screen_sdf",
    "to_csv_text",
]
