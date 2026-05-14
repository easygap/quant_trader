import importlib
import sys
from types import SimpleNamespace

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


def _patch_init_database(monkeypatch):
    import database.models as models

    monkeypatch.setattr(models, "init_database", lambda: None)


def test_paper_preflight_cli_exits_nonzero_when_entry_blocked(monkeypatch):
    import core.paper_preflight as core_preflight
    import tools.paper_preflight as cli

    _patch_init_database(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["paper_preflight.py", "--strategy", "scoring"])
    monkeypatch.setattr(cli, "_print_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        core_preflight,
        "run_preflight",
        lambda *args, **kwargs: SimpleNamespace(overall="warn", entry_allowed=False),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1


def test_paper_preflight_all_exits_nonzero_when_any_strategy_blocks(monkeypatch):
    import core.paper_preflight as core_preflight
    import tools.paper_preflight as cli

    _patch_init_database(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["paper_preflight.py", "--all"])
    monkeypatch.setattr(cli, "_discover_strategies", lambda: ["ready_s", "blocked_s"])
    monkeypatch.setattr(cli, "_print_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(core_preflight, "_save_session_bootstrap", lambda *args, **kwargs: "session.json")
    monkeypatch.setattr(
        core_preflight,
        "run_preflight",
        lambda strategy, *args, **kwargs: SimpleNamespace(
            overall="warn" if strategy == "blocked_s" else "pass",
            entry_allowed=strategy != "blocked_s",
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1


def test_paper_launch_readiness_cli_exits_nonzero_when_not_ready(monkeypatch):
    import tools.paper_launch_readiness as cli

    _patch_init_database(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["paper_launch_readiness.py", "--strategy", "scoring"])
    monkeypatch.setattr(cli, "_run_one", lambda *args, **kwargs: False)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1


def test_paper_launch_readiness_report_only_keeps_zero_exit(monkeypatch):
    import tools.paper_launch_readiness as cli

    _patch_init_database(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["paper_launch_readiness.py", "--strategy", "scoring", "--report-only"])
    monkeypatch.setattr(cli, "_run_one", lambda *args, **kwargs: False)

    cli.main()


def test_paper_launch_readiness_all_exits_nonzero_when_any_strategy_not_ready(monkeypatch):
    import core.strategy_universe as universe
    import tools.paper_launch_readiness as cli

    _patch_init_database(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["paper_launch_readiness.py", "--all"])
    monkeypatch.setattr(universe, "get_paper_strategy_names", lambda: ["ready_s", "blocked_s"])
    monkeypatch.setattr(cli, "_run_one", lambda strategy, *_args: strategy == "ready_s")

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
