from __future__ import annotations

from types import SimpleNamespace

from agent.runtime.verification import evidence_correction, verify_assistant_claims


def _session(*, frozen_count: int = 0, active_plan: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        last_result=SimpleNamespace(top_molecules=[object()] * frozen_count)
        if frozen_count
        else None,
        artifacts={},
        active_plan=active_plan,
    )


def test_rank_explanation_is_not_mistaken_for_completed_topn_claim() -> None:
    session = _session(frozen_count=2)

    violations = verify_assistant_claims(
        session,
        "T19959 是上一轮冻结结果里的 Top 1；这次只解释已有结果，不会重新筛选。",
    )

    assert violations == []


def test_future_topn_guidance_is_not_mistaken_for_completed_topn_claim() -> None:
    session = _session()

    violations = verify_assistant_claims(
        session,
        "请先上传 .sdf 附件，上传后再说生成 top10 候选清单 csv。",
    )

    assert violations == []


def test_capability_description_of_existing_frozen_rank_is_not_completion() -> None:
    session = _session()

    violations = verify_assistant_claims(
        session,
        "我可以基于已经冻结的筛选排名生成机制解释 PDF，也可以解释已有结果。",
    )

    assert violations == []


def test_completed_topn_claim_requires_matching_frozen_evidence() -> None:
    session = _session(frozen_count=2)

    violations = verify_assistant_claims(
        session,
        "本轮已经完成筛选，已冻结 Top10 候选清单。",
    )

    assert {item.code for item in violations} == {"topn_mismatch"}


def test_running_claim_requires_a_running_plan_step() -> None:
    session = _session(active_plan={"steps": [{"status": "queued"}]})

    violations = verify_assistant_claims(session, "正在运行筛选并生成候选清单。")

    assert {item.code for item in violations} == {"running_without_evidence"}


def test_sdf_session_cannot_deny_local_screening_capability() -> None:
    session = _session()
    session.sdf_bytes = b"sdf"
    session.sdf_filename = "library.sdf"

    violations = verify_assistant_claims(
        session,
        "这个对话环境本身不具备实时对分子库进行虚拟筛选的计算能力。",
    )

    assert {item.code for item in violations} == {"local_screening_capability_denial"}
    assert "score_and_rank" in evidence_correction(session, violations)
