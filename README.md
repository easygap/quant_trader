# QUANT TRADER

국내 주식 자동매매를 공부하고 실험해보려고 만든 개인 프로젝트입니다.
지표 기반으로 매매 신호를 만들고, 백테스트부터 모의투자·실전 매매까지 한 흐름으로 실행할 수 있도록 구성했습니다.

현재는 KIS API를 사용하며, 리스크 관리, 알림 이중화, 실시간 대시보드를 지원합니다.

> **현재 상태**: 인프라(리스크 관리, 장애 복구, 알림 이중화 등)는 프로덕션 수준에 가깝지만, **신호(Signal) 자체의 수익성이 검증되지 않은 상태**입니다. 현재 스코어링 가중치(RSI +2, MACD +2 등)는 직관·예시용이며, 이 상태로 실전 자동매매를 돌리면 수익보다 손실 가능성이 더 높습니다. 반드시 아래 "실전 투입 전 필수 사항"을 모두 완료한 후에만 실전 투입을 고려하세요.

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

# 바스켓 리밸런싱 (enabled=true인 모든 바스켓)
python main.py --mode rebalance

# 특정 바스켓만 리밸런싱 (dry-run: 실제 주문 없이 계획만 출력)
python main.py --mode rebalance --basket kr_blue_chip --dry-run

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
* 신호 히스터리시스: BUY↔HOLD↔SELL 순차 전환으로 임계값 근처 과매매 방지
* 최소 보유 기간 3일: 매수 후 즉시 매도 차단 (손절·블랙스완은 예외)
* 포지션 불일치 자동 보정: KIS↔DB 불일치 시 DB 자동 동기화 (설정으로 활성화)
* 시스템 헬스체크: 10분 주기 DB·API·디스크·메모리 자동 점검
* 휴장일 자동 갱신: 연초 또는 90일 경과 시 holidays.yaml 자동 업데이트
* 루프 모니터링: 실행 시간 추적, 연속 스킵 시 Discord 경고

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

## 실전 투입 전 필수 사항

아래 4가지를 모두 완료하기 전까지는 실전 투입을 하지 마세요.

1. **백테스트 유니버스 확인**: `risk_params.yaml`의 `backtest_universe.mode`를 `historical`로 설정 후 백테스트 재실행. `current` 상태라면 수익률이 수십 %p 과대평가되어 있을 수 있습니다.
2. **데이터 소스 고정**: `settings.yaml`에서 `data_source.preferred: fdr`, `allow_kis_fallback: false` 설정.
3. **가중치 최적화 파이프라인 완료**: 아래 3단계를 실제로 실행하고 OOS 샤프 ≥ 1.0을 달성한 가중치로 `strategies.yaml`을 업데이트.
   ```bash
   # STEP 1: 지표 독립성 검증
   python main.py --mode check_correlation --symbol 005930 --validation-years 5
   # STEP 2: 가중치+임계값 최적화
   python main.py --mode optimize --strategy scoring --include-weights --auto-correlation --symbol 005930
   # STEP 3: 워크포워드 안정성 검증
   python main.py --mode validate --walk-forward --strategy scoring --symbol 005930 --validation-years 5
   ```
4. **paper 모드 최소 1개월 운영**: `check_live_readiness` 조건(방향성 일치율 ≥ 70%, 수익률 차이 ≤ 5%p) 통과 확인.

## 알려진 한계 및 개선 계획

### 신호 품질 문제

* 현재 스코어링 가중치는 직관·예시용이며 통계적 근거 없음
* RSI, MACD, 볼린저, 이동평균은 모두 과거 가격의 변형 — 다중공선성 문제
* 앙상블의 technical과 momentum_factor가 실질적으로 같은 정보를 사용 — 허구적 다각화
* 200일선, ADX 25 등 파라미터가 미국 시장 기준 — 한국 시장 미최적화

### 최근 구현 완료

* 신호 히스터리시스 (진입/청산 임계값 분리, BUY↔HOLD↔SELL 순차 전환 강제)
* 최소 보유 기간 3일 (매수 후 3일 미만 매도 차단, 손절·블랙스완 예외)
* 포지션 불일치 자동 보정 (KIS 잔고 기준 DB 동기화)
* 시스템 헬스체크 자동화 (10분 주기), 휴장일 자동 갱신 (90일 주기)
* MACD 점수 3단계 체계 (크로스 당일 풀점수, 유지 중 반점수, 히스토그램 보너스)
* 트레일링 스톱 3→5%, 익절 6/10→4/8%, 슬리피지 틱 2→1로 수익 구조 개선
* 백테스터에 보유 기간 제한 + 당일 손절 후 재매수 방지 반영
* KIS 호출 제어 강화 (지수 백오프+지터, SSL 에러 전용 핸들러, 토큰 쿨다운)
* 주문 실패 Dead-letter 큐 (FailedOrder 테이블, 재처리 API)
* 전략 등록 레지스트리(플러그인형) — `create_strategy(name)`으로 동적 로딩
* **바스켓 포트폴리오 리밸런싱** — 종목별 목표 비중 관리, 드리프트/주기 기반 리밸런싱, 신호 가중 모드. `--mode rebalance --basket <name>`, `--dry-run`으로 미리보기 가능. 스케줄러 장전 단계 자동 통합

### 중기 개선 예정 (3~6개월)

* DART OpenAPI 연동 (실적 발표일·공시 필터 정확도 개선)
* 펀더멘털 독립 신호 추가 (PER·ROE 기반, 앙상블 진정한 다각화)
* 웹 대시보드 강화 (전략별 신호, 주문 목록, API 사용량 등)

상세 내용은 `quant_trader_design.md` §1.3, §4.5~4.7, §10을 참고하세요.

## 주의

실전 매매 전에 반드시 위 "실전 투입 전 필수 사항" 4가지를 모두 완료하세요.
현재 상태(직관값 가중치)로 실전 투입 시 노이즈를 실행하는 것과 같으며, 손실 가능성이 높습니다.
사용으로 인한 손실은 본인 책임입니다.
