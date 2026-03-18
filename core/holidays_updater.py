"""
휴장일 파일(holidays.yaml) 자동 업데이트.

pykrx로 휴장일을 조회해 config/holidays.yaml에 반영.
pykrx 실패 시 연도별 fallback 목록 사용. 매년 수동 관리 없이 갱신 가능.
"""

from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Set

import yaml
from loguru import logger

# 프로젝트 루트
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# pykrx 미사용/실패 시 사용할 연도별 휴장일 (고정 공휴일 위주; 설·추석은 연도별 상이)
FALLBACK_BY_YEAR = {
    2025: {
        "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30",
        "2025-03-01", "2025-05-05", "2025-05-06", "2025-06-06",
        "2025-08-15", "2025-10-03", "2025-10-09", "2025-12-25",
    },
    2026: {
        "2026-01-01", "2026-01-27", "2026-01-28", "2026-01-29",
        "2026-03-01", "2026-05-05", "2026-05-24", "2026-06-06",
        "2026-08-15", "2026-09-24", "2026-09-25", "2026-09-26",
        "2026-10-03", "2026-10-09", "2026-12-25",
    },
    2027: {
        "2027-01-01", "2027-02-10", "2027-02-11", "2027-02-12",
        "2027-03-01", "2027-05-05", "2027-06-06", "2027-08-15",
        "2027-10-03", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-09",
        "2027-12-25",
    },
}


def _fetch_from_pykrx(year_from: int, year_to: int) -> Set[str]:
    """pykrx로 거래일을 조회해 비거래일(주말+휴장)을 휴장일 세트로 반환."""
    out = set()
    try:
        from pykrx import stock
        for year in range(year_from, year_to + 1):
            start = f"{year}0101"
            end = f"{year}1231"
            trading = stock.get_market_trading_date_by_date(start, end)
            if trading is None or trading.empty:
                continue
            all_days = set()
            d = date(year, 1, 1)
            end_d = date(year, 12, 31)
            while d <= end_d:
                all_days.add(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            trading_dates = set(trading.index.strftime("%Y-%m-%d").tolist())
            weekends = set()
            d = date(year, 1, 1)
            while d <= end_d:
                if d.weekday() >= 5:
                    weekends.add(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            out |= (all_days - trading_dates - weekends)
        return out
    except Exception as e:
        logger.warning("pykrx 휴장일 조회 실패: {} — fallback 사용", e)
        return set()


def _fetch_fallback(year_from: int, year_to: int) -> Set[str]:
    """연도 구간에 해당하는 fallback 휴장일 반환."""
    out = set()
    for y in range(year_from, year_to + 1):
        out |= FALLBACK_BY_YEAR.get(y, set())
    return out


def _read_existing(path: Path) -> Set[str]:
    """기존 holidays.yaml에서 날짜 목록 로드 (수동 추가분 유지)."""
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        lst = data.get("holidays", data.get("dates", []))
        return {str(d) for d in lst} if isinstance(lst, list) else set()
    except Exception as e:
        logger.warning("기존 holidays.yaml 읽기 실패: {}", e)
        return set()


def update_holidays_yaml(
    path: Path = None,
    year_from: int = None,
    year_to: int = None,
    merge_existing: bool = True,
) -> Path:
    """
    휴장일을 pykrx(또는 fallback)로 조회해 holidays.yaml에 저장.

    Args:
        path: 저장할 yaml 경로. None이면 config/holidays.yaml
        year_from: 대상 연도 시작 (None이면 현재 연도)
        year_to: 대상 연도 끝 (None이면 현재 연도 + 1)
        merge_existing: True면 기존 파일 내용과 병합 후 저장

    Returns:
        저장된 파일 경로
    """
    path = path or (_PROJECT_ROOT / "config" / "holidays.yaml")
    now = datetime.now()
    year_from = year_from if year_from is not None else now.year
    year_to = year_to if year_to is not None else now.year + 1

    existing = _read_existing(path) if merge_existing else set()

    fetched = _fetch_from_pykrx(year_from, year_to)
    if not fetched:
        fetched = _fetch_fallback(year_from, year_to)
        logger.info("휴장일 fallback 사용 ({}~{}년, {}일)", year_from, year_to, len(fetched))
    else:
        logger.info("pykrx 휴장일 조회 성공 ({}~{}년, {}일)", year_from, year_to, len(fetched))

    # 대상 연도 밖 기존 항목은 유지, 대상 연도는 fetched로 덮기
    merged = {d for d in existing if int(d[:4]) < year_from or int(d[:4]) > year_to}
    merged |= fetched

    sorted_dates = sorted(merged)

    content = {
        "holidays": sorted_dates,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 한국 증시 휴장일 — 자동 갱신 (pykrx + fallback). 수동 편집 가능.\n")
        f.write("# 갱신: python main.py --update-holidays\n\n")
        yaml.dump(content, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    logger.info("holidays.yaml 저장 완료: {} ({}일)", path, len(sorted_dates))
    return path
