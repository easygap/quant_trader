"""positions 구버전 UNIQUE(symbol) → (account_key, symbol) 재구축 마이그레이션 테스트.

배경(2026-07-07 실측): account_key 도입 전 스키마의 symbol 단독 유니크가 물리
테이블에 남아 있어(모델은 이미 복합 제약 — create_all은 기존 테이블을 못 바꾼다),
트랙 재시작으로 아카이브 키에 069500이 남은 상태에서 본 키가 069500을 재매수하는
순간 IntegrityError — 매매 기록은 남고 포지션만 유실돼 평가액이 현금만 남았다
(스냅샷 -41%, 유령 MDD가 리스크 가드까지 발동). 아카이브 조합만이 아니라 바스켓·
전략 트랙이 같은 종목을 겹쳐 드는 순간 언제든 터질 지뢰였다. 스냅샷 UNIQUE(date)
재구축(#439)과 같은 계열·같은 절차. 멱등이고 행수를 보존해야 한다.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text

from database.models import Base, _migrate_position_unique_constraint

LEGACY_DDL = """
CREATE TABLE positions (
    id INTEGER NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    avg_price FLOAT NOT NULL,
    quantity INTEGER NOT NULL,
    total_invested FLOAT NOT NULL,
    stop_loss_price FLOAT,
    take_profit_price FLOAT,
    trailing_stop_price FLOAT,
    highest_price FLOAT,
    strategy VARCHAR(50),
    bought_at DATETIME,
    updated_at DATETIME, account_key VARCHAR(64) DEFAULT '' NOT NULL, partial_tp_done BOOLEAN DEFAULT 0 NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (symbol)
)
"""


def _legacy_engine(tmp_path, symbols=("005930", "069500")):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.connect() as conn:
        conn.execute(text(LEGACY_DDL))
        for i, sym in enumerate(symbols):
            conn.execute(text(
                "INSERT INTO positions "
                "(symbol, avg_price, quantity, total_invested, strategy, account_key, bought_at, updated_at) "
                "VALUES (:s, :p, 1, :p, 'basket_rebalance:kr_x', 'basket_rebalance:kr_x', :t, :t)"
            ), {"s": sym, "p": 100000.0 + i, "t": datetime(2026, 7, 1 + i)})
        conn.commit()
    return engine


class TestPositionUniqueMigration:
    def test_rebuild_preserves_rows_and_allows_same_symbol_two_accounts(self, tmp_path):
        engine = _legacy_engine(tmp_path)
        _migrate_position_unique_constraint(engine)

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(1) FROM positions")).scalar() == 2
            # 데이터 보존 확인
            avg = conn.execute(text(
                "SELECT avg_price FROM positions WHERE symbol = '069500'"
            )).scalar()
            assert avg == pytest.approx(100001.0)
            # 핵심: 같은 종목을 다른 계좌(아카이브 키·전략 트랙)가 이제 들 수 있어야 한다
            conn.execute(text(
                "INSERT INTO positions (symbol, avg_price, quantity, total_invested, account_key, partial_tp_done) "
                "VALUES ('069500', 123810, 1, 123810, 'basket_rebalance:kr_pocket', 0)"
            ))
            conn.commit()
            assert conn.execute(text("SELECT COUNT(1) FROM positions")).scalar() == 3
            # 같은 계좌·같은 종목은 여전히 차단(복합 유니크)
            with pytest.raises(Exception):
                conn.execute(text(
                    "INSERT INTO positions (symbol, avg_price, quantity, total_invested, account_key, partial_tp_done) "
                    "VALUES ('069500', 1, 1, 1, 'basket_rebalance:kr_pocket', 0)"
                ))

    def test_idempotent_on_new_schema(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
        Base.metadata.create_all(engine)
        _migrate_position_unique_constraint(engine)  # no-op이어야 함
        _migrate_position_unique_constraint(engine)
        with engine.connect() as conn:
            ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'"
            )).scalar()
        assert "uq_positions_account_symbol" in ddl

    def test_resume_after_interrupted_rename(self, tmp_path):
        # rename 직후 중단된 상태(legacy 테이블 존재 + 본 테이블 부재)에서 재개
        engine = _legacy_engine(tmp_path)
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE positions RENAME TO positions_legacy_uq"))
            conn.commit()
        _migrate_position_unique_constraint(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(1) FROM positions")).scalar() == 2
            legacy = conn.execute(text(
                "SELECT COUNT(1) FROM sqlite_master WHERE type='table' AND name='positions_legacy_uq'"
            )).scalar()
            assert legacy == 0

    def test_operational_incident_sequence(self, tmp_path):
        """7/7 실측 시나리오 재연: 아카이브 키가 같은 종목을 든 상태에서 본 키 매수.

        마이그레이션 전에는 IntegrityError(포지션 유실 — 유령 -41% 스냅샷의 뿌리),
        마이그레이션 후에는 두 행이 공존해야 한다.
        """
        engine = _legacy_engine(tmp_path, symbols=("069500",))
        with engine.connect() as conn:
            conn.execute(text(
                "UPDATE positions SET account_key = 'basket_rebalance:kr_pocket@archived-20260707', "
                "strategy = 'basket_rebalance:kr_pocket@archived-20260707'"
            ))
            conn.commit()
            with pytest.raises(Exception):
                conn.execute(text(
                    "INSERT INTO positions (symbol, avg_price, quantity, total_invested, account_key) "
                    "VALUES ('069500', 123810, 1, 123810, 'basket_rebalance:kr_pocket')"
                ))
        _migrate_position_unique_constraint(engine)
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO positions (symbol, avg_price, quantity, total_invested, account_key, partial_tp_done) "
                "VALUES ('069500', 123810, 1, 123810, 'basket_rebalance:kr_pocket', 0)"
            ))
            conn.commit()
            n = conn.execute(text(
                "SELECT COUNT(1) FROM positions WHERE symbol = '069500'"
            )).scalar()
            assert n == 2
