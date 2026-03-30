# QUANT TRADER

국내 주식 자동매매를 공부하고 실험보려고 만든 개인 프로젝트입니다.  
지표·펀더멘털 기반으로 매매 신호를 만들고, 백테스트부터 모의투자·실전까지 한 흐름으로 돌릴 수 있게 짜 두었습니다.

실전 주문과 잔고 조회는 **KIS API**를 씁니다. 일봉 수집은 한국(FDR·yfinance·KIS 폴백)과 미국 티커(yfinance)를 쓸 수 있고, 리스크·알림·대시보드·리밸런싱 등은 붙여가며 확장 중입니다.

> 인프라 쪽은 어느 정도 손을 댔지만, **신호가 실제로 돈이 되는지는 아직 충분히 검증하지 않은 상태**로 보는 게 맞습니다. 실전 자동매매보다는 백테스트·모의 중심으로 검증하고 고치는 용도로 생각해 주세요.

## 주요 기능

**실행 흐름**  
백테스트 · 전략 검증(워크포워드 등) · 모의 1회(`paper`) · 모의 상시 루프(`schedule`) · 실전(`live`) · 성과 비교 · 파라미터 최적화 · 지표/앙상블 상관 분석 · 바스켓 리밸런싱 · 웹 대시보드 · 긴급 청산 등 — `python main.py --mode …`로 분기합니다. 모드·진입점 표는 [`docs/PROJECT_GUIDE.md`](docs/PROJECT_GUIDE.md)를 보세요.

**전략·신호**  
기술적 지표(RSI, MACD, 볼린저, 스토캐스틱, ADX, ATR, OBV 등) 기반 스코어링, 평균회귀, 추세추종, 펀더멘털 팩터, 앙상블(구성은 `config/strategies.yaml`의 `ensemble`).

**운용·리스크**  
워치리스트(시총·팩터 모드 등), 손절·익절·트레일링, 포지션·업종 비중, 시장 국면 필터, 블랙스완 대응, 어닝 필터(yfinance + 선택 DART), 알림(디스코드 → 텔레그램 → 이메일).

## 사용 환경

* Python 3.11 ~ 3.12 (`pyproject.toml`: `>=3.11,<3.13`)

## 설치

```bash
pip install -r requirements.txt
```

## 설정

* `config/settings.yaml.example` → `config/settings.yaml`
* `.env.example` 참고 후 `.env` (KIS 키, 알림, 선택: `DART_API_KEY` 등)
* `config/holidays.yaml`(한국)은 없으면 자동 생성될 수 있고, `python main.py --update-holidays`로 갱신 가능
* 미국 휴장일이 필요하면 `config/us_holidays.yaml`을 추가

실전에서 장중 자동 진입을 쓰려면 예를 들어:

```yaml
trading.auto_entry: true
```

## 실행

```bash
# 백테스트
python main.py --mode backtest --strategy scoring --symbol 005930

# 모의투자 (워치리스트 1회)
python main.py --mode paper --strategy scoring

# 모의 스케줄 루프 (상시 구동용)
python main.py --mode schedule --strategy scoring

# 실전 (환경변수 + 플래그 둘 다 필요)
python main.py --mode live --strategy scoring --confirm-live

# 전략 검증
python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5

# 성과 비교
python main.py --mode compare --start 2025-01-01 --end 2025-03-19 --strategy scoring

# 파라미터 최적화 (예: 스코어링 가중치까지)
python main.py --mode optimize --strategy scoring --include-weights --auto-correlation

# 바스켓 리밸런싱
python main.py --mode rebalance
python main.py --mode rebalance --basket kr_blue_chip --dry-run

# 웹 대시보드
python main.py --mode dashboard

# 휴장일 갱신
python main.py --update-holidays
```

실전 `live`는 **`ENABLE_LIVE_TRADING=true`** 와 **`--confirm-live`** 가 함께 있어야 합니다.

## 안정성 관련

백테스트 look-ahead 완화(strict 기본), 포지션·비중 제한, 미체결·중복 주문 방지, 성과 열화 시 진입 제한, 시장 국면·블랙스완, DB 백업·잔고 크로스체크, 긴급 청산, 알림 채널 폴백 등을 넣어 두었습니다. 세부 키는 `config/risk_params.yaml`·`settings.yaml`을 참고하세요.

## 테스트

```bash
pytest tests/ -q
```

외부 API·웹소켓이 필요한 부분은 모킹합니다.

## 프로젝트 구조

* `config/` — 설정
* `core/` — 데이터, 지표, 신호, 리스크, 주문, 스케줄러, 알림
* `strategies/` — 전략
* `api/` — KIS REST·웹소켓
* `backtest/` — 백테스트, 검증, 최적화, 비교
* `database/` — 모델·백업
* `monitoring/` — 로깅, 알림, 대시보드, 청산 트리거
* `tests/` — 테스트
* `docs/` — 문서
* `deploy/` — (선택) 서버 상시 구동 예시

## 더 읽을 곳

| 문서 | 내용 |
|------|------|
| [`docs/PROJECT_GUIDE.md`](docs/PROJECT_GUIDE.md) | 파일 역할, 모드별 흐름, 설정 요약, 실전 전 체크리스트 |
| [`quant_trader_design.md`](quant_trader_design.md) | 아키텍처, 지표·전략·리스크 설계, 검증 관점, 로드맵 |
| [`docs/BACKTEST_IMPROVEMENT.md`](docs/BACKTEST_IMPROVEMENT.md) | 백테스트 손익 개선 포인트(손익비·상승장·손절/익절·가중치 파이프라인) |

## 주의

실전 투입 전에는 백테스트·검증·모의를 충분히 거친 뒤 쓰는 것을 권장합니다. **지금 단계에서는 신호 품질 검증이 운영 안정성보다 더 중요합니다.** 사용으로 인한 손실은 본인 책임입니다.
