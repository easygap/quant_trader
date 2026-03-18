# QUANT TRADER — 프로젝트 가이드

다른 사용자가 코드를 볼 때 **파일별 역할**과 **프로그램 흐름**을 세세히 알 수 있도록 정리한 문서입니다.

---

## 1. 프로그램이 어떻게 돌아가는지

### 1.1 전체 흐름 요약

1. **시작**  
   `main.py`가 실행되면 로거·DB를 초기화하고, `--mode` 인자에 따라 **백테스트 / 모의투자(paper) / 실전(live)** 중 하나로 분기합니다.

2. **백테스트 모드**  
   `DataCollector`로 과거 주가 수집 → `Backtester`가 전략으로 시뮬레이션(수수료·세금·슬리피지·손절/익절/트레일링 스탑 반영) → `ReportGenerator`가 txt/html 리포트 생성.

3. **모의투자 모드**  
   관심 종목마다 `DataCollector`로 데이터 수집 → 전략의 `generate_signal()`로 신호 생성 → BUY/SELL이면 `OrderExecutor`가 **DB에만 기록**하고 디스코드 알림 발송. 실제 주문 없음.

4. **실전 모드**  
   KIS API 인증 후 `Scheduler`가 무한 루프로 동작합니다.  
   - **장전(08:50)**  
     데이터 수집·전략 분석 → `auto_entry: true`이면 매수 후보 선정 → 장중에 실제 매수 실행.  
   - **장중(09:00~15:30)**  
     10분 간격으로 신호·손절/익절/트레일링 스탑 확인 → 필요 시 `OrderExecutor`가 **KIS API로 실제 주문**.  
   - **장마감(15:35)**  
     일일 리포트·포트폴리오 스냅샷 저장, 디스코드 발송.

5. **공통**  
   설정은 `config/` YAML + `.env`, 데이터·포지션·거래 기록은 SQLite(또는 설정에 따른 DB)에 저장됩니다.

### 1.2 모드별 진입점

| 모드 | main.py에서 호출되는 함수 | 핵심 모듈 |
|------|---------------------------|-----------|
| backtest | `run_backtest(args)` | DataCollector → Backtester → ReportGenerator |
| paper | `run_paper_trading(args)` | DataCollector, 전략, OrderExecutor(paper), DiscordBot |
| live | `run_live_trading(args)` | KISApi, PortfolioManager(sync), Scheduler |
| **compare** | `run_compare_paper_backtest(args)` | database.repositories(paper 성과), Backtester, backtest.paper_compare |
| **dashboard** | `run_dashboard(args)` | monitoring.web_dashboard (aiohttp), Dashboard·PortfolioManager·get_portfolio_snapshots |

---

## 2. 디렉터리 구조

```
quant_trader/
├── main.py                 # 실행 진입점 (CLI 파싱, 모드 분기)
├── test_integration.py     # 통합 검증 스크립트 (설정·DB·지표·백테스트·디스코드 등 일괄 점검)
├── config/                 # 설정 로드 및 YAML/환경변수 관리
├── core/                   # 데이터 수집, 지표, 신호, 리스크, 주문, 스케줄러, 포트폴리오 등 핵심 로직
├── strategies/             # 매매 전략 구현 (스코어링, 평균회귀, 추세추종, 앙상블)
├── api/                    # KIS REST API·웹소켓·Circuit Breaker
├── backtest/               # 백테스트 엔진 및 txt/html 리포트 생성
├── database/               # SQLAlchemy 모델 및 Repository(CRUD)
├── monitoring/             # 로거, 디스코드 웹훅, 대시보드
├── tests/                  # 단위·통합·모의 E2E 테스트
├── config/*.yaml           # 설정 파일 (settings, strategies, risk_params, holidays 등)
├── docs/                   # 문서 (본 가이드 등)
└── reports/                # 백테스트 리포트 출력 디렉터리
```

---

## 3. 파일별 설명

### 3.1 루트

| 파일 | 역할 |
|------|------|
| **main.py** | CLI 진입점. `--mode`: backtest / validate / paper / live / **liquidate**(긴급 전량 매도) / **compare**(모의투자 vs 백테스트 비교) / **optimize**(전략 파라미터 Grid/Bayesian 최적화, 오버피팅 주의) / **dashboard**(실시간 웹 대시보드). **strict-lookahead는 기본 True**이며, `--allow-lookahead` 사용 시 stderr 경고 출력. 실전 모드는 `ENABLE_LIVE_TRADING=true` + `--confirm-live` 이중 확인. |
| **test_integration.py** | 설정·DB·지표·신호·리스크·백테스트·리포트·디스코드 등 전체 파이프라인을 한 번에 검증하는 스크립트. pytest가 아닌 단일 실행용. |

---

### 3.2 config/

| 파일 | 역할 |
|------|------|
| **config_loader.py** | YAML 설정 통합 로더. `settings.yaml`, `strategies.yaml`, `risk_params.yaml` 로드. `.env`에서 KIS 키·계좌번호·디스코드 웹훅 등으로 덮어씀. **다중 계좌**: `kis_api.accounts`(전략별 계좌번호), `Config.get_account_no(strategy)`로 해석. `Config.get()` 싱글톤으로 전역 설정 제공. |
| **settings.yaml.example** | 설정 예시. 복사해 `settings.yaml`로 두고 사용. (실제 `settings.yaml`은 .gitignore 대상.) |
| **holidays.yaml.example** | 휴장일 예시. `holidays.yaml`은 `python main.py --update-holidays`로 자동 갱신(pykrx+fallback). 없으면 첫 로드 시 자동 생성 시도. |
| **strategies.yaml** | 전략별 파라미터(스코어링 가중치, 앙상블 모드 등). `SignalGenerator`·각 전략·`StrategyEnsemble`이 참조. |
| **risk_params.yaml** | 초기 자본, **거래 비용**: 수수료(`commission_rate`), **증권거래세 0.18%**(`tax_rate`, 매도 시 의무), **양도소득세**(`capital_gains_tax.enabled`·`rate`, 대주주 해당 시만), 거래량 기반 동적 슬리피지, 포지션 사이징(1% 룰), **자금 관리**: 전체 주식 투자 비중 상한(`max_investment_ratio`, 기본 70%)·동시 보유 최대 종목 수(`max_positions`, 기본 10), **position_limits.max_holding_days**(최대 보유 기간, N일 초과 시 강제 정리·물림 방지, 0이면 비활성), 손절/익절/트레일링 스탑, MDD 한도 등. **paper_backtest_compare**: 모의투자 vs 백테스트 비교 시 수익률·승률 차이 임계값. 설정 누락 시 실제 수익과 백테스트 괴리 발생. 배당소득세(15.4% 등)는 배당 수령 시 별도. `Backtester`·`RiskManager`·`OrderExecutor`·`paper_compare`·`Scheduler`가 참조. |

---

### 3.3 core/

| 파일 | 역할 |
|------|------|
| **data_collector.py** | 주가 데이터 수집. 한국 주식은 FinanceDataReader(우선)·yfinance·KIS 일봉 순으로 시도. 미국 주식은 yfinance. **get_krx_stock_list()**로 KRX 전 종목 리스트(시가총액 등) 조회 — watchlist 자동 선정에 사용. 수집 결과는 정규화된 OHLCV DataFrame으로 반환하며, 필요 시 DB에 저장. |
| **watchlist_manager.py** | 관심 종목 선정. **시가총액 상위 N개**(top_market_cap)·**코스피200 유사**(kospi200) 자동 포함 또는 수동(manual) 목록. KRX 리스트 기반으로 수작업 없이 watchlist 구성. |
| **indicator_engine.py** | 기술 지표 계산. pandas-ta로 RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV 등 계산. 한 DataFrame에 지표 컬럼을 붙여 반환. |
| **signal_generator.py** | 지표가 붙은 DataFrame을 받아 멀티 지표 스코어링으로 BUY/SELL/HOLD 신호 생성. `strategies.yaml`의 스코어링 가중치 사용. |
| **risk_manager.py** | 포지션 사이징(1% 룰), **자금 관리** `check_diversification`, **전략 성과 열화 감지** `check_recent_performance`(최근 N거래 승률 미만 시 신규 매수 중단), 손절/익절/트레일링 스탑, MDD 기반 매매 중단. **거래 비용** `calculate_transaction_costs`: 수수료·증권거래세(0.18%)·양도소득세(대주주 설정 시)·슬리피지. 호가 단위 맞춤. `OrderExecutor`·`Backtester`가 사용. |
| **order_executor.py** | 매수/매도 실행. `trading.mode`가 paper면 DB만 기록, live면 KIS API로 실제 주문. 거래 시간·블랙스완 쿨다운 검사, 실패 시 재시도(지수 백오프). `PositionLock`으로 동시 접근 제어. |
| **portfolio_manager.py** | 보유 포지션·잔고·수익률 집계. 실전 모드에서 KIS 잔고와 DB 포지션 **동기화(sync_with_broker)**. `get_portfolio_summary()`로 총 평가금·현금·포지션 수 등 제공. |
| **scheduler.py** | 실전 모드 자동 스케줄러. 무한 루프로 거래일·장전/장중/장마감 구간 판별. 장전에 데이터 수집·전략 분석·매수 후보 선정; 장중에 10분 간격으로 **최대 보유 기간** 초과 포지션 강제 정리·신호·손절/익절 실행. **타이밍 리스크 대응**: 루프 10분 초과 시 다음 사이클 스킵; 주문 전 미체결 확인은 `order_guard.py`·KIS 미체결 조회. 장마감에 일일 리포트·스냅샷·디스코드 발송. |
| **trading_hours.py** | 장 운영 시간·공휴일 판별. `holidays.yaml`(없으면 자동 생성 시도) → pykrx → 하드코딩 fallback. 주문 가능 시간인지 검사. |
| **holidays_updater.py** | **휴장일 파일 자동 갱신**. pykrx로 휴장일 조회(실패 시 연도별 fallback), `config/holidays.yaml`에 저장. `python main.py --update-holidays`로 호출. 매년 수동 관리 불필요. |
| **blackswan_detector.py** | 급락 감지(개별 종목·포트폴리오·연속 하락). 발동 시 전량 매도·디스코드 경고·쿨다운 동안 신규 매수 차단. |
| **position_lock.py** | 포지션/주문 관련 공유 자원 접근용 `threading.RLock`. 스케줄러·OrderExecutor 등 동시 접근 시 race condition 방지. |
| **strategy_ensemble.py** | 복수 전략(scoring, mean_reversion, trend_following) 신호 통합. 다수결/가중합/보수적 모드. `--strategy ensemble`일 때 사용. |
| **data_validator.py** | 수신 OHLCV 데이터 정합성 검사. Null·NaN·음수 주가·거래량·타임스탬프 역전 등 필터링. |
| **notifier.py** | 알림 발송 추상화. 디스코드 실패 시 이메일 등 fallback(설정에 따라 사용). |

---

### 3.4 strategies/

| 파일 | 역할 |
|------|------|
| **base_strategy.py** | 전략 추상 클래스. `analyze(df)` → 지표·신호가 붙은 DataFrame, `generate_signal(df)` → 최신 BUY/SELL/HOLD와 점수·상세. 모든 전략이 상속. |
| **scoring_strategy.py** | 멀티 지표 스코어링 전략. IndicatorEngine + SignalGenerator로 지표 계산·스코어 합산. 총점 기준 매수/매도. |
| **mean_reversion.py** | 평균 회귀 전략. 과매수/과매도 구간에서 역추세 신호 생성. |
| **trend_following.py** | 추세 추종 전략. 이동평균·추세 강도 기반 신호 생성. |

---

### 3.5 api/

| 파일 | 역할 |
|------|------|
| **kis_api.py** | 한국투자증권 Open API 래퍼. 토큰 발급·갱신, 시세 조회, 주문, 잔고 조회. **토큰 만료·갱신 실패 시 즉시 디스코드 알림**(실전 주문 조용한 실패 방지). 모의/실전 도메인 전환. Rate limit·Circuit Breaker 연동. |
| **websocket_handler.py** | KIS 웹소켓으로 실시간 체결/호가 스트리밍. asyncio 기반. 승인키 발급 후 구독. 콜백으로 가격 업데이트 전달. |
| **circuit_breaker.py** | API 연속 실패 시 요청 차단. CLOSED → OPEN → HALF_OPEN 전환. 장애 시 불필요한 재요청으로 인한 계정 제재 방지. |

---

### 3.6 backtest/

| 파일 | 역할 |
|------|------|
| **backtester.py** | 백테스트 엔진. OHLCV + 전략으로 시뮬레이션. 수수료·세금·슬리피지(거래량 기반 동적 슬리피지), 1% 룰·전체 투자 비중 상한·손절·익절·트레일링 스탑 반영. 성과 지표(수익률, 샤프, MDD, 승률 등) 계산. **strict-lookahead 기본 True** — 시점별 슬라이싱으로 Look-Ahead Bias 방지. |
| **strategy_validator.py** | **전략 유효성 검증** 전용. 최소 3~5년 데이터, **샤프 비율 1.0 이상**·MDD 기준 충족 여부, **벤치마크(코스피 지수 단순 매수)** 대비 초과 수익, **in-sample / out-of-sample 구간 분리**로 오버피팅 방지 검증. `--mode validate` 시 호출. |
| **report_generator.py** | 백테스트 결과를 txt·html 리포트로 출력. 거래 내역, 성과 지표, 자본 곡선. `--output-dir`에 저장. |
| **paper_compare.py** | **모의투자 vs 백테스트 자동 비교**. 지정 기간의 paper 성과(DB 스냅샷·거래 기록)와 동일 기간·동일 전략 단일 종목 백테스트 결과를 비교. 수익률·승률 차이가 `risk_params.yaml`의 `paper_backtest_compare` 임계값을 초과하면 **divergence**로 판정해 경고 로그 및(설정 시) 디스코드 알림. 두 결과가 크게 다르면 구현 버그 또는 데이터 문제 가능성 신호. |
| **param_optimizer.py** | **전략 파라미터 자동 최적화**. Grid Search 또는 Bayesian Optimization(scikit-optimize)으로 전략 파라미터 탐색. `train_ratio`로 학습 구간에서만 최적화하고 OOS 구간 성과를 함께 보고해 **오버피팅** 가능성 확인. `Backtester.run(..., param_overrides=...)`로 파라미터 주입. |

---

### 3.7 database/

| 파일 | 역할 |
|------|------|
| **models.py** | SQLAlchemy ORM 모델. `StockPrice`(일봉), `TradeHistory`(매매 기록), `Position`(보유 포지션), `PortfolioSnapshot`, `DailyReport`. 엔진·세션 생성, `init_database()` 제공. |
| **repositories.py** | DB CRUD. 주가 저장/조회, 거래 기록·포지션·스냅샷·일일 리포트 저장/조회. **get_paper_performance_metrics(start, end)** 로 기간별 모의투자 수익률·승률·매도 건수 계산(compare 모드에서 사용). 전역에서 `get_position`, `get_all_positions`, `save_trade` 등으로 사용. |
| **backup.py** | SQLite **일일 자동 백업**. `database.backup_path` 설정 시 장마감 후 날짜별 복사본 생성·보관 일수 초과 분 삭제. 손상 시 포지션/거래 기록 복구용. |

---

### 3.8 monitoring/

| 파일 | 역할 |
|------|------|
| **logger.py** | loguru 기반 로깅 초기화. 설정의 logging 섹션에 따라 파일 로테이션·콘솔 출력. |
| **discord_bot.py** | 디스코드 웹훅으로 메시지·매매 알림·일일 리포트 발송. `webhook_url`만 설정하면 사용 가능. |
| **liquidate_trigger.py** | 긴급 전체 청산 HTTP 트리거. `LIQUIDATE_TRIGGER_TOKEN` 설정 후 `python -m monitoring.liquidate_trigger`로 서버 실행 시, POST /liquidate (X-Token 또는 ?token=)으로 원격/디스코드 봇에서 청산 호출 가능. |
| **dashboard.py** | 콘솔 대시보드(포지션·수익률 등) 출력. (선택 사용.) |
| **web_dashboard.py** | **실시간 웹 대시보드**. aiohttp 서버로 포트폴리오 요약·포지션·최근 30일 스냅샷 추이를 한 페이지에 표시. 10초 간격 폴링으로 갱신. `python main.py --mode dashboard` 또는 `python -m monitoring.web_dashboard [--port 8080]`. `config/settings.yaml`의 `dashboard.host`·`dashboard.port`로 주소/포트 지정 가능. |

---

### 3.9 tests/

| 파일 | 역할 |
|------|------|
| **test_*.py** | 단위·통합 테스트. 전략, 리스크 매니저, 신호 생성기, 스케줄러, 거래 시간, 블랙스완, OrderExecutor(paper), KIS/웹소켓 모의 E2E 등. `pytest tests/ -q`로 실행. |

---

## 4. 설정 파일(YAML) 요약

| 파일 | 용도 |
|------|------|
| **config/settings.yaml** | KIS API(base_url, use_mock), **database**(sqlite_path, **backup_path**·backup_retention_days — 일일 백업), 로깅, **trading**(mode, auto_entry, **sync_broker_interval_minutes** — KIS 잔고 크로스체크 주기), discord, watchlist. 민감 정보는 .env에서 덮어씀. |
| **config/strategies.yaml** | scoring 가중치, 앙상블 모드·신뢰도. SignalGenerator·ScoringStrategy·StrategyEnsemble이 참조. |
| **config/risk_params.yaml** | 초기 자본, 수수료·세금·거래량 기반 동적 슬리피지, 포지션 사이징(1% 룰). **자금 관리**: `diversification.max_investment_ratio`, `max_positions`. **전략 성과 자동 열화 감지**: 시장 국면 변경 시 손실 구간 대응 — `performance_degradation`(최근 N거래 승률이 `min_win_rate` 미만이면 신규 매수 자동 중단). 손절/익절/트레일링 스탑, MDD 한도. |
| **config/holidays.yaml** | 휴장일 목록. `--update-holidays`로 pykrx+fallback 자동 갱신. 없으면 첫 로드 시 생성 시도, 실패 시 pykrx/fallback. |

---

## 5. 실행 모드별 데이터 흐름

### 백테스트

```
main.py (--mode backtest)
  → DataCollector.fetch_korean_stock(symbol, start, end)
  → Backtester.run(df, strategy_name)  # 전략.analyze → _simulate (수수료·손절·익절·트레일링 스탑)
  → Backtester.print_report(result)
  → ReportGenerator.generate_all(result)  # txt, html
```

### 모의투자(paper)

```
main.py (--mode paper)
  → watchlist 종목마다:
      DataCollector.fetch_korean_stock(symbol)
      → strategy.generate_signal(df)
      → BUY/SELL 시 DiscordBot.send_signal_alert, OrderExecutor.execute_buy/execute_sell (DB만 기록)
  → PortfolioManager.get_portfolio_summary()
```

### 실전(live)

```
main.py (--mode live --confirm-live)
  → KISApi.authenticate(), verify_connection()
  → BlackSwanDetector, PortfolioManager.sync_with_broker()
  → Scheduler.run()  # 무한 루프
       장전: 데이터 수집, 전략 분석, 매수 후보 저장
       장중: 10분마다 신호·손절/익절 확인 → OrderExecutor (KIS 실제 주문)
            주문 전: ① OrderGuard(TTL) ② KIS 미체결 조회 — 미체결 존재 시 중복 주문 보류
            루프 실행 시간이 10분 초과 시 다음 모니터링 사이클 1회 스킵 (타이밍 리스크 방지)
       장중: live 모드 시 KIS 잔고와 DB 포지션 주기적 크로스체크 (sync_broker_interval_minutes, 기본 30분)
       장마감: 일일 리포트, 스냅샷 → live 시 KIS 크로스체크 → DB 일일 백업(backup_path 설정 시), 디스코드
```

### 긴급 전체 청산 명령 (수동 개입)

블랙스완 감지 외에도 **수동으로 즉시 전 종목 매도**가 필요한 상황을 위해 CLI 및 선택적으로 HTTP 트리거를 제공합니다.

| 방법 | 사용 |
|------|------|
| **CLI** | `python main.py --mode liquidate` — DB 보유 포지션 조회 후 종목별 매도 (실전 시 KIS 현재가 조회 후 주문). 원격에서는 SSH 등으로 접속해 동일 명령 실행. |
| **HTTP 트리거** | `LIQUIDATE_TRIGGER_TOKEN`·`LIQUIDATE_TRIGGER_PORT` 설정 후 `python -m monitoring.liquidate_trigger` 실행 시, 해당 URL로 POST하면 청산 실행. 디스코드 봇·IFTTT 등에서 호출 가능. |

```
python main.py --mode liquidate
  → DB 보유 포지션 조회 → 종목별 매도 (실전 시 KIS 현재가 조회 후 주문)
```

### DB 백업 자동화 + KIS 잔고 상시 크로스체크

SQLite 파일이 손상되면 **포지션/거래 기록 전체 소실**이 될 수 있으므로, 아래를 적용합니다.

| 항목 | 설명 |
|------|------|
| **일일 자동 백업** | `config/database.backup_path` 설정 시 장마감 후 SQLite 파일을 날짜별로 복사. `backup_retention_days`(기본 7) 초과 분은 삭제. 미설정 시 백업 없음 — 손상 시 복구 불가. |
| **KIS 잔고 크로스체크** | live 모드에서 **장 시작 전** `sync_with_broker()` 호출(실전 진입 시). **장중**에는 `sync_broker_interval_minutes`(기본 30)마다 KIS 잔고와 DB 포지션을 대조해 불일치 시 로깅·디스코드 알림. **장마감** 시 한 번 더 크로스체크 후 백업. |

불일치 시 DB를 증권사 기준으로 자동 보정하는 기능은 옵션(`auto_correct`)으로 확장 가능하며, 현재는 알림만 발생합니다.

### 자금 관리 — 전체 자산의 몇 %를 주식에 투자할지 상한

1% 룰은 **종목당 최대 손실**만 1%로 제한합니다. 상한이 없으면 30종목에 동시 신호가 나도 전 재산이 주식에 투입될 수 있으므로, 아래 두 가지를 **설계에 포함**해 적용합니다.

| 항목 | 설정 (risk_params.yaml diversification) | 설명 |
|------|----------------------------------------|------|
| **전체 주식 투자 비중 상한** | `max_investment_ratio` (기본 0.70) | 총 자산의 **70%**를 초과해 주식에 투자하지 않음. |
| **동시 보유 최대 종목 수** | `max_positions` (기본 10) | 이 개수만큼만 보유 가능. 초과 시 신규 매수 불가. |

단일 종목 비중은 `max_position_ratio`(기본 20%)로 제한됩니다. `RiskManager.check_diversification()`에서 매수 전 검사하며, 백테스트(`Backtester._simulate`)에서도 동일 기준으로 수량 상한을 적용합니다.

### 전략 성과 자동 열화 감지 (시장 국면 변경 대응)

시장 국면이 바뀌면 기존 전략이 손실 구간에 들어갈 수 있습니다. 이를 위해 **최근 N거래의 승률**이 임계값 아래로 떨어지면 **자동으로 매매(신규 매수) 중단**합니다.

| 항목 | 설정 (risk_params.yaml performance_degradation) | 동작 |
|------|--------------------------------------------------|------|
| **평가 대상** | `recent_trades` (기본 20) | 최근 매도 거래 N건으로 승률 계산. |
| **임계값** | `min_win_rate` (기본 0.35) | 승률이 이 비율 미만이면 신규 매수 중단. |
| **효과** | `enabled: true` | 손절/익절·기존 포지션 관리는 유지, **신규 매수만** 중단. |

`RiskManager.check_recent_performance()`가 매수 전 호출되며, `OrderExecutor`에서 허용되지 않으면 해당 종목 매수를 하지 않습니다.

---

## 6. 전략 유효성 검증 (실전 투입 전 필수)

멀티 지표 스코어링·평균회귀·추세추종 전략은 **한국 시장에서 수익을 낸다는 검증된 근거가 문서에 없습니다.** RSI/MACD 등 기술 지표를 **단독으로 사용할 경우** 랜덤 매매와 수익률이 크게 다르지 않다는 연구가 많습니다. 따라서 실전 투입 전 아래 검증을 **반드시** 수행해야 합니다.

| 검증 항목 | 요구 사항 |
|-----------|-----------|
| **데이터 기간** | **최소 3~5년** 치 데이터로 백테스트 (통계적 신뢰·오버피팅 완화). `--validation-years` 기본 5년, 3년 미만이면 자동으로 3년 적용. |
| **샤프 비율** | 전략 백테스트 **샤프 비율 1.0 이상** 확인. 미달 시 실전 투입 비권장. `--min-sharpe`로 기준 변경 가능(기본 1.0). |
| **벤치마크 비교** | **코스피 지수(KS11) 단순 매수** 대비 초과 수익 여부 확인. 벤치마크를 이기지 못하면 전략 가치 재검토. `--benchmark-symbol` 기본 KS11. |
| **오버피팅 방지** | **in-sample / out-of-sample 구간 분리** 검증. 학습 기간(in-sample)과 미래 구간(out-of-sample) 모두에서 기준 충족 여부 확인. `--split-ratio`로 in-sample 비율 설정(기본 0.7). |

실행 예:

```bash
python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5
```

검증 결과는 `reports/validation_*.txt`에 저장되며, full / in-sample / out-of-sample 구간별 수익률·샤프·MDD와 벤치마크 대비 초과 수익이 출력됩니다.

---

## 7. 실전 투입 전 체크리스트

**전략이 실제로 수익을 내는지**가 가장 중요합니다. 아래를 모두 확인한 뒤 실전에 투입하세요.

| 항목 | 설명 |
|------|------|
| **전략 검증** | §6 참고. `--mode validate`로 **최소 3~5년** 데이터, **샤프 1.0 이상**, **벤치마크(코스피) 비교**, **in/out-of-sample 분리** 검증 완료. |
| **Look-Ahead Bias** | **strict-lookahead 기본 사용**(`--allow-lookahead` 미사용). 해제 후 백테스트 시 수익률이 크게 떨어지면 기존 결과는 신뢰 불가. |
| **모의투자** | 최소 1~2개월 모의투자 운영 후 실전 전환. 모의 결과가 백테스트와 방향성이 일치하는지 확인. |
| **첫 실전 규모** | 전체 운용 예정 금액의 **10% 이하**로 시작. |
| **KIS E2E** | KIS 모의투자 환경에서 API 연결·주문·잔고 조회 전 과정 E2E 테스트 완료. |

### 실전 10분 루프 타이밍 리스크 대응

장중 10분 간격 신호 확인 시 API 지연·네트워크 오류·Rate Limit으로 루프가 꼬이거나 중복 주문이 나가는 것을 막기 위해 아래가 적용됩니다.

| 안전장치 | 설명 |
|----------|------|
| **주문 전 미체결 확인** | ① **OrderGuard**: 동일 종목에 대해 최근 주문 접수 후 TTL(기본 600초) 동안 추가 주문 차단. ② **KIS 미체결 조회**: 주문 전 해당 종목의 미체결 주문 존재 여부를 API로 조회해 있으면 주문 보류. (`api/kis_api.py` `has_unfilled_orders`, `core/order_executor.py`에서 live 모드 시 호출) |
| **루프 10분 초과 시 스킵** | 장중 모니터링 한 사이클 실행 시간이 10분을 초과하면 다음 사이클을 1회 스킵. (`core/scheduler.py` `_run_monitoring` finally, `_should_monitor`) |

---

## 8. watchlist 모드 (관심 종목 선정 기준)

관심 종목을 **수작업 없이** 시가총액 상위 N개 또는 코스피200 유사 종목으로 자동 포함할 수 있습니다. `config/settings.yaml`의 `watchlist` 섹션에서 모드와 옵션을 설정합니다.

| mode | 설명 |
|------|------|
| **manual** | `symbols` 목록을 직접 관리. |
| **top_market_cap** | **시가총액 상위 N개** 자동 선정. `market`(KOSPI/KOSDAQ), `top_n` 설정. KRX 종목 리스트 기준. |
| **kospi200** | **코스피200 구성과 유사**하게 시총 상위 N개 자동 선정. 기본 200개, `kospi200_top_n`으로 변경 가능. |

자동 모드(`top_market_cap`, `kospi200`)는 `DataCollector.get_krx_stock_list()`(FinanceDataReader KRX 리스트)로 종목을 가져와 시가총액 순으로 정렬한 뒤 상위 N개를 watchlist로 사용합니다. 조회 실패 시 `symbols`(수동 목록) 또는 기본 종목(005930)으로 fallback됩니다.
