# packages/ml_optional

可选本地 **DILI / ADMET** ECFP k-NN 头；无模型文件则跳过并标记 `dili_ml_missing` / `admet_ml_missing`。

- 邻居未命中：run 级写入 `AppConfig.finalize_ml_run_stats()` → `diagnostics.notes`，**不**进 `degraded_channels`。
- **降脂 ML**：默认未启用（`lipid_fuse.ml=0`）；接线方案见 `docs/proposals/P0-P1-计算层改动清单.md` P0-A2。
- 清单：`configs/model_manifest.json`；烘焙：`scripts/build_ml_heads.py`。
