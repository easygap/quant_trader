"""
퀀트 트레이더 시스템 — 통합 검증 스크립트 (Python 3.14 호환)
- 설정, DB, 지표, 신호, 리스크, 백테스팅, 리포트, 대시보드, 디스코드 봇 전체 검증
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

passed = 0
failed = 0

def assert_(value):
    if not value:
        raise AssertionError("값이 False/None/빈값")

def check(name, callback):
    global passed, failed
    try:
        callback()
        print(f"  ✅ {name}")
        passed += 1
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        failed += 1

# === 1. 설정 로더 ===
print("=" * 55)
print("1. 설정 로더")
print("=" * 55)
from config.config_loader import Config
config = Config.get()
check("YAML 로드", lambda: assert_(config.active_strategy))
check("디스코드 설정", lambda: assert_(isinstance(config.discord, dict)))
check("리스크 파라미터", lambda: assert_(config.risk_params))

# === 2. DB 초기화 ===
print("\n" + "=" * 55)
print("2. 데이터베이스")
print("=" * 55)
from database.models import init_database
check("테이블 생성", lambda: init_database())

# === 3. 샘플 데이터 생성 ===
print("\n" + "=" * 55)
print("3. 샘플 데이터")
print("=" * 55)
np.random.seed(42)
days = 500
dates = pd.date_range(end=datetime.now(), periods=days, freq="B")
returns = np.random.normal(0.0003, 0.02, days)
prices = 50000 * np.cumprod(1 + returns)
df = pd.DataFrame({
    "open": prices * (1 + np.random.uniform(-0.01, 0.01, days)),
    "high": prices * (1 + np.random.uniform(0, 0.03, days)),
    "low": prices * (1 - np.random.uniform(0, 0.03, days)),
    "close": prices,
    "volume": np.random.randint(100000, 5000000, days),
}, index=dates)
df.index.name = "date"
check("500일 OHLCV 생성", lambda: assert_(len(df) == 500))

# === 4. 기술 지표 계산 ===
print("\n" + "=" * 55)
print("4. 기술 지표 엔진")
print("=" * 55)
from core.indicator_engine import IndicatorEngine
engine = IndicatorEngine()
df_ind = engine.calculate_all(df)
check("RSI 계산", lambda: assert_("rsi" in df_ind.columns))
check("MACD 계산", lambda: assert_("macd" in df_ind.columns))
check("볼린저밴드", lambda: assert_("bb_upper" in df_ind.columns))
check("ADX 계산", lambda: assert_("adx" in df_ind.columns))
check("ATR 계산", lambda: assert_("atr" in df_ind.columns))
check("OBV 계산", lambda: assert_("obv" in df_ind.columns))
check("거래량 비율", lambda: assert_("volume_ratio" in df_ind.columns))

# === 5. 매매 신호 ===
print("\n" + "=" * 55)
print("5. 매매 신호 생성")
print("=" * 55)
from core.signal_generator import SignalGenerator
generator = SignalGenerator()
df_signal = generator.generate(df_ind)
check("신호 컬럼 생성", lambda: assert_("signal" in df_signal.columns))
latest = generator.get_latest_signal(df_signal)
check("최신 신호 반환", lambda: assert_(latest["signal"] in ("BUY", "SELL", "HOLD")))

# === 6. 리스크 관리 ===
print("\n" + "=" * 55)
print("6. 리스크 관리")
print("=" * 55)
from core.risk_manager import RiskManager
rm = RiskManager()
check("손절가 계산", lambda: assert_(rm.calculate_stop_loss(50000) > 0))
check("익절가 계산", lambda: assert_(rm.calculate_take_profit(50000)["target_final"] > 0))
check("포지션 사이징", lambda: assert_(rm.calculate_position_size(10000000, 50000, 48000) > 0))
check("MDD 체크", lambda: assert_(rm.check_mdd(10000000)["mdd"] >= 0))
check("거래 비용 계산", lambda: assert_(rm.calculate_transaction_costs(50000, 10, "BUY")["total_cost"] > 0))

# === 7. 전략 클래스 ===
print("\n" + "=" * 55)
print("7. 매매 전략")
print("=" * 55)
from strategies.scoring_strategy import ScoringStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.trend_following import TrendFollowingStrategy
check("스코어링 전략", lambda: assert_(ScoringStrategy().generate_signal(df)["signal"]))
check("평균회귀 전략", lambda: assert_(MeanReversionStrategy().generate_signal(df)["signal"]))
check("추세추종 전략", lambda: assert_(TrendFollowingStrategy().generate_signal(df)["signal"]))

# === 8. 백테스팅 ===
print("\n" + "=" * 55)
print("8. 백테스팅")
print("=" * 55)
from backtest.backtester import Backtester
bt = Backtester()
result = bt.run(df, strategy_name="scoring")
check("백테스팅 실행", lambda: assert_(result.get("metrics")))
check("수익률 계산", lambda: assert_("total_return" in result["metrics"]))
check("샤프 지수", lambda: assert_("sharpe_ratio" in result["metrics"]))

# === 9. 리포트 생성 ===
print("\n" + "=" * 55)
print("9. 리포트 생성")
print("=" * 55)
from backtest.report_generator import ReportGenerator
rg = ReportGenerator()
check("텍스트 리포트", lambda: assert_(rg.generate_text_report(result)))
check("HTML 리포트", lambda: assert_(rg.generate_html_report(result)))

# === 10. 디스코드 봇 ===
print("\n" + "=" * 55)
print("10. 디스코드 봇")
print("=" * 55)
from monitoring.discord_bot import DiscordBot
bot = DiscordBot()
check("메시지 발송 (콘솔)", lambda: assert_(bot.send_message("테스트")))
check("매매 알림", lambda: bot.send_trade_alert({"action": "BUY", "symbol": "005930", "price": 50000, "quantity": 10}))

# === 11. 대시보드 ===
print("\n" + "=" * 55)
print("11. 대시보드")
print("=" * 55)
from monitoring.dashboard import Dashboard
dash = Dashboard()
check("대시보드 로드", lambda: assert_(dash))
check("요약 라인", lambda: assert_(dash.show_summary_line()))

# === 12. 웹소켓 핸들러 ===
print("\n" + "=" * 55)
print("12. 웹소켓 핸들러")
print("=" * 55)
from api.websocket_handler import WebSocketHandler
ws = WebSocketHandler()
check("핸들러 초기화", lambda: assert_(ws))
check("콜백 등록", lambda: ws.on_price_update(lambda x: None))

# === 13. KIS API ===
print("\n" + "=" * 55)
print("13. KIS API")
print("=" * 55)
from api.kis_api import KISApi
api = KISApi()
check("API 초기화", lambda: assert_(api))

# === 14. 포트폴리오 관리 ===
print("\n" + "=" * 55)
print("14. 포트폴리오 관리")
print("=" * 55)
from core.portfolio_manager import PortfolioManager
pm = PortfolioManager()
check("포트폴리오 요약", lambda: assert_(pm.get_portfolio_summary()["total_value"] > 0))

# =============================================
# 결과 요약
# =============================================
print("\n" + "=" * 55)
total = passed + failed
print(f"  🏁 전체 결과: {passed}/{total} 통과 ({failed}건 실패)")
if failed == 0:
    print("  🎉 모든 테스트 통과!")
print("=" * 55)

sys.exit(0 if failed == 0 else 1)
