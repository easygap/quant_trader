"""종목 성격 분류 — '개별 기업'을 전제로 한 필터를 어디에 적용하지 않을지 한 곳에 둔다.

이 저장소에서 반복된 실패 패턴 하나: 개별 주식용으로 만든 전역 안전장치가 ETF를 만나면
설계를 실행 불가능하게 만든다. 2026-08-26에 kr_pocket(ETF 2종)에서 네 개가 연달아 걸렸다.

    쌍별 상관 거부권      → weight_policy_managed로 위임 (해결)
    단일 종목 20% 상한    → 선언 비중 기반 상한으로 위임 (해결)
    업종 비중(KRX Sector) → ETF는 업종 코드가 없다        ← 이 모듈
    실적 발표일 필터      → ETF는 실적일·기업코드가 없다   ← 이 모듈

뒤 두 개는 '기업 단위'로만 의미가 있는 검사다. 지수 ETF는 그 자체가 여러 업종에 걸친
묶음이고, 금리 파킹 ETF는 주식도 아니다. 둘 다 fail-closed라 등록하지 않으면 '매핑 없음 /
조회 불가' 사유로 매수가 영원히 거부된다.

면제되는 것은 그 두 검사뿐이다. 노출 상한·유동성·갭·현금·거래중단은 그대로 적용된다.
목록은 config/risk_params.yaml의 instrument_classes.non_company_symbols에 둔다.
"""

from __future__ import annotations

from typing import Any


def non_company_symbols(risk_params: dict[str, Any] | None) -> set[str]:
    """개별 기업이 아닌 종목(ETF·펀드) 코드 집합. 미설정이면 빈 집합."""
    classes = (risk_params or {}).get("instrument_classes") or {}
    raw = classes.get("non_company_symbols") or []
    if isinstance(raw, (str, bytes)):
        return set()
    try:
        return {str(s).strip() for s in raw if str(s).strip()}
    except TypeError:
        return set()


def is_non_company_symbol(symbol: str, risk_params: dict[str, Any] | None) -> bool:
    """이 종목이 기업 단위 필터(업종·실적)의 면제 대상인가."""
    if not symbol:
        return False
    return str(symbol).strip() in non_company_symbols(risk_params)
