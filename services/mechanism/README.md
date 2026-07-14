# services/mechanism

机制假说与 HepG2-FFA 验证方案（**不改排名公式**；选榜由 Critic 配额约束）。

## 行为（准确性优先）

1. **默认准确模板**：按假设通路分组叙述 + 通路白名单 + 计算层归因 + 证据拆分（降脂 / 毒理GHS / 其他）。  
2. **统一有效命中口径**：脂质显著下降 且 活力 >=80%（不写自拟 20%/30% 硬阈值）。  
3. GHS/毒理 ID **不得**写成靶点证据。  
4. 写出 `*.mechanism.md` + `*.mechanism.pdf`；PDF 做字符净化防乱码。  
5. 默认 **不调用 LLM** 撰写机制正文（`mechanism_pdf: false`）。  
6. 实验协议与救援读出按通路组共用，避免 Top10 全文复制粘贴。

通路假设推断与选榜家族/通路配额共用 `packages.goldset.hypothesis`。
