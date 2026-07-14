"""Minimal smoke: pytest discovery and formal tree import roots."""


def test_smoke_truth() -> None:
    assert True


def test_packages_importable() -> None:
    import packages  # noqa: F401
    import packages.chem_core  # noqa: F401
    import packages.models  # noqa: F401
    import services  # noqa: F401
    import apps  # noqa: F401
