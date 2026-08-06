"""Regression: durable freeze survives reload and same-turn stale copies."""

from __future__ import annotations

from agent.memory import FileRunStore
from agent.memory.frozen_ranking import (
    ensure_session_last_result,
    hydrate_from_snapshot,
    snapshot_from_result,
)
from agent.memory.models import AgentSession
from agent.runtime.planning import session_capabilities
from packages.models import ScoreRecord


def _mol(molecule_id: str = "T001", **kwargs) -> ScoreRecord:
    base = dict(
        molecule_id=molecule_id,
        smiles="CCO",
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        cas=None,
        scaffold_smiles="CCO",
        lipid_score=0.4,
        tox_risk=0.3,
        novelty_score=0.75,
        conf_e=0.5,
        final_score=0.5,
        tox_heads={},
        lipid_parts={},
        attributions=[],
        lipid_rationale="药效团: carboxylic acid",
        tox_rationale="R_tox=0.3",
        overall_reason="score",
        selection_score=0.48,
        eligibility_status="eligible",
    )
    base.update(kwargs)
    return ScoreRecord(**base)


class _Cfg:
    config_hash = "abc"
    mode = "competition"
    degraded_channels: list[str] = []
    assumptions: dict = {}
    llm: dict = {}
    reserve_n = 20

    def mark_degraded(self, channel: str) -> None:
        if channel not in self.degraded_channels:
            self.degraded_channels.append(channel)


class _Result:
    def __init__(self) -> None:
        self.top_molecules = [_mol("T19959"), _mol("T27832"), _mol("T11137")]
        self.reserve_molecules = [_mol("R1")]
        self.run_id = "mm-test-run"
        self.input_sha256 = "input"
        self.selection_sha256 = "sel"
        self.reserve_selection_sha256 = "rsel"
        self.source_filename = "lib.sdf"
        self.config = _Cfg()
        self.mechanism_graphs = []

    @property
    def output_count(self) -> int:
        return len(self.top_molecules)


def test_copy_session_state_preserves_hot_last_result(tmp_path) -> None:
    store = FileRunStore(root=tmp_path / "runs")
    session = store.create(client_id="freeze-copy")
    session.last_result = _Result()
    session.frozen_ranking = snapshot_from_result(session.last_result)

    stale = AgentSession(session_id=session.session_id, event_seq=0)
    store._copy_session_state(session, stale)
    assert session.last_result is not None
    assert session.frozen_ranking is not None
    assert session.last_result.top_molecules[0].molecule_id == "T19959"


def test_frozen_ranking_roundtrip_hydrate(tmp_path) -> None:
    store = FileRunStore(root=tmp_path / "runs")
    session = store.create(client_id="freeze-roundtrip")
    live = _Result()
    session.last_result = live
    session.frozen_ranking = snapshot_from_result(live)
    session.last_run_id = live.run_id
    store.persist(session)

    # Simulate a fresh worker: drop hot object, reload from meta.
    session.last_result = None
    reloaded = store.get(session.session_id)
    assert reloaded is not None
    assert reloaded.frozen_ranking is not None
    assert reloaded.last_result is None
    hydrated = ensure_session_last_result(reloaded)
    assert hydrated is not None
    assert hydrated.run_id == "mm-test-run"
    assert [m.molecule_id for m in hydrated.top_molecules] == [
        "T19959",
        "T27832",
        "T11137",
    ]
    assert "frozen_result" in session_capabilities(reloaded)
    csv_text = hydrated.to_csv_text()
    assert "T19959" in csv_text


def test_snapshot_hydrate_preserves_rank_explain_fields() -> None:
    snap = snapshot_from_result(_Result())
    assert snap is not None
    hydrated = hydrate_from_snapshot(snap)
    assert hydrated is not None
    third = hydrated.top_molecules[2]
    assert third.molecule_id == "T11137"
    assert third.lipid_score == 0.4
