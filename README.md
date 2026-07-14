# MolMind

<p align="center">
  <img src="apps/web/static/MolMindIntroduction.png" alt="MolMind — Quality-Max 化合物发现流水线" width="100%" />
</p>

<p align="center">
  <strong>新型低毒降脂化合物发现</strong> · Quality-Max 正式版<br />
  科学发现型 AI Agent 流水线<br />
  <a href="README.en.md">English</a>
</p>

---

## 项目做什么

MolMind 是一套面向 **MASLD / HepG2-FFA** 场景的计算筛选系统：输入组委会化合物库（单个 `.sdf`），自动提名一批 **降脂潜力高、毒性风险低、化学空间相对新颖** 的候选分子，并附带可审查的打分依据与机制假说，供湿实验优先验证。

### 要解决的问题

细胞实验里「脂滴变少」并不等于有效命中——若细胞活力受损，脂质下降可能只是死亡假象。赛题口径因此定义为：

> **有效命中 = 降脂 ∧ 低毒**  
> 只降脂、高毒性 = **假阳性**（应优先拦截，而非事后补救）

### 计算层与实验层分轨

| 实验层（组委会验证） | 计算层代理（MolMind） |
|----------------------|------------------------|
| 脂质蓄积下降 | 提高 `S_lipid` |
| 细胞活力 ≥ 80% | 降低 `R_tox` + 毒性硬门控 |
| 有效命中名单 | 过门控后按 `S_final` 提名 Top N |
| SI / EC₅₀ / CC₅₀ | **不计算、不写入 CSV**（避免无剂量–反应数据时的伪精确） |

也就是说：MolMind 负责 **可复现的计算提名**；最终效力与安全边界由 **HepG2-FFA 平行双终点**（及可选 SI 确认）在湿实验确认。

---

## 如何做到

### Quality-Max：一条主路径，自动适配环境

对外默认 `mode=auto`（Quality-Max），不必在 Online / Offline 间二选一：

```text
本地证据快照 →（短名单）live 补洞 → 规则 / GoldSet / 可选 ML → Critic → Top 10
任一通道失败 → 自动降级并写入 degraded_channels[] → 仍输出确定性名单
```

有网时可用 live 证据增强；断网或接口熔断时依赖已烘焙快照，仍能高质量复现同一短名单。

### 七步 Agent 流水线

不是「一个脚本打分出 CSV」，而是分阶段、可观测、可反思的科学发现 Agent：

| 步骤 | 模块 | 做什么 |
|------|------|--------|
| 1 读库 | `ingest` | SDF 流式解析 → 描述符 / 指纹 / InChIKey |
| 2 初筛 | `hard_filter` | Ro5、警示 SMARTS、专家红线硬过滤 |
| 3 降脂评分 | `scorer_lipid` | 多信号融合得到 `S_lipid`，并保留归因 |
| 4 毒性评分 | `scorer_tox` | 多维融合得到 `R_tox`，硬门控直接淘汰高危分子 |
| 5 综合排序 | `ranker` | 公开公式算 `S_final` + Murcko 骨架多样性限额 |
| 6 自我反思 | `critic` | 对照 GoldSet 踢出「不是新发现」或高风险类似物 |
| 7 生成输出 | `export` + `mechanism` | Nomination CSV + 机制 Markdown |

### 降脂：多信号融合，而非单一启发式

`S_lipid` 默认融合（权重纳入 `config_hash`）：

| 信号 | 默认权重 | 含义 |
|------|:--------:|------|
| 规则 / 药效团 | 0.35 | DNL / FAO / AMPK 等通路相关 SMARTS 启发 |
| 阳性相似 | 0.30 | 与 GoldSet 降脂低毒阳性对照的 Tanimoto 相似 |
| 外部证据 | 0.25 | 经 Evidence Facade（如 ChEMBL）查证 |
| 可选 ML | 0.10 | 无模型则动态剔除权重，并标记 `lipid_ml` 降级 |

空证据不会被抬成高分；信号缺失可审计。

### 毒性：假阳性优先拦截

`R_tox` 由警示结构、DILI、ADMET、物化风险、毒理证据等多头融合；对 GoldSet 肝毒类似物可做相似抬升。默认：

- **硬门控** `R_tox > 0.65` → 直接淘汰  
- **软阈值** `0.45` → 影响排序与审慎标记  
- 实验口径 `viability_proxy = 0.80` 用于方法学对齐说明，**不参与伪造 SI**

毒性通道是一等公民：脂滴下降若伴随高毒风险，不会因为「降脂分好看」蒙混进榜。

### 综合排序：效力 × 安全 × 新颖性 × 证据置信

```text
S_final = 0.40·S_lipid + 0.40·(1 − R_tox) + 0.10·novelty + 0.10·conf_e
```

- **新颖性**：近阳性相似会压低 `novelty`，抑制 me-too 刷榜  
- **骨架多样性**：Murcko 骨架按席位限额（交卷默认每骨架 1 席），避免同核扎堆占满 Top 10  
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
- HTTP 超时 + 连续失败熔断；失败记入 `degraded_channels[]`，不崩库、不瞎编高分  

### 可复现与可审计

| 机制 | 作用 |
|------|------|
| `config_hash` | 对打分 / 过滤 / 模型清单等配置做稳定哈希，写入 CSV 与 API |
| 证据快照 | 短名单可预烘焙进 `data/evidence_snapshot/`，镜像内离线复现 |
| `degraded_channels[]` | 如 `evidence_empty`、`lipid_ml`、熔断等，运行降级全程可审计 |
| 归因列 | 降脂 / 毒性判断依据、`overall_reason` 可回溯到信号与证据 ID |

验收口径：**同 SDF + 同 `config_hash` + 同 snapshot → 同一 Top 10 ID 与分数**。

---

## 得到什么结果

### 运行产出

| 产出 | 内容 |
|------|------|
| **Nomination Top N CSV** | 默认可为 Top 10：候选 ID、分项分、排序分、判断依据、`config_hash`、降级通道等 |
| **机制 Markdown** | 通路白名单锚定的可检验假说；默认 DeepSeek 润色，失败则模板降级 |
| **实时诊断日志** | Web / `POST /api/screen/stream`（NDJSON）/ CLI 分阶段进度，过程可看见 |

### 交付入口（同一套配置）

| 入口 | 用途 |
|------|------|
| Web | 浏览器上传 SDF，实时看日志与 Nomination |
| API | 可编程筛选、流式日志、下载结果 |
| CLI | 批处理一键出 CSV |
| Docker Compose | 一键拉起上述能力，评委 / 本地同环境复现 |

### 设计目标（结果画像）

一次成功运行期望得到的不是「分数最高的 10 个类似物」，而是：

1. **过毒性门控** 的降脂候选短名单  
2. **骨架多样**、相对正向对照具有新颖性空间的 Top N  
3. **每条提名可解释**（依据可追溯，机制假说可写进验证方案）  
4. **断网亦可复现** 的确定性名单（依赖快照与配置指纹）

机制文档对齐湿实验叙事：HepG2-FFA 平行双终点（脂滴 ↓ ∧ 活力 ≥ 80%），可选 SI 仅作后续确认协议——**不反向改写计算榜**。

---

## 快速开始

部署细节（macOS / Windows / 可选纯 Python）见 [deploy/README.md](deploy/README.md)。

```bash
docker compose -f deploy/docker-compose.yml up --build
```

浏览器打开 <http://127.0.0.1:18765/>（健康检查：`/health`）。请勿用 `file://` 直接打开静态页。

CLI 一键示例：

```bash
python -m apps.cli.main --input data/sample.sdf --output output/nomination_top10.csv
```

Docker 内离线 CLI 冒烟：

```bash
docker compose -f deploy/docker-compose.yml run --rm cli
```

机制 Markdown 默认调用 DeepSeek `deepseek-v4-pro` 润色（**不改 Top 10**）。无可用 Key 或调用失败时自动模板降级。

---

## 仓库结构

| 路径 | 说明 |
|------|------|
| `apps/` | API、CLI、Web 静态前端 |
| `services/` | 流水线与评分 / 证据 / Critic / 机制等服务 |
| `packages/` | 化学核心、金标、可选 ML、数据模型等共享包 |
| `configs/` | 过滤、打分、排序权重与模型清单（纳入 `config_hash`） |
| `data/` | 样本 SDF、goldset、证据快照、参考表、可选模型 |
| `deploy/` | Dockerfile、Compose、部署说明 |
| `scripts/` | 冒烟、配置门禁、证据烘焙等工具 |
| `tests/` | 单元 / 回归 / 集成测试（pytest） |

---

## 测试

```bash
pytest
```
