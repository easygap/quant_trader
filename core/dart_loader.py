"""
DART Open API 연동 (금융감독원 전자공시).
- 고유번호(corp_code) ↔ 6자리 종목코드 매핑: corpCode.xml ZIP
- 정기공시(분기보고서 등) 접수일 기반 차기 실적 공시 시점 추정
- 주요·발행·지분 공시 검색 (유상증자·전환사채·대주주 등 키워드)
"""

from __future__ import annotations

import json
import statistics
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

# 프로젝트 루트 기준 캐시 (ZIP 원본 보관 후 파싱)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CORP_CODE_CACHE_PATH = _PROJECT_ROOT / "data" / "dart_corpCode.zip"
_CACHE_MAX_AGE_DAYS = 7


class DartEarningsLoader:
    """Open DART API 클라이언트 (인증키 필수)."""

    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()
        self.base_url = "https://opendart.fss.or.kr/api"
        self._stock_to_corp: dict[str, str] | None = None

    def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any] | None:
        if not self.api_key:
            return None
        q = {"crtfc_key": self.api_key, **params}
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(q)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "quant_trader/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            logger.debug("DART 요청 실패 {}: {}", path, e)
            return None

        status = str(data.get("status", ""))
        if status == "000":
            return data
        if status == "013":
            return {"status": "000", "list": [], "total_page": 0, "page_no": 1}
        logger.debug(
            "DART API 오류 status={} message={} path={}",
            status, data.get("message"), path,
        )
        return None

    def _refresh_corp_zip_if_needed(self) -> bool:
        """corpCode.xml ZIP을 주기적으로 갱신."""
        _CORP_CODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        need_fetch = True
        if _CORP_CODE_CACHE_PATH.exists():
            age = datetime.now().timestamp() - _CORP_CODE_CACHE_PATH.stat().st_mtime
            if age < _CACHE_MAX_AGE_DAYS * 86400:
                need_fetch = False
        if not need_fetch:
            return True
        if not self.api_key:
            return False
        url = f"{self.base_url}/corpCode.xml?crtfc_key={urllib.parse.quote(self.api_key)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "quant_trader/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            _CORP_CODE_CACHE_PATH.write_bytes(data)
            logger.info("DART corpCode.xml 캐시 저장: {}", _CORP_CODE_CACHE_PATH)
            return True
        except (urllib.error.URLError, OSError) as e:
            logger.warning("DART corpCode.xml 다운로드 실패: {}", e)
            return _CORP_CODE_CACHE_PATH.exists()

    def _load_stock_to_corp_map(self) -> dict[str, str]:
        if self._stock_to_corp is not None:
            return self._stock_to_corp
        mapping: dict[str, str] = {}
        if not self._refresh_corp_zip_if_needed() or not _CORP_CODE_CACHE_PATH.exists():
            self._stock_to_corp = mapping
            return mapping
        try:
            with zipfile.ZipFile(_CORP_CODE_CACHE_PATH, "r") as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
                if not names:
                    self._stock_to_corp = mapping
                    return mapping
                xml_bytes = zf.read(names[0])
            root = ET.fromstring(xml_bytes)
            for row in root.findall(".//list"):
                stock_el = row.find("stock_code")
                corp_el = row.find("corp_code")
                if stock_el is None or corp_el is None:
                    continue
                sc = (stock_el.text or "").strip()
                cc = (corp_el.text or "").strip()
                if len(sc) == 6 and sc.isdigit() and len(cc) == 8:
                    mapping[sc] = cc
        except (zipfile.BadZipFile, ET.ParseError, OSError) as e:
            logger.warning("DART corpCode XML 파싱 실패: {}", e)
        self._stock_to_corp = mapping
        logger.debug("DART 종목→고유번호 매핑 {}건 로드", len(mapping))
        return mapping

    def get_corp_code(self, stock_code: str) -> str | None:
        """6자리 종목코드 → DART 8자리 고유번호."""
        if not stock_code:
            return None
        code = "".join(c for c in str(stock_code).strip() if c.isdigit()).zfill(6)[-6:]
        if len(code) != 6:
            return None
        mp = self._load_stock_to_corp_map()
        return mp.get(code)

    def _fetch_all_list(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        extra: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        total_page = 1
        while page <= total_page:
            params: dict[str, str] = {
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_no": str(page),
                "page_count": "100",
            }
            if extra:
                params.update(extra)
            data = self._get_json("list.json", params)
            if not data:
                break
            lst = data.get("list")
            if lst is None:
                lst = []
            elif isinstance(lst, dict):
                lst = [lst]
            if isinstance(lst, list):
                items.extend(lst)
            try:
                total_page = max(1, int(data.get("total_page", 1)))
            except (TypeError, ValueError):
                total_page = 1
            page += 1
        return items

    def get_next_earnings_date(self, corp_code: str) -> datetime | None:
        """
        최근 분기·반기 정기공시 접수일을 조회한 뒤, 간격(중앙값 또는 90일)으로 차기 공시일을 추정.
        DART는 Yahoo식 earnings 캘린더가 없어 추정치이며, 데이터 없으면 None.
        """
        if not corp_code or len(corp_code) != 8:
            return None
        end = datetime.now().date()
        start = end - timedelta(days=550)
        bgn_de = start.strftime("%Y%m%d")
        end_de = end.strftime("%Y%m%d")

        items = self._fetch_all_list(
            corp_code, bgn_de, end_de, extra={"pblntf_ty": "A"},
        )
        quarterly: list[datetime] = []
        periodic: list[datetime] = []
        for it in items:
            nm = (it.get("report_nm") or "")
            rd = it.get("rcept_dt")
            if not rd or len(str(rd)) != 8:
                continue
            try:
                dt_p = datetime.strptime(str(rd), "%Y%m%d")
            except ValueError:
                continue
            if "분기보고서" in nm:
                quarterly.append(dt_p)
            if "분기보고서" in nm or "반기보고서" in nm:
                periodic.append(dt_p)

        use_dates = sorted(set(quarterly)) if quarterly else sorted(set(periodic))
        if not use_dates:
            return None

        last = use_dates[-1]
        if len(use_dates) >= 2:
            gaps = [
                (use_dates[i] - use_dates[i - 1]).days
                for i in range(1, len(use_dates))
            ]
            avg_gap = int(statistics.median(gaps)) if gaps else 90
            avg_gap = max(60, min(avg_gap, 120))
        else:
            avg_gap = 90

        next_dt = last + timedelta(days=avg_gap)
        today = datetime.now().date()
        while next_dt.date() < today:
            next_dt += timedelta(days=avg_gap)
        return next_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    def get_major_disclosures(self, corp_code: str, days_ahead: int = 7) -> list[dict[str, Any]]:
        """
        최근·단기 창에서 유상증자·전환사채·대주주·대량보유 관련 공시를 검색.
        DART list API는 접수일 기준이라 '미래 접수'는 대개 비어 있을 수 있음 → 과거 창도 함께 조회.
        """
        if not corp_code or len(corp_code) != 8:
            return []

        keywords = (
            "유상증자",
            "전환사채",
            "전환사채권",
            "CB발행",
            "대주주",
            "최대주주",
            "대량보유",
            "특정증권등소유",
        )
        today = datetime.now().date()
        ranges: list[tuple[str, str]] = [
            (
                (today - timedelta(days=days_ahead)).strftime("%Y%m%d"),
                today.strftime("%Y%m%d"),
            ),
            (
                today.strftime("%Y%m%d"),
                (today + timedelta(days=days_ahead)).strftime("%Y%m%d"),
            ),
        ]

        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []

        for bgn_de, end_de in ranges:
            for ty in ("B", "C", "D"):
                items = self._fetch_all_list(corp_code, bgn_de, end_de, extra={"pblntf_ty": ty})
                for it in items:
                    nm = it.get("report_nm") or ""
                    if not any(k in nm for k in keywords):
                        continue
                    key = (str(it.get("rcept_no", "")), str(it.get("rcept_dt", "")))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "rcept_dt": it.get("rcept_dt"),
                        "report_nm": nm,
                        "rcept_no": it.get("rcept_no"),
                        "corp_name": it.get("corp_name"),
                        "pblntf_ty": ty,
                    })
        out.sort(key=lambda x: str(x.get("rcept_dt", "")))
        return out
