from agent.runtime.governance import frozen_ranking_mutation_requested


def test_frozen_ranking_mutation_is_source_agnostic() -> None:
    assert frozen_ranking_mutation_requested("根据实时文献重新调整候选优先级")
    assert frozen_ranking_mutation_requested("用新的实验结果重算排名")
    assert frozen_ranking_mutation_requested("重新排序主榜")


def test_read_only_explanation_and_new_screen_are_not_mutations() -> None:
    assert not frozen_ranking_mutation_requested("解释 Top1 为什么排名第一")
    assert not frozen_ranking_mutation_requested("查询最新研究文献")
    assert not frozen_ranking_mutation_requested("上传新的 SDF 发起一次筛选")
