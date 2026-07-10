"""
데이터베이스 모델 정의
- SQLAlchemy ORM 모델
- SQLite (개발) / PostgreSQL (운영) 지원
- SQLite WAL 모드·busy_timeout·synchronous=NORMAL 로 동시성 완화
- scoped_session 으로 스레드별 세션 격리 (Scheduler·aiohttp·LiquidateTrigger 동시 접근 안전)
"""

import time
import functools
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text,
    create_engine, Index, UniqueConstraint, event,
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

from config.config_loader import Config


def _sqlite_connect_pragmas(dbapi_conn, connection_record):
    """
    SQLite 연결 시 PRAGMA 설정 (동시성 완화).
    - WAL: 읽기/쓰기가 서로 블로킹하지 않음. Scheduler·LiquidateTrigger·web_dashboard 동시 접근 시 필수.
    - busy_timeout: 다른 프로세스가 write lock을 잡고 있으면 최대 30초 대기 후 예외.
    - synchronous=NORMAL: WAL 모드에서 안전하면서 fsync 빈도를 줄여 쓰기 성능 향상.
    - cache_size: WAL 캐시를 64MB로 확대해 대량 읽기 시 I/O 감소.
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-65536")
    cursor.close()


Base = declarative_base()


class StockPrice(Base):
    """
    주가 데이터 테이블
    - 일봉 OHLCV 데이터 저장
    """
    __tablename__ = "stock_prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)     # 종목 코드
    date = Column(DateTime, nullable=False)                     # 거래일
    open = Column(Float, nullable=False)                        # 시가
    high = Column(Float, nullable=False)                        # 고가
    low = Column(Float, nullable=False)                         # 저가
    close = Column(Float, nullable=False)                       # 종가
    volume = Column(Integer, nullable=False)                    # 거래량
    created_at = Column(DateTime, default=datetime.now)         # 데이터 수집 시점

    # 복합 인덱스: 종목+날짜 조합으로 빠르게 조회
    __table_args__ = (
        Index("ix_stock_prices_symbol_date", "symbol", "date", unique=True),
    )

    def __repr__(self):
        return f"<StockPrice({self.symbol}, {self.date}, C={self.close})>"


class TradeHistory(Base):
    """
    매매 기록 테이블
    - 실행된 모든 주문의 기록
    - account_key: 전략별 계좌 구분 (다중 계좌 시)
    """
    __tablename__ = "trade_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_key = Column(String(64), default="", nullable=False, index=True)  # 전략/계좌 구분
    symbol = Column(String(20), nullable=False, index=True)     # 종목 코드
    action = Column(String(20), nullable=False)                 # BUY / SELL
    price = Column(Float, nullable=False)                       # 체결 가격
    quantity = Column(Integer, nullable=False)                   # 체결 수량
    total_amount = Column(Float, nullable=False)                # 총 금액 (가격 × 수량)
    commission = Column(Float, default=0)                       # 수수료
    tax = Column(Float, default=0)                              # 세금
    slippage = Column(Float, default=0)                         # 슬리피지
    expected_price = Column(Float, nullable=True)               # 주문 시점 예상 체결가(현재가)
    actual_slippage_pct = Column(Float, nullable=True)          # 실제 슬리피지 %(체결 후)
    execution_session_id = Column(String(96), default="", nullable=False, index=True)  # 파일럿/실행 세션 ID
    order_id = Column(String(64), default="", nullable=False, index=True)              # 내부 주문 ID
    strategy = Column(String(50))                               # 사용된 전략명
    signal_score = Column(Float)                                # 매매 신호 점수
    reason = Column(Text)                                       # 매매 사유 (상세)
    mode = Column(String(20), default="paper")                  # paper / live
    signal_at = Column(DateTime)                                # 신호 발생 시각
    order_at = Column(DateTime)                                 # 주문 전송 시각
    executed_at = Column(DateTime, default=datetime.now)        # 체결 시각
    expected_price = Column(Float)                              # 예상 체결가 (신호 시점 close)
    price_gap = Column(Float)                                   # 실제 - 예상 체결가 차이
    created_at = Column(DateTime, default=datetime.now)

    def __repr__(self):
        return f"<Trade({self.action} {self.symbol}, {self.quantity}주 @ {self.price:,.0f})>"


class Position(Base):
    """
    보유 포지션 테이블
    - 현재 보유 중인 종목 관리
    - account_key: 전략별 계좌 구분 (동일 종목을 전략별로 따로 보유 가능)
    """
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_key = Column(String(64), default="", nullable=False, index=True)  # 전략/계좌 구분
    symbol = Column(String(20), nullable=False)                # 종목 코드
    avg_price = Column(Float, nullable=False)                   # 평균 매수가
    quantity = Column(Integer, nullable=False)                   # 보유 수량
    total_invested = Column(Float, nullable=False)              # 총 투자 금액
    stop_loss_price = Column(Float)                             # 손절가
    take_profit_price = Column(Float)                           # 익절가
    trailing_stop_price = Column(Float)                         # 트레일링 스탑가
    highest_price = Column(Float)                               # 보유 중 최고가 (트레일링용)
    partial_tp_done = Column(Boolean, default=False, nullable=False)  # 1차 부분 익절 수행 여부 (재발동 방지)
    strategy = Column(String(50))                               # 매수 시 사용 전략
    bought_at = Column(DateTime, default=datetime.now)          # 최초 매수 시점
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (UniqueConstraint("account_key", "symbol", name="uq_positions_account_symbol"),)

    def __repr__(self):
        return f"<Position(account={self.account_key!r}, {self.symbol}, {self.quantity}주, 평균가={self.avg_price:,.0f})>"

    @property
    def current_value(self):
        """현재 평가금액 (최고가 기준 — 실시간 가격은 별도 업데이트 필요)"""
        return self.avg_price * self.quantity


class PortfolioSnapshot(Base):
    """
    포트폴리오 스냅샷 테이블
    - 일별 포트폴리오 상태 기록 (수익률 추적용)
    - account_key: 전략별 계좌 구분
    """
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_key = Column(String(64), default="", nullable=False, index=True)
    date = Column(DateTime, nullable=False)                     # 기록 날짜
    total_value = Column(Float, nullable=False)                 # 총 평가 금액
    cash = Column(Float, nullable=False)                        # 현금 잔고
    invested = Column(Float, nullable=False)                    # 투자 금액
    daily_return = Column(Float)                                # 일일 수익률 (%)
    cumulative_return = Column(Float)                           # 누적 수익률 (%)
    mdd = Column(Float)                                         # 현재 MDD (%)
    peak_value = Column(Float)                                   # 역대 최고 평가금 (MDD 기준점, 재시작 시 복구용)
    position_count = Column(Integer, default=0)                 # 보유 종목 수
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint("account_key", "date", name="uq_snapshots_account_date"),)

    def __repr__(self):
        return f"<Snapshot(account={self.account_key!r}, {self.date}, 총={self.total_value:,.0f})>"


class DailyReport(Base):
    """
    일일 리포트 테이블
    - 매일 장 마감 후 생성되는 요약 리포트
    - account_key: 전략별 계좌 구분
    """
    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_key = Column(String(64), default="", nullable=False, index=True)
    date = Column(DateTime, nullable=False)
    total_trades = Column(Integer, default=0)                   # 당일 매매 횟수
    buy_count = Column(Integer, default=0)                      # 매수 횟수
    sell_count = Column(Integer, default=0)                     # 매도 횟수
    realized_pnl = Column(Float, default=0)                     # 실현 손익
    unrealized_pnl = Column(Float, default=0)                   # 미실현 손익
    total_commission = Column(Float, default=0)                 # 총 수수료
    total_tax = Column(Float, default=0)                        # 총 세금
    winning_trades = Column(Integer, default=0)                 # 수익 거래 수
    losing_trades = Column(Integer, default=0)                  # 손실 거래 수
    report_text = Column(Text)                                  # 리포트 본문
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint("account_key", "date", name="uq_daily_reports_account_date"),)

    def __repr__(self):
        return f"<DailyReport(account={self.account_key!r}, {self.date}, 매매={self.total_trades}건)>"


class FailedOrder(Base):
    """
    주문 실패 Dead-letter 테이블
    - 재시도 모두 실패한 주문을 영구 저장하여 누락 방지
    - status: pending(미처리) / retried(재시도 완료) / cancelled(수동 취소)
    """
    __tablename__ = "failed_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_key = Column(String(64), default="", nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    action = Column(String(20), nullable=False)          # BUY / SELL
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    reason = Column(Text)
    strategy = Column(String(50))
    signal_score = Column(Float)
    retry_count = Column(Integer, default=0)
    status = Column(String(20), default="pending", index=True)  # pending / retried / cancelled
    mode = Column(String(20), default="paper")
    error_detail = Column(Text)
    failed_at = Column(DateTime, default=datetime.now)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    def __repr__(self):
        return f"<FailedOrder({self.symbol} {self.action} {self.quantity}주 @{self.price}, status={self.status})>"


class CashFlow(Base):
    """
    외부 현금 흐름(입금/출금) 테이블 — 적립식 운영 지원.

    수익률 계산에서 입금은 수익이 아니다: 시간가중수익률(TWR) 계산이 이 기록으로
    입금 구간을 분리한다(docs/POCKET_TRACK_PLAN.md §4). paper는
    tools/record_deposit.py로 기록하고, live도 실제 입금 후 같은 도구로 기록해야
    TWR이 맞는다(증권사 잔고는 현금만 알지, 언제 얼마가 외부에서 왔는지는 모른다).
    """
    __tablename__ = "cash_flows"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_key = Column(String(64), default="", nullable=False, index=True)
    amount = Column(Float, nullable=False)                       # +입금 / -출금 (원)
    occurred_at = Column(DateTime, nullable=False, index=True)   # 발생 시각(귀속 기준)
    note = Column(String(200), default="")                       # 메모 (예: 7월 적립)
    mode = Column(String(20), default="paper")
    created_at = Column(DateTime, default=datetime.now)

    def __repr__(self):
        return f"<CashFlow({self.account_key}, {self.amount:+,.0f}, {self.occurred_at})>"


class OperationEvent(Base):
    """
    운영 이벤트 로그 테이블.
    경고, API 실패, 중복 주문 차단, 블랙스완 발동 등 모든 운영 이벤트를 기록.
    Paper trading 3개월 모니터링의 핵심 데이터.
    """
    __tablename__ = "operation_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50), nullable=False, index=True)  # WARNING, API_FAILURE, DUPLICATE_BLOCKED, BLACKSWAN, SIGNAL, SL_TP
    severity = Column(String(20), default="info")                 # info, warning, error, critical
    symbol = Column(String(20))
    strategy = Column(String(50))
    message = Column(Text, nullable=False)
    detail = Column(Text)                                         # JSON 추가 데이터
    mode = Column(String(20), default="paper")
    created_at = Column(DateTime, default=datetime.now, index=True)

    def __repr__(self):
        return f"<OperationEvent({self.event_type}, {self.severity}, {self.symbol})>"


class PendingOrderGuard(Base):
    """
    중복 주문 방지 DB 테이블.
    프로세스 재시작 시에도 중복 주문 방지를 유지하기 위한 영속 가드.
    TTL 기반으로 expires_at 이후 자동 무효화.
    """
    __tablename__ = "pending_order_guards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    def __repr__(self):
        return f"<PendingOrderGuard({self.symbol}, expires={self.expires_at})>"


class OrderRecord(Base):
    """
    주문 상태 추적 테이블 — 브로커 기준 source of truth.

    상태 전이: NEW → SUBMITTED → ACKED → FILLED / PARTIAL_FILLED / REJECTED / CANCELLED / EXPIRED → RECONCILED
    fill 확인 전 position/cash를 반영하지 않음. (감사 C-5 대응)
    """
    __tablename__ = "order_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), nullable=False, unique=True, index=True)
    account_key = Column(String(64), default="", nullable=False)
    symbol = Column(String(20), nullable=False, index=True)
    action = Column(String(10), nullable=False)           # BUY / SELL
    status = Column(String(20), nullable=False, default="NEW", index=True)
    broker_order_id = Column(String(64))                   # 브로커 측 주문 ID
    requested_qty = Column(Integer, nullable=False)
    requested_price = Column(Float, nullable=False)
    filled_qty = Column(Integer, default=0)
    filled_price = Column(Float, default=0.0)
    remaining_qty = Column(Integer, default=0)
    commission = Column(Float, default=0.0)
    tax = Column(Float, default=0.0)
    slippage = Column(Float, default=0.0)
    reject_reason = Column(Text, default="")
    strategy = Column(String(50), default="")
    signal_score = Column(Float, default=0.0)
    mode = Column(String(20), default="paper")
    created_at = Column(DateTime, default=datetime.now)
    submitted_at = Column(DateTime)
    filled_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self):
        return f"<OrderRecord({self.order_id}, {self.symbol} {self.action} {self.status}, {self.filled_qty}/{self.requested_qty})>"


# =============================================================
# 데이터베이스 엔진 & 세션 관리
# =============================================================

_engine = None
_ScopedSession = None
_SessionFactory = None

# SQLite "database is locked" 재시도 설정
_SQLITE_RETRY_MAX = 3
_SQLITE_RETRY_DELAY = 1.0


def get_engine():
    """
    SQLAlchemy 엔진 반환 (싱글톤).
    - SQLite: WAL + 커넥션 풀(StaticPool 대신 QueuePool, pool_pre_ping=True)
    - PostgreSQL: 기본 QueuePool
    """
    global _engine
    if _engine is None:
        config = Config.get()
        db_config = config.database

        if db_config.get("type") == "postgresql":
            pg = db_config.get("postgresql", {})
            url = (
                f"postgresql://{pg.get('user')}:{pg.get('password')}"
                f"@{pg.get('host')}:{pg.get('port')}/{pg.get('database')}"
            )
            _engine = create_engine(
                url, echo=False, pool_size=5, max_overflow=10, pool_pre_ping=True,
            )
        else:
            project_root = Path(__file__).parent.parent
            db_path = project_root / db_config.get("sqlite_path", "data/quant_trader.db")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{db_path}"
            _engine = create_engine(
                url, echo=False,
                connect_args={"check_same_thread": False},
                pool_pre_ping=True,
            )
            event.listen(_engine, "connect", _sqlite_connect_pragmas)
    return _engine


def get_session():
    """
    스레드 안전 세션 반환 (scoped_session).
    Scheduler(메인 스레드), web_dashboard(aiohttp 이벤트 루프), LiquidateTrigger(HTTP 스레드)가
    각각 독립 세션을 받으므로 세션 충돌이 방지됩니다.
    """
    global _ScopedSession, _SessionFactory
    if _ScopedSession is None:
        _SessionFactory = sessionmaker(bind=get_engine())
        _ScopedSession = scoped_session(_SessionFactory)
    return _ScopedSession()


@contextmanager
def db_session():
    """
    세션 컨텍스트 매니저: commit/rollback/close를 자동 처리.

    사용법:
        with db_session() as session:
            session.add(...)
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def with_retry(func):
    """
    SQLite 'database is locked' 예외 시 자동 재시도 데코레이터.
    WAL + busy_timeout으로 대부분 해결되지만, 극단적 동시 쓰기 시 안전장치.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(_SQLITE_RETRY_MAX):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                err_msg = str(e).lower()
                if "database is locked" in err_msg or "locked" in err_msg:
                    last_exc = e
                    wait = _SQLITE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        "DB locked 재시도 {}/{} ({}초 대기): {}",
                        attempt + 1, _SQLITE_RETRY_MAX, wait, func.__name__,
                    )
                    time.sleep(wait)
                else:
                    raise
        raise last_exc  # type: ignore[misc]
    return wrapper


def _migrate_add_account_key(engine):
    """기존 DB에 account_key 컬럼 추가 (다중 계좌 분리용). 한 번만 실행해도 됨."""
    from sqlalchemy import text
    dialect = engine.url.get_dialect().name
    tables_columns = [
        ("trade_history", "account_key"),
        ("positions", "account_key"),
        ("portfolio_snapshots", "account_key"),
        ("daily_reports", "account_key"),
    ]
    with engine.connect() as conn:
        for table, col in tables_columns:
            try:
                if dialect == "sqlite":
                    r = conn.execute(text(f"PRAGMA table_info({table})"))
                    if any(row[1] == col for row in r.fetchall()):
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} VARCHAR(64) DEFAULT '' NOT NULL"))
                else:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} VARCHAR(64) DEFAULT '' NOT NULL"))
                conn.commit()
            except Exception as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    conn.rollback()
                else:
                    conn.rollback()
                    raise


def _migrate_trade_history_slippage_columns(engine):
    """기존 DB에 expected_price, actual_slippage_pct 추가 (실전 슬리피지 추적)."""
    from sqlalchemy import text
    dialect = engine.url.get_dialect().name
    col_type = "REAL" if dialect == "sqlite" else "DOUBLE PRECISION"
    cols = [
        ("trade_history", "expected_price"),
        ("trade_history", "actual_slippage_pct"),
    ]
    with engine.connect() as conn:
        for table, col in cols:
            try:
                if dialect == "sqlite":
                    r = conn.execute(text(f"PRAGMA table_info({table})"))
                    if any(row[1] == col for row in r.fetchall()):
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                else:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    )
                conn.commit()
            except Exception as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    conn.rollback()
                else:
                    conn.rollback()
                    raise


def _migrate_trade_history_signal_columns(engine):
    """기존 DB에 signal_at, order_at, price_gap 컬럼 추가 (감사 항목 4 대응)."""
    from sqlalchemy import text
    dialect = engine.url.get_dialect().name
    cols = [
        ("trade_history", "signal_at", "DATETIME" if dialect == "sqlite" else "TIMESTAMP"),
        ("trade_history", "order_at", "DATETIME" if dialect == "sqlite" else "TIMESTAMP"),
        ("trade_history", "price_gap", "REAL" if dialect == "sqlite" else "DOUBLE PRECISION"),
        ("portfolio_snapshots", "peak_value", "REAL" if dialect == "sqlite" else "DOUBLE PRECISION"),
    ]
    with engine.connect() as conn:
        for table, col, col_type in cols:
            try:
                if dialect == "sqlite":
                    # 테이블 존재 여부 확인
                    t_check = conn.execute(text(
                        f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
                    ))
                    if not t_check.fetchone():
                        continue  # 테이블 없으면 스킵
                    r = conn.execute(text(f"PRAGMA table_info({table})"))
                    if any(row[1] == col for row in r.fetchall()):
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                else:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    )
                conn.commit()
            except Exception as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    conn.rollback()
                else:
                    conn.rollback()
                    raise


def _migrate_trade_history_execution_link_columns(engine):
    """기존 DB에 실행 세션/주문 연결 컬럼 추가."""
    from sqlalchemy import text
    dialect = engine.url.get_dialect().name
    cols = [
        ("trade_history", "execution_session_id", "VARCHAR(96) DEFAULT '' NOT NULL"),
        ("trade_history", "order_id", "VARCHAR(64) DEFAULT '' NOT NULL"),
    ]
    with engine.connect() as conn:
        for table, col, col_type in cols:
            try:
                if dialect == "sqlite":
                    t_check = conn.execute(text(
                        f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
                    ))
                    if not t_check.fetchone():
                        continue
                    r = conn.execute(text(f"PRAGMA table_info({table})"))
                    if any(row[1] == col for row in r.fetchall()):
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                else:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    )
                conn.commit()
            except Exception as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    conn.rollback()
                else:
                    conn.rollback()
                    raise


def _migrate_positions_partial_tp_done(engine):
    """기존 DB의 positions 테이블에 partial_tp_done 컬럼 추가 (부분 익절 재발동 방지)."""
    from sqlalchemy import text
    dialect = engine.url.get_dialect().name
    # SQLite는 BOOLEAN을 0/1로 저장. 기존 행은 미수행(0)으로 채운다.
    col_type = "BOOLEAN DEFAULT 0 NOT NULL" if dialect == "sqlite" else "BOOLEAN DEFAULT FALSE NOT NULL"
    with engine.connect() as conn:
        try:
            if dialect == "sqlite":
                t_check = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
                ))
                if not t_check.fetchone():
                    return
                r = conn.execute(text("PRAGMA table_info(positions)"))
                if any(row[1] == "partial_tp_done" for row in r.fetchall()):
                    return
                conn.execute(text(f"ALTER TABLE positions ADD COLUMN partial_tp_done {col_type}"))
            else:
                conn.execute(text(
                    f"ALTER TABLE positions ADD COLUMN IF NOT EXISTS partial_tp_done {col_type}"
                ))
            conn.commit()
        except Exception as e:
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                conn.rollback()
            else:
                conn.rollback()
                raise


def _migrate_snapshot_unique_constraint(engine):
    """portfolio_snapshots의 구버전 UNIQUE(date) 단독 제약을 (account_key, date)로 재구축.

    account_key 도입 전 스키마의 유산: date 단독 유니크라 두 번째 계정(예: kr_pocket)이
    같은 날 스냅샷을 저장하는 순간 IntegrityError로 조용히 유실된다(2026-07-06 실측).
    SQLite는 인라인 UNIQUE 삭제가 불가하므로 표준 재구축(rename → 신 스키마 생성 →
    복사 → 검증 → 구 테이블 삭제)을 쓴다. 멱등이며, 중간 중단 시 다음 초기화에서
    legacy 테이블을 감지해 복사부터 재개한다(INSERT OR IGNORE + PK로 중복 안전).
    """
    from sqlalchemy import text

    if engine.url.get_dialect().name != "sqlite":
        return
    cols = (
        "id, date, total_value, cash, invested, daily_return, cumulative_return, "
        "mdd, position_count, created_at, account_key, peak_value"
    )
    legacy_name = "portfolio_snapshots_legacy_uq"

    with engine.connect() as conn:
        legacy_exists = conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:n"
        ), {"n": legacy_name}).scalar()

        if not legacy_exists:
            ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='portfolio_snapshots'"
            )).scalar()
            if not ddl:
                return
            normalized = " ".join(str(ddl).split()).lower()
            is_legacy = (
                "unique (date)" in normalized
                and "uq_snapshots_account_date" not in normalized
            )
            if not is_legacy:
                return
            logger.warning(
                "portfolio_snapshots 구버전 UNIQUE(date) 감지 — (account_key, date) 복합 제약으로 재구축"
            )
            conn.execute(text(
                f"ALTER TABLE portfolio_snapshots RENAME TO {legacy_name}"
            ))
            conn.commit()

    # 신 스키마 재생성 (rename으로 본 테이블이 사라졌으므로 create_all이 새로 만든다)
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        before = conn.execute(text(f"SELECT COUNT(*) FROM {legacy_name}")).scalar()
        conn.execute(text(
            f"INSERT OR IGNORE INTO portfolio_snapshots ({cols}) SELECT {cols} FROM {legacy_name}"
        ))
        after = conn.execute(text("SELECT COUNT(*) FROM portfolio_snapshots")).scalar()
        if after < before:
            conn.rollback()
            raise RuntimeError(
                f"스냅샷 재구축 검증 실패: 복사 후 {after} < 원본 {before} — legacy 테이블 보존"
            )
        conn.execute(text(f"DROP TABLE {legacy_name}"))
        conn.commit()
        logger.info("portfolio_snapshots 재구축 완료: {}행, 복합 유니크(account_key, date)", after)


def _migrate_position_unique_constraint(engine):
    """positions의 구버전 UNIQUE(symbol) 단독 제약을 (account_key, symbol)로 재구축.

    account_key 도입 전 스키마의 유산(스냅샷 UNIQUE(date)와 같은 계열): symbol 단독
    유니크라 서로 다른 계좌가 같은 종목을 드는 순간 IntegrityError — 매매 기록은
    남는데 포지션만 유실돼 평가액이 현금만 남는다(2026-07-07 실측: 트랙 재시작으로
    아카이브 키에 069500이 남은 상태에서 본 키가 069500 재매수 → 스냅샷 -41%).
    아카이브/본 키 조합만이 아니라 바스켓·전략 트랙이 같은 종목을 겹쳐 들 수 없는
    구조적 지뢰다. 모델은 이미 복합 제약인데 물리 테이블만 낡았다(create_all은
    기존 테이블을 못 바꾼다). 표준 재구축(rename → 생성 → 복사 → 검증 → 삭제),
    멱등·중단 재개 가능 — 스냅샷 마이그레이션과 동일 절차.
    """
    from sqlalchemy import text

    if engine.url.get_dialect().name != "sqlite":
        return
    cols = (
        "id, symbol, avg_price, quantity, total_invested, stop_loss_price, "
        "take_profit_price, trailing_stop_price, highest_price, strategy, "
        "bought_at, updated_at, account_key, partial_tp_done"
    )
    legacy_name = "positions_legacy_uq"

    with engine.connect() as conn:
        legacy_exists = conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=:n"
        ), {"n": legacy_name}).scalar()

        if not legacy_exists:
            ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'"
            )).scalar()
            if not ddl:
                return
            normalized = " ".join(str(ddl).split()).lower()
            is_legacy = (
                "unique (symbol)" in normalized
                and "uq_positions_account_symbol" not in normalized
            )
            if not is_legacy:
                return
            logger.warning(
                "positions 구버전 UNIQUE(symbol) 감지 — (account_key, symbol) 복합 제약으로 재구축"
            )
            conn.execute(text(
                f"ALTER TABLE positions RENAME TO {legacy_name}"
            ))
            conn.commit()

    # 신 스키마 재생성 (rename으로 본 테이블이 사라졌으므로 create_all이 새로 만든다)
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        before = conn.execute(text(f"SELECT COUNT(*) FROM {legacy_name}")).scalar()
        conn.execute(text(
            f"INSERT OR IGNORE INTO positions ({cols}) SELECT {cols} FROM {legacy_name}"
        ))
        after = conn.execute(text("SELECT COUNT(*) FROM positions")).scalar()
        if after < before:
            conn.rollback()
            raise RuntimeError(
                f"포지션 재구축 검증 실패: 복사 후 {after} < 원본 {before} — legacy 테이블 보존"
            )
        conn.execute(text(f"DROP TABLE {legacy_name}"))
        conn.commit()
        logger.info("positions 재구축 완료: {}행, 복합 유니크(account_key, symbol)", after)


def init_database():
    """
    데이터베이스 초기화
    - 모든 테이블 생성 (존재하지 않는 경우에만)
    - 기존 DB에 account_key 컬럼 없으면 마이그레이션
    - SQLite WAL 모드 활성화 확인
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    try:
        _migrate_add_account_key(engine)
    except Exception:
        pass
    # 구버전 UNIQUE(date) 재구축 — 실패 시 조용히 넘기지 않는다(두 번째 계정의
    # 스냅샷이 계속 유실되는 상태를 숨기면 안 됨). 단, legacy가 아니면 no-op.
    _migrate_snapshot_unique_constraint(engine)
    try:
        _migrate_trade_history_slippage_columns(engine)
    except Exception:
        pass
    try:
        _migrate_trade_history_signal_columns(engine)
    except Exception:
        pass
    try:
        _migrate_trade_history_execution_link_columns(engine)
    except Exception:
        pass
    try:
        _migrate_positions_partial_tp_done(engine)
    except Exception:
        pass
    # 구버전 UNIQUE(symbol) 재구축 — 스냅샷 UNIQUE(date)와 같은 이유로 조용히 넘기지
    # 않는다(계좌 간 동일 종목 보유가 막혀 매수 포지션이 유실되는 상태). legacy가
    # 아니면 no-op. partial_tp_done 컬럼 추가 이후에 실행해야 복사 컬럼이 갖춰진다.
    _migrate_position_unique_constraint(engine)

    if "sqlite" in engine.url.drivername:
        from sqlalchemy import text
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
            sync = conn.execute(text("PRAGMA synchronous")).scalar()
            if str(mode).lower() != "wal":
                logger.error(
                    "⚠️ SQLite journal_mode={} (WAL 아님). 동시 접근 시 'database is locked' 위험. "
                    "DB 파일 권한 또는 파일 시스템(네트워크 드라이브 등) 확인 필요.",
                    mode,
                )
            else:
                logger.info(
                    "SQLite 초기화 완료: journal_mode={}, busy_timeout={}ms, synchronous={}, "
                    "scoped_session=ON, @with_retry=전체 함수",
                    mode, busy, sync,
                )
    else:
        logger.info("PostgreSQL 초기화 완료: pool_size=5, pool_pre_ping=ON")
    return engine
