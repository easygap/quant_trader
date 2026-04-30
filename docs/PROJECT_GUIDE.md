# QUANT TRADER — 프로젝트 가이드

> **목적**: 코드를 볼 때 **파일별 역할**, **프로그램 흐름**, **알고리즘·설정**을 세세히 알 수 있도록 정리한 문서.
> **문서 버전**: v5.2
> **최종 수정**: 2026-04-30
> **참고**: 전체 아키텍처·지표 공식·전략 상세·시스템 진단은 루트의 `quant_trader_design.md` 참고.

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
   `main.py` 실행 → 로거·DB 초기화 → `--mode`에 따라 **backtest / backtest_momentum_top / validate / paper / schedule / live / liquidate / compare / optimize / dashboard / check_correlation / check_ensemble_correlation / rebalance** 중 하나로 분기.

2. **백테스트 (backtest)**  
   `DataCollector`로 과거 주가 수집 → `Backtester`가 전략으로 시뮬레이션(수수료·세금·슬리피지·손절/익절/트레일링 스탑 반영, **strict-lookahead 기본**) → `ReportGenerator`가 txt/html 리포트 생성.

3. **모의투자 (paper)**  
   워치리스트 **1회 순회** 후 프로세스 종료. `WatchlistManager`로 관심 종목 확정 → 종목마다 `DataCollector.fetch_stock` 수집 → 전략 `generate_signal(df, symbol=symbol)` → BUY 시 **시장 국면 필터** 적용 후 `OrderExecutor`가 **DB에만 기록** + 알림. 실제 주문 없음.

4. **스케줄 루프 (schedule)**  
   **모의 전용 무한 루프**. `config.trading.mode`가 `live`이면 거부(실전은 `--mode live --confirm-live`). `core/runtime_lock.py`로 `data/.scheduler.lock` 파일 락을 잡은 뒤 `Scheduler.run()` — 장전/장중/장마감 루프가 paper와 동일하게 동작하며 프로세스를 유지(systemd 상시 구동용). 기본은 signal-only이며, full paper는 `QUANT_AUTO_ENTRY=true`로만 켠다.

5. **실전 (live)**  
   `ENABLE_LIVE_TRADING=true` + `--confirm-live` 필수. KIS API 인증 → `PortfolioManager.sync_with_broker()` → `Scheduler` 무한 루프.  
   - **장전(08:50)** 데이터 수집·전략 분석·**시장 국면 필터** 확인 후 매수 후보 선정(`auto_entry: true` 시 장중 매수).  
   - **장중(09:00~15:30)** 10분 간격으로 최대 보유 기간 초과 정리·신호·손절/익절 확인 → **시장 국면 필터** 통과 시에만 진입 후보 실행 → `OrderExecutor`가 KIS API로 실제 주문. 주문 전 OrderGuard·KIS 미체결 조회로 중복 방지.  
   - **장마감(15:35)** 일일 리포트·스냅샷·KIS 크로스체크·DB 백업(설정 시)·디스코드.

6. **공통**  
   설정: `config/` YAML + `.env`. 데이터·포지션·거래 기록: SQLite(또는 설정 DB).

### 1.2 모드별 진입점

| 모드 | main.py 호출 함수 | 핵심 모듈 |
|------|-------------------|-----------|
| **backtest** | `run_backtest(args)` | DataCollector → Backtester → ReportGenerator |
| **backtest_momentum_top** | `run_backtest_momentum_top(args)` | momentum_top_portfolio.run_momentum_top_portfolio_backtest() — 다종목 동일비중 모멘텀 포트폴리오, 리밸런싱·시장 국면·포트폴리오 스탑 |
| **validate** | `run_strategy_validation(args)` | backtest.strategy_validator (3~5년, 샤프·MDD·벤치마크 KS11·코스피 상위 50 동일비중, in/out-of-sample, **손익비 자동 경고+디스코드**). `--no-benchmark-top50` 으로 Top50 비활성화 |
| **paper** | `run_paper_trading(args)` | WatchlistManager, DataCollector, 전략, OrderExecutor(paper), Notifier |
| **schedule** | `run_scheduler_loop(args)` | `runtime_lock.scheduler_lock`, Scheduler (무한 루프, paper 전용). 기본 signal-only, `QUANT_AUTO_ENTRY=true` 시 full paper. runtime state가 entry만 차단해도 exit/finalize/evidence는 유지 |
| **live** | `run_live_trading(args)` | 4중 보안(전략 상태·환경변수·CLI 플래그·hard gate 5조건) → KISApi, PortfolioManager(sync), Scheduler |
| **liquidate** | `run_emergency_liquidate(args)` | DB 포지션 조회 → 종목별 매도(KIS 현재가 주문) |
| **compare** | `run_compare_paper_backtest(args)` | backtest.paper_compare (run_compare + **check_live_readiness**), divergence 경고 + **실전 전환 준비 자동 평가·디스코드 알림** |
| **optimize** | `run_param_optimize(args)` | backtest.param_optimizer (Grid/Bayesian), Backtester.run(param_overrides=) |
| **dashboard** | `run_dashboard(args)` | monitoring.web_dashboard (aiohttp), PortfolioManager, get_portfolio_snapshots |
| **check_correlation** | `run_check_indicator_correlation(args)` | DataCollector, IndicatorEngine, SignalGenerator → core.indicator_correlation (스코어 상관계수·고상관 쌍 권고) |
| **check_ensemble_correlation** | `run_check_ensemble_correlation(args)` | DataCollector, StrategyEnsemble.analyze → core.ensemble_correlation (신호 상관 + BUY 동시 발생률 + 대안 전략 권고). **validate --strategy ensemble** 시 자동 실행 |
| **rebalance** | `run_rebalance(args)` | BasketRebalancer (baskets.yaml 기반 목표 비중 vs 실제 비중 드리프트 체크 → 주문 생성·실행). `--basket`, `--dry-run` 옵션 지원 |

---

## 2. 실제 디렉터리·파일 구조

```
quant_trader/
├── main.py                      # CLI 진입점, --mode 분기 (14개 모드)
├── test_integration.py          # 통합 검증 스크립트 (단일 실행, pytest 아님)
├── pyproject.toml               # 프로젝트 메타데이터 (Python >=3.11,<3.13, 패키지, pytest 설정)
├── requirements.txt             # pip 의존성 목록
├── .env.example                 # 환경변수 템플릿 (KIS API, 디스코드, 텔레그램, 이메일, 긴급청산)
├── .gitignore                   # 제외 규칙 (.env, settings.yaml, data/, logs/, reports/* 등)
├── README.md                    # 프로젝트 소개·빠른 시작·실행 예시
├── quant_trader_design.md       # 전체 아키텍처·전략·리스크 설계서
├── config/
│   ├── __init__.py
│   ├── config_loader.py         # YAML 통합 로더, .env 덮어쓰기, QUANT_AUTO_ENTRY 해석, YAML/resolved hash, Config.get() 싱글톤
│   ├── settings.yaml.example    # 설정 예시 (settings.yaml은 .gitignore)
│   ├── settings.yaml            # KIS API, database, data_source, trading, discord, telegram, dashboard, watchlist
│   ├── strategies.yaml          # indicators, scoring, mean_reversion, trend_following, fundamental_factor, momentum_factor, volatility_condition, ensemble(components)
│   ├── risk_params.yaml         # backtest_universe, liquidity_filter, 포지션/손절/익절/트레일링/분산/MDD/성과열화/거래비용
│   ├── baskets.yaml             # 바스켓 포트폴리오 & 리밸런싱 설정 (종목별 목표 비중, drift/weekly/monthly 트리거, 신호 가중)
│   ├── holidays.yaml.example    # 한국 휴장일 예시
│   ├── holidays.yaml            # --update-holidays 로 자동 갱신
│   └── us_holidays.yaml         # 미국 휴장일(선택, NYSE 판별 보조)
├── core/
│   ├── __init__.py
│   ├── data_collector.py        # fetch_stock(통합): 미국 티커 yfinance, 한국 FDR→yfinance→KIS 폴백. 소스 추적·is_us_ticker(), get_krx_stock_list(), get_sector_map()
│   ├── watchlist_manager.py     # 관심 종목: manual/top_market_cap/kospi200/momentum_top/low_vol_top/momentum_lowvol + 유동성 필터 + 리밸런싱 캐시 + as_of_date
│   ├── indicator_engine.py      # pandas-ta: RSI, MACD, 볼린저, MA(SMA/EMA), 스토캐스틱, ADX, ATR, OBV, volume_ratio
│   ├── signal_generator.py      # 멀티 지표 스코어링 → BUY/SELL/HOLD, collinearity_mode(representative_only 권장)
│   ├── risk_manager.py          # 포지션 사이징(1% 룰), 분산(업종 비중 포함), 성과 열화, 손절/익절/트레일링, 거래 비용
│   ├── order_executor.py        # 매수/매도 (paper: DB만, live: KIS), PositionLock, OrderGuard, 유동성·어닝 필터, 매수 직전 재검증, Dead-letter 큐(재시도 실패 시 FailedOrder 저장)
│   ├── portfolio_manager.py     # 포지션·잔고·수익률, sync_with_broker(KIS↔DB 크로스체크), save_daily_snapshot()
│   ├── basket_rebalancer.py     # 바스켓 리밸런싱: 목표 비중 vs 실제 비중 드리프트 감지, 주문 생성·실행, 신호 가중 모드, 스케줄러 장전 자동 통합
│   ├── scheduler.py             # 무한 루프: 장전/장중(10분)/장마감, 시장 국면 필터, 블랙스완 recovery, 바스켓 리밸런싱, paper 실전전환 자동 평가
│   ├── runtime_lock.py          # schedule 모드 단일 인스턴스 락 (`data/.scheduler.lock`)
│   ├── trading_hours.py         # 한국 장·휴장일 + 미국(NYSE 정규장, us_holidays.yaml)
│   ├── holidays_updater.py      # 휴장일 YAML 자동 갱신 (pykrx 또는 fallback)
│   ├── blackswan_detector.py    # 급락 감지 → 전량 매도·쿨다운·recovery(점진적 재진입, recovery_scale)
│   ├── market_regime.py         # 시장 국면 필터: 3중 신호(200일선 + 단기모멘텀 + MA크로스) → bearish/caution/bullish
│   ├── fundamental_loader.py    # 펀더멘털(PER·부채비율) — pykrx(우선) → yfinance(폴백)
│   ├── dart_loader.py           # DART Open API (corp_code, 정기공시 기반 실적 시점 추정)
│   ├── earnings_filter.py       # 실적일 필터: yfinance → (선택) DART. `trading.skip_earnings_days`
│   ├── indicator_correlation.py # 스코어링 지표 상관계수 분석·고상관 쌍 제거 권고
│   ├── ensemble_correlation.py  # 앙상블 전략 신호 상관계수 + BUY 동시 발생률 + 대안 전략 권고 + auto_downgrade
│   ├── strategy_ensemble.py     # 앙상블: ensemble.components (technical·momentum_factor·volatility_condition·fundamental_factor 선택), auto_downgrade
│   ├── data_validator.py        # OHLCV 정합성 검사 (Null, NaN, 음수 주가, 타임스탬프 역전)
│   ├── notifier.py              # 통합 알림 이중화 (1차 디스코드 → 2차 텔레그램 → 3차 이메일, critical 전채널 동시)
│   ├── strategy_diagnostics.py  # 전략 진단 보조: DiagnosticLine — 전략별 신호·점수 진단 라인 생성
│   ├── paper_evidence.py        # Paper Evidence 수집 (일별 22개 지표, benchmark excess, anomaly detection)
│   ├── paper_runtime.py         # Paper Runtime State Machine (5개 상태, schema quarantine, allowed_actions)
│   ├── paper_pilot.py           # Paper Pilot Authorization (launch readiness + pilot auth + 리스크 캡)
│   ├── paper_preflight.py       # Paper Preflight Check (운영 준비 상태 점검)
│   ├── strategy_universe.py     # Paper 대상 전략 canonical 목록
│   ├── target_weight_rotation.py # Portfolio-level target-weight plan 생성/검증
│   ├── evidence_collector.py    # 일일 실적 증거 자동 누적 (장마감 후 scheduler 호출)
│   ├── promotion_engine.py      # metrics 기반 전략 승격 판정 (research→paper→live)
│   ├── position_lock.py         # threading.RLock (포지션/주문 동시 접근 제어)
│   └── order_guard.py           # 동일 종목 TTL(기본 600초) 동안 중복 주문 차단
├── tools/
│   ├── run_paper_evidence_pipeline.py  # Paper Evidence 파이프라인 (backfill/finalize/package/quality-report)
│   ├── paper_pilot_control.py          # Paper Pilot 활성화/비활성화/상태 확인 CLI
│   ├── paper_bootstrap.py              # Paper 초기화 (runtime state 셋업)
│   ├── paper_preflight.py              # Paper 세션 전 체크리스트 CLI
│   ├── paper_launch_readiness.py       # Paper 진입 준비 상태 확인 CLI
│   ├── paper_runtime_status.py         # Paper 실행 상태 모니터링 CLI
│   ├── evaluate_and_promote.py         # Canonical 평가 → artifact → 승격 판정(+canonicalized research candidate)
│   ├── research_candidate_sweep.py     # Research-only 후보 sweep → benchmark-aware ranking artifact
│   ├── target_weight_rotation_pilot.py # target-weight 후보 전용 paper/pilot adapter
│   ├── rebuild_paper_runtime.py        # Paper 런타임 재구성
│   └── quarantine_test_artifacts.py    # 테스트 artifact 격리
├── strategies/
│   ├── __init__.py              # 전략 레지스트리(플러그인형): create_strategy(name), get_strategy_names(), register_strategy()
│   ├── base_strategy.py         # 추상 클래스: analyze(df), generate_signal(df, **kwargs)
│   ├── scoring_strategy.py      # IndicatorEngine + SignalGenerator, 멀티 지표 스코어링
│   ├── mean_reversion.py        # Z-Score·ADX·52주 이중 필터·코스피200 제한·펀더멘털 필터
│   ├── trend_following.py       # ADX·200일선·MACD·ATR 추세 추종
│   ├── fundamental_factor.py    # 펀더멘털 팩터 (--strategy fundamental_factor, 앙상블 구성 가능)
│   ├── momentum_factor.py       # 모멘텀 (N일 수익률, CLI `--strategy momentum_factor` 등록 + 앙상블 구성용)
│   └── volatility_condition.py  # 변동성 조건 (앙상블 내부 전용)
├── api/
│   ├── __init__.py
│   ├── kis_api.py               # KIS REST API: 토큰·시세·주문·잔고·일봉. 이중 Rate Limiter(초당+분당) + 지수 백오프+지터 + SSL/커넥션 에러 핸들러 + 토큰 쿨다운 + 사용량 모니터링 + Circuit Breaker
│   ├── websocket_handler.py     # KIS 웹소켓 실시간 체결/호가 (asyncio, Heartbeat 45초, 자동 재연결)
│   └── circuit_breaker.py       # CLOSED → OPEN → HALF_OPEN, API 연속 5회 실패 시 60초 차단
├── backtest/
│   ├── __init__.py
│   ├── backtester.py            # 시뮬레이션: strict_lookahead 기본, 수수료·세금·동적 슬리피지·손절/익절/트레일링, 과매매 분석
│   ├── report_generator.py      # txt·html 리포트 (거래 내역, 성과 지표, 자본 곡선, 과매매 분석)
│   ├── strategy_validator.py    # validate: KS11·코스피 상위 50 동일비중 벤치마크, 손익비 자동 경고+디스코드
│   ├── momentum_top_portfolio.py # 다종목 동일비중 모멘텀 포트폴리오 백테스트 (리밸런싱·시장 국면·포트폴리오 스탑)
│   ├── paper_compare.py         # 모의투자 vs 백테스트 비교, 실전 전환 준비 자동 평가(check_live_readiness)
│   └── param_optimizer.py       # Grid / Bayesian(scikit-optimize) 최적화, 가중치 대칭 Grid Search + OOS 게이트
├── database/
│   ├── __init__.py
│   ├── models.py                # ORM 모델 6종(StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport, FailedOrder), SQLite WAL/PostgreSQL, scoped_session, @with_retry, db_session()
│   ├── repositories.py          # CRUD — 읽기·쓰기 전체 @with_retry, get_paper_performance_metrics, save_failed_order/get_pending_failed_orders/resolve_failed_order (Dead-letter)
│   └── backup.py                # SQLite Online Backup API로 WAL 안전 백업 (실패 시 -wal/-shm 포함 폴백), 보관 일수 자동 삭제
├── monitoring/
│   ├── __init__.py
│   ├── logger.py                # loguru 초기화 (파일 로테이션·콘솔 출력), log_trade(), log_signal()
│   ├── discord_bot.py           # 디스코드 웹훅 전송 (Notifier를 통해 호출 권장)
│   ├── liquidate_trigger.py     # HTTP POST /liquidate 긴급 청산 (X-Token 인증)
│   ├── dashboard.py             # 콘솔 대시보드 (선택, show_summary_line)
│   ├── dashboard_runtime_state.py # 대시보드 런타임 상태 관리 (스케줄러·전략 실행 현황 실시간 상태 전달)
│   ├── web_dashboard.py         # aiohttp 웹 대시보드 (포트폴리오·스냅샷 JSON/HTML, 10초 폴링)
│   └── paper_monitor.py         # Paper 운영 모니터링: log_event(), WeeklyReportGenerator, GoLiveChecker
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
│   ├── test_watchlist_manager.py        # watchlist 모드별 resolve 검증
│   ├── test_basket_rebalancer.py       # 바스켓 리밸런서 (설정·비중·드리프트·트리거·주문·실행)
│   ├── test_us_market_support.py      # 미국 티커·TradingHours NYSE 등
│   └── test_paper_lifecycle.py        # Full paper lifecycle 테스트 (BUY/SELL/snapshot, 격리 DB)
├── scripts/                     # 검증·분석 스크립트 (C-4 OOS, C-5 sleeve/필터/TP sweep/rolling WF/paper 리포트)
├── deploy/                      # (선택) Oracle Cloud ARM 서버 상시 구동
│   ├── README.md               # Oracle Cloud Free Tier ARM 배포 가이드
│   ├── setup.sh                # 시스템 셋업 (Python 3.11, venv, pip install)
│   ├── install_service.sh      # systemd 서비스 등록 스크립트
│   ├── quant_trader.service    # systemd 유닛 파일 (schedule 모드, auto-restart)
│   └── logrotate.conf          # 로그 로테이션 정책 (copytruncate)
├── docs/
│   ├── PROJECT_GUIDE.md         # 본 문서
│   └── BACKTEST_IMPROVEMENT.md  # 백테스트 손익 개선 포인트
└── reports/                     # 백테스트 txt/html + Paper 운영 산출물
    ├── paper_evidence/          # Paper Evidence JSONL (append-only) + promotion package
    │   ├── daily_evidence_{strategy}.jsonl  # 일별 22개 지표
    │   ├── anomalies.jsonl                  # 이상 탐지 로그
    │   └── promotion_evidence_{strategy}.json
    ├── paper_runtime/           # Paper Runtime 상태
    │   ├── {strategy}_pilot_launch_readiness.json/md
    │   ├── pilot_authorizations.jsonl
    │   └── notifier_health.json
    ├── experiment_freeze_pack.md        # 60영업일 실험 동결 기준, hash, 실행 모드
    ├── daily_ops_checklist.md           # 일일 운영 체크리스트
    ├── weekly_ops_checklist.md          # 주간 운영 체크리스트
    ├── experiment_stop_conditions.md    # 중단/동결/재개 기준
    ├── paper_modes_explained.md         # signal-only/full paper 모드 차이
    ├── paper_runbook.md                 # Paper 운영 기본 명령
    └── promotion/               # Promotion 판정 결과
```

---

## 3. 파일별 상세 역할

### 3.1 루트

| 파일 | 역할 |
|------|------|
| **main.py** | CLI 진입점. `--mode`: backtest / **backtest_momentum_top** / **portfolio_backtest** / validate / paper / **schedule** / live / liquidate / compare / optimize / dashboard / check_correlation / check_ensemble_correlation / rebalance (14종). **strict-lookahead 기본 True**, `--allow-lookahead` 시 해제(경고 출력). paper·schedule·live 시 스케줄러 경로에서 시장 국면 필터 등 동일 로직. 실전: `ENABLE_LIVE_TRADING=true` + `--confirm-live` 필수. |
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
| **config_loader.py** | `load_settings()`, `load_strategies()`, `load_risk_params()`, `load_all_config()`. `.env`로 KIS 키·계좌·디스코드·**DART_API_KEY** 등 덮어씀. `QUANT_AUTO_ENTRY`는 ENV > YAML > default(false) 순서로 해석하고 live 모드에서는 ENV override를 무시. `Config.yaml_hash`(YAML 원본)와 `Config.resolved_hash`(환경변수 반영 실행 설정)를 분리해 freeze-pack drift를 감지. 다중 계좌: `kis_api.accounts`, `Config.get_account_no(strategy)`. `Config.dart` 속성. `Config.get()` 싱글톤. |
| **settings.yaml.example** | 설정 예시. 복사해 `settings.yaml`로 사용. (`settings.yaml`은 .gitignore에 포함) |
| **strategies.yaml** | `active_strategy`, `indicators`, `scoring`, `mean_reversion`, `trend_following`, **`fundamental_factor`**, `momentum_factor`, `volatility_condition`, **`ensemble`**(`components`로 technical·momentum_factor·volatility_condition·fundamental_factor on/off·가중치, `mode`, `auto_downgrade`, `independence_threshold`). |
| **risk_params.yaml** | **backtest_universe**(mode: current/historical/kospi200, exclude_administrative), **liquidity_filter**(20일 평균 거래대금 하한, strict, check_on_entry), position_sizing(1% 룰, initial_capital), stop_loss, take_profit(부분 익절), trailing_stop, diversification(max_sector_ratio 포함), position_limits, drawdown, performance_degradation, paper_backtest_compare(live_readiness), transaction_costs(commission, tax, slippage, dynamic_slippage). |
| **baskets.yaml** | 바스켓 포트폴리오 & 리밸런싱 설정. 바스켓별 종목·목표 비중·리밸런싱 트리거(drift/weekly/monthly)·신호 가중 모드 정의. `--mode rebalance`에서 사용. |
| **holidays.yaml** | 한국 휴장일. `python main.py --update-holidays`로 pykrx+fallback 자동 갱신. |
| **holidays.yaml.example** | 한국 휴장일 예시. |
| **us_holidays.yaml** | 미국 휴장일(선택). 없으면 미국 거래일 판별 시 주말만 제외. |

### 3.3 core/

| 파일 | 역할 |
|------|------|
| **data_collector.py** | **`fetch_stock`**: 미국 티커(`is_us_ticker`)는 yfinance(수정주가). 한국: FDR → yfinance → KIS 폴백. **소스 추적**: `_last_source`, `_source_history`, `check_source_consistency()`. `allow_kis_fallback: false`로 KIS 일봉 폴백 차단 가능. `fetch_korean_stock`은 한국 전용 내부/레거시 호출에 사용. `get_krx_stock_list`, **`get_sector_map()`**. |
| **watchlist_manager.py** | manual / top_market_cap / kospi200 / momentum_top / low_vol_top / momentum_lowvol. **유동성 필터**(20일 거래대금 하한, strict 모드: 데이터 없는 종목도 제외). **리밸런싱 캐시**: 팩터 모드는 `rebalance_interval_days`(기본 20)마다 재계산, 사이에는 `data/watchlist_cache.json` 사용. **as_of_date** 지원: 백테스트 시 과거 시점 유니버스 사용 가능. |
| **indicator_engine.py** | pandas-ta로 RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV, volume_ratio. `calculate_all(df)`로 지표 컬럼 추가. |
| **signal_generator.py** | `strategies.yaml` 스코어링 가중치로 점수 합산 → BUY/SELL/HOLD. `generate(df)`, `get_latest_signal(df)`. **`collinearity_mode`**: `max_per_direction`(방향별 최대 1개) 또는 `representative_only`(3그룹 대표 1개씩=MACD+볼린저+거래량만 사용, 권장). 초기화 시 가격 모멘텀 그룹 다중공선성 경고 자동 출력. |
| **risk_manager.py** | 포지션 사이징(1% 룰), `check_diversification`(**업종 비중 포함**: `max_sector_ratio`, FDR Sector), `check_recent_performance`, 손절/익절/트레일링, MDD 한도. `calculate_transaction_costs`. |
| **order_executor.py** | `trading.mode`: paper면 DB만, live면 KIS API. 거래 시간·블랙스완 쿨다운·**실적 발표일 필터**(`skip_earnings_days`) 검사, 재시도(지수 백오프+지터). PositionLock, OrderGuard·KIS 미체결 조회. **Dead-letter 큐**: 모든 재시도 실패 시 `FailedOrder` 테이블에 영구 저장. |
| **portfolio_manager.py** | 보유 포지션·잔고·수익률. `sync_with_broker()`로 KIS 잔고↔DB 크로스체크. `get_portfolio_summary()`. |
| **basket_rebalancer.py** | 바스켓 리밸런싱 엔진. `baskets.yaml`에서 바스켓 로드. `get_target_weights()`(신호 가중 지원), `get_current_weights()`, `calculate_drift()`, `should_rebalance()`(drift/weekly/monthly 트리거), `plan_rebalance()`(SELL→BUY 순서, max_turnover 제한), `execute()`(dry_run 지원). `get_status_report()`로 현황 리포트 생성. |
| **scheduler.py** | 실전 무한 루프. 장전/장중/장마감. **시장 국면 필터**(단계적: bearish→매수 중단, caution→사이징 축소). 장중 10분 간격. 루프 10분 초과 시 다음 사이클 스킵. 장전 단계에서 **바스켓 리밸런싱 자동 체크** (`_run_basket_rebalance_check`). **전략 레지스트리** 기반 `_get_strategy()`. |
| **runtime_lock.py** | `scheduler_lock(lock_file)` 컨텍스트: 스케줄 프로세스 중복 실행 방지. |
| **trading_hours.py** | 한국 장·휴장일(holidays.yaml → pykrx → fallback). **미국**: `us_holidays.yaml`, 동부 09:30~16:00 (`is_us_trading_day`, `is_us_market_open` 등). 주문 가능 시간 검사. |
| **holidays_updater.py** | pykrx(또는 fallback)로 휴장일 조회 → `config/holidays.yaml` 저장. `update_holidays_yaml()`. |
| **blackswan_detector.py** | 급락 감지 시 전량 매도·디스코드 경고·쿨다운. **쿨다운 해제 시** 즉시 재스캔 트리거 + recovery 기간(기본 120분) 중 사이징 50% 축소. `blackswan_recovery_minutes`, `blackswan_recovery_scale`. |
| **market_regime.py** | `check_market_regime()` → 3중 신호 단계적 국면 판별. **신호 A**: 200일선 이탈, **신호 B**: 20일 수익률 ≤ -5%, **신호 C**: MA(20)<MA(60) 데드크로스(선택적). 2개↑ 충족 → bearish(매수 중단), 1개 → caution(사이징 50%), 0 → bullish. 신호 C는 200일선 이탈보다 2~3주 빠르게 추세 전환 포착. `market_regime_ma_cross_enabled: false`면 기존 2-신호 로직과 동일. |
| **fundamental_loader.py** | `get_fundamentals(symbol)`, `check_fundamental_filter()`. **pykrx(우선) → yfinance(폴백)** 순서로 PER·부채비율 조회. pykrx는 한국 종목 PER 정확도 높음. yfinance는 부채비율 등 보충. |
| **dart_loader.py** | `DartEarningsLoader`: DART API로 corp_code 매핑·정기공시 기반 차기 실적 시점 추정. `data/dart_corpCode.zip` 캐시. |
| **earnings_filter.py** | `is_near_earnings(symbol, skip_days, config=...)`. **1순위** yfinance `earningsDate`, **2순위** DART(`settings.dart.enabled`·API 키). 둘 다 없으면 통과. `trading.skip_earnings_days`(기본 3). |
| **indicator_correlation.py** | 스코어링 지표 점수 시리즈 상관계수·고상관 쌍 권고. `--mode check_correlation` 시 사용. 다중공선성 안내: 3그룹 각 대표 1개만 권장. `suggest_disable_weights()`: 고상관 쌍에서 자동 비활성화 키 추출. 리포트 하단에 다음 단계 CLI 명령어 자동 출력. |
| **ensemble_correlation.py** | 앙상블 전략 **신호** 시리즈 상관계수 + **BUY/SELL 동시 발생률** + 구체적 **대안 전략 권고**. `quick_independence_check()`: 런타임 경량 검사. `should_force_conservative()`: 고상관 시 conservative 전환 판단. |
| **position_lock.py** | 포지션/주문 공유 자원용 `threading.RLock`. |
| **order_guard.py** | 동일 종목에 대해 최근 주문 접수 후 TTL(기본 600초) 동안 추가 주문 차단. |
| **strategy_ensemble.py** | `strategies.yaml` → `ensemble.components`에 정의된 구성(기본: technical·momentum_factor·volatility_condition·**fundamental_factor** 등) 신호 통합. majority_vote / weighted_sum / conservative. **auto_downgrade**. 설계서 §4.4. |
| **data_validator.py** | OHLCV Null·NaN·음수 주가·거래량·타임스탬프 역전 등 검사. |
| **notifier.py** | 통합 알림 이중화. 1차 디스코드 → 2차 텔레그램 Bot API → 3차 이메일(SMTP). `critical=True` 시 모든 채널 동시 발송. `Scheduler`, `CircuitBreaker`, `main.py` 등 주요 모듈이 `DiscordBot` 대신 `Notifier` 사용. 알림 실패 5회 누적 시 점검 경고. |
| **strategy_diagnostics.py** | `DiagnosticLine` — 전략별 신호·점수 진단 라인 생성. 스케줄러·대시보드에서 전략 실행 현황 요약 시 사용. |
| **paper_evidence.py** | Paper Evidence 런타임 수집. `DailyEvidence` 데이터클래스, `collect_daily_evidence()`, `finalize_daily_evidence()`, `generate_promotion_package()`, 3종 benchmark excess (same_universe/exposure_matched/cash_adjusted), 6 anomaly rule (repeated_reject, phantom_position, stale_pending, duplicate_flood, reconcile, deep_drawdown), cash-only carry-forward (zero-return semantics). |
| **paper_runtime.py** | Paper Runtime State Machine. 5개 상태 (research_disabled/normal/degraded/frozen/blocked_insufficient_evidence), schema quarantine (legacy record 제외), allowed_actions (모든 상태에서 exit/cancel/reconcile/finalize/evidence/reporting 허용). `get_paper_runtime_state()`, `filter_runtime_eligible()`. |
| **paper_pilot.py** | Paper Pilot Authorization. `PilotAuthorization` 데이터클래스, `enable_pilot()`, `get_active_pilot()`, `check_pilot_prerequisites()`, `compute_launch_readiness()`, `generate_launch_readiness_artifact()`. launch readiness: clean_final_days ≥ 3 + evidence_fresh + benchmark_final_ratio ≥ 40% + notifier_ready. |
| **paper_preflight.py** | Paper 세션 전 운영 준비 상태 점검. runtime state, allowed_actions, evidence freshness, notifier health 등 확인. |
| **strategy_universe.py** | Paper 대상 전략 canonical 목록. 전략별 paper eligibility, 승격 상태, 활성화 여부 관리. |
| **target_weight_rotation.py** | Target-weight 후보의 portfolio-level plan 생성. 직전 거래일 점수, KS11 risk overlay, 목표비중 수량 산출, pilot cap 검증을 담당. 일반 `generate_signal()` 전략으로 위장하지 않는다. 전용 pilot 실행은 주문 실패 시 후속 주문을 중단한다. |
| **evidence_collector.py** | 일일 실적 증거 자동 누적. scheduler 장마감 후 호출. `collect_daily_evidence()` wrapper. |
| **promotion_engine.py** | metrics 기반 전략 승격 판정. `research_only → paper_only → provisional_paper_candidate → live_candidate`. debiased WF + PF + Sharpe + EV/turnover + paper evidence 기준. `tools/evaluate_and_promote.py --canonical`으로 실행하며, 현재 canonical bundle에는 `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35` canonicalized research candidate도 포함. |

### 3.4 strategies/

| 파일 | 역할 |
|------|------|
| **\_\_init\_\_.py** | 전략 레지스트리. 등록명: **`scoring`**, **`mean_reversion`**, **`trend_following`**, **`trend_pullback`**, **`breakout_volume`**, **`relative_strength_rotation`**, **`fundamental_factor`**, **`momentum_factor`**, **`ensemble`**. `volatility_condition`은 레지스트리에 없음(앙상블 내부 전용). `create_strategy`, `get_strategy_names`, `register_strategy`. |
| **base_strategy.py** | `analyze(df)` → 지표·신호 붙은 DataFrame, `generate_signal(df, **kwargs)` → 최신 BUY/SELL/HOLD·점수·상세. |
| **scoring_strategy.py** | IndicatorEngine + SignalGenerator. 총점 ≥ buy_threshold 매수, ≤ sell_threshold 매도. |
| **mean_reversion.py** | Z-Score·ADX 필터. **52주 이중 필터**, **`restrict_to_kospi200`**, **펀더멘털 필터**(pykrx→yfinance). 설계서 §4.2. |
| **trend_following.py** | ADX·200일선·MACD·ATR 추세 추종. 손익비 ≥ 2.0 검증 필수(§4.3). |
| **trend_pullback.py** | C-3A: SMA60 상승추세 + RSI 눌림목 + ADX 추세 확인. edge-trigger 진입. 설계서 §4. |
| **breakout_volume.py** | C-4: 전고점 돌파 + 거래량 급증 + ADX 추세 확인. edge-trigger 진입. ATR 2.5 trailing stop 위임. frozen params(period=10, surge=1.5, adx=20). 설계서 §4.4b. |
| **relative_strength_rotation.py** | C-5: 60d+120d 복합 모멘텀 상위 종목 월간 회전 보유. SMA60 추세 필터. max_positions=2. TS OFF(disable_trailing_stop), TP 7%(per-strategy override). KS11 SMA200 시장 필터·abs momentum 필터 코드 포함(비활성). 설계서 §4.4c. |
| **fundamental_factor.py** | 재무(PER 상대·ROE·부채·영업이익 성장 등)만으로 신호. pykrx→yfinance. 백테스트 시 `df.attrs['symbol']` 권장. |
| **momentum_factor.py** | N일 수익률만. **CLI 등록 완료** — `--strategy momentum_factor`로 단독 사용 가능 + 앙상블 구성용. |
| **volatility_condition.py** | N일 실현변동성만. **앙상블 구성용** — 단독 CLI 전략 아님. |

### 3.5 api/

| 파일 | 역할 |
|------|------|
| **kis_api.py** | OAuth 토큰 발급·갱신, 시세·주문·잔고·일봉 조회. **이중 Rate Limiter**: Token Bucket(초당) + 슬라이딩 윈도우(분당). **지수 백오프+지터**: `_backoff_with_jitter()`로 thundering-herd 방지. **SSL/커넥션 에러 전용 핸들러**: ConnectionError, SSLError 등 별도 처리 + `_total_conn_errors` 추적. **토큰 쿨다운**: 인증 실패 시 60초간 요청 차단(`_token_error_until`). `get_rate_limit_stats()`: 사용량 모니터링(최근 60초 활용률, 429 누적, 커넥션 에러 누적, 쿨다운 상태). CircuitBreaker 연동. |
| **websocket_handler.py** | KIS 웹소켓 실시간 체결/호가. asyncio 기반, Heartbeat 45초 타임아웃, 자동 재연결, 콜백으로 가격 전달. |
| **circuit_breaker.py** | API 연속 5회 실패 시 CLOSED → OPEN(60초 차단) → HALF_OPEN. 요청 차단으로 계정 제재 방지. Notifier 알림. |

### 3.6 backtest/

| 파일 | 역할 |
|------|------|
| **backtester.py** | OHLCV + 전략 시뮬레이션. 수수료·세금·슬리피지·1% 룰·손절/익절/트레일링 스탑. **strict_lookahead 기본 True**. 성과 지표 + **과매매 분석**(평균 보유 기간, 총 수수료). |
| **strategy_validator.py** | 최소 3~5년, 샤프·MDD·벤치마크(KS11 + **코스피 상위 50 동일비중**). in/out-of-sample. `run()`, `run_walk_forward()`. `--no-benchmark-top50` 으로 Top50 비활성화. **손익비 자동 경고**: 추세 추종 < 2.0, 기타 < 1.0 시 WARN + 디스코드 알림. |
| **report_generator.py** | txt·html 리포트. 거래 내역, 성과 지표, 자본 곡선, **과매매 분석**(평균 보유 기간, 총 수수료). `--output-dir`. |
| **paper_compare.py** | 지정 기간 paper 성과 vs 동일 기간·전략 백테스트. divergence 시 경고·디스코드(설정 시). **`check_live_readiness()`**: 방향성 일치율 ≥70%, 수익률 차이 ≤5%, 최소 거래일·거래건 충족 시 "실전 전환 준비 완료" 신호 + 디스코드 알림. paper 모드 장마감 시 자동 평가. |
| **momentum_top_portfolio.py** | 다종목 동일비중 모멘텀 포트폴리오 백테스트. `run_momentum_top_portfolio_backtest()`: WatchlistManager(momentum_top) → 종목별 데이터 수집 → 리밸런싱 주기(기본 20일)마다 포트폴리오 재구성 → 시장 국면 필터·포트폴리오 스탑 적용. `print_momentum_top_portfolio_report()`. `--mode backtest_momentum_top`에서 사용. |
| **param_optimizer.py** | Grid Search / Bayesian(scikit-optimize). train_ratio·OOS 보고. `--include-weights` 시 **스코어링 가중치 대칭 Grid Search + OOS 샤프≥1.0 게이트**. `--auto-correlation`: 최적화 전 상관 분석 자동 실행, 고상관 지표 자동 비활성화. `--disable-weights w_rsi,w_ma` 등으로 수동 지정도 가능. `Backtester.run(..., param_overrides=)`. |
| **tools/research_candidate_sweep.py** | promotion/live artifact와 분리된 research-only 후보 공장. `--candidate-family rotation|momentum|breakout|pullback|benchmark_relative|risk_budget|cash_switch|benchmark_aware_rotation|target_weight_rotation|all` 후보를 포트폴리오 단위로 평가하고 raw EW B&H benchmark excess, exposure-matched B&H diagnostic, EV, CAGR, turnover, WF 안정성으로 랭킹/진단하며 decision action을 포함해 `reports/research_sweeps/`에 저장. target-weight 후보는 `min_score_floor_pct` score-floor, `hold_rank_buffer` rank-hysteresis, `market_exposure_mode=benchmark_risk` SMA/낙폭/변동성 risk-off 노출 축소를 지원. |

### 3.7 database/

| 파일 | 역할 |
|------|------|
| **models.py** | ORM 모델 6종 (StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport, **FailedOrder**). `init_database()` 시 WAL 활성화 **검증** — WAL이 아니면 ERROR 로그. **scoped_session**: 스레드별 세션 격리. **`@with_retry`**: DB locked 시 3회 지수 백오프 재시도. **`db_session()`**: 컨텍스트 매니저(commit/rollback/close 자동). **FailedOrder**: 주문 실패 Dead-letter 큐 (status: pending/retried/cancelled). |
| **repositories.py** | CRUD — **읽기·쓰기 전체 함수**에 `@with_retry` 적용 (WAL 체크포인트 중 일시적 locked에도 안전). `get_paper_performance_metrics(start, end)` (compare 모드). |
| **backup.py** | **SQLite Online Backup API** (`sqlite3.Connection.backup()`)로 WAL 모드에서도 일관된 스냅샷 백업. 실패 시 `-wal`/`-shm` 파일 포함 `shutil.copy2` 폴백. 보관 일수 초과 분 삭제. |

### 3.8 monitoring/

| 파일 | 역할 |
|------|------|
| **logger.py** | loguru 초기화. logging 설정에 따라 파일 로테이션·콘솔 출력. |
| **discord_bot.py** | 디스코드 웹훅 전송 전용. 직접 사용보다는 `Notifier`를 통해 호출 권장 (이중화 보장). |
| **liquidate_trigger.py** | `LIQUIDATE_TRIGGER_TOKEN`·`LIQUIDATE_TRIGGER_PORT` 설정 시 POST /liquidate (X-Token 또는 ?token=)으로 긴급 청산. |
| **dashboard.py** | 콘솔 대시보드(선택). |
| **dashboard_runtime_state.py** | 대시보드 런타임 상태 관리. 스케줄러·전략 실행 현황 등 실시간 상태를 웹 대시보드에 전달하는 중간 계층. |
| **web_dashboard.py** | aiohttp. 포트폴리오 요약·포지션·최근 30일 스냅샷. 10초 폴링. `--mode dashboard` 또는 `python -m monitoring.web_dashboard [--port 8080]`. |
| **paper_monitor.py** | Paper 운영 모니터링 (v2.8 추가). `log_event()`: OperationEvent DB 기록 (SIGNAL/API_FAILURE/BLACKSWAN 등). `WeeklyReportGenerator`: 주간 리포트 JSON/TXT 자동 생성. `GoLiveChecker`: 8개 기준 + 5개 blocker로 live 전환 판정. |

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
| **test_paper_lifecycle.py** | Full paper lifecycle (BUY/SELL/Snapshot, 격리 DB truncate, 4/4 PASS). |
| **test_paper_evidence.py** | Paper Evidence 수집/검증 (51건): JSONL I/O, anomaly detection, benchmark missing, E2E replay 7일, cash-only zero-return deadlock regression, shadow evidence 분리. |
| **test_paper_runtime.py** | Paper Runtime State Machine (45건): 상태 전이, schema quarantine, allowed_actions, auto-unfreeze. |
| **test_paper_pilot.py** | Paper Pilot Authorization: pilot enable/disable, cap enforcement, launch readiness, preflight prerequisites, artifact-only target-weight 후보 eligibility. |
| **test_paper_preflight.py** | Paper Preflight Check: 운영 준비 상태 점검 시나리오. |
| **test_promotion_engine.py** | Promotion 규칙: metrics 기반 자동 판정, threshold 경계. |
| **test_order_state_machine.py** | 주문 상태기계: 9개 상태 전이, assert invariant. |
| **test_order_lifecycle_integration.py** | E2E 주문 흐름 통합 테스트. |
| **test_audit_safety.py** | 안전성 감시: live gate, force-live 제거 검증. |
| **test_positive_path.py** | 성공 경로 (happy path) 검증. |
| **test_watchlist_manager.py** | watchlist 모드별 resolve. |
| **test_basket_rebalancer.py** | 바스켓 리밸런서 (설정 로딩, 비중 계산, 드리프트 감지, 트리거 판단, 주문 계획, dry-run 실행). |
| **test_us_market_support.py** | `fetch_stock` 미국 라우팅, 미국 장/휴장일 관련 `TradingHours` 동작. |

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
| **dart** | enabled, api_key(또는 `DART_API_KEY` 환경변수) | 전자공시 기반 실적 시점 추정 → `earnings_filter` 폴백 |
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
| **fundamental_factor** | per_sector_relative, roe_min, debt_ratio_max, earnings_growth_min, data_cache_hours 등 | 펀더멘털 단독 전략·앙상블 구성 공통 설정 |
| **ensemble** | `components`(name, enabled, weight), mode(majority_vote), auto_downgrade(true), independence_threshold(0.6), confidence_weight(레거시 폴백) | 앙상블 통합 |

### config/risk_params.yaml

| 섹션 | 주요 키 | 설명 |
|------|---------|------|
| **backtest_universe** | mode(historical), exclude_administrative(true) | 백테스트 유니버스·생존자 편향 완화 |
| **position_sizing** | max_risk_per_trade(0.01), initial_capital(10,000,000) | 1% 룰 포지션 사이징 |
| **stop_loss** | type(atr), fixed_rate, atr_multiplier(2.0) | 손절매 |
| **take_profit** | type(fixed), fixed_rate(0.08), partial_exit, partial_ratio(0.5), partial_target(0.04) | 익절매 (부분 익절 포함) |
| **trailing_stop** | enabled, type(fixed), fixed_rate(0.05), atr_multiplier | 트레일링 스탑 |
| **liquidity_filter** | enabled(true), min_avg_trading_value_20d_krw(5e9), strict(true), check_on_entry(true) | 유동성 필터 (20일 평균 거래대금) |
| **diversification** | max_position_ratio(0.20), max_investment_ratio(0.70), max_positions(10), min_cash_ratio, max_sector_ratio(0.40) | 분산 투자·업종 비중 제한 |
| **position_limits** | max_holding_days(30), min_holding_days(3) | 최대/최소 보유 기간 |
| **drawdown** | max_portfolio_mdd(0.15), max_daily_loss(0.03), recovery_scale | MDD 제한 |
| **performance_degradation** | enabled, recent_trades(20), min_win_rate(0.35) | 성과 열화 감지 |
| **paper_backtest_compare** | live_readiness(min_direction_agreement_pct, max_return_diff_pct, min_trading_days, min_trades) | 실전 전환 기준 |
| **transaction_costs** | commission_rate(0.00015), tax_rate(0.0020), slippage(0.0005), dynamic_slippage, capital_gains_tax | 거래 비용 |

### config/baskets.yaml

| 섹션 | 주요 키 | 설명 |
|------|---------|------|
| **baskets.\<name\>** | name, enabled | 바스켓 정의. `enabled: true`인 바스켓만 스케줄러/CLI에서 실행 |
| **rebalance** | trigger(drift/weekly/monthly), drift_threshold(0.05), min_trade_amount, max_turnover_ratio | 리밸런싱 트리거·제약 조건 |
| **holdings** | symbol: weight | 종목별 목표 비중 (합계 1.0) |
| **signal_weighted** | signal_weighted, signal_strategy, signal_weight_range | 전략 점수로 비중 동적 조정 (선택) |

### config/holidays.yaml

휴장일 목록. `python main.py --update-holidays`로 pykrx+fallback 자동 갱신.

---

## 5. 실행 모드별 데이터 흐름

### 백테스트 (단일 종목)

```
main.py (--mode backtest)
  → DataCollector.fetch_stock(symbol, start, end)   # 미국 티커는 yfinance 분기
  → Backtester.run(df, strategy_name)  # strict_lookahead 기본, 전략.analyze → _simulate
  → Backtester.print_report(result)
  → ReportGenerator.generate_all(result)  # txt, html
```

### 백테스트 (다종목 모멘텀 포트폴리오)

```
main.py (--mode backtest_momentum_top --top-n 20 --rebalance-days 20)
  → momentum_top_portfolio.run_momentum_top_portfolio_backtest()
      → WatchlistManager(momentum_top) → 모멘텀 상위 N종목 선정
      → 종목별 DataCollector.fetch_stock()
      → 리밸런싱 주기(기본 20일)마다 포트폴리오 재구성
      → 시장 국면 필터·포트폴리오 스탑 적용 (옵션)
  → print_momentum_top_portfolio_report()
```

### 모의투자(paper)

```
main.py (--mode paper)
  → WatchlistManager.resolve()
  → 종목마다: DataCollector.fetch_stock(symbol) → strategy.generate_signal(df)
  → BUY/SELL 시 Notifier, OrderExecutor.execute_buy/execute_sell (DB만)
  → PortfolioManager.get_portfolio_summary()
```

### 스케줄 루프(schedule)

```
main.py (--mode schedule --strategy scoring)
  → trading.mode == live 이면 거부
  → runtime_lock(data/.scheduler.lock) 획득 실패 시 종료
  → Scheduler.run()  # 장전/장중/장마감 무한 루프 (paper 설정 하에 동일 비즈니스 로직)
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
  → DataCollector.fetch_stock(symbol, ...)
  → IndicatorEngine.calculate_all(df)
  → SignalGenerator.generate(df)  # 각 지표별 스코어 시리즈 추출
  → indicator_correlation.run_indicator_correlation_check()  # Pearson 상관계수
  → 리포트 저장 (reports/indicator_correlation_*.txt)

main.py (--mode check_ensemble_correlation)
  → DataCollector.fetch_stock(symbol, ...)
  → StrategyEnsemble.analyze(df)  # 3개 전략 신호 시리즈
  → ensemble_correlation.run_ensemble_signal_correlation_check()  # 상관 + BUY 동시 발생률
  → 리포트 저장 + 대안 전략 권고

main.py (--mode optimize --include-weights --auto-correlation)
  → indicator_correlation (자동 실행) → 고상관 지표 비활성화
  → param_optimizer.grid_search_scoring_weights() → 대칭 Grid Search
  → OOS 샤프 ≥ 1.0 게이트 → 통과 시 YAML 스니펫 출력

main.py (--mode rebalance --basket kr_blue_chip --dry-run)
  → BasketRebalancer(basket_name) → baskets.yaml 로드
  → get_target_weights() (signal_weighted 시 전략 점수로 동적 조정)
  → get_current_weights() (PortfolioManager + 현재가 조회)
  → should_rebalance() → drift/weekly/monthly 트리거 판단
  → plan_rebalance() → SELL 주문 먼저 배치 → BUY 주문 (max_turnover 제한)
  → execute(orders, dry_run) → OrderExecutor로 실제 주문 또는 로그만 출력
```

---

## 6. 알고리즘·지표 요약

- **지표**: `core/indicator_engine.py`에서 pandas-ta로 RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV, volume_ratio 계산. 설정은 `config/strategies.yaml` → `indicators`.
- **스코어링**: `core/signal_generator.py`가 가중치(weights)로 점수 합산 → buy_threshold/sell_threshold로 BUY/SELL 판단. **⚠️ 가중치는 미검증 직관값이며, 이 상태로 실전 투입하면 노이즈를 실행하는 것**. `collinearity_mode: representative_only`(권장)로 설정하면 MACD+볼린저+거래량 3개만 합산하여 다중공선성을 근본적으로 차단. 반드시 `check_correlation → optimize --include-weights --auto-correlation → validate --walk-forward` 파이프라인으로 최적화 후 사용. **스코어링 전략 단독으로 안정적 수익을 낼 가능성은 낮음** — 설계서 §4.5.1 참고.
- **전략 (CLI 등록)**: scoring, mean_reversion, trend_following, **trend_pullback**, **breakout_volume**, **relative_strength_rotation**, **fundamental_factor**, **momentum_factor**, **ensemble**. **volatility_condition**은 앙상블 내부 구성용. 앙상블은 `ensemble.components`로 구성·가중치 설정(기본 예시에 fundamental_factor 포함). breakout_volume(C-4)과 relative_strength_rotation(C-5)은 2-sleeve 멀티전략 포트폴리오 구조로 검증 완료 -- BV50/R50 paper 후보 확정(Rotation TS OFF, TP 7%). 각 전략의 시장 비효율성 가정은 설계서 §4 참고.

공식·파라미터·신호 조건 등 **상세는 루트의 `quant_trader_design.md` §3(지표), §4(전략), §5(리스크)** 참고.

---

## 7. 전략 유효성 검증 및 실전 체크리스트

> **중요**: 현재 시스템의 신호 품질이 검증되지 않은 상태입니다. 아래 체크리스트를 모두 통과하기 전까지 실전 투입은 금지입니다. 상세 진단은 `quant_trader_design.md` §1.3 참고.

### 전략 상태 레지스트리 (v5.2 — `core/promotion_engine.py` 자동 판정)

| 전략 | 상태 | 허용 모드 | Ret% | PF | WF P%/Sh+% | Paper Status |
|------|------|-----------|------|-----|-----------|--------------|
| **relative_strength_rotation** | `provisional_paper_candidate` | backtest, paper | +18.09 | 1.62 | 100/83.3 | — |
| **scoring** | `paper_only` | backtest, paper | +11.22 | 1.07 | 83.3/50.0 | risk-adjusted alpha 미달 |
| **breakout_volume** | `disabled` | backtest only | -13.31 | 0.79 | 0/0 | — |
| **mean_reversion** | `disabled` | backtest only | -8.36 | 0.85 | 33.3/0 | — |
| **trend_following** | `disabled` | backtest only | -6.94 | 0.67 | 16.7/0 | — |
| **ensemble** | `disabled` | backtest only | — | — | 0/0 | — |

승격 규칙: `research_only → paper_only → provisional_paper_candidate → live_candidate`  
판정: `python tools/evaluate_and_promote.py --canonical` → artifact → engine → registry CI 검증

### Live 진입 Hard Gate (우회 불가 — `--force-live` 제거됨)

1. `strategies/__init__.py:is_strategy_allowed(strategy, "live")` — live_candidate만 허용
2. 환경변수 `ENABLE_LIVE_TRADING=true`
3. CLI 플래그 `--confirm-live`
4. `main.py:_check_live_readiness_gate()` → `core/live_gate.py:validate_live_readiness()`
5. `reports/promotion/` canonical bundle이 현재 git commit, `Config.yaml_hash`, `Config.resolved_hash`와 일치해야 함
6. `promotion_result.json`의 해당 전략 status가 `live_candidate`이고 `allowed_modes`에 `live`가 있어야 함
7. `benchmark_comparison.json`에 전략별 양의 excess return과 excess Sharpe가 있어야 함
8. `reports/paper_evidence/promotion_evidence_{strategy}.json` recommendation이 `ELIGIBLE`이어야 하며, 60영업일 execution-backed evidence, benchmark_final_ratio 80% 이상, 양의 excess/cumulative return, 최소 sell 5건, win_rate 45% 이상, frozen day 0을 만족해야 함
9. canonical/evidence gate 통과 후 데이터 소스 health check를 수행

레거시 `reports/approved_strategies.json`와 오래된 `validation_walkforward_*.json`은 live 근거로 사용하지 않는다. 이 파일들이 남아 있어도 canonical bundle과 paper evidence가 현재 코드·설정과 맞지 않으면 live는 차단된다.

### Paper 모드 2가지

| 모드 | 설정 | 동작 |
|------|------|------|
| **signal-only** (기본) | YAML `trading.auto_entry=false`, `QUANT_AUTO_ENTRY` 미설정 | 신호 분석·evidence/finalize만, 신규 주문 없음 |
| **full paper** | `QUANT_AUTO_ENTRY=true` 환경변수 | BUY/SELL 자동 실행 (paper DB만 기록). YAML 원본은 유지하고 resolved hash만 달라짐 |

60영업일 실험은 `reports/experiment_freeze_pack.md`와 `reports/paper_experiment_manifest.json`을 기준으로 동결한다. 실행 전 `Config.yaml_hash`, `Config.resolved_hash`, `Config.auto_entry_source`를 확인해 파일 변경과 환경변수 변경을 분리해서 기록한다.

### 현재 시스템의 핵심 한계

| 한계 | 설명 |
|------|------|
| **가중치 미검증** | 스코어링 가중치가 직관·예시용. 통계적 근거 없음 |
| **다중공선성** | RSI, MACD, MA 등이 같은 가격 정보를 중복 반영 |
| **앙상블 독립성 부족** | technical과 momentum_factor가 실질적으로 같은 정보 사용 |
| **한국 시장 미최적화** | 파라미터가 미국 시장 기준값 (200일선, ADX 25 등) |
| **과매매 방어** | ✅ 히스터리시스·최소 보유 기간 5일·월간 왕복 8회 제한 구현 완료 |
| **운영 자동화** | ✅ 헬스체크·포지션 보정·OperationEvent·주간 리포트·GoLive 체크 모두 구현 완료 |

### 검증 요구 사항

| 항목 | 요구 |
|------|------|
| **데이터 기간** | 최소 3~5년. `--validation-years` 기본 5. |
| **샤프 비율** | OOS 기준 1.0 이상. `--min-sharpe` 변경 가능. |
| **벤치마크** | 코스피(KS11) + 코스피 상위 50종목 동일비중 대비 OOS 초과 수익 검증. `--no-benchmark-top50` 으로 Top50 비활성화. |
| **오버피팅** | in/out-of-sample 분리. `--split-ratio` 기본 0.7. |
| **생존자 편향** | `backtest_universe.mode: historical` 필수. `current` 사용 시 수익률 과대평가 |
| **과매매 점검** | 백테스트 리포트의 평균 보유 기간·총 수수료·연간 왕복 수 확인. 종목당 월 5회 초과 왕복 시 경고 |

**검증 한계**: 통과해도 실전 수익 보장 없음(국면 편향·OOS 과적합 가능). 여러 시장 국면(상승·하락·횡보)이 포함된 기간으로 검증 필수. **워크포워드**: `--mode validate --walk-forward` 로 슬라이딩 윈도우 반복 검증 가능 (train 2년→test 1년, 1년 스텝). `quant_trader_design.md` §8.2.

### 실전 투입 전 체크리스트 (필수 — 순서대로)

| # | 항목 | 설명 | 완료 |
|---|------|------|------|
| 1 | **백테스트 유니버스** | `backtest_universe.mode: historical` 설정 확인 후 백테스트 재실행 | [ ] |
| 2 | **데이터 소스 고정** | `data_source.preferred: fdr`, `allow_kis_fallback: false` 설정 | [ ] |
| 3 | **지표 독립성 검증** | `--mode check_correlation` 실행. 고상관(|r| ≥ 0.7) 쌍 제거/비활성화 | [ ] |
| 4 | **가중치 최적화** | `--mode optimize --include-weights --auto-correlation` 실행. OOS 샤프 ≥ 1.0 통과 | [ ] |
| 5 | **워크포워드 검증** | `--mode validate --walk-forward` 실행. 80% 이상 창 통과 | [ ] |
| 6 | **손익비 확인** | 추세 추종 전략: profit factor ≥ 2.0. 기타: ≥ 1.0 | [ ] |
| 7 | **Look-Ahead** | strict-lookahead 기본 유지. `--allow-lookahead` 미사용 | [ ] |
| 8 | **paper 모드 1개월** | 실시세 paper 운영 후 `check_live_readiness` 통과 (방향성 일치 ≥ 70%, 수익률 차이 ≤ 5%p) | [ ] |
| 9 | **KIS E2E 테스트** | 모의투자 환경에서 API·주문·잔고 전 과정 테스트 | [ ] |
| 10 | **첫 실전 규모** | 운용 예정 금액의 **10% 이하**로 시작 | [ ] |

### 실전 10분 루프 안전장치

| 안전장치 | 설명 |
|----------|------|
| **주문 전 미체결** | OrderGuard(TTL 600초) + KIS 미체결 조회. 미체결 있으면 주문 보류. |
| **루프 10분 초과** | 한 사이클 실행이 10분 초과 시 다음 사이클 1회 스킵. **종목 50개 이상 시 데이터 수집만으로 10분 가능 — 모니터링 필요** |

### 운영 안정성 — 구현 완료

| 항목 | 설명 |
|------|------|
| ✅ 신호 히스터리시스 | BUY↔HOLD↔SELL 순차 전환 강제, 직접 전환 차단 (§5.13) |
| ✅ 최소 보유 기간 | 매수 후 3일 미만 매도 차단, 손절·블랙스완 예외 (§5.14) |
| ✅ 포지션 불일치 자동 보정 | KIS↔DB 크로스체크 → KIS 기준 DB 자동 동기화 (§9.1) |
| ✅ 시스템 헬스체크 | 10분 주기 DB·API·디스크·메모리 자동 점검 (§9.1) |
| ✅ 휴장일 자동 갱신 | 90일 경과 또는 연초 자동 호출 (§9.1) |
| ✅ 10분 루프 모니터링 | LoopMetrics 추적, 연속 스킵 경고, 장마감 리포트 포함 |
| ✅ KIS 호출 제어 강화 | 지수 백오프+지터, SSL/커넥션 에러 핸들러, 토큰 쿨다운 60초 |
| ✅ 주문 실패 Dead-letter 큐 | FailedOrder 테이블에 영구 저장, 재처리 API 지원 |
| ✅ 전략 레지스트리(플러그인형) | `create_strategy(name)`으로 동적 로딩 |
| ✅ 바스켓 리밸런싱 | 목표 비중 관리, 드리프트/주기 트리거, 신호 가중, CLI+스케줄러 통합 |
| ✅ schedule 모드 + 런타임 락 | 모의 무한 루프, `data/.scheduler.lock` 중복 방지 |
| ✅ 미국 티커·휴장일 | `fetch_stock`, `us_holidays.yaml`, NYSE 장세션 헬퍼 |
| ✅ DART(선택) | `DART_API_KEY` / `settings.dart` 시 실적일 폴백 |
| ✅ momentum_factor CLI 등록 | `--strategy momentum_factor`로 단독 사용 가능 (앙상블 구성도 유지) |
| ✅ 다종목 모멘텀 포트폴리오 백테스트 | `--mode backtest_momentum_top` — 리밸런싱·시장 국면·포트폴리오 스탑 |
| ✅ 전략 진단 보조 | `strategy_diagnostics.py` DiagnosticLine — 전략별 신호·점수 진단 |
| ✅ 대시보드 런타임 상태 | `dashboard_runtime_state.py` — 스케줄러·전략 실행 현황 실시간 전달 |
| ✅ C-4 breakout_volume | 전고점 돌파+거래량 급증 전략. frozen params, 4종목 OOS 통과 |
| ✅ C-5 relative_strength_rotation | 월간 상대강도 회전 전략. TS OFF + TP 7% 최적화, BV50/R50 paper 후보 확정 |
| ✅ 멀티전략 sleeve 비교 | `c5_sleeve_backtest.py`, `c5_weight_sweep.py` — 독립 sleeve 결합 검증 인프라 |
| ✅ Rotation exit 최적화 | trailing stop 제거(capture rate 71%->79%), TP 8%->7%(per-strategy override) |
| ✅ Entry filter 탐색 | KS11 SMA200, abs momentum, min_hold_days 테스트 — 모두 불채택 |
| ✅ Rolling walk-forward | 10 windows x 12mo, 6mo step. BV50/R50 positive 60%, median +0.45% |
| ✅ Paper 모니터링 | `c5_paper_monthly_report.py`, signal/executed/skipped 카운터, guardrail 설정 |
| ✅ **Paper 실험 freeze pack** | scoring 2026-03-27~2026-06-19 60영업일 관측 기준 동결. BV50/R50 paper 운영 산출물은 legacy/비교 자료로 보존 |
| ✅ **주문 상태기계** | `core/order_state.py` — OrderStatus 9개 상태, FILLED 전 position 없음 invariant |
| ✅ **승격 규칙 v3** | `core/promotion_engine.py` — metrics 기반 자동 판정 + artifact-driven |
| ✅ **`--force-live` 제거** | canonical bundle + paper evidence hard gate 우회 불가 |
| ✅ **벤치마크 거래비용** | `_buy_and_hold_metrics`에 commission/tax/slippage 반영 |
| ✅ **debiased 전략 재평가** | 거래대금 기반 ex-ante proxy 20종목, portfolio WF 6 windows |
| ✅ **테스트 298건 green** | live/paper/promotion/research sweep 회귀 묶음 기준 |
| ✅ **Paper Runtime State Machine** | `core/paper_runtime.py` — 5개 상태(normal/degraded/frozen/blocked/research_disabled), schema quarantine |
| ✅ **Paper Pilot Authorization** | `core/paper_pilot.py` — launch readiness + pilot auth + 리스크 캡 |
| ✅ **Paper Preflight** | `core/paper_preflight.py` — 세션 전 운영 준비 상태 점검 |
| ✅ **Strategy Universe** | `core/strategy_universe.py` — paper 대상 전략 canonical 목록 |
| ✅ **Paper 운영 도구** | `tools/` — evidence pipeline, pilot control, bootstrap, preflight, launch readiness CLI |
| ✅ **Research candidate sweep** | `tools/research_candidate_sweep.py` — rotation/momentum/breakout/pullback/benchmark-relative/risk-budget/cash-switch/benchmark-aware rotation/target-weight top-N rotation 후보군을 benchmark-aware artifact로 랭킹하고 decision action 생성. raw EW B&H gate는 유지하면서 exposure-matched B&H 진단값도 기록. promotion/live gate와 분리 |
| ✅ **2026-04-29 all-family quick sweep** | 5종목, 후보 14개 비교 결과 `NO_ALPHA_CANDIDATE`. best=`rotation_slow_momentum`이나 excess=-165.22%p / excess Sharpe=-1.07 |
| ✅ **2026-04-30 top-20 all-family quick sweep** | canonical liquidity universe 20종목, 후보 14개 비교 결과 `NO_ALPHA_CANDIDATE`. best=`momentum_factor_120d`, return=+118.56%, excess=-30.83%p, MDD=-40.08% |
| ✅ **pullback 후보군 추가** | `trend_pullback` 기반 research-only 후보 4개 추가. 외부 재무 데이터 없이 SMA/RSI/ADX 눌림목 진입을 benchmark-aware sweep에서 검증 |
| ✅ **benchmark-relative momentum 추가** | `momentum_factor`에 KS11 대비 초과 모멘텀/변동성 게이트 옵션 추가. research-only 후보 3개로 현재 실패 원인(benchmark underperformance)을 직접 검증 |
| ✅ **2026-04-30 신규 후보 smoke sweep** | 5종목 기준 `benchmark_relative`/`pullback` 모두 `NO_ALPHA_CANDIDATE`. best 신규 후보도 excess=-169%p 이하라 promotion 미진행 |
| ✅ **risk-budget 후보군 추가** | `CandidateSpec.diversification`을 artifact에 기록하고 momentum/rotation 신호를 집중형·균형형·방어형 exposure budget으로 비교 |
| ✅ **2026-04-30 risk-budget smoke sweep** | 5종목 기준 `NO_ALPHA_CANDIDATE`. 방어형 rotation은 MDD=-6.41%로 개선됐지만 excess=-162.72%p라 promotion 미진행 |
| ✅ **cash-switch 후보군 추가** | `relative_strength_rotation.market_filter_exit`로 KS11 이동평균 하회 시 보유 포지션을 현금화하는 research-only 후보 3개 추가 |
| ✅ **2026-04-30 cash-switch smoke sweep** | 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`cash_switch_rotation_slow_defensive`, return=+1.87%, excess=-171.76%p, MDD=-11.78%라 promotion 미진행 |
| ✅ **exposure-matched benchmark 진단 추가** | 후보별 `avg_exposure_pct`, `avg_cash_pct`, `exposure_matched_bh_return/sharpe/mdd`, `exposure_matched_excess_return/sharpe` 기록. cash-switch 평균 노출 8.4~10.0%, exposure-matched excess=-7.87%p~-0.36%p로 신호 edge도 미확인 |
| ✅ **benchmark-aware rotation 후보군 추가** | `relative_strength_rotation.score_mode=benchmark_excess`, `rank_entry_mode=dense_ranked`, `exit_rebalance_mode=score_floor`를 추가해 KS11 대비 상대강도 랭킹과 노출 유지형 회전을 research-only로 검증 |
| ✅ **benchmark-aware rotation smoke sweep** | 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`benchmark_aware_rotation_60_120_balanced`, return=+21.65%, Sharpe=0.50, avg exposure=24.1%였지만 raw excess=-151.98%p라 promotion 미진행. fast 40/100은 exposure-matched excess=+2.04%p로 다음 연구 힌트만 제공 |
| ✅ **target-weight top-N rotation 백테스터 추가** | sparse BUY/SELL 신호 대신 매월 직전 거래일 기준 top-N을 목표비중으로 보유/교체하는 research-only 경로 추가. delta 리밸런싱, 거래비용, 일별 cash/value/n_positions 노출 진단 기록 |
| ✅ **target-weight top-N rotation smoke sweep** | 5종목 기준 `NO_ALPHA_CANDIDATE`. best=`target_weight_rotation_top3_40_100_excess`, return=+128.44%, Sharpe=1.13, avg exposure=85.3%로 노출은 개선됐지만 raw excess=-45.19%p라 promotion 미진행 |
| ✅ **canonical top-20 target-weight full sweep** | 20종목 기준 alpha 후보 확인. best 기존 후보=`target_weight_rotation_top3_40_100_excess`, return=+212.21%, raw excess=+62.82%p, exposure-matched excess=+83.66%p. 다만 promotion=`paper_only`, turnover/year=1412.1%라 `KEEP_RESEARCH_ONLY` |
| ✅ **target-weight score-floor 후보 추가** | `min_score_floor_pct`로 약한 KS11 초과 모멘텀 슬롯을 현금으로 남기는 후보 3개 추가. best=`target_weight_rotation_top5_60_120_floor0`, return=+210.21%, Sharpe=1.41, WF positive=100%, raw excess=+60.82%p였지만 turnover/year=1081.5%라 승격 금지 |
| ✅ **target-weight rank-hysteresis 후보 추가** | `hold_rank_buffer`로 기존 보유 종목이 top-N 밖으로 소폭 밀려도 버퍼 안이면 유지. best=`target_weight_rotation_top5_60_120_floor0_hold3`, return=+278.57%, raw excess=+129.18%p, Sharpe=1.65, WF positive/Sh+ 100%, turnover/year=807.8%. turnover 병목은 해소했지만 MDD=-28.25%라 research-only |
| ✅ **target-weight benchmark-risk overlay 후보 추가** | KS11 SMA/낙폭/변동성 risk-off 구간에 부분 노출을 줄이는 후보 6개 추가. best=`target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`, return=+210.24%, raw excess=+60.85%p, exposure-matched excess=+130.96%p, Sharpe=1.60, PF=5.73, MDD=-19.24%, turnover/year=858.0%, WF positive/Sh+ 100%로 research sweep 기준 `provisional_paper_candidate` 도달 |
| ✅ **target-weight canonical bridge 추가** | `tools/evaluate_and_promote.py --canonical`이 `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`를 동일 후보 ID/params hash로 재평가하고 `reports/promotion/*` canonical bundle에 기록. `promotion_result.json`에서 `provisional_paper_candidate` 확인 |
| ✅ **target-weight paper/pilot adapter 추가** | `core/target_weight_rotation.py` + `tools/target_weight_rotation_pilot.py`로 직전 거래일 점수 기반 목표비중 plan을 만들고 pilot cap을 plan-level로 검증. `OrderExecutor.execute_buy_quantity()`로 paper-only exact quantity 매수를 지원. live 모드는 계속 거부 |
| ✅ **Zero-return Semantics** | cash-only/no-position day deadlock 해소 — daily_return=0.0 추론 |
| ✅ **scoring paper_only 강등** | Sharpe/PF/WF 안정성 미달. 관찰은 가능하지만 우선 pilot 후보 아님 |

### 다음 연구 방향 — 2026-04-30 기준

| 항목 | 결정 |
|------|------|
| 즉시 canonical promotion | 완료. `target_weight_rotation_top5_60_120_floor0_hold3_risk60_35`가 canonical promotion bundle에서도 `provisional_paper_candidate`로 재현됨 |
| 현재 후보군 | rotation은 등록 전략 기준 provisional, target-weight risk overlay 후보는 canonical artifact 기준 provisional이며 전용 paper/pilot adapter가 준비됨. 일반 scheduler registry에는 아직 넣지 않음 |
| 다음 후보 탐색 | 새 알파 탐색보다 target-weight pilot을 shadow/dry-run → capped paper 순서로 돌려 execution-backed evidence 품질을 검증 |
| 운영 원칙 | research artifact만으로 paper/live 전환 금지. canonical promotion + paper evidence + live gate 필요 |

### 운영 안정성 — 미구현 (중기 개선)

| 항목 | 설명 | 우선순위 |
|------|------|----------|
| DART·어닝 필터 고도화 | 기본 연동 완료(`dart_loader`+`earnings_filter`). 공시 키워드·커버리지·폴백 정책 확대 | 중기 (3~6개월) |
| 펀더멘털 신호 고도화 | `fundamental_factor`·앙상블 구성 반영됨. 지표·해외 종목·공시 연계 강화 | 중기 |
| 웹 대시보드 강화 | 전략별 신호, 주문 목록, API 사용량 표시 | 중기 |
| WebSocket 갭 처리 | 재연결 시 REST API 보충 조회, 갭 중 급변 감지 | 중기 |

상세는 `quant_trader_design.md` §5.13, §5.14, §9.1, §10 참고.

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
| `DART_API_KEY` | 선택 | 전자공시 API — 실적일 DART 폴백 (`settings.dart.enabled`와 함께) |
| `DISCORD_WEBHOOK_URL` | 권장 | 디스코드 알림 웹훅 URL |
| `QUANT_AUTO_ENTRY` | full paper 시 | schedule 모드에서 DB 모의 주문을 켬. 허용값: true/false/1/0/on/off/yes/no. live 모드에서는 ENV override 무시 |
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
| **문서** | `README.md`, `quant_trader_design.md`, `docs/PROJECT_GUIDE.md`, `deploy/README.md`(선택) |
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

> 📌 **상세 설계·지표 공식·전략 로직·시스템 진단**: `quant_trader_design.md`
> **문서 버전**: v5.2
> **최종 수정**: 2026-04-30 (target-weight paper/pilot adapter 반영)
