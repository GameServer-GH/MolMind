"""Deterministic, JSON-safe molecule identity resolution for evidence lookup.

The resolver does not decide whether an evidence claim is scientifically valid.
It only records which identifiers are available, which one has lookup priority,
and whether the supplied identifiers disagree.  Callers must keep a
``identity_review_required`` result out of scoring channels.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from rdkit import Chem


_CAS_SPLIT_RE = re.compile(r"\s*(?:[,;|]|\band\b)\s*", re.I)
_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
_CAS_RE = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _inchikey(value: Any) -> str:
    return _text(value).upper()


def _valid_inchikey(value: str) -> bool:
    return bool(_INCHIKEY_RE.fullmatch(value))


def _valid_cas(value: str) -> bool:
    """Validate CAS syntax and its public checksum digit."""

    match = _CAS_RE.fullmatch(value)
    if not match:
        return False
    body = f"{match.group(1)}{match.group(2)}"
    checksum = sum(
        int(digit) * multiplier
        for multiplier, digit in enumerate(reversed(body), start=1)
    ) % 10
    return checksum == int(match.group(3))


def _cas_values(value: Any) -> list[str]:
    if value is None:
        return []
    raw: Iterable[Any]
    if isinstance(value, str):
        raw = _CAS_SPLIT_RE.split(value)
    elif isinstance(value, Iterable):
        raw = value
    else:
        raw = (value,)
    values: list[str] = []
    for item in raw:
        text = _text(item)
        if text and text not in values:
            values.append(text)
    return values


def _smiles_identity(value: str) -> tuple[str, str, str]:
    """Return canonical SMILES, InChIKey and a parse error string."""

    text = _text(value)
    if not text:
        return "", "", ""
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        return "", "", "smiles_unparseable"
    try:
        canonical = Chem.MolToSmiles(mol, isomericSmiles=True)
        derived_key = Chem.MolToInchiKey(mol) or ""
    except Exception:
        return "", "", "smiles_identity_generation_failed"
    return canonical, derived_key.upper(), ""


@dataclass(frozen=True)
class MoleculeIdentity:
    """Normalized identity values that can be serialized without custom hooks."""

    molecule_id: str = ""
    original_inchikey: str = ""
    standardized_inchikey: str = ""
    cas: str = ""
    cas_values: tuple[str, ...] = ()
    standardized_smiles: str = ""
    original_smiles: str = ""
    smiles_derived_inchikey: str = ""
    original_smiles_derived_inchikey: str = ""
    standardization_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cas_values"] = list(self.cas_values)
        payload["standardization_steps"] = list(self.standardization_steps)
        return payload


@dataclass(frozen=True)
class IdentityResolution:
    """Selected lookup identity plus every auditable alternative and conflict."""

    identity: MoleculeIdentity
    status: str
    candidates: tuple[dict[str, str], ...] = ()
    lookup_field: str = ""
    lookup_value: str = ""
    match_type: str = ""
    conflicts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def molecule_id(self) -> str:
        return self.identity.molecule_id

    @property
    def is_resolved(self) -> bool:
        return bool(self.lookup_field and self.lookup_value)

    @property
    def requires_review(self) -> bool:
        return self.status == "identity_review_required"

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "status": self.status,
            "candidates": [dict(item) for item in self.candidates],
            "lookup_field": self.lookup_field,
            "lookup_value": self.lookup_value,
            "match_type": self.match_type,
            "conflicts": list(self.conflicts),
            "notes": list(self.notes),
        }

    def lookup_for(
        self, identity_order: Iterable[str]
    ) -> tuple[str | None, str | None, str | None]:
        """Pick a provider-compatible candidate without changing global priority."""

        aliases = {
            "smiles": "standardized_smiles",
            "canonical_smiles": "standardized_smiles",
        }
        for requested in identity_order:
            field_name = aliases.get(str(requested), str(requested))
            for candidate in self.candidates:
                if candidate.get("lookup_field") == field_name:
                    return (
                        field_name,
                        candidate.get("lookup_value") or None,
                        candidate.get("match_type") or None,
                    )
        return None, None, None


def _mapping_value(entity: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in entity and entity.get(name) not in (None, ""):
            return entity.get(name)
    return ""


def resolve_identity(
    molecule_id: str = "",
    original_inchikey: str = "",
    standardized_inchikey: str = "",
    cas: Any = "",
    smiles: str = "",
    original_smiles: str = "",
    standardization_steps: Iterable[str] = (),
) -> IdentityResolution:
    """Resolve lookup identity with original -> standardized -> CAS -> SMILES priority.

    A different original and standardized InChIKey is not itself a conflict:
    salt stripping, charge normalization and tautomer canonicalization may change
    that key.  A SMILES-derived key that disagrees with the corresponding
    explicit key *is* a conflict and must be reviewed.
    """

    original_key_input = _inchikey(original_inchikey)
    standardized_key_input = _inchikey(standardized_inchikey)
    original_key = original_key_input if _valid_inchikey(original_key_input) else ""
    standardized_key = (
        standardized_key_input if _valid_inchikey(standardized_key_input) else ""
    )
    cas_items = _cas_values(cas)
    valid_cas_items = [item for item in cas_items if _valid_cas(item)]
    normalized_steps = tuple(
        dict.fromkeys(_text(item) for item in standardization_steps if _text(item))
    )
    canonical_smiles, smiles_key, smiles_error = _smiles_identity(smiles)
    canonical_original, original_smiles_key, original_smiles_error = _smiles_identity(
        original_smiles
    )

    conflicts: list[str] = []
    notes: list[str] = []
    if original_key_input and not original_key:
        conflicts.append("original_inchikey_invalid")
    if standardized_key_input and not standardized_key:
        conflicts.append("standardized_inchikey_invalid")
    if len(valid_cas_items) != len(cas_items):
        conflicts.append("cas_invalid")
    if len(cas_items) > 1:
        conflicts.append("multiple_cas_identifiers")
    if smiles_error:
        conflicts.append(smiles_error)
    if original_smiles_error:
        conflicts.append(original_smiles_error)

    if standardized_key and smiles_key and standardized_key != smiles_key:
        conflicts.append("standardized_smiles_inchikey_conflict")
    if original_key and original_smiles_key and original_key != original_smiles_key:
        conflicts.append("original_smiles_inchikey_conflict")

    # Original and standardized structures may legitimately differ after salt
    # stripping, charge normalization or parent selection, but only when the
    # Run carries an explicit standardization trail.  Otherwise a cross-side
    # mismatch is an identity conflict, not a silent normalization guess.
    original_side_key = original_key or original_smiles_key
    standardized_side_key = standardized_key or smiles_key
    if (
        original_side_key
        and standardized_side_key
        and original_side_key != standardized_side_key
    ):
        if normalized_steps:
            notes.append("standardization_changed_inchikey_with_audited_steps")
        else:
            conflicts.append("unexplained_standardization_identity_change")

    # Deduplicate conflict codes while preserving deterministic discovery order.
    conflicts = list(dict.fromkeys(conflicts))
    if original_key and standardized_key and original_key != standardized_key:
        notes.append("original_and_standardized_inchikey_differ")

    identity = MoleculeIdentity(
        molecule_id=_text(molecule_id),
        original_inchikey=original_key,
        standardized_inchikey=standardized_key or smiles_key,
        cas=valid_cas_items[0] if valid_cas_items else "",
        cas_values=tuple(valid_cas_items),
        standardized_smiles=canonical_smiles or _text(smiles),
        original_smiles=canonical_original or _text(original_smiles),
        smiles_derived_inchikey=smiles_key,
        original_smiles_derived_inchikey=original_smiles_key,
        standardization_steps=normalized_steps,
    )

    candidates: list[dict[str, str]] = []

    def add(field_name: str, value: str, match_type: str) -> None:
        if not value:
            return
        key = (field_name, value)
        if any(
            (item["lookup_field"], item["lookup_value"]) == key
            for item in candidates
        ):
            return
        candidates.append(
            {
                "lookup_field": field_name,
                "lookup_value": value,
                "match_type": match_type,
            }
        )

    add("original_inchikey", original_key, "exact_original_inchikey")
    if not original_key:
        add(
            "original_inchikey",
            original_smiles_key,
            "inchikey_derived_from_original_smiles",
        )
    add(
        "standardized_inchikey",
        standardized_key,
        "exact_standardized_inchikey",
    )
    for cas_value in valid_cas_items:
        add("cas", cas_value, "cas_identifier")
    if not standardized_key:
        add("standardized_inchikey", smiles_key, "inchikey_derived_from_smiles")
    add("standardized_smiles", canonical_smiles, "canonicalized_smiles")

    selected = candidates[0] if candidates else {}
    # Parse errors alone do not create a resolvable identity to review.  They
    # remain an auditable reason for ``audit_missing``.
    if not selected:
        status = "audit_missing"
    elif conflicts:
        status = "identity_review_required"
    else:
        status = "hit"

    return IdentityResolution(
        identity=identity,
        status=status,
        candidates=tuple(candidates),
        lookup_field=selected.get("lookup_field", ""),
        lookup_value=selected.get("lookup_value", ""),
        match_type=selected.get("match_type", ""),
        conflicts=tuple(conflicts),
        notes=tuple(notes),
    )


def resolution_from_mapping(entity: Mapping[str, Any]) -> IdentityResolution:
    """Resolve the identity vocabulary used by SDF, run and tool payloads."""

    cas_values = entity.get("cas_values")
    cas_input = cas_values if cas_values else _mapping_value(entity, "cas", "casrn")
    return resolve_identity(
        molecule_id=_text(_mapping_value(entity, "molecule_id", "id")),
        original_inchikey=_text(_mapping_value(entity, "original_inchikey")),
        standardized_inchikey=_text(
            _mapping_value(entity, "standardized_inchikey", "inchikey")
        ),
        cas=cas_input,
        smiles=_text(_mapping_value(entity, "standardized_smiles", "smiles")),
        original_smiles=_text(_mapping_value(entity, "original_smiles")),
        standardization_steps=entity.get("standardization_steps") or (),
    )
