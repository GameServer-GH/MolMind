"""Ingest：流式 SDF → 标准化记录 + 描述符/指纹。

化合物库常含价态异常 / 无法 kekulize / 非常规键类型记录。
解析期关闭 RDKit 日志噪音（含 C++ cerr），无效分子静默跳过并计入 skipped。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, Lipinski
from rdkit.Chem.MolStandardize import rdMolStandardize

from packages.chem_core import morgan_fp
from packages.models import MoleculeRecord, ParseIssue


@dataclass
class ParseResult:
    records: list[MoleculeRecord]
    raw_count: int
    skipped: int
    inchikey_missing: int
    issues: list[ParseIssue]

    @property
    def parsed_count(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class ParseProgress:
    """解析进度快照；供流水线日志心跳使用。"""

    processed: int
    estimated_total: int
    valid: int
    skipped: int


ParseProgressCallback = Callable[[ParseProgress], None]


def estimate_sdf_record_count(path: str | Path) -> int:
    """按 SDF 记录分隔符 ``$$$$`` 粗估条目数（预扫，不做化学计算）。"""
    count = 0
    with Path(path).open("rb") as handle:
        for line in handle:
            if line.startswith(b"$$$$"):
                count += 1
    return count


@contextmanager
def quiet_rdkit():
    """关闭 Python RDLogger + 重定向 fd2，挡住 RDKit C++ 写入 stderr 的 ERROR。"""
    RDLogger.DisableLog("rdApp.*")
    devnull = open(os.devnull, "w")
    stderr_fd = 2
    saved_fd = os.dup(stderr_fd)
    try:
        os.dup2(devnull.fileno(), stderr_fd)
        yield
    finally:
        os.dup2(saved_fd, stderr_fd)
        os.close(saved_fd)
        devnull.close()
        RDLogger.EnableLog("rdApp.*")


def _molecule_id(mol: Chem.Mol, index: int) -> str:
    for prop in ("ID", "id", "NAME", "name", "_Name"):
        if mol.HasProp(prop):
            value = mol.GetProp(prop).strip()
            if value:
                return value
    return f"MOL_{index:05d}"


def _cas(mol: Chem.Mol) -> str | None:
    for prop in ("CAS", "cas", "CASNO", "Cas"):
        if mol.HasProp(prop):
            value = mol.GetProp(prop).strip()
            if value:
                return value
    return None


def _safe_inchikey(mol: Chem.Mol) -> str:
    try:
        key = Chem.MolToInchiKey(mol)
    except Exception:
        return ""
    if not key or key.startswith("InChIKey=ERROR") or " " in key:
        return ""
    if len(key) < 14:
        return ""
    return key


def _standardize_mol(mol: Chem.Mol) -> tuple[Chem.Mol, tuple[str, ...]]:
    """确定性标准化：清理、取主体片段、去电荷并规范互变异构体。"""
    steps: list[str] = []
    working = Chem.Mol(mol)
    Chem.SanitizeMol(working)
    original = Chem.MolToSmiles(working, isomericSmiles=True)

    cleaned = rdMolStandardize.Cleanup(working)
    if Chem.MolToSmiles(cleaned, isomericSmiles=True) != original:
        steps.append("cleanup")
    parent = rdMolStandardize.FragmentParent(cleaned)
    if Chem.MolToSmiles(parent, isomericSmiles=True) != Chem.MolToSmiles(
        cleaned, isomericSmiles=True
    ):
        steps.append("fragment_parent")
    uncharged = rdMolStandardize.Uncharger().uncharge(parent)
    if Chem.MolToSmiles(uncharged, isomericSmiles=True) != Chem.MolToSmiles(
        parent, isomericSmiles=True
    ):
        steps.append("uncharged")
    standardized = rdMolStandardize.TautomerEnumerator().Canonicalize(uncharged)
    if Chem.MolToSmiles(standardized, isomericSmiles=True) != Chem.MolToSmiles(
        uncharged, isomericSmiles=True
    ):
        steps.append("canonical_tautomer")
    Chem.SanitizeMol(standardized)
    return standardized, tuple(steps or ["canonical_no_change"])


def _try_build_record(
    mol: Chem.Mol, index: int
) -> tuple[MoleculeRecord | None, str, str]:
    source_id = _molecule_id(mol, index)
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        return None, "sanitize_failed", source_id

    try:
        original_smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
        mol, standardization_steps = _standardize_mol(mol)
    except Exception:
        return None, "standardization_failed", source_id

    try:
        smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
    except Exception:
        return None, "smiles_generation_failed", source_id
    if not smiles:
        return None, "empty_standardized_smiles", source_id

    try:
        fp = morgan_fp(mol)
    except Exception:
        return None, "fingerprint_failed", source_id

    inchikey = _safe_inchikey(mol)
    try:
        record = MoleculeRecord(
            molecule_id=source_id,
            smiles=smiles,
            inchikey=inchikey,
            cas=_cas(mol),
            mw=float(Descriptors.MolWt(mol)),
            logp=float(Descriptors.MolLogP(mol)),
            hbd=int(Lipinski.NumHDonors(mol)),
            hba=int(Lipinski.NumHAcceptors(mol)),
            tpsa=float(Descriptors.TPSA(mol)),
            rotatable_bonds=int(Lipinski.NumRotatableBonds(mol)),
            aromatic_rings=int(Descriptors.NumAromaticRings(mol)),
            fp_bits=fp,
            source_index=index,
            source_molecule_id=source_id,
            original_smiles=original_smiles,
            standardization_steps=standardization_steps,
        )
        return record, "", source_id
    except Exception:
        return None, "descriptor_generation_failed", source_id


def parse_sdf(path: str | Path) -> list[MoleculeRecord]:
    """兼容旧调用：只返回成功解析的分子列表。"""
    return parse_sdf_detailed(path).records


def parse_sdf_detailed(
    path: str | Path,
    *,
    on_progress: ParseProgressCallback | None = None,
    estimated_total: int | None = None,
) -> ParseResult:
    """解析 SDF；公共入口始终抑制 RDKit C++ 噪音并返回结构化问题。"""
    with quiet_rdkit():
        return _parse_sdf_detailed_inner(
            path,
            on_progress=on_progress,
            estimated_total=estimated_total,
        )


def _parse_sdf_detailed_inner(
    path: str | Path,
    *,
    on_progress: ParseProgressCallback | None = None,
    estimated_total: int | None = None,
) -> ParseResult:
    sdf_path = Path(path)
    if not sdf_path.is_file():
        raise FileNotFoundError(f"SDF 文件不存在: {sdf_path}")

    records: list[MoleculeRecord] = []
    skipped = 0
    inchikey_missing = 0
    raw_count = 0
    issues: list[ParseIssue] = []
    seen_ids: dict[str, int] = {}
    total_hint = max(0, int(estimated_total or 0))

    def _report() -> None:
        if on_progress is None:
            return
        on_progress(
            ParseProgress(
                processed=raw_count,
                estimated_total=total_hint,
                valid=len(records),
                skipped=skipped,
            )
        )

    if on_progress is not None:
        on_progress(
            ParseProgress(
                processed=0,
                estimated_total=total_hint,
                valid=0,
                skipped=0,
            )
        )

    supplier = Chem.ForwardSDMolSupplier(
        str(sdf_path),
        removeHs=False,
        sanitize=False,
    )
    for index, mol in enumerate(supplier, start=1):
        raw_count += 1
        if mol is None:
            skipped += 1
            issues.append(
                ParseIssue(
                    source_index=index,
                    molecule_id=f"MOL_{index:05d}",
                    status="invalid",
                    reason_code="sdf_parse_failed",
                    reason="RDKit 无法解析 SDF 记录",
                )
            )
            _report()
            continue
        record, reason_code, source_id = _try_build_record(mol, index)
        if record is None:
            skipped += 1
            issues.append(
                ParseIssue(
                    source_index=index,
                    molecule_id=source_id,
                    status="invalid",
                    reason_code=reason_code,
                    reason=f"分子记录无效: {reason_code}",
                )
            )
            _report()
            continue
        seen_ids[source_id] = seen_ids.get(source_id, 0) + 1
        if seen_ids[source_id] > 1:
            record.molecule_id = f"{source_id}__{index:05d}"
        if not record.inchikey:
            inchikey_missing += 1
        records.append(record)
        _report()

    if on_progress is not None and (total_hint == 0 or raw_count != total_hint):
        # 预估不准或未预扫时，再推一次最终状态。
        _report()

    return ParseResult(
        records=records,
        raw_count=raw_count,
        skipped=skipped,
        inchikey_missing=inchikey_missing,
        issues=issues,
    )
