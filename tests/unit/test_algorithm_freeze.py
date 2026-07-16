from scripts.check_algorithm_freeze import verify_algorithm_freeze


def test_frozen_algorithm_files_have_not_drifted() -> None:
    assert verify_algorithm_freeze() == []
