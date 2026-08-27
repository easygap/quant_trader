"""거래일 달력을 실제 시장 데이터와 대조한다.

왜 필요한가(2026-08-26 실측):
config/holidays.yaml에 2026-07-17이 빠져 있어 그날이 거래일로 판정됐다. 그만큼 스냅샷
커버리지 분모가 부풀려져 두 트랙이 실제보다 낮게 나왔다 — kr_diversified_hold 96.3%가
94.5%로, kr_pocket 96.9%가 93.9%로. 승격 기준이 95%라 이 한 줄이 판정을 뒤집는다.

달력 오류는 양방향으로 조용히 해롭다:
  휴장일을 거래일로 오인 → 분모 과대 → 커버리지 저평가, 없는 결측을 쫓게 된다
  거래일을 휴장일로 오인 → 진짜 결측이 집계에서 사라진다(더 위험)

자동 갱신(pykrx)이 이 환경에서 깨져 있어 조용히 놓친 것이므로, 달력을 신뢰하지 말고
시장이 실제로 열렸는지를 지수 데이터로 직접 확인한다.

사용:
    .venv\\Scripts\\python.exe tools/verify_trading_calendar.py
    .venv\\Scripts\\python.exe tools/verify_trading_calendar.py --start 2026-01-01 --end 2026-12-31

종료 코드: 0 = 일치, 1 = 불일치 발견(수정 필요).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BENCHMARK = "KS11"
_WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]


def market_open_days(start: date, end: date, symbol: str = BENCHMARK) -> set[date] | None:
    """지수 데이터가 존재하는 날 = 시장이 열린 날. 조회 실패 시 None."""
    try:
        import FinanceDataReader as fdr

        df = fdr.DataReader(symbol, start.isoformat(), end.isoformat())
        if df is None or df.empty:
            return None
        return {d.date() for d in df.index}
    except Exception as exc:
        print(f"시장 데이터 조회 실패({symbol}): {exc}")
        return None


def compare(start: date, end: date) -> dict:
    """달력 판정 vs 실제 개장 여부를 대조한다.

    반환: {"wrong_open": [...], "wrong_closed": [...], "checked": int}
      wrong_open   달력은 거래일이라는데 시장은 닫혀 있던 날 (분모 과대)
      wrong_closed 달력은 휴장이라는데 시장은 열려 있던 날 (결측 은폐)
    """
    from config.config_loader import Config
    from core.trading_hours import TradingHours

    opened = market_open_days(start, end)
    if opened is None:
        return {"error": "시장 데이터를 가져오지 못해 대조 불가", "wrong_open": [], "wrong_closed": []}

    # 데이터가 아직 안 올라온 최근 날은 '휴장'이 아니라 '모름'이다. 장중에 돌리면
    # 오늘 봉이 없어서 오늘이 휴장으로 오판된다 — 확인 범위를 지수의 마지막 봉까지로
    # 자른다. 없는 사실을 오류로 보고하지 않는 게 이 도구의 존재 이유다.
    last_known = max(opened)
    if end > last_known:
        end = last_known

    th = TradingHours(Config.get())
    wrong_open: list[date] = []
    wrong_closed: list[date] = []
    checked = 0
    d = start
    while d <= end:
        # 주말은 양쪽 다 휴장이 자명하므로 건너뛴다(데이터 없음 = 정상).
        if d.weekday() < 5:
            checked += 1
            says_trading = th.is_trading_day(d)
            actually_open = d in opened
            if says_trading and not actually_open:
                wrong_open.append(d)
            elif not says_trading and actually_open:
                wrong_closed.append(d)
        d += timedelta(days=1)

    return {
        "wrong_open": wrong_open,
        "wrong_closed": wrong_closed,
        "checked": checked,
        "verified_through": end,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="거래일 달력 vs 실제 시장 데이터 대조")
    ap.add_argument("--start", default=None, help="시작일 (기본: 180일 전)")
    ap.add_argument("--end", default=None, help="종료일 (기본: 오늘)")
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=180)

    print(f"거래일 달력 대조: {start} ~ {end} (기준 {BENCHMARK})")
    result = compare(start, end)
    if result.get("error"):
        print(f"  ⚠️ {result['error']}")
        return 0  # 데이터 소스 장애를 달력 오류로 보고하지 않는다

    wo, wc = result["wrong_open"], result["wrong_closed"]
    through = result.get("verified_through")
    print(f"  평일 {result['checked']}일 확인 (~{through} 까지 — 지수 마지막 봉 기준)")

    if wo:
        print(f"\n  ❌ 거래일로 판정했지만 실제 휴장 ({len(wo)}일) — 커버리지 분모 과대")
        for d in wo:
            print(f"       {d} ({_WEEKDAY[d.weekday()]})")
        print("     → config/holidays.yaml 에 추가하세요.")
    if wc:
        print(f"\n  ❌ 휴장으로 판정했지만 실제 개장 ({len(wc)}일) — 진짜 결측이 가려짐")
        for d in wc:
            print(f"       {d} ({_WEEKDAY[d.weekday()]})")
        print("     → config/holidays.yaml 에서 제거하세요.")

    if not wo and not wc:
        print("  ✅ 달력과 시장 데이터 일치")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
