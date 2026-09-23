"""Live 진입 전 공통 readiness gate."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from loguru import logger

# 데이터 소스 신선도 점검 대상: 벤치마크는 바스켓 평가와 같은 KOSPI 지수.
_DATA_HEALTH_BENCHMARK = "KS11"
# 신호 전략 게이트의 대표 종목(바스켓처럼 설정에 보유 종목 목록이 없으므로).
_DATA_HEALTH_DEFAULT_SYMBOL = "005930"
# 마지막 봉이 직전 거래일보다 이만큼(거래일) 늦는 것까지는 허용한다 — 거래일 달력
# 오류 하루(예: 2026-09-28 대체공휴일 오표기 의심) 때문에 멀쩡한 피드를 막거나,
# 반대로 틀린 달력에 맞춰 묵은 피드를 통과시키지 않도록 여유는 1거래일로 둔다.
_DATA_FRESHNESS_TOLERANCE_TRADING_DAYS = 1
_DATA_HEALTH_WINDOW_CALENDAR_DAYS = 10


def check_basket_live_readiness(config, strategy_name: str) -> list[str]:
    """바스켓 승인 단위(basket_rebalance:<basket>)의 live 전환 게이트.

    바스켓은 신호 전략의 canonical promotion 체계(canonical bundle·live_candidate
    승격·벤치마크 양의 초과수익) 밖이다 — 특히 "벤치마크 초과수익" 요구는 베타
    전략의 정착된 결론(시장 초과 불가)과 모순이라 영구 통과 불가가 된다.
    대신 docs/BASKET_PAPER_EVALUATION.md의 승격 기준으로 판정한다:

      1) 승인 단위에 바스켓 이름이 명시돼 있고(basket_rebalance:<name>),
         baskets.yaml에 존재 + enabled=true
      2) 목표 비중 합 = 1.0 (±0.1%)
      3) paper 운영 평가(60영업일·스냅샷 커버리지 ≥95%·dead-letter 0건·
         비용 드래그 ≤1%/년) 판정이 PASS_CANDIDATE

    모든 예외는 fail-closed(이슈 추가)로 처리한다. 빈 리스트 = 통과.
    """
    issues: list[str] = []

    parts = str(strategy_name or "").split(":", 1)
    basket_name = parts[1].strip() if len(parts) == 2 else ""
    if not basket_name:
        return [
            "바스켓 live 승인 단위에 바스켓 이름이 없습니다 — "
            "'basket_rebalance:<basket>' 형식이어야 합니다."
        ]

    try:
        from core.basket_rebalancer import BasketRebalancer

        baskets_cfg = BasketRebalancer._load_baskets_config()
        basket = baskets_cfg.get(basket_name)
        if basket is None:
            return [f"바스켓 '{basket_name}'이 baskets.yaml에 없습니다."]
        if not basket.get("enabled", False):
            issues.append(
                f"바스켓 '{basket_name}'이 enabled=false — paper 운영(트랙레코드)부터 시작하세요."
            )
        if (basket.get("promotion") or {}).get("paper_only", False):
            issues.append(
                f"바스켓 '{basket_name}'은 모의투자 전용(paper_only)입니다. "
                "변경한 운용 규칙을 검증하기 전에는 실전으로 전환할 수 없습니다."
            )
        holdings = basket.get("holdings", {}) or {}
        total_w = sum(float(w) for w in holdings.values())
        if abs(total_w - 1.0) > 0.001:
            issues.append(
                f"바스켓 '{basket_name}' 목표 비중 합 {total_w:.4f} ≠ 1.0 — baskets.yaml 확인."
            )
    except Exception as exc:
        return [f"바스켓 설정 검증 오류(fail-closed): {exc}"]

    try:
        from core.basket_evaluation import collect_basket_paper_evaluation

        # 반드시 '이 바스켓'의 기록으로 판정한다 — 이름 없이 합산 평가하면
        # 다른 바스켓의 60일 트랙레코드로 신규 바스켓이 승격되는 구멍이 생긴다.
        # 승격 기간(promotion.min_trading_days, 기본 60)은 collect가 바스켓 설정에서
        # 해석한다 — CLI와 게이트가 같은 값으로 판정(단일 소스).
        result, _label = collect_basket_paper_evaluation(
            config=config, include_benchmark=False, basket_name=basket_name,
        )
        if result["verdict"] != "PASS_CANDIDATE":
            detail = "; ".join(result["issues"]) if result["issues"] else (
                f"진행 {result['progress_days']}/{result['min_trading_days']} 영업일"
            )
            # KIS 모의투자 서버(use_mock=true)는 실돈이 아니다 — 게이트의 목적은
            # 실자금 보호이므로, 모의서버에서의 live 경로 리허설(런북 Phase 1)은
            # 평가 기간을 기다리지 않고 허용한다. 실계좌(use_mock=false)는 그대로 차단.
            # 단, use_mock=true라도 mock_url이 실전 도메인으로 오설정돼 있으면 주문이
            # 실서버로 가므로(KISApi는 use_mock 시 mock_url을 base_url로 사용) 실효
            # 도메인이 모의투자(openapivts)일 때만 완화한다 — fail-closed.
            kis_cfg = (getattr(config, "kis_api", {}) or {})
            effective_mock_url = str(
                kis_cfg.get("mock_url", "https://openapivts.koreainvestment.com:29443")
            )
            is_mock_rehearsal = (
                bool(kis_cfg.get("use_mock", True))
                and "openapivts" in effective_mock_url
            )
            if is_mock_rehearsal:
                logger.warning(
                    "바스켓 '{}' 평가 미통과({})지만 KIS 모의투자 서버(use_mock=true, vts 도메인) — "
                    "live 경로 리허설 허용. 실계좌 전환 전 평가 통과 필수.",
                    basket_name, result["verdict"],
                )
            else:
                issues.append(
                    f"바스켓 paper 운영 평가 미통과 (verdict={result['verdict']}): {detail}. "
                    "기준: docs/BASKET_PAPER_EVALUATION.md"
                )
    except Exception as exc:
        issues.append(f"바스켓 paper 운영 평가 조회 오류(fail-closed): {exc}")

    return issues


def check_live_readiness_gate(config, strategy_name: str) -> list[str]:
    """
    라이브 전 필수 검증 게이트.

    하나라도 실패하면 live 주문 경로로 진입하지 않는다. 빈 리스트는 통과를 의미한다.
    바스켓 승인 단위(basket_rebalance[:<basket>])는 신호 전략의 canonical promotion
    체계 대신 바스켓 전용 게이트(check_basket_live_readiness)로 판정한다 — 공통
    데이터 소스 health check는 두 경로 모두 동일하게 적용된다.
    """
    if str(strategy_name or "").split(":", 1)[0] == "basket_rebalance":
        issues = check_basket_live_readiness(config, strategy_name)
    else:
        from core.live_gate import validate_live_readiness

        issues = validate_live_readiness(config, strategy_name)
    if issues:
        return issues

    try:
        issues.extend(check_data_source_freshness(config, strategy_name))
    except Exception as exc:
        issues.append(f"데이터 소스 health check 오류: {exc}")

    return issues


def _health_check_symbols(strategy_name: str) -> list[str]:
    """신선도를 볼 종목: 바스켓이면 보유 종목 전부, 아니면 대표 종목. 끝에 벤치마크."""
    symbols: list[str] = []
    parts = str(strategy_name or "").split(":", 1)
    if parts[0] == "basket_rebalance" and len(parts) == 2 and parts[1].strip():
        from core.basket_rebalancer import BasketRebalancer

        basket = BasketRebalancer._load_baskets_config().get(parts[1].strip()) or {}
        symbols = [str(symbol) for symbol in (basket.get("holdings") or {})]
    if not symbols:
        symbols = [_DATA_HEALTH_DEFAULT_SYMBOL]
    return symbols + [_DATA_HEALTH_BENCHMARK]


def _recent_trading_days(trading_hours, today: date, count: int) -> list[date]:
    """today 직전의 KRX 거래일 count개(최근 → 과거). 달력 이상이면 더 적을 수 있다."""
    found: list[date] = []
    day = today - timedelta(days=1)
    for _ in range(62):
        if trading_hours.is_trading_day(datetime(day.year, day.month, day.day)):
            found.append(day)
            if len(found) >= count:
                break
        day -= timedelta(days=1)
    return found


def _last_bar_date(df) -> date | None:
    import pandas as pd

    index = df.index
    if isinstance(index, pd.DatetimeIndex) and len(index):
        return index.max().date()
    if "date" in df.columns:
        values = pd.to_datetime(df["date"], errors="coerce").dropna()
        if len(values):
            return values.max().date()
    return None


def check_data_source_freshness(
    config,
    strategy_name: str,
    *,
    today: date | None = None,
    collector=None,
) -> list[str]:
    """live 게이트의 데이터 소스 점검 — 최근 봉이 직전 거래일 수준으로 갱신되는지 본다.

    예전에는 005930의 고정 구간(2026-01-01~03-26)만 받아 봐서, 과거 이력은 주지만
    갱신이 멈춘 피드(live 사이징이 묵은 가격을 쓰게 되는 바로 그 고장)와 바스켓
    자신의 종목(069500/357870 등) 문제를 잡지 못했다. 이제 바스켓 보유 종목 전부와
    벤치마크의 최근 약 10일을 받아, 마지막 봉이 직전 거래일에서 1거래일 이내인지
    확인하고 실제 마지막 봉 날짜를 로그로 남긴다. 빈 리스트 = 통과.
    """
    from core.trading_hours import TradingHours

    today = today or datetime.now().date()
    recent = _recent_trading_days(
        TradingHours(config), today, 1 + _DATA_FRESHNESS_TOLERANCE_TRADING_DAYS,
    )
    if len(recent) <= _DATA_FRESHNESS_TOLERANCE_TRADING_DAYS:
        return [
            "데이터 소스 health check 실패: 거래일 달력에서 최근 거래일을 찾지 못했습니다 "
            "— config/holidays.yaml을 확인하세요."
        ]
    previous_trading_day = recent[0]
    oldest_allowed = recent[-1]
    start = min(
        today - timedelta(days=_DATA_HEALTH_WINDOW_CALENDAR_DAYS),
        oldest_allowed - timedelta(days=3),
    )

    if collector is None:
        from core.data_collector import DataCollector

        collector = DataCollector()

    issues: list[str] = []
    for symbol in _health_check_symbols(strategy_name):
        try:
            df = collector.fetch_korean_stock(symbol, start.isoformat(), today.isoformat())
        except Exception as exc:
            issues.append(f"데이터 소스 health check 오류: {symbol} 수집 실패 — {exc}")
            continue
        if df is None or df.empty:
            issues.append(
                f"데이터 소스 health check 실패: {symbol} 최근 데이터 없음 ({start}~{today})."
            )
            continue
        last_bar = _last_bar_date(df)
        if last_bar is None:
            issues.append(f"데이터 소스 health check 실패: {symbol} 마지막 봉 날짜를 읽지 못함.")
            continue
        logger.info(
            "데이터 소스 health check: {} 마지막 봉 {} (직전 거래일 {}, 허용 하한 {})",
            symbol, last_bar, previous_trading_day, oldest_allowed,
        )
        if last_bar < oldest_allowed:
            issues.append(
                f"데이터 소스 갱신 멈춤 의심: {symbol} 마지막 봉 {last_bar} < 허용 하한 "
                f"{oldest_allowed} (직전 거래일 {previous_trading_day}, "
                f"{_DATA_FRESHNESS_TOLERANCE_TRADING_DAYS}거래일 허용)."
            )

    history = (collector.get_last_source_info() or {}).get("history") or {}
    kis_symbols = sorted(symbol for symbol, source in history.items() if source == "KIS")
    if kis_symbols:
        issues.append(
            "데이터 소스가 KIS(비수정주가) — FDR 또는 yfinance 사용을 권장합니다: "
            + ", ".join(kis_symbols)
        )
    return issues
