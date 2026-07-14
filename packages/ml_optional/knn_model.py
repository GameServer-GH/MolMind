"""ECFP k-NN 毒性代理模型（JSON 可版本化；无 sklearn/TDC 运行时依赖）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator

from packages.chem_core import clamp


@dataclass(frozen=True)
class KnnEntry:
    name: str
    score: float
    on_bits: tuple[int, ...]


@dataclass
class KnnModel:
    version: str
    kind: str
    radius: int
    n_bits: int
    k: int
    sim_threshold: float
    entries: list[KnnEntry]
    _fps: list[Any] | None = None

    def _ensure_fps(self) -> list[Any]:
        if self._fps is not None:
            return self._fps
        gen = rdFingerprintGenerator.GetMorganGenerator(radius=self.radius, fpSize=self.n_bits)
        fps = []
        for entry in self.entries:
            bv = DataStructs.ExplicitBitVect(self.n_bits)
            for bit in entry.on_bits:
                if 0 <= bit < self.n_bits:
                    bv.SetBit(int(bit))
            fps.append(bv)
        self._fps = fps
        return fps

    def predict(self, mol: Chem.Mol) -> tuple[float, str | None, float]:
        """返回 (score, neighbor_name, similarity)。无命中则 (0, None, 0)。"""
        if mol is None or not self.entries:
            return 0.0, None, 0.0
        gen = rdFingerprintGenerator.GetMorganGenerator(radius=self.radius, fpSize=self.n_bits)
        query = gen.GetFingerprint(mol)
        fps = self._ensure_fps()
        sims: list[tuple[float, int]] = []
        for idx, fp in enumerate(fps):
            sim = float(DataStructs.TanimotoSimilarity(query, fp))
            if sim >= self.sim_threshold:
                sims.append((sim, idx))
        if not sims:
            return 0.0, None, 0.0
        sims.sort(reverse=True)
        top = sims[: max(1, self.k)]
        weight_sum = sum(s for s, _ in top) or 1.0
        score = sum(s * self.entries[i].score for s, i in top) / weight_sum
        best_sim, best_i = top[0]
        return clamp(score), self.entries[best_i].name, best_sim


def load_knn_model(path: Path) -> KnnModel:
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    entries = [
        KnnEntry(
            name=str(e.get("name") or ""),
            score=float(e["score"]),
            on_bits=tuple(int(b) for b in e.get("on_bits") or []),
        )
        for e in raw.get("entries") or []
    ]
    return KnnModel(
        version=str(raw.get("version") or "unknown"),
        kind=str(raw.get("kind") or "knn"),
        radius=int(raw.get("radius") or 2),
        n_bits=int(raw.get("n_bits") or 2048),
        k=int(raw.get("k") or 3),
        sim_threshold=float(raw.get("sim_threshold") or 0.40),
        entries=entries,
    )


def mol_on_bits(mol: Chem.Mol, *, radius: int = 2, n_bits: int = 2048) -> list[int]:
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits)
    fp = gen.GetFingerprint(mol)
    return list(fp.GetOnBits())
