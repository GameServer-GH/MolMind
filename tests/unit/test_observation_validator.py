from agent.registry import AgentRegistry
from agent.runtime.observation_validator import ObservationValidator


def test_relevance_rejects_wrong_disease_and_target() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="查询 MASLD 与 PPARα 激动剂相关文献",
        values=["PPAR gamma signaling in alcoholic liver disease"],
    )
    assert result.status == "degraded"
    assert "MASLD" in result.missing_concepts
    assert "PPARA" in result.missing_concepts
    assert result.degraded_channels == ["literature_search"]


def test_relevance_accepts_declared_aliases() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="查询 MASLD 与 PPARα 激动剂相关文献",
        values=["NAFLD treatment through PPAR-alpha agonism in hepatic tissue"],
    )
    assert result.relevant is True
    assert result.missing_concepts == []


def test_relevance_checks_explicit_minimum_year() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="查找 2020 年以后关于 MASLD 的研究",
        values=["NAFLD cohort study. pub_year: 2018"],
    )
    assert result.relevant is False
    assert "time_range_not_met:2020" in result.reasons


def test_relevance_requires_requested_liver_organ() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="检索 PPARα 调控肝脏脂肪酸氧化的研究论文",
        values=["PPAR-alpha is essential for retinal lipid metabolism and neuronal survival"],
    )
    assert result.relevant is False
    assert "liver" in result.missing_concepts
    assert "retina" in result.excluded_concepts_present


def test_declared_incompatible_topic_is_allowed_when_explicitly_requested() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="检索 PPARα 在视网膜脂质代谢中的研究",
        values=["PPAR-alpha regulates retinal lipid metabolism"],
    )
    assert "retina" not in result.excluded_concepts_present


def test_unscoped_literature_observation_is_not_automatically_relevant() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="请基于刚才的证据进行总结",
        values=["An unrelated paper title"],
    )
    assert result.status == "unscoped"
    assert result.relevant is False
    assert result.reasons == ["observation_scope_missing"]


def test_non_liver_exclusion_keeps_liver_as_requested_scope() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="查找 MASLD 肝脏研究，排除非肝脏组织研究",
        values=["NAFLD study in hepatic tissue"],
    )
    assert "liver" in result.matched_concepts
    assert "liver" not in result.excluded_concepts_present


def test_relevance_rejects_explicitly_excluded_topics() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="literature_search",
        question="查找 MASLD 研究，排除酒精性肝病和视网膜相关论文",
        values=["NAFLD and PPAR-alpha in retinal lipid metabolism"],
    )
    assert result.relevant is False
    assert "retina" in result.excluded_concepts_present
    assert "excluded_concept_present:retina" in result.reasons


def test_mechanism_empty_data_does_not_match_kg_metadata() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="mechanism_relation_search",
        question="PPARα 在 MASLD 肝脏脂质代谢中扮演什么角色？",
        values=['{"success":true,"kg_name":"NAFLDkb","count":0,"data":[]}'],
    )
    assert result.status == "insufficient"
    assert result.score == 0.0
    assert result.matched_concepts == []
    assert {"MASLD", "liver", "PPARA", "lipid_metabolism"} <= set(
        result.missing_concepts
    )
    assert result.reasons == ["observation_empty_result"]


def test_mechanism_combined_graph_and_literature_evidence_can_be_relevant() -> None:
    result = ObservationValidator(AgentRegistry()).validate(
        plugin_id="scp-hub",
        capability_id="mechanism_relation_search",
        question="PPARα 在 MASLD 肝脏脂质代谢中扮演什么角色？",
        values=[
            '{"success":true,"count":1,"data":[{"target":"PPARA",'
            '"context":"NAFLD hepatic tissue"}]}',
            '{"output":[{"paper_title":"PPAR-alpha control of lipid metabolism"}]}',
        ],
    )
    assert result.relevant is True
    assert result.missing_concepts == []


def test_protocol_validation_checks_declared_structure() -> None:
    result = ObservationValidator(AgentRegistry()).validate_protocol(
        plugin_id="scp-hub",
        capability_id="validation_protocol",
        question="设计实验方案",
        values=["细胞模型 Huh7；药物干预；对照组；剂量梯度；主要终点；统计方法 ANOVA"],
    )
    assert result["complete"] is True


def test_protocol_validation_marks_missing_statistics() -> None:
    result = ObservationValidator(AgentRegistry()).validate_protocol(
        plugin_id="scp-hub",
        capability_id="validation_protocol",
        question="设计实验方案",
        values=["细胞模型、药物干预、对照组、剂量梯度、主要终点"],
    )
    assert result["complete"] is False
    assert "statistics" in result["missing_fields"]


def test_protocol_validation_flags_suspicious_control_direction() -> None:
    result = ObservationValidator(AgentRegistry()).validate_protocol(
        plugin_id="scp-hub",
        capability_id="validation_protocol",
        question="设计脂肪酸氧化实验",
        values=["模型、干预、对照、剂量、终点、统计；Etomoxir 作为阳性对照促进脂肪酸氧化"],
    )
    assert result["status"] == "review_required"
    assert result["requires_review"] is True
