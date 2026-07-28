"""模板 Tool 示例。"""

from __future__ import annotations

from typing import Any

from plugins.catalog_common import assert_no_selection_write, enrichment_envelope


def example_enrich(**_: Any) -> dict[str, Any]:
    result = enrichment_envelope(
        tool="example_enrich",
        plugin="_template",
        message="模板 Tool：请复制后改为真实 enrichment。",
        degraded=["template_stub"],
    )
    assert_no_selection_write(result)
    return result
