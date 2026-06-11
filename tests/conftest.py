"""Pytest 전역 설정 — 운영 DB 보호.

이 프로젝트의 여러 테스트는 `init_database()` 후 TradeHistory/Position 등 모든 행을
삭제하거나 BUY/SELL lifecycle을 기록한다. DB 경로가 격리되어 있지 않으면 이 테스트들이
운영용 `data/quant_trader.db`를 그대로 비우거나 오염시켜, paper pilot 증거(60영업일 누적)
의 원천 데이터를 파괴한다. 실제로 이것이 반복적인 "DB 복구(restore)" 작업의 근본 원인이었다.

해결: 테스트 세션 시작 시점(어떤 테스트도 DB에 접근하기 전)에 `QUANT_DB_PATH`를 임시 파일로
설정하고, 캐싱된 Config 싱글톤과 SQLAlchemy 엔진 글로벌을 리셋한다. config_loader가
`QUANT_DB_PATH`를 읽어 SQLite 경로를 덮어쓰므로, 이후 모든 DB 접근은 임시 DB로 격리된다.
싱글톤을 리셋하는 테스트들도 sticky한 env override 덕분에 임시 DB로 다시 빌드된다.

명시적으로 `QUANT_DB_PATH`를 지정한 경우(예: 특정 DB로 통합 검증)에는 그대로 존중한다.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path


def pytest_configure(config):
    # 사용자가 명시적으로 DB 경로를 지정했다면 존중한다.
    if os.environ.get("QUANT_DB_PATH"):
        return

    tmpdir = tempfile.mkdtemp(prefix="quant_test_db_")
    os.environ["QUANT_DB_PATH"] = str(Path(tmpdir) / "test_quant_trader.db")

    # 이미 로드되었을 수 있는 Config 싱글톤과 엔진 글로벌을 리셋하여
    # 다음 접근 시 격리된 임시 DB로 다시 빌드되도록 한다.
    try:
        from config.config_loader import Config
        Config._instance = None
    except Exception:
        pass
    try:
        import database.models as _m
        _m._engine = None
        _m._ScopedSession = None
        _m._SessionFactory = None
        # 격리된 임시 DB에 스키마를 생성해 둔다. 일부 evidence/report 함수는
        # init_database()를 호출하지 않는 테스트에서도 trade_history 등을 조회하므로,
        # 스키마가 없으면 "no such table" 오류가 난다. (기존엔 운영 DB에 테이블이
        # 우연히 존재해 통과하던 latent 결합을 격리하면서 드러난 부분.)
        _m.init_database()
    except Exception:
        pass

    atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))


# ---------------------------------------------------------------------------
# 운영 추적 파일 일괄 격리 (DB 격리와 같은 원리, reports/ 오염 방지)
#
# 섹터 맵 캐시(reports/sector_map_cache.json)는 git 추적되는 운영 캐시인데
# (상관관계 리스크 체크가 소비), 개별 테스트가 monkeypatch를 잊으면 테스트
# 데이터가 운영 캐시를 덮어쓴다 — 백업 오염(2026-06-11 발견)과 동형의 경로.
# autouse로 전 테스트에서 임시 경로로 강제한다.
# ---------------------------------------------------------------------------
import pytest


@pytest.fixture(autouse=True)
def _isolate_sector_map_cache(tmp_path, monkeypatch):
    try:
        import core.data_collector as _dc
        monkeypatch.setattr(_dc, "SECTOR_MAP_CACHE_PATH", tmp_path / "sector_map_cache.json")
    except Exception:
        pass
