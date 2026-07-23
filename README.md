# MolMind · 分子思维

<p align="center">
  <img src="apps/web/static/MolMindIntroduction.png" alt="MolMind — 可审计计算候选优先级系统" width="100%" />
</p>

<p align="center">
  <strong>分子思维（MolMind）</strong> · 可审计的计算候选优先级系统 · MASLD / HepG2-FFA<br />
  公开实验数据 · 毒理证据 · 多组学机制上下文<br />
  <a href="README.en.md">English</a>
</p>

---

## 项目做什么

**分子思维（MolMind）** 是面向 **MASLD / HepG2-FFA** 场景的**可审计计算候选优先级系统**：输入化合物库（单个 `.sdf`），基于公开活性、毒理与多组学机制证据，输出可复现、可追溯的优先短名单与机制假说，供实验优先验证。

它**不是**已通过 HepG2-FFA 湿实验验证的降脂/低毒预测器。Top N 属于计算优先级层；科学声明必须受 `scientific_status` / `claim_ceiling` 约束。

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
| **联网补证据** `allow_live` | 关 | ChEMBL/PubChem live 补洞（仅短名单） |

推荐默认：**快照开 + 联网关**（可复现）。需要补证据时先 `bake-evidence` 或临时开联网，烘焙后再关联网复跑。

兼容：`--mode online` / `mode=online` 等价于 `allow_live=true`；`offline` 仅作旧别名，不再单独代表算法路径。

### 七步 Agent 流水线

不是「一个脚本打分出 CSV」，而是分阶段、可观测、可审计的优先级编排流水线：

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

统一门面 `EvidenceFacade.query()`：

- 适配器版本化（主路径默认 `chembl_lipid_v1` + `pubchem_tox_v1`）  
- 结果写入 `attributions[]` / `evidence_id`，可追溯到 CSV 依据列  
- `prefer_snapshot=true`：命中本地快照则跳过外网  
- 显式 `online` 时启用 HTTP 超时 + 连续失败熔断；失败记入 `degraded_channels[]`，不崩库、不瞎编高分

### 可复现与可审计

| 机制 | 作用 |
|------|------|
| `config_hash` | 对配置、模型文件、GoldSet/通路表、快照内容和核心算法实现做稳定哈希，写入 CSV 与 API |
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
docker pull --platform linux/amd64 8.133.197.65:5001/molmind:0.1.1
docker tag 8.133.197.65:5001/molmind:0.1.1 molmind:0.1.1
mkdir -p output
docker compose -f deploy/docker-compose.yml up -d
```

本地访问 <http://127.0.0.1:18765/>（健康检查：`/health`）。请勿用 `file://` 直接打开静态页。

仅开发调试时再现场构建：

```bash
docker compose -f deploy/docker-compose.yml up --build
```

CLI 一键示例：

```bash
python -m apps.cli.main --input data/sample.sdf --output output/nomination_top10.csv
```

Docker 内离线 CLI 冒烟：

```bash
docker compose -f deploy/docker-compose.yml run --rm cli
```

机制 Markdown 默认使用**准确模板**生成（**不改 Top 10**）；`llm_client` 保留兼容位，不作为默认主路径。

---

## 仓库结构

| 路径 | 说明 |
|------|------|
| `apps/` | API、CLI、Web 静态前端 |
| `services/` | 流水线与评分 / 证据 / Critic / 机制等服务 |
| `packages/` | 化学核心、金标、可选 ML、数据模型等共享包 |
| `configs/` | 过滤、打分、排序权重与模型清单（纳入 `config_hash`） |
| `data/` | 样本 SDF、goldset、证据快照、`public/` 注册表与导入工作区、参考表、可选模型 |
| `deploy/` | Dockerfile、Compose、部署说明 |
| `scripts/` | 冒烟、配置门禁、证据烘焙等工具 |
| `tests/` | 单元 / 回归 / 集成测试（pytest） |

---

## 测试

```bash
pytest
```
