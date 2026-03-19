# QUANT TRADER

국내 주식 자동매매를 공부하고 실험해보려고 만든 개인 프로젝트입니다.
지표 기반으로 매매 신호를 만들고, 백테스트부터 모의투자·실전 매매까지 한 흐름으로 실행할 수 있도록 구성했습니다.

현재는 KIS API를 사용하며, 리스크 관리, 알림 이중화, 실시간 대시보드를 지원합니다.

## 주요 기능

* 백테스트 / 모의투자 / 실전 매매 (10개 CLI 모드)
* RSI, MACD, 볼린저밴드, 스토캐스틱, ADX, ATR, OBV 기반 신호 생성
* 스코어링 / 평균회귀 / 추세추종 전략 + 앙상블(기술지표 + 모멘텀 팩터 + 변동성 조건)
* 워치리스트: 시가총액·코스피200·모멘텀·저변동성 팩터 모드 + 유동성 필터 + 리밸런싱 캐시
* 시장 국면 필터: 3중 신호(200일선 + 단기 모멘텀 + MA 크로스) 단계적 대응(bearish/caution/bullish)
* 블랙스완 감지 → 전량 매도 → 쿨다운 → 점진적 재진입(recovery)
* 실적 발표일(어닝) 필터: 전후 N일 신규 매수 금지
* 펀더멘털 필터(PER·부채비율): pykrx → yfinance 폴백
* 전략 검증: KS11 + 코스피 상위 50 동일비중 벤치마크, 워크포워드 검증
* 지표·앙상블 상관계수 분석 및 다중공선성 자동 검출
* 파라미터 최적화: Grid / Bayesian + 가중치 대칭 Grid Search + OOS 샤프 게이트
* 알림 이중화: 디스코드 → 텔레그램 → 이메일 (critical 시 전채널 동시 발송)
* 손절, 익절, 트레일링 스탑, 업종별 비중 제한 등 리스크 관리
* 모의투자 vs 백테스트 비교 + 실전 전환 준비 자동 평가
* 웹 대시보드(aiohttp)로 포트폴리오 상태 확인

## 사용 환경

* Python 3.11 ~ 3.12 (`pyproject.toml`: `>=3.11,<3.13`)

## 설치

```bash
pip install -r requirements.txt
```

## 설정

실행 전 설정 파일과 환경변수를 준비해야 합니다.

* `config/settings.yaml.example` → `config/settings.yaml` 복사 후 수정
* `.env.example` 참고 후 `.env` 작성 (KIS API 키, 디스코드 웹훅 등)
* `config/holidays.yaml`은 없으면 자동 생성되며, `--update-holidays`로 갱신 가능

실전 자동 매매를 사용할 경우 아래 설정을 활성화해야 합니다.

```yaml
trading.auto_entry: true
```

## 실행

```bash
# 백테스트
python main.py --mode backtest --strategy scoring --symbol 005930
python main.py --mode backtest --strategy ensemble --symbol 005930

# 모의투자
python main.py --mode paper --strategy scoring

# 실전 매매 (ENABLE_LIVE_TRADING=true 필수)
python main.py --mode live --strategy scoring --confirm-live

# 전략 검증
python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5
python main.py --mode validate --walk-forward --strategy scoring --symbol 005930 --validation-years 6

# 지표·앙상블 상관 분석
python main.py --mode check_correlation --symbol 005930 --validation-years 5
python main.py --mode check_ensemble_correlation --symbol 005930 --validation-years 5

# 파라미터 최적화 (가중치 포함, 상관 분석 자동 연동)
python main.py --mode optimize --strategy scoring --include-weights --auto-correlation

# 성과 비교 + 실전 전환 준비 평가
python main.py --mode compare --start 2025-01-01 --end 2025-03-19 --strategy scoring

# 긴급 전체 청산
python main.py --mode liquidate

# 웹 대시보드
python main.py --mode dashboard

# 휴장일 갱신
python main.py --update-holidays
```

## 안정성 관련 메모

실전 사용을 고려해서 몇 가지 안전장치를 넣어두었습니다.

* 백테스트는 strict-lookahead 기본 적용 (look-ahead bias 방지)
* 자금 비중·업종별 비중 제한과 동시 보유 종목 수 제한
* 미체결 주문 확인 및 중복 주문 방지(OrderGuard, TTL)
* 전략 성과 열화 감지 시 신규 진입 자동 제한
* 시장 국면 필터: 3중 신호 단계적 대응 (bearish 시 매수 전면 중단, caution 시 사이징 축소)
* 블랙스완 감지 → 전량 매도 → 쿨다운 → 점진적 재진입
* 실적 발표일 전후 신규 매수 금지
* KIS API 이중 Rate Limiter (초당 + 분당) + Circuit Breaker
* DB 백업(SQLite Online Backup API) 및 잔고 크로스체크
* 알림 이중화: 디스코드 장애 시 텔레그램·이메일 fallback
* 긴급 전체 청산 기능 (CLI + HTTP 트리거)

`config/risk_params.yaml`에서 생존자 편향 완화(`backtest_universe`), 유동성 필터(`liquidity_filter`) 등을 설정할 수 있습니다.

## 테스트

```bash
pytest tests/ -q
```

14개 테스트 파일로 지표, 신호, 리스크, 스케줄러, 거래시간, 블랙스완, OrderExecutor, KIS 웹소켓 등을 검증합니다. 외부 API는 모킹 처리합니다.

## 프로젝트 구조

* `config/` : YAML 설정 파일 + 로더
* `core/` : 데이터 수집, 지표 계산, 신호 생성, 리스크 관리, 주문 실행, 스케줄러, 알림
* `strategies/` : 매매 전략 (스코어링, 평균회귀, 추세추종, 모멘텀, 변동성, 앙상블)
* `api/` : KIS REST API + 웹소켓 + Circuit Breaker
* `backtest/` : 백테스트, 전략 검증, 파라미터 최적화, 성과 비교
* `database/` : ORM 모델, CRUD, 백업
* `monitoring/` : 로깅, 디스코드, 웹 대시보드, 긴급 청산 트리거
* `tests/` : pytest 테스트
* `docs/` : 프로젝트 가이드

상세 내용은 `docs/PROJECT_GUIDE.md`와 `quant_trader_design.md`에 정리해두었습니다.

## 주의

실전 매매 전에 반드시 백테스트와 모의투자로 충분히 검증한 뒤 사용하는 것을 권장합니다.
사용으로 인한 손실은 본인 책임입니다.
