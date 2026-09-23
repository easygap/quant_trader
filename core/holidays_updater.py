"""
휴장일 파일(holidays.yaml) 자동 업데이트.

pykrx로 휴장일을 조회해 config/holidays.yaml에 반영.
pykrx 실패 시 연도별 fallback 목록 사용. 매년 수동 관리 없이 갱신 가능.
"""

from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional, Set

import yaml
from loguru import logger

# 프로젝트 루트
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# pykrx 미사용/실패 시 사용할 연도별 휴장일.
# 대체공휴일 규정: 설·추석 연휴는 '일요일' 또는 다른 공휴일과 겹칠 때만 대체한다
# (토요일 겹침은 대체 없음). 3·1절·광복절·개천절·한글날·어린이날·부처님오신날·
# 성탄절은 토·일 겹침 시 다음 평일 대체. 신정·현충일은 미적용.
# KRX 연말 휴장(마지막 거래일 12/31)도 포함한다 — 누락되면 휴장일이 거래일로
# 계산돼 트랙레코드 커버리지 분모가 부풀고 스냅샷 귀속(가격 기준일)이 어긋난다.
# 반대로 거래일을 휴장으로 넣으면 그날 스냅샷이 직전 거래일 행을 덮어써 결측이
# 숨는다. config/holidays.yaml 과 평일 휴장 집합이 같아야 한다(테스트로 고정).
FALLBACK_BY_YEAR = {
    2025: {
        "2025-01-01", "2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30",  # 1/27 임시공휴일
        "2025-03-01", "2025-03-03",  # 3·1절(토) 대체
        "2025-05-05", "2025-05-06", "2025-06-06",
        "2025-08-15", "2025-10-03",
        "2025-10-06", "2025-10-07", "2025-10-08",  # 추석 연휴(일~화) + 대체
        "2025-10-09", "2025-12-25", "2025-12-31",
    },
    2026: {
        "2026-01-01",
        "2026-02-16", "2026-02-17", "2026-02-18",  # 설 연휴
        "2026-03-01", "2026-03-02",  # 3·1절(일) 대체
        "2026-05-01",                 # 근로자의 날
        "2026-05-05",
        "2026-05-24", "2026-05-25",  # 부처님오신날(일) 대체
        "2026-06-03",                 # 지방선거
        "2026-06-06",                 # 현충일(토) — 대체 미적용
        "2026-07-17",                 # 제헌절(공휴일 재지정)
        "2026-08-15", "2026-08-17",  # 광복절(토) 대체
        "2026-09-24", "2026-09-25", "2026-09-26",  # 추석 연휴(토 겹침 — 대체 없음)
        "2026-10-03", "2026-10-05",  # 개천절(토) 대체
        "2026-10-09", "2026-12-25",
        "2026-12-31",                 # KRX 연말 휴장
    },
    2027: {
        "2027-01-01",
        "2027-02-06", "2027-02-07", "2027-02-08",  # 설 연휴(설날 2/7 일)
        "2027-02-09",                 # 설 연휴 일요일 겹침 대체
        "2027-03-01",
        "2027-05-01",                 # 노동절(토) — 대체 여부 KRX 공지 확인 전
        "2027-05-05",
        "2027-05-13",                 # 부처님오신날(목)
        "2027-06-06",                 # 현충일(일) — 대체 미적용
        "2027-07-17",                 # 제헌절(토) — 대체 여부 KRX 공지 확인 전
        "2027-08-15", "2027-08-16",  # 광복절(일) 대체
        "2027-09-14", "2027-09-15", "2027-09-16",  # 추석 연휴(추석 9/15 수)
        "2027-10-03", "2027-10-04",  # 개천절(일) 대체
        "2027-10-09", "2027-10-11",  # 한글날(토) 대체
        "2027-12-25", "2027-12-27",  # 성탄절(토) 대체
        "2027-12-31",                 # KRX 연말 휴장
    },
}


def _pykrx_trading_dates(start: str, end: str) -> Set[date]:
    """pykrx 영업일 목록. 버전마다 함수가 달라 둘 다 시도한다(없으면 예외)."""
    from pykrx import stock

    if hasattr(stock, "get_previous_business_days"):
        days = stock.get_previous_business_days(fromdate=start, todate=end)
    else:
        days = stock.get_market_trading_date_by_date(start, end)
        days = getattr(days, "index", days)
    return {d.date() if hasattr(d, "date") else d for d in days}


def _fetch_from_pykrx(year_from: int, year_to: int) -> tuple[Set[str], Optional[date]]:
    """pykrx 영업일로 확인된 구간의 평일 휴장일과, 확인된 마지막 거래일을 반환.

    pykrx에는 미래 영업일이 없다. '전체 평일 - 영업일'을 그대로 쓰면 오늘 이후
    모든 평일이 휴장으로 찍히므로, 마지막으로 돌려받은 거래일까지만 판정한다.
    실패하면 (빈 집합, None).
    """
    out: Set[str] = set()
    verified_through: Optional[date] = None
    try:
        for year in range(year_from, year_to + 1):
            trading = _pykrx_trading_dates(f"{year}0101", f"{year}1231")
            if not trading:
                continue
            last = max(trading)
            d = date(year, 1, 1)
            while d <= last:
                if d.weekday() < 5 and d not in trading:
                    out.add(d.isoformat())
                d += timedelta(days=1)
            verified_through = max(verified_through or last, last)
        return out, verified_through
    except Exception as e:
        logger.warning("pykrx 휴장일 조회 실패: {} — fallback 사용", e)
        return set(), None


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
    fallback = _fetch_fallback(year_from, year_to)
    fetched, verified_through = _fetch_from_pykrx(year_from, year_to)

    def _in_range(d: str) -> bool:
        return year_from <= int(d[:4]) <= year_to

    if verified_through is not None:
        # 시장 영업일로 확인된 구간(≤ verified_through)은 pykrx가 기준이다 — 틀린 기존
        # 항목도 여기서 교정된다. 주말 항목은 판정에 영향이 없으니 그대로 둔다.
        # 확인되지 않은 구간(미래)은 기존 파일 ∪ fallback을 유지한다.
        vt = verified_through.isoformat()
        merged = {d for d in existing if not _in_range(d) or d > vt or _is_weekend(d)}
        merged |= {d for d in fallback if d > vt}
        merged |= fetched
        logger.info(
            "pykrx 휴장일 조회 성공 ({}~{}년, {}까지 확인, 평일 휴장 {}일)",
            year_from, year_to, vt, len(fetched),
        )
    else:
        # pykrx 실패. fallback 표는 사람이 시장 데이터와 대조해 고친 기존 파일보다
        # 오래됐을 수 있다(2026-08-26 교정이 이 경로로 되돌아갈 뻔했다). 그래서
        # 기존 항목은 절대 지우지 않고 합집합만 한다.
        merged = set(existing) | fallback
        logger.info("휴장일 fallback 사용 ({}~{}년) — 기존 항목은 유지", year_from, year_to)

    added = sorted(merged - existing)
    removed = sorted(existing - merged)
    if added or removed:
        logger.warning("holidays.yaml 변경 — 추가 {} / 제거 {}", added, removed)

    sorted_dates = sorted(merged)
    content = {"holidays": sorted_dates}
    header = _read_header(path) if merge_existing else []
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if header:
            f.writelines(header)
            f.write("\n")
        else:
            f.write("# 한국 증시 휴장일 — 자동 갱신 (pykrx + fallback). 수동 편집 가능.\n")
            f.write("# 갱신: python main.py --update-holidays\n\n")
        yaml.dump(content, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    logger.info("holidays.yaml 저장 완료: {} ({}일)", path, len(sorted_dates))
    return path


def _is_weekend(d: str) -> bool:
    try:
        return date.fromisoformat(str(d)[:10]).weekday() >= 5
    except ValueError:
        return False


def _read_header(path: Path) -> List[str]:
    """파일 맨 앞의 주석 블록. 다시 쓸 때 교정 이력 주석이 사라지지 않게 보존한다."""
    if not path.exists():
        return []
    lines: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    lines.append(line)
                else:
                    break
    except Exception as e:
        logger.warning("holidays.yaml 머리 주석 읽기 실패: {}", e)
        return []
    while lines and not lines[-1].strip():
        lines.pop()
    return lines
