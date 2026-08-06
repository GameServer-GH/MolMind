"""Plugin-driven relevance checks for remote scientific observations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any


@dataclass(frozen=True)
class ObservationAssessment:
    status: str
    relevant: bool
    score: float
    matched_concepts: list[str]
    missing_concepts: list[str]
    excluded_concepts_present: list[str]
    reasons: list[str]
    degraded_channels: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObservationValidator:
    """Validate evidence against concepts declared by the owning plugin."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def validate(
        self,
        *,
        plugin_id: str,
        capability_id: str,
        question: str,
        values: list[str],
    ) -> ObservationAssessment:
        plugin = self.registry.plugins.get(plugin_id)
        evidence = "\n".join(str(value) for value in values if str(value).strip())
        if not evidence:
            return ObservationAssessment(
                status="insufficient",
                relevant=False,
                score=0.0,
                matched_concepts=[],
                missing_concepts=[],
                excluded_concepts_present=[],
                reasons=["observation_empty"],
                degraded_channels=[capability_id or "remote_observation"],
            )

        terminology = getattr(plugin, "terminology", {}) if plugin else {}
        excluded = self._excluded_concepts(question, terminology)
        capability = next(
            (
                item
                for item in (getattr(plugin, "capabilities", None) or [])
                if isinstance(item, dict)
                and str(item.get("capability_id") or "") == capability_id
            ),
            {},
        )
        excluded.update(
            self._declared_incompatible_concepts(
                question,
                terminology,
                capability.get("incompatible_concepts") or [],
            )
        )
        requested = self._requested_concepts(question, terminology)
        for canonical in excluded:
            requested.pop(canonical, None)
        if (
            capability_id in {"literature_search", "mechanism_relation_search"}
            and not requested
        ):
            return ObservationAssessment(
                status="unscoped",
                relevant=False,
                score=0.0,
                matched_concepts=[],
                missing_concepts=[],
                excluded_concepts_present=[],
                reasons=["observation_scope_missing"],
                degraded_channels=[capability_id],
            )
        focused_evidence = self._focused_evidence(evidence, capability_id)
        if not focused_evidence.strip():
            return ObservationAssessment(
                status="insufficient",
                relevant=False,
                score=0.0,
                matched_concepts=[],
                missing_concepts=list(requested),
                excluded_concepts_present=[],
                reasons=["observation_empty_result"],
                degraded_channels=[capability_id or "remote_observation"],
            )
        evidence_low = focused_evidence.lower()
        matched = [
            canonical
            for canonical, aliases in requested.items()
            if any(alias.lower() in evidence_low for alias in aliases)
        ]
        missing = [canonical for canonical in requested if canonical not in matched]
        reasons = [f"missing_concept:{canonical}" for canonical in missing]
        excluded_present = [
            canonical
            for canonical, aliases in excluded.items()
            if any(alias.lower() in evidence_low for alias in aliases)
        ]
        reasons.extend(
            f"excluded_concept_present:{canonical}" for canonical in excluded_present
        )

        min_year = self._minimum_year(question)
        if min_year is not None:
            years = [int(value) for value in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", evidence)]
            if not years or max(years) < min_year:
                reasons.append(f"time_range_not_met:{min_year}")

        denominator = max(
            1, len(requested) + len(excluded) + (1 if min_year is not None else 0)
        )
        failures = (
            len(missing)
            + len(excluded_present)
            + sum(reason.startswith("time_range_not_met:") for reason in reasons)
        )
        score = max(0.0, min(1.0, 1.0 - failures / denominator))
        relevant = not reasons
        return ObservationAssessment(
            status="relevant" if relevant else "degraded",
            relevant=relevant,
            score=score,
            matched_concepts=matched,
            missing_concepts=missing,
            excluded_concepts_present=excluded_present,
            reasons=reasons or ["declared_constraints_satisfied"],
            degraded_channels=[] if relevant else [capability_id or "remote_observation"],
        )

    def validate_protocol(
        self, *, plugin_id: str, capability_id: str, question: str, values: list[str]
    ) -> dict[str, Any]:
        """Check protocol fields declared by the owning plugin."""
        plugin = self.registry.plugins.get(plugin_id)
        capability = next(
            (
                item
                for item in (getattr(plugin, "capabilities", None) or [])
                if isinstance(item, dict)
                and str(item.get("capability_id") or "") == capability_id
            ),
            {},
        )
        fields = capability.get("required_evidence_fields") or {}
        evidence = "\n".join(str(value) for value in values if str(value).strip()).lower()
        missing = [
            str(field)
            for field, aliases in fields.items()
            if not any(str(alias).lower() in evidence for alias in aliases or [])
        ]
        risk_flags = []
        for pattern in capability.get("risk_patterns") or []:
            if not isinstance(pattern, dict):
                continue
            terms = [str(value).lower() for value in pattern.get("terms") or []]
            contexts = [str(value).lower() for value in pattern.get("context_terms") or []]
            if any(term in evidence for term in terms) and any(
                context in evidence for context in contexts
            ):
                risk_flags.append(
                    {
                        "risk_id": pattern.get("risk_id") or "protocol_review",
                        "message": pattern.get("message") or "需要科学复核",
                    }
                )
        return {
            "status": "review_required" if risk_flags else "complete" if not missing else "degraded",
            "complete": not missing,
            "requires_review": bool(risk_flags),
            "required_fields": list(fields),
            "missing_fields": missing,
            "risk_flags": risk_flags,
            "reasons": [f"missing_protocol_field:{field}" for field in missing]
            or ["declared_protocol_fields_present"],
            "degraded_channels": []
            if not missing and not risk_flags
            else [capability_id],
        }

    @staticmethod
    def _focused_evidence(evidence: str, capability_id: str) -> str:
        """Avoid matching incidental KG metadata as if it were paper scope."""
        if capability_id not in {"literature_search", "mechanism_relation_search"}:
            return evidence
        focused: list[str] = []
        for block in evidence.splitlines():
            try:
                payload = json.loads(block)
            except (TypeError, ValueError):
                focused.append(block)
                continue
            if capability_id == "mechanism_relation_search" and isinstance(payload, dict):
                data = payload.get("data")
                if isinstance(data, list):
                    if data:
                        focused.append(
                            json.dumps(data, ensure_ascii=False, default=str)
                        )
                    continue
            for item in payload.get("output", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    continue
                for key in ("paper_title", "title", "node_text", "abstract", "summary"):
                    value = item.get(key)
                    if value:
                        focused.append(str(value))
                kg = item.get("kg_json")
                if isinstance(kg, dict):
                    for section in ("B_Textually_Mentioned_Entities", "C_Implicit_Abstracted_Entities"):
                        value = kg.get(section)
                        if value:
                            focused.append(json.dumps(value, ensure_ascii=False, default=str))
        rendered = "\n".join(focused)
        return rendered if capability_id == "mechanism_relation_search" else rendered or evidence

    @staticmethod
    def _requested_concepts(
        question: str, terminology: dict[str, Any]
    ) -> dict[str, list[str]]:
        low = str(question or "").lower()
        requested: dict[str, list[str]] = {}
        for canonical_map in terminology.values():
            if not isinstance(canonical_map, dict):
                continue
            for canonical, raw_aliases in canonical_map.items():
                aliases = list(
                    dict.fromkeys(
                        [str(canonical), *[str(value) for value in raw_aliases or []]]
                    )
                )
                if any(alias.lower() in low for alias in aliases if alias):
                    requested[str(canonical)] = aliases
        return requested

    @staticmethod
    def _minimum_year(question: str) -> int | None:
        text = str(question or "")
        patterns = (
            r"((?:19|20)\d{2})\s*年?\s*(?:以后|之后|以来|起)",
            r"(?:since|after)\s*((?:19|20)\d{2})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def _excluded_concepts(
        cls, question: str, terminology: dict[str, Any]
    ) -> dict[str, list[str]]:
        text = str(question or "")
        clauses = re.findall(
            r"(?:排除|剔除|不包括|不要|exclude|excluding|without)\s*([^。；;\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        excluded_text = " ".join(clauses).lower()
        if not excluded_text:
            return {}
        concepts: dict[str, list[str]] = {}
        for canonical_map in terminology.values():
            if not isinstance(canonical_map, dict):
                continue
            for canonical, raw_aliases in canonical_map.items():
                aliases = list(
                    dict.fromkeys(
                        [str(canonical), *[str(value) for value in raw_aliases or []]]
                    )
                )
                if any(
                    cls._positive_alias_mention(excluded_text, alias)
                    for alias in aliases
                    if alias
                ):
                    concepts[str(canonical)] = aliases
        return concepts

    @staticmethod
    def _positive_alias_mention(text: str, alias: str) -> bool:
        for match in re.finditer(re.escape(str(alias).lower()), str(text).lower()):
            prefix = str(text).lower()[max(0, match.start() - 8) : match.start()]
            if re.search(r"(?:非|非-|non[-\s]?)$", prefix):
                continue
            return True
        return False

    @staticmethod
    def _declared_incompatible_concepts(
        question: str,
        terminology: dict[str, Any],
        canonicals: list[str],
    ) -> dict[str, list[str]]:
        low = str(question or "").lower()
        declared = {str(value) for value in canonicals}
        concepts: dict[str, list[str]] = {}
        for canonical_map in terminology.values():
            if not isinstance(canonical_map, dict):
                continue
            for canonical, raw_aliases in canonical_map.items():
                if str(canonical) not in declared:
                    continue
                aliases = list(
                    dict.fromkeys(
                        [str(canonical), *[str(value) for value in raw_aliases or []]]
                    )
                )
                if not any(alias.lower() in low for alias in aliases if alias):
                    concepts[str(canonical)] = aliases
        return concepts
