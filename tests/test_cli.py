from __future__ import annotations

import pytest

from cost_per_task import __version__, cli


def test_version_flag_prints_the_version_and_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"cpt {__version__}"
