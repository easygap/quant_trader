# QUANT TRADER — 프로젝트 가이드

> **목적**: 코드를 볼 때 **파일별 역할**, **프로그램 흐름**, **알고리즘·설정**을 세세히 알 수 있도록 정리한 문서.  
> **문서 버전**: v2.2  
> **최종 수정**: 2026-03-19  
> **참고**: 전체 아키텍처·지표 공식·전략 상세는 루트의 `quant_trader_design.md` 참고.

---

## 목차

1. [프로그램이 어떻게 돌아가는지](#1-프로그램이-어떻게-돌아가는지)
2. [실제 디렉터리·파일 구조](#2-실제-디렉터리파일-구조)
3. [파일별 상세 역할](#3-파일별-상세-역할)
4. [설정 파일(YAML) 요약](#4-설정-파일yaml-요약)
5. [실행 모드별 데이터 흐름](#5-실행-모드별-데이터-흐름)
6. [알고리즘·지표 요약](#6-알고리즘지표-요약)
7. [전략 유효성 검증 및 실전 체크리스트](#7-전략-유효성-검증-및-실전-체크리스트)
8. [watchlist 모드](#8-watchlist-모드)
9. [의존성 및 환경](#9-의존성-및-환경)
10. [저장소(Git) 관리](#10-저장소git-관리)

---

## 1. 프로그램이 어떻게 돌아가는지

### 1.1 전체 흐름 요약

1. **시작**  
   `main.py` 실행 → 로거·DB 초기화 → `--mode`에 따라 **backtest / validate / paper / live / liquidate / compare / optimize / dashboard / check_correlation / check_ensemble_correlation** 중 하나로 분기.

2. **백테스트 (backtest)**  
   `DataCollector`로 과거 주가 수집 → `Backtester`가 전략으로 시뮬레이션(수수료·세금·슬리피지·손절/익절/트레일링 스탑 반영, **strict-lookahead 기본**) → `ReportGenerator`가 txt/html 리포트 생성.

3. **모의투자 (paper)**  
   `WatchlistManager`로 관심 종목 확정 → 종목마다 `DataCollector` 수집 → 전략 `generate_signal(df, symbol=symbol)` → BUY 시 **시장 국면 필터**(코스피 200일선 이하면 신규 매수 중단) 적용 후 `OrderExecutor`가 **DB에만 기록** + 디스코드 알림. 실제 주문 없음.

4. **실전 (live)**  
   `ENABLE_LIVE_TRADING=true` + `--confirm-live` 필수. KIS API 인증 → `PortfolioManager.sync_with_broker()` → `Scheduler` 무한 루프.  
   - **장전(08:50)** 데이터 수집·전략 분석·**시장 국면 필터** 확인 후 매수 후보 선정(`auto_entry: true` 시 장중 매수).  
   - **장중(09:00~15:30)** 10분 간격으로 최대 보유 기간 초과 정리·신호·손절/익절 확인 → **시장 국면 필터** 통과 시에만 진입 후보 실행 → `OrderExecutor`가 KIS API로 실제 주문. 주문 전 OrderGuard·KIS 미체결 조회로 중복 방지.  
   - **장마감(15:35)** 일일 리포트·스냅샷·KIS 크로스체크·DB 백업(설정 시)·디스코드.

5. **공통**  
   설정: `config/` YAML + `.env`. 데이터·포지션·거래 기록: SQLite(또는 설정 DB).

### 1.2 모드별 진입점

| 모드 | main.py 호출 함수 | 핵심 모듈 |
|------|-------------------|-----------|
| **backtest** | `run_backtest(args)` | DataCollector → Backtester → ReportGenerator |
| **validate** | `run_strategy_validation(args)` | backtest.strategy_validator (3~5년, 샤프·MDD·벤치마크 KS11·코스피 상위 50 동일비중, in/out-of-sample, **손익비 자동 경고+디스코드**). `--no-benchmark-top50` 으로 Top50 비활성화 |
| **paper** | `run_paper_trading(args)` | WatchlistManager, DataCollector, 전략, OrderExecutor(paper), DiscordBot |
| **live** | `run_live_trading(args)` | KISApi, PortfolioManager(sync), Scheduler |
| **liquidate** | `run_emergency_liquidate(args)` | DB 포지션 조회 → 종목별 매도(KIS 현재가 주문) |
| **compare** | `run_compare_paper_backtest(args)` | backtest.paper_compare (run_compare + **check_live_readiness**), divergence 경고 + **실전 전환 준비 자동 평가·디스코드 알림** |
| **optimize** | `run_param_optimize(args)` | backtest.param_optimizer (Grid/Bayesian), Backtester.run(param_overrides=) |
| **dashboard** | `run_dashboard(args)` | monitoring.web_dashboard (aiohttp), PortfolioManager, get_portfolio_snapshots |
| **check_correlation** | `run_check_indicator_correlation(args)` | DataCollector, IndicatorEngine, SignalGenerator → core.indicator_correlation (스코어 상관계수·고상관 쌍 권고) |
| **check_ensemble_correlation** | `run_check_ensemble_correlation(args)` | DataCollector, StrategyEnsemble.analyze → core.ensemble_correlation (신호 상관 + BUY 동시 발생률 + 대안 전략 권고). **validate --strategy ensemble** 시 자동 실행 |

---

## 2. 실제 디렉터리·파일 구조

```
quant_trader/
├── main.py                      # CLI 진입점, --mode 분기 (10개 모드)
├── test_integration.py          # 통합 검증 스크립트 (단일 실행, pytest 아님)
├── pyproject.toml               # 프로젝트 메타데이터 (Python >=3.11,<3.13, 패키지, pytest 설정)
├── requirements.txt             # pip 의존성 목록
├── .env.example                 # 환경변수 템플릿 (KIS API, 디스코드, 텔레그램, 이메일, 긴급청산)
├── .gitignore                   # 제외 규칙 (.env, settings.yaml, data/, logs/, reports/* 등)
├── README.md                    # 프로젝트 소개·빠른 시작·실행 예시
├── quant_trader_design.md       # 전체 아키텍처·전략·리스크 설계서
├── config/
│   ├── __init__.py
│   ├── config_loader.py         # YAML 통합 로더, .env 덮어쓰기, Config.get() 싱글톤
│   ├── settings.yaml.example    # 설정 예시 (settings.yaml은 .gitignore)
│   ├── settings.yaml            # KIS API, database, data_source, trading, discord, telegram, dashboard, watchlist
│   ├── strategies.yaml          # indicators, scoring, mean_reversion(fundamental_filter), trend_following, momentum_factor, volatility_condition, ensemble
│   ├── risk_params.yaml         # backtest_universe, liquidity_filter, 포지션/손절/익절/트레일링/분산/MDD/성과열화/거래비용
│   ├── holidays.yaml.example    # 휴장일 예시
│   └── holidays.yaml            # --update-holidays 로 자동 갱신
├── core/
│   ├── __init__.py
│   ├── data_collector.py        # 주가 수집 (FDR→yfinance→KIS 폴백, 소스 추적·수정주가 검증), get_krx_stock_list(), get_sector_map()
│   ├── watchlist_manager.py     # 관심 종목: manual/top_market_cap/kospi200/momentum_top/low_vol_top/momentum_lowvol + 유동성 필터 + 리밸런싱 캐시 + as_of_date
│   ├── indicator_engine.py      # pandas-ta: RSI, MACD, 볼린저, MA(SMA/EMA), 스토캐스틱, ADX, ATR, OBV, volume_ratio
│   ├── signal_generator.py      # 멀티 지표 스코어링 → BUY/SELL/HOLD, collinearity_mode(representative_only 권장)
│   ├── risk_manager.py          # 포지션 사이징(1% 룰), 분산(업종 비중 포함), 성과 열화, 손절/익절/트레일링, 거래 비용
│   ├── order_executor.py        # 매수/매도 (paper: DB만, live: KIS), PositionLock, OrderGuard, 유동성·어닝 필터, 매수 직전 재검증
│   ├── portfolio_manager.py     # 포지션·잔고·수익률, sync_with_broker(KIS↔DB 크로스체크), save_daily_snapshot()
│   ├── scheduler.py             # 실전 무한 루프: 장전/장중(10분)/장마감, 시장 국면 필터, 블랙스완 recovery, paper 실전전환 자동 평가
│   ├── trading_hours.py         # 장 시간·휴장일 판별 (holidays.yaml → pykrx → fallback)
│   ├── holidays_updater.py      # 휴장일 YAML 자동 갱신 (pykrx 또는 fallback)
│   ├── blackswan_detector.py    # 급락 감지 → 전량 매도·쿨다운·recovery(점진적 재진입, recovery_scale)
│   ├── market_regime.py         # 시장 국면 필터: 3중 신호(200일선 + 단기모멘텀 + MA크로스) → bearish/caution/bullish
│   ├── fundamental_loader.py    # 펀더멘털(PER·부채비율) — pykrx(우선) → yfinance(폴백)
│   ├── earnings_filter.py       # 실적 발표일 필터 (전후 N일 신규 매수 금지, yfinance earningsDate)
│   ├── indicator_correlation.py # 스코어링 지표 상관계수 분석·고상관 쌍 제거 권고
│   ├── ensemble_correlation.py  # 앙상블 전략 신호 상관계수 + BUY 동시 발생률 + 대안 전략 권고 + auto_downgrade
│   ├── strategy_ensemble.py     # 앙상블: technical + momentum_factor + volatility_condition (정보 소스 분리, auto_downgrade)
│   ├── data_validator.py        # OHLCV 정합성 검사 (Null, NaN, 음수 주가, 타임스탬프 역전)
│   ├── notifier.py              # 통합 알림 이중화 (1차 디스코드 → 2차 텔레그램 → 3차 이메일, critical 전채널 동시)
│   ├── position_lock.py         # threading.RLock (포지션/주문 동시 접근 제어)
│   └── order_guard.py           # 동일 종목 TTL(기본 600초) 동안 중복 주문 차단
├── strategies/
│   ├── __init__.py
│   ├── base_strategy.py         # 추상 클래스: analyze(df), generate_signal(df, **kwargs)
│   ├── scoring_strategy.py      # IndicatorEngine + SignalGenerator, 멀티 지표 스코어링
│   ├── mean_reversion.py        # Z-Score·ADX·52주 이중 필터·코스피200 제한·펀더멘털 필터
│   ├── trend_following.py       # ADX·200일선·MACD·ATR 추세 추종
│   ├── momentum_factor.py       # 모멘텀 팩터 (N일 수익률만, 앙상블용)
│   └── volatility_condition.py  # 변동성 조건 (N일 실현변동성만, 앙상블용)
├── api/
│   ├── __init__.py
│   ├── kis_api.py               # KIS REST API: 토큰·시세·주문·잔고·일봉. 이중 Rate Limiter(초당+분당) + 사용량 모니터링 + Circuit Breaker
│   ├── websocket_handler.py     # KIS 웹소켓 실시간 체결/호가 (asyncio, Heartbeat 45초, 자동 재연결)
│   └── circuit_breaker.py       # CLOSED → OPEN → HALF_OPEN, API 연속 5회 실패 시 60초 차단
├── backtest/
│   ├── __init__.py
│   ├── backtester.py            # 시뮬레이션: strict_lookahead 기본, 수수료·세금·동적 슬리피지·손절/익절/트레일링, 과매매 분석
│   ├── report_generator.py      # txt·html 리포트 (거래 내역, 성과 지표, 자본 곡선, 과매매 분석)
│   ├── strategy_validator.py    # validate: KS11·코스피 상위 50 동일비중 벤치마크, 손익비 자동 경고+디스코드
│   ├── paper_compare.py         # 모의투자 vs 백테스트 비교, 실전 전환 준비 자동 평가(check_live_readiness)
│   └── param_optimizer.py       # Grid / Bayesian(scikit-optimize) 최적화, 가중치 대칭 Grid Search + OOS 게이트
├── database/
│   ├── __init__.py
│   ├── models.py                # ORM 모델 5종(StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport), SQLite WAL/PostgreSQL, scoped_session, @with_retry, db_session()
│   ├── repositories.py          # CRUD — 읽기·쓰기 전체 @with_retry, get_paper_performance_metrics
│   └── backup.py                # SQLite Online Backup API로 WAL 안전 백업 (실패 시 -wal/-shm 포함 폴백), 보관 일수 자동 삭제
├── monitoring/
│   ├── __init__.py
│   ├── logger.py                # loguru 초기화 (파일 로테이션·콘솔 출력), log_trade(), log_signal()
│   ├── discord_bot.py           # 디스코드 웹훅 전송 (Notifier를 통해 호출 권장)
│   ├── liquidate_trigger.py     # HTTP POST /liquidate 긴급 청산 (X-Token 인증)
│   ├── dashboard.py             # 콘솔 대시보드 (선택, show_summary_line)
│   └── web_dashboard.py         # aiohttp 웹 대시보드 (포트폴리오·스냅샷 JSON/HTML, 10초 폴링)
├── tests/                       # pytest tests/ -q
│   ├── __init__.py
│   ├── test_backtester_strategies.py    # 백테스터 전략별 시뮬레이션 검증
│   ├── test_backtester_trailing_stop.py # 트레일링 스탑 로직 검증
│   ├── test_blackswan_detector.py       # 블랙스완 감지·쿨다운 검증
│   ├── test_discord_bot.py              # 디스코드 알림 모킹·콘솔 fallback
│   ├── test_integration_smoke.py        # 설정·DB·지표·신호 등 연동 스모크 테스트
│   ├── test_kis_websocket_e2e.py        # KIS API·웹소켓 모의 E2E 테스트
│   ├── test_order_executor_paper.py     # OrderExecutor paper 모드 검증
│   ├── test_portfolio_manager.py        # 포트폴리오·sync 검증
│   ├── test_risk_manager.py             # 리스크 매니저 (포지션·손절·동적 슬리피지)
│   ├── test_scheduler.py                # 스케줄러 구간·동작 검증
│   ├── test_signal_generator.py         # 신호 생성·스코어링 검증
│   ├── test_strategy_validator.py       # 전략 검증(validate) 로직 검증
│   ├── test_trading_hours.py            # 장 시간·휴장일 검증
│   └── test_watchlist_manager.py        # watchlist 모드별 resolve 검증
├── docs/
│   └── PROJECT_GUIDE.md         # 본 문서
└── reports/                     # 백테스트 txt/html 출력 (.gitignore)
```

---

## 3. 파일별 상세 역할

### 3.1 루트

| 파일 | 역할 |
|------|------|
| **main.py** | CLI 진입점. `--mode`: backtest / validate / paper / live / liquidate / compare / optimize / dashboard / check_correlation / check_ensemble_correlation. **strict-lookahead 기본 True**, `--allow-lookahead` 시 해제(경고 출력). paper/live 시 시장 국면 필터 적용. 실전: `ENABLE_LIVE_TRADING=true` + `--confirm-live` 필수. |
| **test_integration.py** | 설정·DB·지표·신호·리스크·백테스트·리포트·디스코드 등 전체 파이프라인 일괄 검증(14단계). 단일 실행 스크립트 (pytest 아님). |
| **pyproject.toml** | 프로젝트 메타데이터: name=`quant_trader`, version=`0.1.0`, Python `>=3.11,<3.13`, 패키지 구성, pytest 설정 (`tests/` 대상, pandas 경고 필터). |
| **requirements.txt** | pip 의존성 목록. pandas, numpy, scipy, pandas-ta, pykrx, finance-datareader, yfinance, requests, aiohttp, websockets, sqlalchemy, pyyaml, loguru, click, pytest 등. |
| **.env.example** | 환경변수 템플릿. KIS API 키, 디스코드 웹훅, 텔레그램 봇, 이메일 SMTP, Rate Limit, 긴급 청산 토큰. 복사해 `.env`로 사용. |
| **.gitignore** | `.env`, `settings.yaml`, `data/`, `logs/`, `*.db`, `reports/*`, `fintics/`, `__pycache__/`, `.venv/` 등 제외. |
| **README.md** | 프로젝트 소개, 주요 기능, 빠른 시작, 실행 예시(backtest/paper/live). |
| **quant_trader_design.md** | 전체 아키텍처·지표 공식·전략·리스크 상세 설계서 (본 문서의 상위 참고 문서). |

### 3.2 config/

| 파일 | 역할 |
|------|------|
| **config_loader.py** | `load_settings()`, `load_strategies()`, `load_risk_params()`, `load_all_config()`. `.env`로 KIS 키·계좌·디스코드 웹훅 덮어씀. 다중 계좌: `kis_api.accounts`, `Config.get_account_no(strategy)`. `Config.get()` 싱글톤. `ConfigOverlay`로 YAML 설정과 .env 병합. |
| **settings.yaml.example** | 설정 예시. 복사해 `settings.yaml`로 사용. (`settings.yaml`은 .gitignore에 포함) |
| **strategies.yaml** | `active_strategy`, `indicators`(RSI·MACD·볼린저·MA·스토캐스틱·ADX·ATR·거래량), `scoring`(weights, collinearity_mode), `mean_reversion`(z_score, adx, fundamental_filter), `trend_following`, `momentum_factor`, `volatility_condition`, `ensemble`(mode, auto_downgrade, independence_threshold). |
| **risk_params.yaml** | **backtest_universe**(mode: current/historical/kospi200, exclude_administrative), **liquidity_filter**(20일 평균 거래대금 하한, strict, check_on_entry), position_sizing(1% 룰, initial_capital), stop_loss, take_profit(부분 익절), trailing_stop, diversification(max_sector_ratio 포함), position_limits, drawdown, performance_degradation, paper_backtest_compare(live_readiness), transaction_costs(commission, tax, slippage, dynamic_slippage). |
| **holidays.yaml** | 휴장일 목록. `python main.py --update-holidays`로 pykrx+fallback 자동 갱신. |
| **holidays.yaml.example** | 휴장일 예시 파일. |

### 3.3 core/

| 파일 | 역할 |
|------|------|
| **data_collector.py** | 한국: FDR(수정주가, 우선) → yfinance(수정주가) → KIS(비수정 가능, 폴백). **소스 추적**: `_last_source`, `_source_history`, `check_source_consistency()`로 불일치 감지. `data_source.allow_kis_fallback: false`로 비수정주가 폴백 차단 가능. `get_krx_stock_list(universe_mode)`: current/historical/kospi200. **`get_sector_map()`** 종목→업종 매핑. |
| **watchlist_manager.py** | manual / top_market_cap / kospi200 / momentum_top / low_vol_top / momentum_lowvol. **유동성 필터**(20일 거래대금 하한, strict 모드: 데이터 없는 종목도 제외). **리밸런싱 캐시**: 팩터 모드는 `rebalance_interval_days`(기본 20)마다 재계산, 사이에는 `data/watchlist_cache.json` 사용. **as_of_date** 지원: 백테스트 시 과거 시점 유니버스 사용 가능. |
| **indicator_engine.py** | pandas-ta로 RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV, volume_ratio. `calculate_all(df)`로 지표 컬럼 추가. |
| **signal_generator.py** | `strategies.yaml` 스코어링 가중치로 점수 합산 → BUY/SELL/HOLD. `generate(df)`, `get_latest_signal(df)`. **`collinearity_mode`**: `max_per_direction`(방향별 최대 1개) 또는 `representative_only`(3그룹 대표 1개씩=MACD+볼린저+거래량만 사용, 권장). 초기화 시 가격 모멘텀 그룹 다중공선성 경고 자동 출력. |
| **risk_manager.py** | 포지션 사이징(1% 룰), `check_diversification`(**업종 비중 포함**: `max_sector_ratio`, FDR Sector), `check_recent_performance`, 손절/익절/트레일링, MDD 한도. `calculate_transaction_costs`. |
| **order_executor.py** | `trading.mode`: paper면 DB만, live면 KIS API. 거래 시간·블랙스완 쿨다운·**실적 발표일 필터**(`skip_earnings_days`) 검사, 재시도(지수 백오프). PositionLock, OrderGuard·KIS 미체결 조회. |
| **portfolio_manager.py** | 보유 포지션·잔고·수익률. `sync_with_broker()`로 KIS 잔고↔DB 크로스체크. `get_portfolio_summary()`. |
| **scheduler.py** | 실전 무한 루프. 장전/장중/장마감. **시장 국면 필터**(단계적: bearish→매수 중단, caution→사이징 축소). 장중 10분 간격. 루프 10분 초과 시 다음 사이클 스킵. |
| **trading_hours.py** | 장 시간·휴장일. holidays.yaml → pykrx → fallback. 주문 가능 시간 검사. |
| **holidays_updater.py** | pykrx(또는 fallback)로 휴장일 조회 → `config/holidays.yaml` 저장. `update_holidays_yaml()`. |
| **blackswan_detector.py** | 급락 감지 시 전량 매도·디스코드 경고·쿨다운. **쿨다운 해제 시** 즉시 재스캔 트리거 + recovery 기간(기본 120분) 중 사이징 50% 축소. `blackswan_recovery_minutes`, `blackswan_recovery_scale`. |
| **market_regime.py** | `check_market_regime()` → 3중 신호 단계적 국면 판별. **신호 A**: 200일선 이탈, **신호 B**: 20일 수익률 ≤ -5%, **신호 C**: MA(20)<MA(60) 데드크로스(선택적). 2개↑ 충족 → bearish(매수 중단), 1개 → caution(사이징 50%), 0 → bullish. 신호 C는 200일선 이탈보다 2~3주 빠르게 추세 전환 포착. `market_regime_ma_cross_enabled: false`면 기존 2-신호 로직과 동일. |
| **fundamental_loader.py** | `get_fundamentals(symbol)`, `check_fundamental_filter()`. **pykrx(우선) → yfinance(폴백)** 순서로 PER·부채비율 조회. pykrx는 한국 종목 PER 정확도 높음. yfinance는 부채비율 등 보충. |
| **earnings_filter.py** | `is_near_earnings(symbol, skip_days)`. 실적 발표일 전후 N일 이내 시 신규 매수 금지. yfinance earningsDate 기반(한국 종목 누락 가능). `trading.skip_earnings_days`(기본 3). |
| **indicator_correlation.py** | 스코어링 지표 점수 시리즈 상관계수·고상관 쌍 권고. `--mode check_correlation` 시 사용. 다중공선성 안내: 3그룹 각 대표 1개만 권장. `suggest_disable_weights()`: 고상관 쌍에서 자동 비활성화 키 추출. 리포트 하단에 다음 단계 CLI 명령어 자동 출력. |
| **ensemble_correlation.py** | 앙상블 전략 **신호** 시리즈 상관계수 + **BUY/SELL 동시 발생률** + 구체적 **대안 전략 권고**. `quick_independence_check()`: 런타임 경량 검사. `should_force_conservative()`: 고상관 시 conservative 전환 판단. |
| **position_lock.py** | 포지션/주문 공유 자원용 `threading.RLock`. |
| **order_guard.py** | 동일 종목에 대해 최근 주문 접수 후 TTL(기본 600초) 동안 추가 주문 차단. |
| **strategy_ensemble.py** | **technical** + **momentum_factor** + **volatility_condition** 신호 통합. majority_vote / weighted_sum / conservative. **auto_downgrade**(기본 true): 첫 analyze()에서 고상관 감지 시 → conservative 자동 전환. 정보 소스 분리(설계서 §4.4). |
| **data_validator.py** | OHLCV Null·NaN·음수 주가·거래량·타임스탬프 역전 등 검사. |
| **notifier.py** | 통합 알림 이중화. 1차 디스코드 → 2차 텔레그램 Bot API → 3차 이메일(SMTP). `critical=True` 시 모든 채널 동시 발송. `Scheduler`, `CircuitBreaker`, `main.py` 등 주요 모듈이 `DiscordBot` 대신 `Notifier` 사용. 알림 실패 5회 누적 시 점검 경고. |

### 3.4 strategies/

| 파일 | 역할 |
|------|------|
| **base_strategy.py** | `analyze(df)` → 지표·신호 붙은 DataFrame, `generate_signal(df, **kwargs)` → 최신 BUY/SELL/HOLD·점수·상세. |
| **scoring_strategy.py** | IndicatorEngine + SignalGenerator. 총점 ≥ buy_threshold 매수, ≤ sell_threshold 매도. |
| **mean_reversion.py** | Z-Score·ADX 필터. **52주 이중 필터**: 고점 대비 -30% 하락 또는 저점 대비 +5% 이내 → 매수 제외. **`restrict_to_kospi200: true`**: 코스피200만 매수 허용. **펀더멘털 필터**(pykrx→yfinance). 한국 시장 한계·실전 권장: 설계서 §4.2. |
| **trend_following.py** | ADX·200일선·MACD·ATR 기반 추세 추종. 진입 늦음·손익비 ≥ 2.0 검증 필수. 한국 시장 추세 지속성 약함(§4.3). |
| **momentum_factor.py** | N일 수익률만 사용(기술지표 없음). 앙상블용. lookback_days, buy_threshold_pct, sell_threshold_pct. |
| **volatility_condition.py** | N일 실현변동성(연율화)만 사용. 저변동성=매수, 고변동성=매도. 앙상블용. |

### 3.5 api/

| 파일 | 역할 |
|------|------|
| **kis_api.py** | OAuth 토큰 발급·갱신, 시세·주문·잔고·일봉 조회. **이중 Rate Limiter**: Token Bucket(초당, `max_calls_per_sec` 기본 10) + 슬라이딩 윈도우(분당, `max_calls_per_min` 기본 300). `get_rate_limit_stats()`: 사용량 모니터링(최근 60초 활용률, 429 누적). 429 시 `Retry-After` 대기 후 재시도. 401 시 `KISTokenExpiredError` 발생. CircuitBreaker 연동. |
| **websocket_handler.py** | KIS 웹소켓 실시간 체결/호가. asyncio 기반, Heartbeat 45초 타임아웃, 자동 재연결, 콜백으로 가격 전달. |
| **circuit_breaker.py** | API 연속 5회 실패 시 CLOSED → OPEN(60초 차단) → HALF_OPEN. 요청 차단으로 계정 제재 방지. Notifier 알림. |

### 3.6 backtest/

| 파일 | 역할 |
|------|------|
| **backtester.py** | OHLCV + 전략 시뮬레이션. 수수료·세금·슬리피지·1% 룰·손절/익절/트레일링 스탑. **strict_lookahead 기본 True**. 성과 지표 + **과매매 분석**(평균 보유 기간, 총 수수료). |
| **strategy_validator.py** | 최소 3~5년, 샤프·MDD·벤치마크(KS11 + **코스피 상위 50 동일비중**). in/out-of-sample. `run()`, `run_walk_forward()`. `--no-benchmark-top50` 으로 Top50 비활성화. **손익비 자동 경고**: 추세 추종 < 2.0, 기타 < 1.0 시 WARN + 디스코드 알림. |
| **report_generator.py** | txt·html 리포트. 거래 내역, 성과 지표, 자본 곡선, **과매매 분석**(평균 보유 기간, 총 수수료). `--output-dir`. |
| **paper_compare.py** | 지정 기간 paper 성과 vs 동일 기간·전략 백테스트. divergence 시 경고·디스코드(설정 시). **`check_live_readiness()`**: 방향성 일치율 ≥70%, 수익률 차이 ≤5%, 최소 거래일·거래건 충족 시 "실전 전환 준비 완료" 신호 + 디스코드 알림. paper 모드 장마감 시 자동 평가. |
| **param_optimizer.py** | Grid Search / Bayesian(scikit-optimize). train_ratio·OOS 보고. `--include-weights` 시 **스코어링 가중치 대칭 Grid Search + OOS 샤프≥1.0 게이트**. `--auto-correlation`: 최적화 전 상관 분석 자동 실행, 고상관 지표 자동 비활성화. `--disable-weights w_rsi,w_ma` 등으로 수동 지정도 가능. `Backtester.run(..., param_overrides=)`. |

### 3.7 database/

| 파일 | 역할 |
|------|------|
| **models.py** | ORM 모델 5종. `init_database()` 시 WAL 활성화 **검증** — WAL이 아니면 ERROR 로그. **scoped_session**: 스레드별 세션 격리. **`@with_retry`**: DB locked 시 3회 지수 백오프 재시도. **`db_session()`**: 컨텍스트 매니저(commit/rollback/close 자동). SQLite: WAL+busy_timeout(30s)+synchronous=NORMAL. PostgreSQL: pool_size=5, pool_pre_ping. |
| **repositories.py** | CRUD — **읽기·쓰기 전체 함수**에 `@with_retry` 적용 (WAL 체크포인트 중 일시적 locked에도 안전). `get_paper_performance_metrics(start, end)` (compare 모드). |
| **backup.py** | **SQLite Online Backup API** (`sqlite3.Connection.backup()`)로 WAL 모드에서도 일관된 스냅샷 백업. 실패 시 `-wal`/`-shm` 파일 포함 `shutil.copy2` 폴백. 보관 일수 초과 분 삭제. |

### 3.8 monitoring/

| 파일 | 역할 |
|------|------|
| **logger.py** | loguru 초기화. logging 설정에 따라 파일 로테이션·콘솔 출력. |
| **discord_bot.py** | 디스코드 웹훅 전송 전용. 직접 사용보다는 `Notifier`를 통해 호출 권장 (이중화 보장). |
| **liquidate_trigger.py** | `LIQUIDATE_TRIGGER_TOKEN`·`LIQUIDATE_TRIGGER_PORT` 설정 시 POST /liquidate (X-Token 또는 ?token=)으로 긴급 청산. |
| **dashboard.py** | 콘솔 대시보드(선택). |
| **web_dashboard.py** | aiohttp. 포트폴리오 요약·포지션·최근 30일 스냅샷. 10초 폴링. `--mode dashboard` 또는 `python -m monitoring.web_dashboard [--port 8080]`. |

### 3.9 tests/

| 파일 | 역할 |
|------|------|
| **test_backtester_strategies.py** | 백테스터 전략별 시뮬레이션 검증. |
| **test_backtester_trailing_stop.py** | 트레일링 스탑 로직 검증. |
| **test_blackswan_detector.py** | 블랙스완 감지·쿨다운. |
| **test_discord_bot.py** | 디스코드 알림(모킹). |
| **test_integration_smoke.py** | 설정·DB·지표·신호 등 연동 스모크. |
| **test_kis_websocket_e2e.py** | KIS 웹소켓 모의 E2E. |
| **test_order_executor_paper.py** | OrderExecutor paper 모드. |
| **test_portfolio_manager.py** | 포트폴리오·sync. |
| **test_risk_manager.py** | 리스크 매니저(포지션·손절·거래 비용 등). |
| **test_scheduler.py** | 스케줄러 구간·동작. |
| **test_signal_generator.py** | 신호 생성·스코어링. |
| **test_strategy_validator.py** | 전략 검증(validate) 로직. |
| **test_trading_hours.py** | 장 시간·휴장일. |
| **test_watchlist_manager.py** | watchlist 모드별 resolve. |

실행: `pytest tests/ -q`

---

## 4. 설정 파일(YAML) 요약

### config/settings.yaml

| 섹션 | 주요 키 | 설명 |
|------|---------|------|
| **kis_api** | app_key, app_secret, account_no, base_url, mock_url, use_mock, max_calls_per_sec(10), max_calls_per_min(300), max_retry(3) | KIS API 인증·호출 제한. `.env`로 키 덮어씀 |
| **database** | type(sqlite), sqlite_path(data/quant_trader.db) | DB 설정. type: postgresql 전환 가능 |
| **logging** | level(INFO), log_dir(logs), rotation(10MB), retention(30 days) | loguru 로그 설정 |
| **data_source** | preferred(auto), allow_kis_fallback(true), warn_on_source_mismatch(true) | 데이터 소스·수정주가 일치 제어 |
| **trading** | market_open(09:00), market_close(15:30), mode(paper), auto_entry(false), skip_earnings_days(3), market_regime_filter(true), market_regime_index(KS11), blackswan_recovery_minutes(120), sync_broker_interval_minutes 등 | 매매 모드·시장 국면 필터·블랙스완 |
| **discord** | enabled(false), webhook_url, username | 디스코드 알림 |
| **telegram** | enabled(false), bot_token, chat_id | 텔레그램 알림 (이중화) |
| **dashboard** | host(0.0.0.0), port(8080) | 웹 대시보드 |
| **watchlist** | mode(top_market_cap), market(KOSPI), top_n(20), rebalance_interval_days(20), symbols | 관심 종목 |

### config/strategies.yaml

| 섹션 | 주요 키 | 설명 |
|------|---------|------|
| **active_strategy** | scoring | 기본 전략 |
| **indicators** | rsi(period, oversold, overbought), macd, bollinger, moving_average, stochastic, adx, atr, volume | 지표 파라미터 |
| **scoring** | buy_threshold(3), sell_threshold(-3), collinearity_mode(representative_only), weights | 스코어링 가중치·임계값 |
| **mean_reversion** | z_score_buy(-2), z_score_sell(2), lookback_period(20), adx_filter, exclude_52w_low_near, fundamental_filter(PER·부채비율), restrict_to_kospi200 | 평균 회귀 |
| **trend_following** | adx_threshold, trend_ma_period, atr_stop_multiplier, trailing_atr_multiplier | 추세 추종 |
| **momentum_factor** | lookback_days(20), buy_threshold_pct, sell_threshold_pct | 모멘텀 팩터 (앙상블용) |
| **volatility_condition** | lookback_days(60), low_vol_max_pct, high_vol_min_pct | 변동성 조건 (앙상블용) |
| **ensemble** | mode(majority_vote), auto_downgrade(true), independence_threshold(0.6), confidence_weight | 앙상블 통합 |

### config/risk_params.yaml

| 섹션 | 주요 키 | 설명 |
|------|---------|------|
| **backtest_universe** | mode(historical), exclude_administrative(true) | 백테스트 유니버스·생존자 편향 완화 |
| **position_sizing** | max_risk_per_trade(0.01), initial_capital(10,000,000) | 1% 룰 포지션 사이징 |
| **stop_loss** | type(atr), fixed_rate, atr_multiplier(2.0) | 손절매 |
| **take_profit** | type(fixed), fixed_rate(0.10), partial_exit, partial_ratio, partial_target | 익절매 (부분 익절 포함) |
| **trailing_stop** | enabled, type(fixed), fixed_rate(0.03), atr_multiplier | 트레일링 스탑 |
| **liquidity_filter** | enabled(true), min_avg_trading_value_20d_krw(5e9), strict(true), check_on_entry(true) | 유동성 필터 (20일 평균 거래대금) |
| **diversification** | max_position_ratio(0.20), max_investment_ratio(0.70), max_positions(10), min_cash_ratio, max_sector_ratio(0.40) | 분산 투자·업종 비중 제한 |
| **position_limits** | max_holding_days | 최대 보유 기간 |
| **drawdown** | max_portfolio_mdd(0.15), max_daily_loss(0.03), recovery_scale | MDD 제한 |
| **performance_degradation** | enabled, recent_trades(20), min_win_rate(0.35) | 성과 열화 감지 |
| **paper_backtest_compare** | live_readiness(min_direction_agreement_pct, max_return_diff_pct, min_trading_days, min_trades) | 실전 전환 기준 |
| **transaction_costs** | commission_rate(0.00015), tax_rate(0.0020), slippage(0.0005), dynamic_slippage, capital_gains_tax | 거래 비용 |

### config/holidays.yaml

휴장일 목록. `python main.py --update-holidays`로 pykrx+fallback 자동 갱신.

---

## 5. 실행 모드별 데이터 흐름

### 백테스트

```
main.py (--mode backtest)
  → DataCollector.fetch_korean_stock(symbol, start, end)
  → Backtester.run(df, strategy_name)  # strict_lookahead 기본, 전략.analyze → _simulate
  → Backtester.print_report(result)
  → ReportGenerator.generate_all(result)  # txt, html
```

### 모의투자(paper)

```
main.py (--mode paper)
  → WatchlistManager.resolve()
  → 종목마다: DataCollector.fetch_korean_stock(symbol) → strategy.generate_signal(df)
  → BUY/SELL 시 DiscordBot.send_signal_alert, OrderExecutor.execute_buy/execute_sell (DB만)
  → PortfolioManager.get_portfolio_summary()
```

### 실전(live)

```
main.py (--mode live --confirm-live)
  → KISApi.authenticate(), verify_connection()
  → BlackSwanDetector, PortfolioManager.sync_with_broker()
  → Scheduler.run()
       장전: 데이터 수집, 전략 분석, 매수 후보
       장중: 10분마다 최대 보유 기간 정리·신호·손절/익절 → OrderExecutor (OrderGuard·미체결 확인 후 KIS 주문)
            루프 10분 초과 시 다음 사이클 1회 스킵
       장중: sync_broker_interval_minutes마다 KIS↔DB 크로스체크
       장마감: 일일 리포트, 스냅샷, 크로스체크, DB 백업(backup_path 설정 시), 디스코드
```

### 긴급 청산

| 방법 | 사용 |
|------|------|
| **CLI** | `python main.py --mode liquidate` — DB 포지션 조회 후 종목별 매도(실전 시 KIS 현재가). |
| **HTTP** | `LIQUIDATE_TRIGGER_TOKEN`·`LIQUIDATE_TRIGGER_PORT` 설정 후 `python -m monitoring.liquidate_trigger`, POST /liquidate. |

### DB 백업·KIS 크로스체크

| 항목 | 설명 |
|------|------|
| **일일 백업** | `database.backup_path` 설정 시 장마감 후 SQLite 날짜별 복사. backup_retention_days 초과 분 삭제. |
| **KIS 크로스체크** | live에서 장 시작 전·장중(sync_broker_interval_minutes)·장마감 시 KIS 잔고와 DB 포지션 대조. 불일치 시 로깅·디스코드(자동 보정은 미구현, 알림만). |

### 자금 관리·성과 열화 감지

- **자금 관리**: `diversification.max_investment_ratio`(전체 주식 비중 상한), `max_positions`(동시 보유 종목 수), `max_position_ratio`(단일 종목 비중), **`max_sector_ratio`**(단일 업종 최대 비중, FDR Sector 기준). RiskManager.check_diversification, Backtester._simulate에서 적용.
- **성과 열화**: `performance_degradation.recent_trades`, `min_win_rate`. 최근 N거래 승률이 임계값 미만이면 **신규 매수만** 중단. RiskManager.check_recent_performance → OrderExecutor에서 매수 전 호출.

### 분석·검증 모드

```
main.py (--mode check_correlation)
  → DataCollector.fetch_korean_stock(symbol)
  → IndicatorEngine.calculate_all(df)
  → SignalGenerator.generate(df)  # 각 지표별 스코어 시리즈 추출
  → indicator_correlation.run_indicator_correlation_check()  # Pearson 상관계수
  → 리포트 저장 (reports/indicator_correlation_*.txt)

main.py (--mode check_ensemble_correlation)
  → DataCollector.fetch_korean_stock(symbol)
  → StrategyEnsemble.analyze(df)  # 3개 전략 신호 시리즈
  → ensemble_correlation.run_ensemble_signal_correlation_check()  # 상관 + BUY 동시 발생률
  → 리포트 저장 + 대안 전략 권고

main.py (--mode optimize --include-weights --auto-correlation)
  → indicator_correlation (자동 실행) → 고상관 지표 비활성화
  → param_optimizer.grid_search_scoring_weights() → 대칭 Grid Search
  → OOS 샤프 ≥ 1.0 게이트 → 통과 시 YAML 스니펫 출력
```

---

## 6. 알고리즘·지표 요약

- **지표**: `core/indicator_engine.py`에서 pandas-ta로 RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV, volume_ratio 계산. 설정은 `config/strategies.yaml` → `indicators`.
- **스코어링**: `core/signal_generator.py`가 가중치(weights)로 점수 합산 → buy_threshold/sell_threshold로 BUY/SELL 판단. **⚠️ 가중치는 미검증 직관값이며, 이 상태로 실전 투입하면 신호가 노이즈**. `collinearity_mode: representative_only`(권장)로 설정하면 MACD+볼린저+거래량 3개만 합산하여 다중공선성을 근본적으로 차단. 반드시 `check_correlation → optimize --include-weights --auto-correlation → validate --walk-forward` 파이프라인으로 최적화 후 사용. 자세한 내용은 `quant_trader_design.md` §4.1 "가중치 설정 유의사항" 참고.
- **전략**: scoring(멀티 지표), mean_reversion(Z-Score·ADX·52주 이중 필터·코스피200 제한·펀더멘털(pykrx→yfinance)), trend_following(ADX·200일선·MACD·ATR), **momentum_factor**(N일 수익률만), **volatility_condition**(N일 실현변동성만), **ensemble**(technical + momentum_factor + volatility_condition). 각 전략의 시장 비효율성 가정은 설계서 §4 참고.

공식·파라미터·신호 조건 등 **상세는 루트의 `quant_trader_design.md` §3(지표), §4(전략), §5(리스크)** 참고.

---

## 7. 전략 유효성 검증 및 실전 체크리스트

### 검증 요구 사항

| 항목 | 요구 |
|------|------|
| **데이터 기간** | 최소 3~5년. `--validation-years` 기본 5. |
| **샤프 비율** | 1.0 이상. `--min-sharpe` 변경 가능. |
| **벤치마크** | 코스피(KS11) 대비 초과 수익. **코스피 상위 50종목 동일비중** 대비 OOS 초과 수익 검증(기본 사용, `--no-benchmark-top50` 으로 비활성화). `--benchmark-symbol`. |
| **오버피팅** | in/out-of-sample 분리. `--split-ratio` 기본 0.7. |

실행 예: `python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5`  
결과: `reports/validation_*.txt`.

**검증 한계**: 통과해도 실전 수익 보장 없음(국면 편향·OOS 과적합 가능). **워크포워드**: `--mode validate --walk-forward` 로 슬라이딩 윈도우 반복 검증 가능 (train 2년→test 1년, 1년 스텝). `quant_trader_design.md` §8.2.

### 실전 투입 전 체크리스트

| 항목 | 설명 |
|------|------|
| 전략 검증 | `--mode validate`로 3~5년·샤프 1.0 이상·벤치마크·in/out-of-sample 완료. |
| Look-Ahead | strict-lookahead 기본 유지. `--allow-lookahead` 미사용. |
| 모의투자 | 1~2개월 paper 후 실전. 방향성 일치 확인. |
| 첫 실전 규모 | 운용 예정 금액의 10% 이하. |
| KIS E2E | 모의투자 환경에서 API·주문·잔고 전 과정 테스트. |

### 실전 10분 루프 안전장치

| 안전장치 | 설명 |
|----------|------|
| **주문 전 미체결** | OrderGuard(TTL 600초) + KIS 미체결 조회. 미체결 있으면 주문 보류. |
| **루프 10분 초과** | 한 사이클 실행이 10분 초과 시 다음 사이클 1회 스킵. |

---

## 8. watchlist 모드

`config/settings.yaml` → `watchlist`.

| mode | 설명 |
|------|------|
| **manual** | `symbols` 목록 직접 관리. |
| **top_market_cap** | 시가총액 상위 N개. `market`(KOSPI/KOSDAQ), `top_n`. DataCollector.get_krx_stock_list() 기준. |
| **kospi200** | 코스피200 유사 시총 상위 N개. `kospi200_top_n`. |
| **momentum_top** | 12개월 수익률 상위 N개(모멘텀 팩터). 시총 풀에서 1년 수익률 계산 후 상위 `top_n`. |
| **low_vol_top** | 60일 실현변동성 하위 = 저변동성 상위 N개. |
| **momentum_lowvol** | 저변동성 필터 통과 종목 중 12개월 수익률 상위 N개. |

**리밸런싱 주기** (팩터 모드: momentum_top, low_vol_top, momentum_lowvol):
- `rebalance_interval_days`(기본 20) 경과 시에만 재계산, 그 사이에는 `data/watchlist_cache.json` 캐시 사용.
- 매일 재계산 → 종목 교체 잦아 거래비용 증가 / 너무 드물면 팩터 효과 희석. 월 1회(≈20일)가 학술 기준.
- 캐시 강제 갱신: `data/watchlist_cache.json` 삭제 후 다음 `resolve()` 호출 시 즉시 재계산.

자동 모드 실패 시 `symbols` 또는 기본(005930) fallback.

---

## 9. 의존성 및 환경

### Python 버전

`pyproject.toml`: `>=3.11,<3.13`. 3.11 또는 3.12 사용 권장.

### 주요 의존성 (`requirements.txt`)

| 카테고리 | 패키지 | 용도 |
|----------|--------|------|
| **데이터 처리** | `pandas>=2.0`, `numpy>=2.0`, `scipy>=1.14` | OHLCV 시계열, 수치 계산, 통계 |
| **기술적 지표** | `pandas-ta>=0.4.67b0` | RSI, MACD, 볼린저, ADX, ATR, OBV 등 |
| **데이터 수집** | `pykrx>=0.1`, `finance-datareader>=0.9.50`, `yfinance>=0.2.18` | KRX 종목·시세, 한국/미국 주가, 수정주가 |
| **HTTP/비동기** | `requests>=2.32`, `aiohttp>=3.10`, `websockets>=14.0` | KIS REST API, 웹 대시보드, 실시간 웹소켓 |
| **DB** | `sqlalchemy>=2.0` | ORM (SQLite WAL / PostgreSQL) |
| **설정** | `pyyaml>=6.0` | YAML 설정 파일 로드 |
| **로깅** | `loguru>=0.7` | 구조화 로그, 파일 로테이션 |
| **CLI** | `click>=8.1` | 명령줄 인터페이스 (일부) |
| **테스트** | `pytest>=8.0`, `pytest-asyncio>=0.24` | 단위·통합 테스트 |

### 환경변수 (`.env`)

| 변수 | 필수 | 설명 |
|------|------|------|
| `KIS_APP_KEY` | ✅ | KIS API 앱 키 |
| `KIS_APP_SECRET` | ✅ | KIS API 앱 시크릿 |
| `KIS_ACCOUNT_NO` | ✅ | KIS 계좌번호 |
| `DISCORD_WEBHOOK_URL` | 권장 | 디스코드 알림 웹훅 URL |
| `ENABLE_LIVE_TRADING` | live 시 | `true` 설정 + `--confirm-live` 필수 |
| `MAX_CALLS_PER_SEC` | 선택 | KIS API 초당 호출 제한 (기본 10) |
| `MAX_CALLS_PER_MIN` | 선택 | KIS API 분당 호출 제한 (기본 300) |
| `TELEGRAM_BOT_TOKEN` | 선택 | 텔레그램 봇 토큰 (알림 이중화) |
| `TELEGRAM_CHAT_ID` | 선택 | 텔레그램 채팅 ID |
| `SMTP_SERVER` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | 선택 | 이메일 알림 (알림 삼중화) |
| `ALERT_EMAIL_TO` | 선택 | 알림 수신 이메일 |
| `LIQUIDATE_TRIGGER_TOKEN` | 선택 | HTTP 긴급 청산 인증 토큰 |
| `LIQUIDATE_TRIGGER_PORT` | 선택 | HTTP 긴급 청산 포트 (기본 9090) |

---

## 10. 저장소(Git) 관리

**커밋 대상**:

| 항목 | 파일 |
|------|------|
| **Python 소스** | `main.py`, `test_integration.py`, `core/*.py`, `strategies/*.py`, `api/*.py`, `backtest/*.py`, `database/*.py`, `monitoring/*.py`, `tests/*.py`, `config/config_loader.py` |
| **설정 예시** | `config/settings.yaml.example`, `config/holidays.yaml.example`, `.env.example` |
| **메타데이터** | `pyproject.toml`, `requirements.txt` |
| **문서** | `README.md`, `quant_trader_design.md`, `docs/PROJECT_GUIDE.md` |
| **설정 (공개 가능)** | `config/strategies.yaml`, `config/risk_params.yaml` |
| **기타** | `.gitignore` |

**제외(.gitignore)**:

| 카테고리 | 대상 |
|----------|------|
| **비밀/환경** | `.env`, `.env.local`, `.env.*.local`, `config/settings.yaml` |
| **Python 런타임** | `__pycache__/`, `.venv/`, `venv/`, `.pytest_cache/`, `*.py[cod]`, `*.egg-info/` |
| **데이터/로그** | `data/`, `logs/`, `*.db`, `*.sqlite`, `*.log` |
| **백테스트 산출물** | `reports/backtest_*.html`, `reports/backtest_*.txt`, `reports/*.md` |
| **외부 프로젝트** | `fintics/` (본 저장소는 quant_trader 소스만 관리) |
| **IDE/OS** | `.idea/`, `.vscode/`, `Thumbs.db`, `.DS_Store` |

불필요한 소스·생성물·외부 프로젝트는 저장소에 포함하지 않습니다.

---

> 📌 **상세 설계·지표 공식·전략 로직**: `quant_trader_design.md`  
> **문서 버전**: v2.2  
> **최종 수정**: 2026-03-19 (프로젝트 구조 정확화, 의존성·환경변수 섹션 추가, ORM 모델 명세, 테스트 파일 개별 명시, Git 관리 정밀화, 불필요 파일 정리)
