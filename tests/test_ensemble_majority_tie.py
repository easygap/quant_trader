"""앙상블 다수결 동률 처리 회귀 테스트.

최다 득표가 동률(예: 4개 구성에서 BUY 2 / SELL 2)이면 '다수'가 없으므로 HOLD여야
한다. 이전 구현은 Counter.most_common(1)이 첫 삽입 신호를 반환해 동률을 방향성
매매로 깨뜨렸다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.config_loader import Config
from core.strategy_ensemble import StrategyEnsemble


def _ensemble():
    Config._instance = None
    return StrategyEnsemble(Config.get(), skip_independence_check=True)


def _parts(*sigs):
    return [(f"s{i}", sig, 1.0) for i, sig in enumerate(sigs)]


def test_majority_tie_returns_hold():
    ens = _ensemble()
    # 2 BUY / 2 SELL → 동률 → HOLD
    assert ens._resolve_majority_vote(_parts("BUY", "BUY", "SELL", "SELL")) == "HOLD"
    # BUY / SELL → 1-1 동률 → HOLD
    assert ens._resolve_majority_vote(_parts("BUY", "SELL")) == "HOLD"


def test_clear_majority_wins():
    ens = _ensemble()
    assert ens._resolve_majority_vote(_parts("BUY", "BUY", "SELL")) == "BUY"
    assert ens._resolve_majority_vote(_parts("SELL", "SELL", "HOLD")) == "SELL"
    assert ens._resolve_majority_vote(_parts("BUY")) == "BUY"


def test_tie_with_hold_top_returns_hold():
    ens = _ensemble()
    # BUY 1 / HOLD 1 / SELL 1 → 3중 동률 → HOLD
    assert ens._resolve_majority_vote(_parts("BUY", "HOLD", "SELL")) == "HOLD"
