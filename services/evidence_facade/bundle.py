"""EvidenceBundle：lipid/tox/novelty/pathway 证据聚合。"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.models import EvidenceHit


@dataclass
class EvidenceBundle:
    lipid: list[EvidenceHit] = field(default_factory=list)
    tox: list[EvidenceHit] = field(default_factory=list)
    novelty: list[EvidenceHit] = field(default_factory=list)
    pathway: list[EvidenceHit] = field(default_factory=list)

    @property
    def conf_e(self) -> float:
        hits = [*self.lipid, *self.tox, *self.novelty]
        if not hits:
            return 0.0
        return sum(h.confidence for h in hits) / len(hits)

    @property
    def lipid_score(self) -> float:
        if not self.lipid:
            return 0.0
        return max(h.score for h in self.lipid)

    @property
    def tox_score(self) -> float:
        if not self.tox:
            return 0.0
        return max(h.score for h in self.tox)

    @property
    def novelty_score(self) -> float:
        if not self.novelty:
            return 0.5
        return max(h.score for h in self.novelty)

    @property
    def has_any(self) -> bool:
        return bool(self.lipid or self.tox or self.novelty or self.pathway)

    def all_ids(self) -> list[str]:
        return [h.evidence_id for h in [*self.lipid, *self.tox, *self.novelty, *self.pathway]]
