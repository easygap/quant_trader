"""PostgreSQL mode 장부 DDL 전환의 멱등성·트랜잭션 복구 검증.

로컬 CI에 PostgreSQL 서버·드라이버가 없으므로 PostgreSQL dialect의 식별자
규칙은 그대로 쓰고, pg_catalog 응답과 transactional DDL 상태만 결정론적으로
모의한다. 실행 SQL 순서와 중단 rollback 후 재시도 계약을 검증한다.
"""

from copy import deepcopy

import pytest
from sqlalchemy.dialects import postgresql

from database.models import (
    _migrate_position_unique_constraint,
    _migrate_snapshot_unique_constraint,
)


class _Result:
    def __init__(self, *, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar(self):
        return self._scalar

    def fetchall(self):
        return list(self._rows)


class _PostgresUrl:
    @staticmethod
    def get_dialect():
        return type("DialectName", (), {"name": "postgresql"})


class _Transaction:
    def __init__(self, engine):
        self.engine = engine
        self.snapshot = None

    def __enter__(self):
        self.snapshot = deepcopy(self.engine.constraints)
        return self.engine.connection

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.engine.constraints = self.snapshot
            self.engine.rollbacks += 1
            return False
        self.engine.commits += 1
        return False


class _PostgresConnection:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        params = parameters or {}
        self.engine.statements.append(sql)

        if sql.startswith("SELECT to_regclass"):
            return _Result(scalar=params["table_name"])
        if "FROM pg_constraint AS c" in sql:
            return _Result(rows=self.engine.constraints.items())
        if "FROM information_schema.columns" in sql:
            return _Result(scalar=len(params["columns"]))
        if " DROP CONSTRAINT " in sql:
            name = sql.rsplit(" ", 1)[-1].strip('"')
            self.engine.constraints.pop(name, None)
        if " ADD CONSTRAINT " in sql:
            if self.engine.fail_add_constraint:
                raise RuntimeError("simulated PostgreSQL interruption")
            name = sql.split(" ADD CONSTRAINT ", 1)[1].split(" ", 1)[0].strip('"')
            definition = "UNIQUE (" + sql.rsplit("UNIQUE (", 1)[1]
            self.engine.constraints[name] = definition
        return _Result()


class _PostgresEngine:
    def __init__(self, constraints):
        self.url = _PostgresUrl()
        self.dialect = postgresql.dialect()
        self.constraints = dict(constraints)
        self.connection = _PostgresConnection(self)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_add_constraint = False

    def begin(self):
        return _Transaction(self)


@pytest.mark.parametrize(
    ("migration", "old_name", "old_definition", "new_name", "new_columns"),
    [
        (
            _migrate_position_unique_constraint,
            "uq_positions_account_symbol",
            "UNIQUE (account_key, symbol)",
            "uq_positions_mode_account_symbol",
            "mode, account_key, symbol",
        ),
        (
            _migrate_snapshot_unique_constraint,
            "uq_snapshots_account_date",
            "UNIQUE (account_key, date)",
            "uq_snapshots_mode_account_date",
            "mode, account_key, date",
        ),
    ],
)
def test_postgres_mode_migration_is_idempotent(
    migration, old_name, old_definition, new_name, new_columns
):
    engine = _PostgresEngine({old_name: old_definition})

    migration(engine)

    assert old_name not in engine.constraints
    assert engine.constraints[new_name] == f"UNIQUE ({new_columns})"
    assert engine.commits == 1
    assert any("ADD COLUMN IF NOT EXISTS mode VARCHAR(20)" in sql for sql in engine.statements)
    assert any("SET DEFAULT 'paper'" in sql for sql in engine.statements)
    assert any("ALTER COLUMN mode SET NOT NULL" in sql for sql in engine.statements)
    inference_count = sum(sql.startswith("WITH inferred AS") for sql in engine.statements)
    assert inference_count == 1

    migration(engine)

    assert engine.commits == 2
    assert engine.constraints[new_name] == f"UNIQUE ({new_columns})"
    # 완료 후 재실행은 legacy 행을 새 거래 이력으로 재분류하지 않는다.
    assert sum(sql.startswith("WITH inferred AS") for sql in engine.statements) == 1
    assert sum(f"ADD CONSTRAINT {new_name}" in sql for sql in engine.statements) == 1


@pytest.mark.parametrize(
    ("migration", "old_name", "old_definition", "new_name"),
    [
        (
            _migrate_position_unique_constraint,
            "uq_positions_account_symbol",
            "UNIQUE (account_key, symbol)",
            "uq_positions_mode_account_symbol",
        ),
        (
            _migrate_snapshot_unique_constraint,
            "uq_snapshots_account_date",
            "UNIQUE (account_key, date)",
            "uq_snapshots_mode_account_date",
        ),
    ],
)
def test_postgres_interruption_rolls_back_and_next_run_recovers(
    migration, old_name, old_definition, new_name
):
    engine = _PostgresEngine({old_name: old_definition})
    engine.fail_add_constraint = True

    with pytest.raises(RuntimeError, match="simulated PostgreSQL interruption"):
        migration(engine)

    assert engine.rollbacks == 1
    assert engine.constraints == {old_name: old_definition}
    assert new_name not in engine.constraints

    engine.fail_add_constraint = False
    migration(engine)

    assert engine.commits == 1
    assert old_name not in engine.constraints
    assert new_name in engine.constraints
