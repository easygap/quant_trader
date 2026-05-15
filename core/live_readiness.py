"""Live 진입 전 공통 readiness gate."""

from __future__ import annotations


def check_live_readiness_gate(config, strategy_name: str) -> list[str]:
    """
    라이브 전 필수 검증 게이트.

    하나라도 실패하면 live 주문 경로로 진입하지 않는다. 빈 리스트는 통과를 의미한다.
    """
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
