# QUANT TRADER

국내 주식 자동매매를 공부하고 실험해보려고 만든 개인 프로젝트입니다.
지표 기반으로 매매 신호를 만들고, 백테스트부터 모의투자/실전 매매까지 한 흐름으로 실행할 수 있게 구성했습니다.

현재는 KIS API를 사용하고, 기본적인 리스크 관리와 디스코드 알림 기능을 포함하고 있습니다.

최근 반영된 핵심 안전장치는 아래와 같습니다.

* 백테스트는 `strict-lookahead`가 기본값으로 강제됩니다.
* **자금 관리**: 1% 룰(종목당 최대 손실) 외에 **전체 자산의 주식 투자 비중 상한**(기본 70%)·**동시 보유 최대 종목 수**(기본 10개)를 함께 적용해, 다수 종목 동시 신호 시에도 전 재산이 주식에 투입되지 않도록 합니다. (`config/risk_params.yaml` diversification)
* 거래량 기반 동적 슬리피지: 일평균 거래량의 1% 이상 주문 시 슬리피지 자동 상향(한국 소형주 1~3% 반영).
* live 모드: 주문 전 **미체결 확인**(OrderGuard TTL + KIS 미체결 조회), 장중 루프 **10분 초과 시 다음 사이클 스킵**으로 타이밍·중복 주문 리스크를 줄입니다.
* **watchlist 자동 선정**: 수작업 없이 **시가총액 상위 N개**(`top_market_cap`) 또는 **코스피200 구성 유사**(`kospi200`)로 관심 종목을 자동 포함. `config/settings.yaml`에서 `watchlist.mode`, `market`, `top_n` 설정.
* **전략 성과 자동 열화 감지**: 시장 국면이 바뀌면 기존 전략이 손실을 낼 수 있음. **최근 N거래의 승률이 임계값 아래로 떨어지면 신규 매수 자동 중단**(손절/익절은 유지). `risk_params.yaml`의 `performance_degradation`(recent_trades, min_win_rate)로 설정.
* **긴급 전체 청산**: 수동으로 즉시 전 종목 매도. 블랙스완 외 수동 개입 시 **CLI** `python main.py --mode liquidate`. 선택적으로 **HTTP 트리거**(`python -m monitoring.liquidate_trigger`)로 디스코드 봇·원격에서 호출 가능.
* **DB 백업 자동화 + KIS 잔고 크로스체크**: SQLite 손상 시 포지션 정보 전체 소실 방지를 위해 **일일 자동 백업**(`database.backup_path` 설정 시 장마감 후 실행). live 모드에서는 **KIS 잔고와 DB 포지션 상시 크로스체크**(장 시작 전·장중 주기·장마감), 불일치 시 로깅·알림.
* **KIS API 토큰 만료·갱신 실패 시 즉시 디스코드 알림**: 토큰이 만료되고 자동 갱신에 실패하면 실전 모드에서 주문이 조용히 실패할 수 있음. 이 경우 **즉시 디스코드(및 Notifier 치명 알림)** 로 알려 주문 실패를 인지할 수 있음.
* **모의투자 vs 백테스트 자동 비교**: 지정 기간의 모의투자 성과와 동일 기간·동일 전략 백테스트 결과를 비교해, 수익률·승률 차이가 크면 **구현 버그 또는 데이터 문제 신호**로 간주하고 경고(및 선택 시 디스코드 알림). `--mode compare --start YYYY-MM-DD --end YYYY-MM-DD`로 실행. 임계값은 `config/risk_params.yaml`의 `paper_backtest_compare`에서 설정.
* **거래세/양도소득세 처리**: 국내 주식은 **증권거래세 0.18%**(매도 시 의무)를 `risk_params.yaml`의 `transaction_costs.tax_rate`로 반영. **양도소득세**는 대주주 해당 시에만 적용되며, `transaction_costs.capital_gains_tax.enabled`·`rate`로 설정 가능(기본 비활성). 설정 누락 시 실제 수익과 백테스트 괴리가 발생하므로 반드시 확인. 배당소득세(15.4% 등)는 배당 수령 시 해당(별도 연동).
* **최대 보유 기간**: 신호가 없어도 **N일 초과 보유 포지션은 강제 정리**(물리는 상황 방지). `risk_params.yaml`의 `position_limits.max_holding_days`(0이면 비활성, 기본 30일). 스케줄러(실전) 및 페이퍼 1회 실행 시 모두 적용.
* **휴장일 파일 자동 갱신**: `holidays.yaml`을 매년 수동 관리하지 않도록 **pykrx + fallback**으로 갱신. `python main.py --update-holidays` 실행 시 `config/holidays.yaml` 생성·갱신. 파일이 없으면 첫 로드 시 자동 생성 시도. pykrx만 의존하면 불안정하므로 fallback 목록으로 보완.
* **다중 계좌 분리(전략별)**: 전략A용/전략B용 계좌를 나누어 보유·매매 가능. `settings.yaml`의 `kis_api.accounts`에 전략명별 계좌번호 설정(예: `scoring`, `mean_reversion`). `--strategy scoring` 실행 시 해당 계좌·DB 계좌키로만 포지션·거래 기록 분리. 환경변수 `KIS_ACCOUNT_NO_SCORING` 등으로 덮어쓰기 가능.
* **전략 파라미터 자동 최적화**: Grid Search 또는 Bayesian Optimization으로 전략 파라미터 탐색. `--mode optimize --strategy scoring` (기본 Grid), `--optimizer bayesian` 시 `scikit-optimize` 사용. **오버피팅 주의**: `--train-ratio`로 학습 구간만 최적화하고, OOS 구간 성과를 함께 출력해 과적합 여부를 반드시 확인할 것.
* **실시간 웹 대시보드**: 콘솔 대시보드를 확장한 웹 UI. 포트폴리오 요약·보유 포지션·최근 30일 수익률 추이를 한 페이지에 표시하고 10초 간격으로 자동 갱신. `python main.py --mode dashboard` (기본 http://127.0.0.1:8080). `config/settings.yaml`의 `dashboard.port`·`dashboard.host`로 변경 가능.

## 사용 환경

* Python 3.11 ~ 3.12 권장
* 3.14는 호환성 확인 전

## 설치

```bash
pip install -r requirements.txt
```

## 설정

실행 전에 설정 파일과 환경변수를 먼저 준비해야 합니다.

* `config/settings.yaml.example` → `config/settings.yaml`
* `config/holidays.yaml`은 없으면 첫 실행 시 자동 생성되며, `python main.py --update-holidays`로 연도별 갱신 가능 (예시는 `holidays.yaml.example` 참고)
* `.env.example` 참고해서 `.env` 작성

실전 자동 매매를 사용할 경우에는 설정에서 아래 값을 활성화해야 합니다.

```yaml
trading.auto_entry: true
```

대표 설정:

```yaml
trading:
  pending_order_ttl_seconds: 600

watchlist:
  mode: "top_market_cap"
  market: "KOSPI"
  top_n: 20
```

## 실행

```bash
# 백테스트
python main.py --mode backtest --strategy scoring --symbol 005930

# 위험 옵션: 빠르지만 미래 데이터 혼입 가능
python main.py --mode backtest --strategy scoring --symbol 005930 --allow-lookahead

# 전략 검증 (in-sample / out-of-sample + 코스피 벤치마크 비교)
python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5

# 백테스트 결과 저장
python main.py --mode backtest --strategy scoring --symbol 005930 --output-dir reports

# 모의투자
python main.py --mode paper --strategy scoring

# 실전 매매
python main.py --mode live --strategy scoring --confirm-live

# 긴급 전체 청산 (수동 개입·블랙스완 외 즉시 전 종목 매도)
python main.py --mode liquidate

# 모의투자 vs 백테스트 비교 (차이 크면 버그/데이터 문제 신호)
python main.py --mode compare --start 2025-01-01 --end 2025-03-18 --strategy scoring

# 휴장일 파일 자동 갱신 (pykrx + fallback, 매년 수동 관리 불필요)
python main.py --update-holidays

# 전략 파라미터 최적화 (Grid Search, 오버피팅 주의·OOS 확인 권장)
python main.py --mode optimize --strategy scoring --start 2020-01-01 --end 2025-12-31
# Bayesian 최적화 (pip install scikit-optimize)
python main.py --mode optimize --strategy scoring --optimizer bayesian --optimize-metric sharpe_ratio --train-ratio 0.7

# 실시간 웹 대시보드 (포트폴리오·포지션·수익률 추이, 10초 간격 갱신)
python main.py --mode dashboard
# 포트 지정: python main.py --mode dashboard --dashboard-port 9090
```

원격/디스코드에서 청산을 걸고 싶다면 `LIQUIDATE_TRIGGER_TOKEN`을 설정한 뒤 `python -m monitoring.liquidate_trigger`로 HTTP 서버를 띄우고, 해당 URL로 POST하면 됩니다. 자세한 내용은 `docs/PROJECT_GUIDE.md` § 긴급 전체 청산 참고.

실전 매매는 `ENABLE_LIVE_TRADING=true` 설정이 필요합니다.

## 전략 검증 원칙 (실전 투입 전 필수)

**전략이 실제로 수익을 내는지**가 가장 중요합니다. 스코어링·평균회귀·추세추종 전략은 한국 시장에서 검증된 근거가 없으며, RSI/MACD 등 단독 사용 시 랜덤과 수익률 차이가 크지 않다는 연구가 많습니다. 아래를 모두 확인한 뒤 실전에 투입하세요.

* **최소 3~5년** 데이터로 백테스트. 샤프 비율 **1.0 이상**, MDD **-20% 이내** 확인
* **벤치마크(코스피 지수 단순 매수)** 와 반드시 비교해 초과 수익 여부 확인
* **오버피팅 방지**: `--mode validate`로 **in-sample / out-of-sample 구간 분리** 검증 (기본 5년, 3년 미만 시 3년 적용)
* **strict-lookahead 기본 사용** — `--allow-lookahead` 사용 시 수익률이 크게 떨어지면 기존 결과 신뢰 불가
* 모의투자 **최소 1~2개월** 운영 후 실전 전환 (모의 결과가 백테스트와 방향성 일치 확인)
* 첫 실전 투입 금액은 **전체 운용 예정 금액의 10% 이하**
* KIS 모의투자 환경에서 API 연결·주문·잔고 조회 **E2E 테스트** 완료

자세한 검증 항목과 체크리스트는 `docs/PROJECT_GUIDE.md` §6·§7 참고.

## 테스트

```bash
pytest tests/ -q
```

외부 API나 웹소켓이 필요한 부분은 모킹해서 테스트합니다.

## 프로젝트 구조

* `config/` : 설정 파일
* `core/` : 데이터 수집, 지표 계산, 신호 생성, 리스크 관리, 주문 처리
* `strategies/` : 매매 전략
* `api/` : KIS 연동
* `backtest/` : 백테스트 및 리포트
* `database/` : DB 관련 코드
* `tests/` : 테스트 코드
* `main.py` : 실행 진입점

## 참고

* 백테스트 데이터가 비어 있으면 설정 파일과 `.env` 값을 먼저 확인해주세요.
* `FinanceDataReader` 또는 `yfinance` 설치 여부도 같이 확인하면 됩니다.
* 자세한 내용은 `reports/current_project_deep_report.md`에 정리해두었습니다.

## 주의

실전 매매 전에 반드시 모의투자와 백테스트로 충분히 확인한 뒤 사용하는 것을 권장합니다.
사용으로 인한 손실은 본인 책임입니다.
