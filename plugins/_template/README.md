# 如何添加一个 Catalog Plugin

本目录是**示范空壳**，不进入默认 Profile 启用表。

## 1. 复制骨架

```bash
cp -R plugins/_template plugins/my_enrichment
# 按需改名包内模块
```

## 2. 写 YAML 清单（必做）

新建 `configs/agent/plugins/catalog/my-enrichment.yaml`：

```yaml
plugin_id: my-enrichment
title: My Enrichment · 旁证示例
builtin: false
enabled: false
catalog: true
description: 兼容可选。只做 enrichment；须在设置中主动添加。
source: https://example.com/my-enrichment
requires:
  network: false
  gpu: false
tools:
  - example_enrich
skills: []
activation: user_opt_in
```

硬规则：

- `builtin: false` — 禁止伪装成内置
- `enabled: false` — 出厂关闭；运行时以会话 **opt-in** 为准
- Tools **不得** 命名/实现写榜逻辑；唯一写榜入口是 `molmind-core.score_and_rank`
- 返回信封使用 `plugins.catalog_common.enrichment_envelope`，且 `writes_selection=False`

## 3. 实现 Tool

```python
# plugins/my_enrichment/tools/example.py
from plugins.catalog_common import enrichment_envelope, assert_no_selection_write

def example_enrich(**kwargs):
    result = enrichment_envelope(
        tool="example_enrich",
        plugin="my-enrichment",
        message="旁证完成",
        degraded=[],
    )
    assert_no_selection_write(result)
    return result
```

在 `plugins/catalog_dispatch.py` 的 `TOOL_HANDLERS` 注册（若希望 Loop 自动调用）。

## 4. 验证

```bash
.venv/bin/python -m pytest tests/unit/test_agent_catalog_r4.py -q
```

检查点：

1. 默认 Profile 下该插件 **未** installed
2. `POST .../catalog/install` 后 settings 显示 installed
3. Registry 中该插件的所有 tools `writes_selection is False`
4. 断网/无 GPU 时必须可降级，不影响 CSV `selection_sha256`

## 5. 设置文案

- ❌「已内置 My Enrichment」
- ✅「可从扩展目录主动添加 My Enrichment」
