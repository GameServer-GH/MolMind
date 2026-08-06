# MolMind · 分子思维

<p align="center">
  <img src="apps/web/static/MolMindIntroduction.png" alt="MolMind — 可审计计算候选优先级系统" width="100%" />
</p>

<p align="center">
  <strong>分子思维（MolMind）</strong> · AI Agent 驱动的可审计候选优先级系统 · MASLD / HepG2-FFA<br />
  对话 / Skills 编排 · 公开实验数据 · 毒理证据 · 多组学机制上下文<br />
  <a href="README.en.md">English</a>
</p>

---

## 项目做什么

**分子思维（MolMind）** 是面向 **MASLD / HepG2-FFA** 场景的 **AI Agent 驱动候选清单平台**：上传化合物库（单个 `.sdf`）后，用自然语言或 Skill 驱动内置科学核，基于公开活性、毒理与多组学机制证据，输出可复现、可追溯的优先短名单、机制假说与交卷包，供实验优先验证。

排名与毒性门控由确定性科学核 `molmind-core` 写出；LLM / 可选 Catalog 插件**不得**私改主榜。它**不是**已通过 HepG2-FFA 湿实验验证的降脂/低毒预测器。Top N 属于计算优先级层；科学声明必须受 `scientific_status` / `claim_ceiling` 约束。

### 要解决的问题

细胞实验里「脂滴变少」并不等于有效命中——若细胞活力受损，脂质下降可能只是死亡假象。MolMind 因此采用：

> **有效命中 = 降脂 ∧ 低毒**  
> 只降脂、高毒性 = **假阳性**（应优先拦截，而非事后补救）

### 计算层与实验层分轨

| 实验层（湿实验验证） | 计算层代理（MolMind） |
|----------------------|------------------------|
| 脂质蓄积下降 | 提高 `S_lipid`（公开活性 + 结构代理） |
| 细胞活力 ≥ 80%（项目暂定实验对齐参考） | 降低 `R_tox` + 毒性硬门控 |
| 有效命中名单 | 过门控后按 `S_final` 输出 Top N 优先名单 |
| SI / EC₅₀ / CC₅₀ | **不计算、不写入 CSV**（避免无剂量–反应数据时的伪精确） |

也就是说：MolMind 负责 **可复现、可审计的计算优先级**；最终效力与安全边界由 **HepG2-FFA 平行双终点**（及可选 SI 确认）在湿实验确认。

### 公开数据导入优先级

策略与契约见 [`data/public/registry.yaml`](data/public/registry.yaml)：

| 波次 | 数据源 | 用途边界 |
|------|--------|----------|
| **1 活性** | ChEMBL → PubChem BioAssay → BindingDB | 候选级终点证据 / 机制支持；库存在 ≠ 有效 |
| **2 毒理** | ToxCast/Tox21 → DILIrank 2.0 → ToxRef/ToxVal | 仅风险信号；无记录 ≠ 低毒 |
| **3 多组学** | GEO → PRIDE → 代谢组 → LINCS/CMap | 仅机制/QC 上下文；默认不进排序分 |

当前快照（详见 [`data/public/README.md`](data/public/README.md)）：PubChem **176** → QC **26**；ChEMBL **137** → QC **117**（含 **59** HepG2-FFA 阳性行 / 19 seed assays）；BindingDB **74** → QC **71**；ToxCast/CTX **9** → QC **7** active（`risk_signal` only）；DILIrank 已导入。EvidenceFacade 按 InChIKey 合并 QC 表；PubChem Active / BindingDB 不抬降脂 `conf_e`；ToxCast 活性 hit 不抬安全清除。缺失/失败一律 `audit_missing` / `network_error` / `auth_missing`，绝不写成阴性或低毒。

---

## 如何做到

### Quality-Max：一条主路径 + 两个运行开关

对外只有 **Quality-Max**（`mode=auto`），不再区分 Online / Offline 模式入口：

```text
冻结的本地证据快照 → 规则 / GoldSet / 可选 ML → Critic → Top 10
任一通道失败 → 自动降级并写入 degraded_channels[] → 仍输出确定性名单
```

| 开关 | 默认 | 含义 |
|------|------|------|
| **使用快照** `use_snapshot` | 开 | 读取 `data/evidence_snapshot/` |
| **显式联网补证** `allow_live` | 关 | 只授权 `query_evidence` / `bake-evidence` enrichment；不进入同轮排名 |

推荐默认：**快照开 + 联网关**（可复现）。需要补证据时显式运行
`query_evidence(..., allow_live=True)` 查看只读证据卡，或运行 `bake-evidence`
规范化并冻结结果；随后用新快照离线复跑。筛选流水线即使收到 `allow_live=true`，
也只记录 enrichment 授权，不让本轮 HTTP 响应进入评分对象。

兼容：`--mode online` / `mode=online` 仍解析为 `allow_live=true`，但不会恢复隐式
联网排名；`offline` 仅作旧别名，不再单独代表算法路径。

### 七步科学核（由 Agent Tool 调用）

不是「一个脚本打分出 CSV」，而是分阶段、可观测、可审计的优先级编排流水线；评委主路径经 Agent Skill（如 `masld_nominate`）调用 `score_and_rank`，亦可 CLI / `/api/screen*` 直跑同一核：

| 步骤 | 模块 | 做什么 |
|------|------|--------|
| 1 读库 | `ingest` | SDF 流式解析 → 描述符 / 指纹 / InChIKey |
| 2 初筛 | `hard_filter` | Ro5 复核、分级警示 SMARTS、专家极端红线硬过滤 |
| 3 降脂评分 | `scorer_lipid` | 多信号融合得到 `S_lipid`，并保留归因 |
| 4 毒性评分 | `scorer_tox` | 多维融合得到 `R_tox`，硬门控直接淘汰高危分子 |
| 5 综合排序 | `ranker` | 公开公式算 `S_final` + Murcko 骨架多样性限额 |
| 6 自我反思 | `critic` | 对照 GoldSet 踢出「不是新发现」或高风险类似物 |
| 7 生成输出 | `export` + `mechanism` | Nomination CSV + 筛选审计 CSV + 机制 Markdown/PDF |

### 降脂：多信号融合，而非单一启发式

`S_lipid` 默认融合（权重纳入 `config_hash`）：

| 信号 | 默认权重 | 含义 |
|------|:--------:|------|
| 规则 / 药效团 | 0.35 | DNL / FAO / AMPK 等通路相关 SMARTS 启发 |
| 阳性相似 | 0.30 | 与 GoldSet 降脂低毒阳性对照的 Tanimoto 相似 |
| 外部证据 | 0.25 | 经 Evidence Facade（如 ChEMBL）查证 |
| 可选 ML | 0.00 | 当前未接入已验证降脂模型，不制造恒零通道；接口保留 |

空证据不会被抬成高分；信号缺失可审计。

### 毒性：假阳性优先拦截

`R_tox` 由警示结构、DILI、ADMET、物化风险、毒理证据等独立多头融合；采用“最高风险头 + 加权上下文”的单调聚合，并对低置信度增加保守惩罚。对 GoldSet 肝毒类似物可做相似抬升。默认：

- **硬门控** `R_tox >= 0.65` → 直接淘汰
- **自动入选上限** `R_tox < 0.45`；`0.45–0.65` → `review_required`，不得自动进入 Top 10
- **科学状态独立输出**：结构/理化代理可进入 `proxy_only` 计算优先级层，但没有直接安全证据时 `safety_clearance_confidence=0`，不得写成“低毒”
- 实验口径 `viability_proxy = 0.80` 用于方法学对齐说明，**不参与伪造 SI**

毒性通道是一等公民：脂滴下降若伴随高毒风险，不会因为「降脂分好看」蒙混进榜。

### 综合排序：效力 × 安全 × 新颖性 × 证据置信

```text
S_final = 0.40·S_lipid + 0.40·(1 − R_tox) + 0.10·novelty + 0.10·conf_e
```

- **新颖性**：近阳性相似会压低 `novelty`，抑制 me-too 刷榜  
- **骨架多样性**：Murcko 骨架按席位限额（默认每骨架 1 席），避免同核扎堆占满 Top 10  
- **证据置信** `conf_e`：反映取证质量均值，**不等于**生物学真实新颖性

### Critic：Agent 的自我反思环

规则 Critic 默认启用，典型动作包括：

- 与库内阳性对照几乎等同 → 移出（那是对照药，不是新发现）  
- 过近阳性 → 软踢出 Top，替补异骨架候选  
- 假阳性 / 肝毒高相似且高 `R_tox` → 剔除  

LLM Critic 架构支持证据约束（只能引用本 Run 已出现的 `evidence_id`）；默认 **不改排名**。机制 LLM 更只负责润色 Markdown，**排名冻结后才生成**。

### Evidence Facade：会用工具的取证层

本地科学门面 `EvidenceFacade.query()`：

- 适配器版本化（主路径默认 `chembl_lipid_v1` + `pubchem_tox_v1`）  
- 结果写入 `attributions[]` / `evidence_id`，可追溯到 CSV 依据列  
- `prefer_snapshot=true`：确定性重放本地快照与公开 QC 表
- `query()` 默认且在排名链路中始终 `allow_live=false`；HTTP 只由统一 Evidence Gateway
  在显式 Tool / bake enrichment 中调用，并按 provider 独立限流、超时与熔断
- EPA CTX 的 live 精确 InChIKey 查询只会按既有 cytotox 阈值产生风险信号；CAS/多
  DTXSID 只提示身份复核。BindingDB 与 GEO/PRIDE/代谢组仍是本地机制/QC 上下文，分数为 0。
- live 结果不会写入当前主榜；必须经 `bake-evidence` 规范化、审计并冻结到 snapshot，后续离线
  Run 才可能读取它。EPA live 凭据优先使用 `CTX_API_KEY`、`CTX_API_KEY_FILE` 或
  OS keychain，缺省时使用发行内置的公开受限 key。该 key 与机制 LLM 的发行
  默认 key 均由服务商侧限权、限额并可即时吊销，不得授予管理权限或生产数据
  访问权限；环境变量仍可覆盖，`MOLMIND_USE_EMBEDDED_PUBLIC_KEYS=0` 和
  `MOLMIND_LLM_USE_EMBEDDED=0` 可分别禁用默认 key。

### 候选证据查询：独立、只读、local-first

内置 Tool `query_evidence` 可按当前筛选 Run 的 `molecule_id`，或按完整的
InChIKey / CAS / SMILES 身份单独查询候选证据。它只生成结构化
`EvidenceBundle` 与证据卡，不重排候选、不写主榜，Registry 中保持
`writes_selection=false`。

查询顺序固定为：

```text
冻结快照
  → 本地公开数据 / QC 表
  → Evidence Gateway 查询状态缓存
  → 仅 allow_live=true 时访问远端
  → 规范化 EvidenceBundle
  → 写入查询状态与审计记录
```

默认 `allow_live=false`。`force_refresh=true` 只表示在显式允许联网时忽略可刷新的
缓存决策；它本身不会开启网络。远端补证据属于 enrichment，本次查询产生的证据卡
不会改变原 Run 的 `S_lipid`、`R_tox`、`novelty`、`conf_e`、Top N 或
`selection_sha256`。若证据未来需要参与排名，必须先规范化、审计并冻结为 snapshot，
再离线复跑科学核。

身份优先级为原始 InChIKey → 标准化 InChIKey → CAS → 标准化 SMILES。
原始与标准化 InChIKey 不同只有在当前 Run 保留明确 `standardization_steps` 时才可接受；
缺少标准化轨迹、CAS 绑定到其他结构、多 CID 或 provider compound identity 漂移都会进入
`identity_review_required`，并阻止效力、创新性与安全置信度提升。既有项目规则只允许在
审计清楚时保守传播毒性风险。

证据卡会报告实际采用的 `lookup_field` / `lookup_value` / `match_type`、命中来源、
未查询/空结果/失败/缺凭据来源、评分证据与注释证据的区别、身份冲突、
`claim_ceiling` 以及是否建议显式联网。规范查询状态为 `hit`、
`verified_empty`、`query_failed`、`auth_missing`、`not_queried`、
`identity_review_required`、`annotation_only`；其中空结果、失败和缺凭据均不是
生物学阴性、无毒或无效。

离线查询一个已冻结身份：

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
from plugins.molmind_core.tools.scientific import run_query_evidence

out = run_query_evidence(
    inchikey="PCZOHLXUXFIOCF-BXMDZJJMSA-N",
    allow_live=False,
)
print(out.card)
PY
```

显式开启 live enrichment（远端失败会写入降级审计，其他通道仍可返回）：

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
from plugins.molmind_core.tools.scientific import run_query_evidence

out = run_query_evidence(
    inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    cas="64-17-5",
    smiles="CCO",
    providers=["chembl", "pubchem"],
    allow_live=True,
    force_refresh=True,
)
print(out.card)
PY
```

更完整的缓存、身份和状态契约见
[`data/public/EVIDENCE_GATEWAY.md`](data/public/EVIDENCE_GATEWAY.md)。

### 可复现与可审计

| 机制 | 作用 |
|------|------|
| `config_hash` | 对配置、模型文件、GoldSet/通路表、快照内容和 `plugins/molmind_core/scientific/` canonical 核心实现做稳定哈希，写入 CSV 与 API |
| 证据快照 | 短名单可预烘焙进 `data/evidence_snapshot/`，镜像内离线复现 |
| `degraded_channels[]` | 如 `evidence_empty`、`lipid_ml`、熔断等，运行降级全程可审计 |
| 归因列 | 降脂 / 毒性判断依据、`overall_reason` 可回溯到信号与证据 ID |

复现口径：**同 SDF + 同 `config_hash` + 同 snapshot → 同一 Top 10 ID 与分数**。

---

## 得到什么结果

### 运行产出

| 产出 | 内容 |
|------|------|
| **Nomination Top N CSV** | 默认可为 Top 10：候选 ID、分项分、排序分、判断依据、`config_hash`、降级通道等 |
| **机制 Markdown/PDF** | 区分相似性推断、结构推断与证据不足的可检验假说；无候选级证据时明确标记 `UNRESOLVED` |
| **实时诊断日志** | Web / `POST /api/screen/stream`（NDJSON）/ CLI 分阶段进度，过程可看见 |
| **候选评分 JSONL** | 所有进入评分阶段的候选、计算资格、科学状态、审计缺口与适用域 |
| **证据账本 JSONL** | 每条证据的来源 URL、查询状态、方向、时间、响应哈希、许可证与声明上限 |
| **引用 / 入选审计 JSONL** | 候选级 citation 清单；入选与落榜的结构化 `selection_factors` |

公开数据工作区与导入波次见 [`data/public/README.md`](data/public/README.md)。

### 使用入口（同一套配置）

| 入口 | 用途 |
|------|------|
| Web | 浏览器上传 SDF，实时看日志与 Nomination |
| API | 可编程筛选、流式日志、下载结果 |
| CLI | 批处理一键出 CSV |
| Docker Compose | 一键拉起上述能力，本地与部署环境同路径复现 |

### 设计目标（结果画像）

一次成功运行期望得到的不是「分数最高的 10 个类似物」，而是：

1. **过毒性门控**、证据状态可审计的计算优先短名单  
2. **骨架多样**、相对正向对照具有新颖性空间的 Top N  
3. **每条结果可解释**（assay/证据 ID、科学声明上限、机制假说可写进验证方案）  
4. **断网亦可复现** 的确定性名单（依赖快照与配置指纹）

机制文档对齐湿实验叙事：HepG2-FFA 平行双终点（脂滴 ↓ ∧ 活力 ≥ 80%），可选 SI 仅作后续确认协议——**不反向改写计算榜**。不得把计算资格写成「已验证低毒/降脂」。

---

## 快速开始

### 在线试用（无需本地部署）

浏览器直接打开：**[https://molmind.cn/](https://molmind.cn/)**  

上传 `.sdf` 即可筛选；健康检查：<https://molmind.cn/health>。

### 本地部署

完整步骤（国内优先 NAS 镜像仓库 / ghcr / 本地构建 / 纯 Python）见 [deploy/README.md](deploy/README.md)。

国内推荐（拉取成品镜像后启动，勿依赖慢速外网构建）：

```bash
# Docker Engine 需一次性配置 insecure-registries: ["8.133.197.65:5001"]
docker pull --platform linux/amd64 8.133.197.65:5001/molmind:0.2.3
docker tag 8.133.197.65:5001/molmind:0.2.3 molmind:0.2.3
mkdir -p output
# Compose 文件在 deploy/，需显式传入仓库根 .env（可选 SCP_HUB_API_KEY 等）
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

本地访问 <http://127.0.0.1:18765/>（健康检查：`/health`）。请勿用 `file://` 直接打开静态页。Compose 默认拉起 **PostgreSQL + Redis**（Agent 会话 / 队列真源与短锁）；可选 `--profile object` 启用 MinIO 作 Blob。

仅开发调试时再现场构建：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

CLI 一键示例：

```bash
python -m apps.cli.main --input data/sample.sdf --output output/nomination_top10.csv
```

同次运行会在同目录生成 `nomination_reserve.csv`（或输入名前缀对应的
`*_nomination_reserve.csv`）。正式主榜保持 Top 10；候补默认最多 20 个，
仅在主榜候选不可采购、无法配制或身份复核失败时按冻结 `reserve_rank` 顺延。
在 Agent 中可说“导出 Top10 和候补名单”或“生成竞赛提交包，包含主榜和候补名单”。

Docker 内离线 CLI 冒烟：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml run --rm cli
```

机制 Markdown 默认使用**准确模板**生成（**不改 Top 10**）；`llm_client` 保留兼容位，不作为默认主路径。

---

## 0.2.3 Agent 能力摘要

| 能力 | 说明 |
|------|------|
| **持久化队列** | Turn 可排队（默认最多 3）；进程重启后可回收 lease / 继续 drain |
| **硬中断** | UI 停止按钮 → `interrupt`；后台机制 PDF / SCP Job 可取消 |
| **Turn 附件** | Blob 暂存，不改写进行中的 Run；排队卡片展示文件摘要 |
| **SCP Hub（可选）** | 白名单 MCP enrichment；`writes_selection=false`，永不参与同轮排名 |
| **任务路由** | 多能力意图拆分与执行门禁；观测经 validator 后再进回复 |

界面与 API 细节见 [`apps/web/README.md`](apps/web/README.md)、[`apps/api/README.md`](apps/api/README.md)；SCP 边界见 [`plugins/scp_hub/README.md`](plugins/scp_hub/README.md)。

Agent 对话与路由演进请遵循设计原则：  
[`docs/agent-design-principles.md`](docs/agent-design-principles.md)（**状态进上下文，决策留给模型**；禁止再堆关键词规则表）。

---

## 仓库结构

| 路径 | 说明 |
|------|------|
| `apps/` | API、CLI、Web 静态前端 |
| `agent/` | Agent 运行时：决策环、Turn 队列、任务路由、Postgres/Redis/Blob 存储 |
| `docs/` | 项目设计原则与架构约定 |
| `plugins/molmind_core/` | 流水线、评分、证据 Gateway / Facade、Critic、机制与 Tool 的 canonical 实现 |
| `plugins/scp_hub/` | 可选 SCP Hub MCP 边界（Catalog opt-in；不改主榜） |
| `services/` | 指向 canonical 实现的向后兼容 shim；新科学逻辑不放在此处 |
| `packages/` | 化学核心、金标、可选 ML、数据模型等共享兼容入口 |
| `configs/` | 过滤、打分、排序权重与模型清单（纳入 `config_hash`） |
| `data/` | 样本 SDF、goldset、证据快照、`public/` 注册表与导入工作区、参考表、可选模型 |
| `deploy/` | Dockerfile、Compose（含 postgres/redis）、部署说明 |
| `scripts/` | 冒烟、配置门禁、证据烘焙等工具 |
| `tests/` | 单元 / 回归 / 集成测试（pytest） |

---

## 测试

```bash
pytest
```
