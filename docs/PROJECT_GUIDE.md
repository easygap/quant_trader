# QUANT TRADER — 프로젝트 가이드

> **목적**: 코드를 볼 때 **파일별 역할**, **프로그램 흐름**, **알고리즘·설정**을 세세히 알 수 있도록 정리한 문서.  
> **문서 버전**: v2.1  
> **최종 수정**: 2026-03-18  
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
9. [저장소(Git) 관리](#9-저장소git-관리)

---

## 1. 프로그램이 어떻게 돌아가는지

### 1.1 전체 흐름 요약

1. **시작**  
   `main.py` 실행 → 로거·DB 초기화 → `--mode`에 따라 **backtest / validate / paper / live / liquidate / compare / optimize / dashboard / check_correlation** 중 하나로 분기.

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
| **validate** | `run_strategy_validation(args)` | backtest.strategy_validator (3~5년, 샤프·MDD·벤치마크 KS11·코스피 상위 50 동일비중, in/out-of-sample). `--no-benchmark-top50` 으로 Top50 비활성화 |
| **paper** | `run_paper_trading(args)` | WatchlistManager, DataCollector, 전략, OrderExecutor(paper), DiscordBot |
| **live** | `run_live_trading(args)` | KISApi, PortfolioManager(sync), Scheduler |
| **liquidate** | `run_emergency_liquidate(args)` | DB 포지션 조회 → 종목별 매도(KIS 현재가 주문) |
| **compare** | `run_compare_paper_backtest(args)` | database.repositories(get_paper_performance_metrics), Backtester, backtest.paper_compare |
| **optimize** | `run_param_optimize(args)` | backtest.param_optimizer (Grid/Bayesian), Backtester.run(param_overrides=) |
| **dashboard** | `run_dashboard(args)` | monitoring.web_dashboard (aiohttp), PortfolioManager, get_portfolio_snapshots |
| **check_correlation** | `run_check_indicator_correlation(args)` | DataCollector, IndicatorEngine, SignalGenerator → core.indicator_correlation (스코어 상관계수·고상관 쌍 권고) |

---

## 2. 실제 디렉터리·파일 구조

```
quant_trader/
├── main.py                      # CLI 진입점, --mode 분기
├── test_integration.py          # 통합 검증 스크립트 (단일 실행, pytest 아님)
├── requirements.txt
├── README.md
├── quant_trader_design.md       # 아키텍처·지표·전략·리스크 상세 설계서
├── config/
│   ├── config_loader.py         # YAML 통합 로더, .env 덮어쓰기, Config.get() 싱글톤
│   ├── settings.yaml.example
│   ├── settings.yaml            # trading(market_regime_filter 등), watchlist(mode: momentum_top 등)
│   ├── strategies.yaml          # indicators, scoring, mean_reversion(fundamental_filter), trend_following, momentum_factor, volatility_condition, ensemble
│   ├── risk_params.yaml
│   ├── holidays.yaml.example
│   └── holidays.yaml            # --update-holidays 로 자동 갱신
├── core/
│   ├── data_collector.py        # 주가 수집 (FinanceDataReader → yfinance → KIS), get_krx_stock_list
│   ├── watchlist_manager.py    # 관심 종목: manual / top_market_cap / kospi200 / momentum_top / low_vol_top / momentum_lowvol
│   ├── indicator_engine.py      # RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV
│   ├── signal_generator.py      # 멀티 지표 스코어링 → BUY/SELL/HOLD
│   ├── risk_manager.py         # 포지션 사이징, 분산, 성과 열화, 손절/익절/트레일링, 거래 비용
│   ├── order_executor.py        # 매수/매도 (paper: DB만, live: KIS), PositionLock, OrderGuard
│   ├── portfolio_manager.py     # 포지션·잔고·수익률, sync_with_broker
│   ├── scheduler.py             # 실전 무한 루프: 장전/장중/장마감, 시장 국면 필터 반영
│   ├── trading_hours.py        # 장 시간·휴장일 판별
│   ├── holidays_updater.py      # 휴장일 YAML 갱신
│   ├── blackswan_detector.py   # 급락 감지 → 전량 매도·쿨다운
│   ├── market_regime.py        # 시장 국면 필터 (코스피 200일선 이하 시 신규 매수 중단)
│   ├── fundamental_loader.py   # 펀더멘털(PER·부채비율) 조회 (yfinance, 평균회귀 필터용)
│   ├── indicator_correlation.py # 스코어링 지표 상관계수 (check_correlation 모드)
│   ├── position_lock.py        # threading.RLock
│   ├── order_guard.py           # 동일 종목 TTL 동안 중복 주문 차단
│   ├── strategy_ensemble.py    # 앙상블: technical + momentum_factor + volatility_condition (정보 소스 분리)
│   ├── data_validator.py       # OHLCV 정합성 검사
│   └── notifier.py              # 알림 추상화 (디스코드 실패 시 fallback)
├── strategies/
│   ├── base_strategy.py        # analyze(df), generate_signal(df, **kwargs) 추상
│   ├── scoring_strategy.py     # IndicatorEngine + SignalGenerator
│   ├── mean_reversion.py       # Z-Score·ADX·펀더멘털 필터
│   ├── trend_following.py      # ADX·200일선·MACD·ATR
│   ├── momentum_factor.py      # 모멘텀 팩터 (N일 수익률만, 앙상블용)
│   └── volatility_condition.py # 변동성 조건 (N일 실현변동성만, 앙상블용)
├── api/
│   ├── kis_api.py              # 토큰·시세·주문·잔고, Circuit Breaker 연동
│   ├── websocket_handler.py   # KIS 실시간 체결/호가
│   └── circuit_breaker.py      # CLOSED → OPEN → HALF_OPEN
├── backtest/
│   ├── backtester.py           # 시뮬레이션, strict_lookahead 기본, 과매매 분석(평균 보유기간·총 수수료)
│   ├── report_generator.py     # txt·html (과매매 분석 포함)
│   ├── strategy_validator.py   # validate: KS11·코스피 상위 50 동일비중 벤치마크
│   ├── paper_compare.py        # 모의투자 vs 백테스트 비교
│   └── param_optimizer.py     # Grid/Bayesian 최적화
├── database/
│   ├── models.py               # StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport
│   ├── repositories.py         # CRUD, get_paper_performance_metrics
│   └── backup.py               # SQLite 일일 백업
├── monitoring/
│   ├── logger.py
│   ├── discord_bot.py
│   ├── liquidate_trigger.py   # HTTP POST /liquidate
│   ├── dashboard.py           # 콘솔 대시보드 (선택)
│   └── web_dashboard.py       # aiohttp 웹 대시보드
├── tests/                      # pytest tests/ -q
│   ├── test_backtester_strategies.py
│   ├── test_backtester_trailing_stop.py
│   ├── test_blackswan_detector.py
│   ├── test_discord_bot.py
│   ├── test_integration_smoke.py
│   ├── test_kis_websocket_e2e.py
│   ├── test_order_executor_paper.py
│   ├── test_portfolio_manager.py
│   ├── test_risk_manager.py
│   ├── test_scheduler.py
│   ├── test_signal_generator.py
│   ├── test_strategy_validator.py
│   ├── test_trading_hours.py
│   └── test_watchlist_manager.py
├── docs/
│   └── PROJECT_GUIDE.md        # 본 문서
└── reports/                    # 백테스트 txt/html 출력 (생성물은 .gitignore)
```

---

## 3. 파일별 상세 역할

### 3.1 루트

| 파일 | 역할 |
|------|------|
| **main.py** | CLI 진입점. `--mode`: backtest / validate / paper / live / liquidate / compare / optimize / dashboard / check_correlation. **strict-lookahead 기본 True**, `--allow-lookahead` 시 해제(경고 출력). paper/live 시 시장 국면 필터(코스피 200일선) 적용. 실전: `ENABLE_LIVE_TRADING=true` + `--confirm-live` 필수. |
| **test_integration.py** | 설정·DB·지표·신호·리스크·백테스트·리포트·디스코드 등 전체 파이프라인 일괄 검증. 단일 실행 스크립트. |

### 3.2 config/

| 파일 | 역할 |
|------|------|
| **config_loader.py** | `load_settings()`, `load_strategies()`, `load_risk_params()`, `load_all_config()`. `.env`로 KIS 키·계좌·디스코드 웹훅 덮어씀. 다중 계좌: `kis_api.accounts`, `Config.get_account_no(strategy)`. `Config.get()` 싱글톤. |
| **settings.yaml.example** | 설정 예시. 복사해 `settings.yaml`로 사용. (`settings.yaml`은 .gitignore) |
| **strategies.yaml** | `indicators`, `scoring`, `mean_reversion`(fundamental_filter), `trend_following`, `momentum_factor`, `volatility_condition`, `ensemble`(technical·momentum_factor·volatility_condition). |
| **risk_params.yaml** | 포지션 사이징, 손절/익절/트레일링, diversification, position_limits, drawdown, performance_degradation, paper_backtest_compare, transaction_costs. |
| **holidays.yaml** | 휴장일. `python main.py --update-holidays`로 pykrx+fallback 갱신. |

### 3.3 core/

| 파일 | 역할 |
|------|------|
| **data_collector.py** | 한국: FinanceDataReader → yfinance → KIS 일봉. 미국: yfinance. `get_krx_stock_list()`로 KRX 종목 리스트(watchlist 자동 선정용). OHLCV DataFrame 반환. |
| **watchlist_manager.py** | manual / top_market_cap / kospi200 / **momentum_top** / **low_vol_top** / **momentum_lowvol**. `resolve()`로 심볼 리스트 반환. |
| **indicator_engine.py** | pandas-ta로 RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV, volume_ratio. `calculate_all(df)`로 지표 컬럼 추가. |
| **signal_generator.py** | `strategies.yaml` 스코어링 가중치로 점수 합산 → BUY/SELL/HOLD. `generate(df)`, `get_latest_signal(df)`. |
| **risk_manager.py** | 포지션 사이징(1% 룰), `check_diversification`, `check_recent_performance`, 손절/익절/트레일링, MDD 한도. `calculate_transaction_costs`(수수료·증권거래세·양도소득세·슬리피지·호가 단위). |
| **order_executor.py** | `trading.mode`: paper면 DB만, live면 KIS API. 거래 시간·블랙스완 쿨다운 검사, 재시도(지수 백오프). PositionLock, OrderGuard·KIS 미체결 조회로 중복 방지. |
| **portfolio_manager.py** | 보유 포지션·잔고·수익률. `sync_with_broker()`로 KIS 잔고↔DB 크로스체크. `get_portfolio_summary()`. |
| **scheduler.py** | 실전 무한 루프. 장전/장중/장마감. **시장 국면 필터** 적용(200일선 이하 시 진입 후보 미적재·실행 생략). 장중 10분 간격 모니터링. 루프 10분 초과 시 다음 사이클 스킵. |
| **trading_hours.py** | 장 시간·휴장일. holidays.yaml → pykrx → fallback. 주문 가능 시간 검사. |
| **holidays_updater.py** | pykrx(또는 fallback)로 휴장일 조회 → `config/holidays.yaml` 저장. `update_holidays_yaml()`. |
| **blackswan_detector.py** | 급락 감지 시 전량 매도·디스코드 경고·쿨다운 동안 신규 매수 차단. |
| **market_regime.py** | `allow_new_buys_by_market_regime()`. 지수(기본 KS11) 200일선 위/아래 판별. 하락장 시 신규 매수 중단. settings: market_regime_filter, market_regime_index, market_regime_ma_days. |
| **fundamental_loader.py** | `get_fundamentals(symbol)`, `check_fundamental_filter()`. yfinance로 PER·부채비율 조회. 평균회귀 전략 매수 전 펀더멘털 필터용. |
| **indicator_correlation.py** | 스코어링 지표 점수 시리즈 상관계수·고상관 쌍 권고. `--mode check_correlation` 시 사용. |
| **position_lock.py** | 포지션/주문 공유 자원용 `threading.RLock`. |
| **order_guard.py** | 동일 종목에 대해 최근 주문 접수 후 TTL(기본 600초) 동안 추가 주문 차단. |
| **strategy_ensemble.py** | **technical**(ScoringStrategy) + **momentum_factor** + **volatility_condition** 신호 통합. majority_vote / weighted_sum / conservative. 정보 소스 분리(설계서 §4.4). |
| **data_validator.py** | OHLCV Null·NaN·음수 주가·거래량·타임스탬프 역전 등 검사. |
| **notifier.py** | 알림 추상화. 디스코드 실패 시 이메일 등 fallback. |

### 3.4 strategies/

| 파일 | 역할 |
|------|------|
| **base_strategy.py** | `analyze(df)` → 지표·신호 붙은 DataFrame, `generate_signal(df, **kwargs)` → 최신 BUY/SELL/HOLD·점수·상세. |
| **scoring_strategy.py** | IndicatorEngine + SignalGenerator. 총점 ≥ buy_threshold 매수, ≤ sell_threshold 매도. |
| **mean_reversion.py** | Z-Score·ADX 필터. **펀더멘털 필터**(PER·부채비율, symbol 전달 시)로 매수 전 정상 범위 검사. 한국 시장 한계: 설계서 §4.2 참고. |
| **trend_following.py** | ADX·200일선·MACD·ATR 기반 추세 추종. 진입 늦음·손익비 ≥ 2.0 검증 필수. 한국 시장 추세 지속성 약함(§4.3). |
| **momentum_factor.py** | N일 수익률만 사용(기술지표 없음). 앙상블용. lookback_days, buy_threshold_pct, sell_threshold_pct. |
| **volatility_condition.py** | N일 실현변동성(연율화)만 사용. 저변동성=매수, 고변동성=매도. 앙상블용. |

### 3.5 api/

| 파일 | 역할 |
|------|------|
| **kis_api.py** | 토큰 발급·갱신, 시세·주문·잔고. 토큰 만료·갱신 실패 시 디스코드 알림. 모의/실전 도메인. Circuit Breaker 연동. live 시 미체결 조회 `has_unfilled_orders`. |
| **websocket_handler.py** | KIS 웹소켓 실시간 체결/호가. asyncio, 콜백으로 가격 전달. |
| **circuit_breaker.py** | API 연속 실패 시 CLOSED → OPEN → HALF_OPEN. 요청 차단으로 계정 제재 방지. |

### 3.6 backtest/

| 파일 | 역할 |
|------|------|
| **backtester.py** | OHLCV + 전략 시뮬레이션. 수수료·세금·슬리피지·1% 룰·손절/익절/트레일링 스탑. **strict_lookahead 기본 True**. 성과 지표 + **과매매 분석**(평균 보유 기간, 총 수수료). |
| **strategy_validator.py** | 최소 3~5년, 샤프·MDD·벤치마크(KS11 + **코스피 상위 50 동일비중**). in/out-of-sample. `run()`, `run_walk_forward()`. `--no-benchmark-top50` 으로 Top50 비활성화. |
| **report_generator.py** | txt·html 리포트. 거래 내역, 성과 지표, 자본 곡선, **과매매 분석**(평균 보유 기간, 총 수수료). `--output-dir`. |
| **paper_compare.py** | 지정 기간 paper 성과 vs 동일 기간·전략 백테스트. divergence 시 경고·디스코드(설정 시). |
| **param_optimizer.py** | Grid Search / Bayesian(scikit-optimize). train_ratio·OOS 보고. `Backtester.run(..., param_overrides=)`. |

### 3.7 database/

| 파일 | 역할 |
|------|------|
| **models.py** | StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport. `init_database()`. |
| **repositories.py** | CRUD. `get_paper_performance_metrics(start, end)` (compare 모드). get_position, get_all_positions, save_trade 등. |
| **backup.py** | `database.backup_path` 설정 시 장마감 후 SQLite 날짜별 복사. 보관 일수 초과 분 삭제. |

### 3.8 monitoring/

| 파일 | 역할 |
|------|------|
| **logger.py** | loguru 초기화. logging 설정에 따라 파일 로테이션·콘솔 출력. |
| **discord_bot.py** | 웹훅으로 매매 알림·일일 리포트·블랙스완·동기화 불일치 발송. |
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

| 파일 | 용도 |
|------|------|
| **config/settings.yaml** | KIS API, database, logging, **trading**(mode, auto_entry, **market_regime_filter**, **market_regime_index**, **market_regime_ma_days**, sync_broker_interval_minutes, pending_order_ttl_seconds), discord, **dashboard**, **watchlist**(mode: manual / top_market_cap / kospi200 / momentum_top / low_vol_top / momentum_lowvol). 민감 정보는 .env에서 덮어씀. |
| **config/strategies.yaml** | **indicators**, **scoring**, **mean_reversion**(fundamental_filter: enabled, per_min, per_max, debt_ratio_max), trend_following, **momentum_factor**, **volatility_condition**, **ensemble**(mode, confidence_weight: technical, momentum_factor, volatility_condition). |
| **config/risk_params.yaml** | position_sizing, stop_loss, take_profit, trailing_stop, diversification, position_limits, drawdown, performance_degradation, paper_backtest_compare, transaction_costs(commission_rate, tax_rate 0.18%, slippage, capital_gains_tax, dynamic_slippage). |
| **config/holidays.yaml** | 휴장일 목록. `--update-holidays`로 갱신. |

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

- **자금 관리**: `diversification.max_investment_ratio`(전체 주식 비중 상한), `max_positions`(동시 보유 종목 수), `max_position_ratio`(단일 종목 비중). RiskManager.check_diversification, Backtester._simulate에서 적용.
- **성과 열화**: `performance_degradation.recent_trades`, `min_win_rate`. 최근 N거래 승률이 임계값 미만이면 **신규 매수만** 중단. RiskManager.check_recent_performance → OrderExecutor에서 매수 전 호출.

---

## 6. 알고리즘·지표 요약

- **지표**: `core/indicator_engine.py`에서 pandas-ta로 RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV, volume_ratio 계산. 설정은 `config/strategies.yaml` → `indicators`.
- **스코어링**: `core/signal_generator.py`가 가중치(weights)로 점수 합산 → buy_threshold/sell_threshold로 BUY/SELL 판단. **가중치는 예시/직관값이며 한국 시장 검증값이 아님.** 가중치를 과거 데이터로 최적화하면 오버피팅 위험이 있으므로 OOS 검증 권장. 자세한 내용은 `quant_trader_design.md` §4.1 "가중치 설정 유의사항" 참고.
- **전략**: scoring(멀티 지표), mean_reversion(Z-Score·ADX·펀더멘털 필터), trend_following(ADX·200일선·MACD·ATR), **momentum_factor**(N일 수익률만), **volatility_condition**(N일 실현변동성만), **ensemble**(technical + momentum_factor + volatility_condition, 다수결/가중합/보수적). 각 전략의 시장 비효율성 가정은 설계서 §4 참고.

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

자동 모드 실패 시 `symbols` 또는 기본(005930) fallback.

---

## 9. 저장소(Git) 관리

**커밋 대상**: 위 `quant_trader` 소스, `config/*.example`, `requirements.txt`, `README.md`, `quant_trader_design.md`, `docs/PROJECT_GUIDE.md`.  
**제외(.gitignore)**:

- 비밀/환경: `.env`, `config/settings.yaml`
- Python: `__pycache__/`, `.venv/`, `.pytest_cache/` 등
- 데이터/로그: `data/`, `logs/`, `*.db`, `*.log`
- 백테스트 산출물: `reports/backtest_*.html`, `reports/backtest_*.txt`, `reports/*.md`
- 타 프로젝트: `fintics/` (본 저장소는 quant_trader 소스만 관리)

필요 없는 소스는 저장소에 두지 않습니다. 생성된 리포트·fintics 폴더는 로컬에서만 사용하고 커밋하지 않습니다.

---

> 📌 **상세 설계·지표 공식·전략 로직**: `quant_trader_design.md`  
> **문서 버전**: v2.1  
> **최종 수정**: 2026-03-18 (시장 국면 필터, 펀더멘털 필터, 앙상블 정보 소스 분리, Top50 벤치마크, 과매매 분석, 팩터 워치리스트 반영)
