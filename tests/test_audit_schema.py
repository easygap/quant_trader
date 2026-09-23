"""모델 ↔ 실제 스키마 대조 (2026-09-23).

create_all은 이미 있는 테이블을 고치지 않는다 — 모델의 제약을 바꿔도 예전 DB에는 옛 제약이
남는다. 7/10 kr_pocket 가짜 낙폭이 이 경우였다(positions의 옛 UNIQUE(symbol)).
"""

from sqlalchemy import create_engine, text

from database.models import Base, _repair_schema_drift, check_schema_drift


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drift.db'}")
    Base.metadata.create_all(engine)
    return engine


def test_fresh_schema_has_no_drift(tmp_path):
    assert check_schema_drift(_engine(tmp_path)) == []


def test_legacy_unique_and_missing_index_are_reported(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_trade_history_account_key"))
        conn.execute(text('DROP TABLE "daily_reports"'))
        conn.execute(text(
            'CREATE TABLE daily_reports (id INTEGER PRIMARY KEY, account_key VARCHAR(64), '
            'date DATETIME, UNIQUE(date))'
        ))
        conn.commit()

    issues = check_schema_drift(engine)

    assert any("trade_history" in i and "ix_trade_history_account_key" in i for i in issues)
    assert any("daily_reports" in i and "옛 UNIQUE" in i for i in issues)


def test_repair_fixes_safe_drift_without_losing_rows(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_trade_history_account_key"))
        conn.execute(text(
            "INSERT INTO operation_events (event_type, severity, message, mode, created_at) "
            "VALUES ('X', 'info', 'm', 'paper', '2026-09-23')"
        ))
        conn.commit()

    _repair_schema_drift(engine)

    assert check_schema_drift(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM operation_events")).scalar() == 1


def test_non_empty_legacy_daily_reports_is_left_for_operator(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        conn.execute(text('DROP TABLE "daily_reports"'))
        conn.execute(text(
            'CREATE TABLE daily_reports (id INTEGER PRIMARY KEY, account_key VARCHAR(64), '
            'date DATETIME, UNIQUE(date))'
        ))
        conn.execute(text("INSERT INTO daily_reports (account_key, date) VALUES ('', '2026-09-01')"))
        conn.commit()

    _repair_schema_drift(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM daily_reports")).scalar() == 1
    assert any("옛 UNIQUE" in i for i in check_schema_drift(engine))
