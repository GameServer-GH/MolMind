"""Ingest：流式 SDF → 标准化记录 + 描述符/指纹。

组委会库常含价态异常 / 无法 kekulize / 非常规键类型记录。
解析期关闭 RDKit 日志噪音（含 C++ cerr），无效分子静默跳过并计入 skipped。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, Lipinski

from packages.chem_core import morgan_fp
from packages.models import MoleculeRecord


@dataclass
class ParseResult:
    records: list[MoleculeRecord]
    raw_count: int
    skipped: int
    inchikey_missing: int

    @property
    def parsed_count(self) -> int:
        return len(self.records)


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


def _try_build_record(mol: Chem.Mol, index: int) -> MoleculeRecord | None:
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        return None

    try:
        smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
    except Exception:
        return None
    if not smiles:
        return None

    try:
        fp = morgan_fp(mol)
    except Exception:
        return None

    inchikey = _safe_inchikey(mol)
    try:
        return MoleculeRecord(
            molecule_id=_molecule_id(mol, index),
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
        )
    except Exception:
        return None


def parse_sdf(path: str | Path) -> list[MoleculeRecord]:
    """兼容旧调用：只返回成功解析的分子列表。"""
    with quiet_rdkit():
        return parse_sdf_detailed(path).records


def parse_sdf_detailed(path: str | Path) -> ParseResult:
    """解析 SDF；调用方应处于 `quiet_rdkit()` 中（或接受 RDKit 噪音）。"""
    sdf_path = Path(path)
    if not sdf_path.is_file():
        raise FileNotFoundError(f"SDF 文件不存在: {sdf_path}")

    records: list[MoleculeRecord] = []
    skipped = 0
    inchikey_missing = 0
    raw_count = 0

    supplier = Chem.ForwardSDMolSupplier(
        str(sdf_path),
        removeHs=False,
        sanitize=False,
    )
    for index, mol in enumerate(supplier, start=1):
        raw_count += 1
        if mol is None:
            skipped += 1
            continue
        record = _try_build_record(mol, index)
        if record is None:
            skipped += 1
            continue
        if not record.inchikey:
            inchikey_missing += 1
        records.append(record)

    return ParseResult(
        records=records,
        raw_count=raw_count,
        skipped=skipped,
        inchikey_missing=inchikey_missing,
    )
