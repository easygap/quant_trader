"""Live 진입 전 공통 readiness gate."""

from __future__ import annotations

from loguru import logger


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
            if bool((getattr(config, "kis_api", {}) or {}).get("use_mock", True)):
                logger.warning(
                    "바스켓 '{}' 평가 미통과({})지만 KIS 모의투자 서버(use_mock=true) — "
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
        from core.data_collector import DataCollector

        dc = DataCollector()
        test_df = dc.fetch_korean_stock("005930", "2026-01-01", "2026-03-26")
        if test_df.empty:
            issues.append("데이터 소스 health check 실패: 005930 데이터 수집 불가.")
        source_info = dc.get_last_source_info()
        if source_info.get("source") == "KIS":
            issues.append(
                "데이터 소스가 KIS(비수정주가) — FDR 또는 yfinance 사용을 권장합니다."
            )
    except Exception as exc:
        issues.append(f"데이터 소스 health check 오류: {exc}")

    return issues
