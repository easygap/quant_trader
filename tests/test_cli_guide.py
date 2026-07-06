import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_main(*args):
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=15,
    )


def test_main_without_args_prints_guide_only():
    result = _run_main()

    assert result.returncode == 0
    assert "QUANT TRADER 실행 가이드" in result.stdout
    assert "python main.py --mode backtest" in result.stdout
    assert "퀀트 트레이더 시작" not in result.stderr


def test_guide_mode_accepts_strategy_and_symbol_defaults():
    result = _run_main("--mode", "guide", "--strategy", "scoring", "--symbol", "000660")

    assert result.returncode == 0
    assert "python main.py --mode guide" in result.stdout
    assert "python main.py --mode backtest --strategy scoring --symbol 000660" in result.stdout


def test_guide_lists_operation_modes():
    # 운영 모드(health/deploy_check/weekly_report)가 가이드에 빠지면 운영자가
    # 존재 자체를 모른다 — 가이드 노출을 고정한다.
    result = _run_main("--mode", "guide")

    assert result.returncode == 0
    assert "python main.py --mode health" in result.stdout
    assert "python main.py --mode deploy_check" in result.stdout
    assert "python main.py --mode weekly_report" in result.stdout
