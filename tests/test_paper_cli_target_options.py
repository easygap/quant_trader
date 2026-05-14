import importlib
import sys

import pytest


@pytest.mark.parametrize(
    "module_name, argv",
    [
        (
            "tools.paper_preflight",
            ["paper_preflight.py", "--strategy", "scoring", "--all"],
        ),
        (
            "tools.paper_launch_readiness",
            ["paper_launch_readiness.py", "--strategy", "scoring", "--all"],
        ),
    ],
)
def test_paper_cli_rejects_conflicting_target_options(monkeypatch, capsys, module_name, argv):
    """운영 대상 범위가 모순되면 DB 초기화/아티팩트 생성 전에 실패한다."""
    module = importlib.import_module(module_name)
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
