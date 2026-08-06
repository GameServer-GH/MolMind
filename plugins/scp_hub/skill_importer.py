"""Safe SKILL.md importer: parse metadata and pin content, never execute code."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass, asdict
from typing import Any
import yaml

@dataclass
class SkillLock:
    skill_id: str; repository: str; path: str; commit: str; content_hash: str
    server_ids: list[str]; tool_names: list[str]
    def as_dict(self) -> dict[str, Any]: return asdict(self)

def import_skill_markdown(content: str, *, repository: str, path: str, commit: str, expected_skill_id: str = "") -> tuple[dict[str, Any], SkillLock]:
    if not commit or commit in {"main", "master", "HEAD"}: raise ValueError("skill source must be pinned to an immutable commit")
    if not path.startswith("skills/") or not path.endswith("/SKILL.md"): raise ValueError("skill path must be skills/<id>/SKILL.md")
    front: dict[str, Any] = {}
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end < 0: raise ValueError("unterminated SKILL.md front matter")
        parsed = yaml.safe_load(content[4:end]) or {}
        if not isinstance(parsed, dict): raise ValueError("SKILL.md front matter must be a mapping")
        front = parsed
    skill_id = str(front.get("skill_id") or front.get("name") or path.split("/")[1])
    if expected_skill_id and skill_id != expected_skill_id: raise ValueError("skill id does not match catalog entry")
    server_ids = [str(x) for x in front.get("servers") or front.get("server_ids") or []]
    tool_names = [str(x) for x in front.get("tools") or front.get("tool_names") or []]
    lock = SkillLock(skill_id, repository, path, commit, "sha256:"+hashlib.sha256(content.encode()).hexdigest(), server_ids, tool_names)
    spec = {"skill_id": skill_id, "title": str(front.get("title") or skill_id), "description": str(front.get("description") or ""), "servers": server_ids, "tools": tool_names, "instructions": content}
    return spec, lock
