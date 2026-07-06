"""portfolio_snapshots 구버전 UNIQUE(date) → (account_key, date) 재구축 마이그레이션 테스트.

배경(2026-07-06 실측): account_key 도입 전 스키마의 date 단독 유니크가 남아 있어,
두 번째 바스켓(kr_pocket)이 같은 날 스냅샷을 저장하는 최초의 순간 IntegrityError로
조용히 유실됐다. 마이그레이션은 멱등이고 행수를 보존해야 한다.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text

from database.models import Base, _migrate_snapshot_unique_constraint

LEGACY_DDL = """
CREATE TABLE portfolio_snapshots (
    id INTEGER NOT NULL,
    date DATETIME NOT NULL,
    total_value FLOAT NOT NULL,
    cash FLOAT NOT NULL,
    invested FLOAT NOT NULL,
    daily_return FLOAT,
    cumulative_return FLOAT,
    mdd FLOAT,
    position_count INTEGER,
    created_at DATETIME, account_key VARCHAR(64) DEFAULT '' NOT NULL, peak_value REAL,
    PRIMARY KEY (id),
    UNIQUE (date)
)
"""


def _legacy_engine(tmp_path, n_rows=3):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.connect() as conn:
        conn.execute(text(LEGACY_DDL))
        for i in range(n_rows):
            conn.execute(text(
                "INSERT INTO portfolio_snapshots "
                "(date, total_value, cash, invested, cumulative_return, account_key, created_at) "
                "VALUES (:d, 1000000, 500000, 500000, :c, 'basket_rebalance:kr_x', :t)"
            ), {"d": datetime(2026, 7, 1 + i), "c": float(i), "t": datetime(2026, 7, 1 + i, 10, 7)})
        conn.commit()
    return engine


class TestSnapshotUniqueMigration:
    def test_rebuild_preserves_rows_and_allows_two_accounts_same_date(self, tmp_path):
        engine = _legacy_engine(tmp_path, n_rows=3)
        _migrate_snapshot_unique_constraint(engine)

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM portfolio_snapshots")).scalar() == 3
            # 데이터 보존 확인
            row = conn.execute(text(
                "SELECT cumulative_return FROM portfolio_snapshots WHERE date = :d"
            ), {"d": datetime(2026, 7, 2)}).scalar()
            assert row == pytest.approx(1.0)
            # 핵심: 같은 날 다른 계정 저장이 이제 가능해야 한다
            conn.execute(text(
                "INSERT INTO portfolio_snapshots (date, total_value, cash, invested, account_key) "
                "VALUES (:d, 300000, 150000, 150000, 'basket_rebalance:kr_pocket')"
            ), {"d": datetime(2026, 7, 1)})
            conn.commit()
            assert conn.execute(text("SELECT COUNT(*) FROM portfolio_snapshots")).scalar() == 4
            # 같은 날 같은 계정은 여전히 차단(복합 유니크)
            with pytest.raises(Exception):
                conn.execute(text(
                    "INSERT INTO portfolio_snapshots (date, total_value, cash, invested, account_key) "
                    "VALUES (:d, 1, 1, 0, 'basket_rebalance:kr_pocket')"
                ), {"d": datetime(2026, 7, 1)})

    def test_idempotent_on_new_schema(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
        Base.metadata.create_all(engine)
        _migrate_snapshot_unique_constraint(engine)  # no-op이어야 함
        _migrate_snapshot_unique_constraint(engine)
        with engine.connect() as conn:
            ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='portfolio_snapshots'"
            )).scalar()
        assert "uq_snapshots_account_date" in ddl

    def test_resume_after_interrupted_rename(self, tmp_path):
        # rename 직후 중단된 상태(legacy 테이블 존재 + 본 테이블 부재)에서 재개
        engine = _legacy_engine(tmp_path, n_rows=2)
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE portfolio_snapshots RENAME TO portfolio_snapshots_legacy_uq"
            ))
            conn.commit()
        _migrate_snapshot_unique_constraint(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM portfolio_snapshots")).scalar() == 2
            legacy = conn.execute(text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='portfolio_snapshots_legacy_uq'"
            )).scalar()
            assert legacy == 0
