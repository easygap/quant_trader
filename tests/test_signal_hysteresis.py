"""신호 히스터리시스 상태 전이 회귀 테스트.

exit 임계값은 실제 점수 레벨이며 부호 그대로 사용한다:
  - BUY → HOLD: 점수 < exit_buy_threshold
  - SELL → HOLD: 점수 > exit_sell_threshold
이전 버그: SELL 해제 조건이 `s > -exit_sell` 로 이중 부호화돼 exit_sell=-1 일 때
점수가 +1 위로 올라야만 SELL이 풀려 SELL 상태가 비정상적으로 끈끈했다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.signal_generator import SignalGenerator


class _HysteresisConfig:
    strategies = {
        "scoring": {
            "weights": {},
            "hysteresis": {
                "enabled": True,
                "exit_sell_threshold": -1,
                "exit_buy_threshold": 0.5,
            },
        }
    }
    indicators = {}


def _signals(scores):
    gen = SignalGenerator(_HysteresisConfig())
    df = pd.DataFrame({"total_score": scores})
    out = gen._generate_with_hysteresis(df, buy_threshold=2, sell_threshold=-2)
    return list(out["signal"].values)


def test_sell_releases_when_score_recovers_above_exit_sell():
    """SELL 진입(-2) 후 점수가 -1 위로 회복하면 HOLD로 해제된다(끈끈하지 않음)."""
    # 진입 -2 → SELL, -0.5(>-1) → HOLD 로 풀려야 한다.
    assert _signals([-2, -0.5]) == ["SELL", "HOLD"]
    # 회복이 0/+0.9 여도 이미 -1 초과 시점에 풀린다.
    assert _signals([-2, 0.0, 0.9]) == ["SELL", "HOLD", "HOLD"]


def test_sell_stays_while_score_below_exit_sell():
    """점수가 아직 -1 이하(여전히 약세)면 SELL 유지."""
    assert _signals([-2, -1.5, -1.0]) == ["SELL", "SELL", "SELL"]
    # -1 정확히는 해제 아님(> 비교), -0.9 에서 해제.
    assert _signals([-2, -0.9]) == ["SELL", "HOLD"]


def test_buy_releases_when_score_drops_below_exit_buy():
    """BUY 진입(+2) 후 점수가 0.5 아래로 약화되면 HOLD로 해제, 위면 유지."""
    assert _signals([2, 1.0]) == ["BUY", "BUY"]      # 1.0 >= 0.5 → 유지
    assert _signals([2, 0.3]) == ["BUY", "HOLD"]     # 0.3 < 0.5 → 해제


def test_no_direct_buy_to_sell_flip():
    """BUY에서 급락해도 한 봉에 바로 SELL로 가지 않고 HOLD를 거친다(과매매 방지)."""
    # BUY(+2) → 다음 봉 -3: BUY 해제(HOLD) 먼저, 그 다음 봉에서 SELL.
    assert _signals([2, -3, -3]) == ["BUY", "HOLD", "SELL"]


def test_hold_entry_thresholds():
    """HOLD에서 진입선 도달 시에만 BUY/SELL 진입."""
    assert _signals([1.9, 2.0]) == ["HOLD", "BUY"]
    assert _signals([-1.9, -2.0]) == ["HOLD", "SELL"]
