"""
데이터베이스 모델 정의
- SQLAlchemy ORM 모델
- SQLite (개발) / PostgreSQL (운영) 지원
"""

from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text,
    create_engine, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker

from config.config_loader import Config


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
    strategy = Column(String(50))                               # 사용된 전략명
    signal_score = Column(Float)                                # 매매 신호 점수
    reason = Column(Text)                                       # 매매 사유 (상세)
    mode = Column(String(20), default="paper")                  # paper / live
    executed_at = Column(DateTime, default=datetime.now)        # 체결 시간
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


# =============================================================
# 데이터베이스 엔진 & 세션 관리
# =============================================================

_engine = None
_SessionLocal = None


def get_engine():
    """
    SQLAlchemy 엔진 반환 (싱글톤)
    - settings.yaml의 database 섹션 참조
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
        else:
            # SQLite (기본값)
            project_root = Path(__file__).parent.parent
            db_path = project_root / db_config.get("sqlite_path", "data/quant_trader.db")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{db_path}"

        _engine = create_engine(url, echo=False)
    return _engine


def get_session():
    """세션 팩토리 반환"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal()


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


def init_database():
    """
    데이터베이스 초기화
    - 모든 테이블 생성 (존재하지 않는 경우에만)
    - 기존 DB에 account_key 컬럼 없으면 마이그레이션
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    try:
        _migrate_add_account_key(engine)
    except Exception:
        pass  # 마이그레이션 실패해도 기동은 유지
    return engine
