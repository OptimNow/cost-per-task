from pathlib import Path

from spend import total_by_team

CSV = str(Path(__file__).parent / "spend.csv")


def test_totals_per_team():
    assert total_by_team(CSV) == {"platform": 1628.75, "data": 426.0, "web": 42.42}


def test_missing_file_raises():
    import pytest

    with pytest.raises(FileNotFoundError):
        total_by_team("does-not-exist.csv")
