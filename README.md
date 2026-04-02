# QUANT TRADER

국내 주식 자동매매를 공부하고 실험보려고 만든 개인 프로젝트입니다.  
지표·펀더멘털 기반으로 신호를 만들고, 백테스트부터 모의투자·실전 매매까지 한 흐름으로 실행할 수 있게 구성했습니다.

실전 주문과 잔고 조회는 KIS API를 사용합니다.  
데이터 수집, 리스크 관리, 알림, 대시보드, 리밸런싱 기능도 함께 붙여가며 확장하고 있습니다.

> **현재 상태 (2026-04-02)**: BV50/R50 멀티전략 sleeve(거래량 돌파 + 상대강도 회전) Paper Trading 가동 중 (2026-04-01 개시, 목표 60영업일).  
> 인프라는 프로덕션 수준에 가깝고, Paper 운영 결과에 따라 실전 전환 여부를 판단할 예정입니다.

## 주요 기능

- 백테스트 / 포트폴리오 백테스트 / 멀티전략 sleeve 비교
- 모의투자 / 실전 매매
- 전략 검증 / 성과 비교 / 파라미터 최적화
- 스코어링 / 평균회귀 / 추세추종 / 펀더멘털 / 앙상블 전략
- 거래량 돌파(C-4) / 상대강도 회전(C-5) / 2-sleeve 포트폴리오 (BV50/R50 **Paper 가동 중**)
- 리스크 관리, 알림, 바스켓 리밸런싱, 웹 대시보드

## 사용 환경

- Python 3.11 ~ 3.12

## 설치

```bash
pip install -r requirements.txt
```

## 설정

실행 전에 설정 파일과 환경변수를 먼저 준비해야 합니다.

- `config/settings.yaml.example` → `config/settings.yaml`
- `.env.example` 참고 후 `.env` 작성
- `config/holidays.yaml`은 필요 시 갱신 가능
- 미국 휴장일이 필요하면 `config/us_holidays.yaml` 추가

실전 자동 진입을 사용할 경우에는 아래 설정이 필요합니다.

```yaml
trading.auto_entry: true
```

## 실행

```bash
# 단일 종목 백테스트
python main.py --mode backtest --strategy scoring --symbol 005930

# 포트폴리오 백테스트
python main.py --mode portfolio_backtest --strategy scoring --symbols 005930,000660 --start 2023-01-01 --end 2024-12-31

# 거래량 돌파 전략 (C-4) 포트폴리오 백테스트
python main.py --mode portfolio_backtest --strategy breakout_volume --symbols 005930,000660,035720,051910 --start 2024-01-01 --end 2025-12-31

# 멀티전략 sleeve 비중 스윕 (C-5)
python scripts/c5_weight_sweep.py

# paper trading 월간 리포트 (BV50/R50 guardrail 모니터링)
python scripts/c5_paper_monthly_report.py

# paper trading 특정 기간 리포트
python scripts/c5_paper_monthly_report.py 2026-04-01 2026-04-02

# 모의투자
python main.py --mode paper --strategy scoring

# 모의 스케줄 루프
python main.py --mode schedule --strategy scoring

# 실전 매매
python main.py --mode live --strategy scoring --confirm-live

# 전략 검증
python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5

# 성과 비교
python main.py --mode compare --start 2025-01-01 --end 2025-03-19 --strategy scoring

# 파라미터 최적화
python main.py --mode optimize --strategy scoring --include-weights --auto-correlation

# 바스켓 리밸런싱
python main.py --mode rebalance

# 웹 대시보드
python main.py --mode dashboard

# 휴장일 갱신
python main.py --update-holidays
```

실전 매매는 `ENABLE_LIVE_TRADING=true` 설정과 `--confirm-live` 옵션이 함께 필요합니다.

## 리스크 관리

기본적인 안전장치는 넣어두었습니다.

- look-ahead 완화 백테스트
- 포지션 수 / 자금 비중 제한
- 미체결 / 중복 주문 방지
- 성과 열화 시 진입 제한
- 시장 국면 / 블랙스완 대응
- DB 백업 / 잔고 크로스체크 / 긴급 청산
- 알림 채널 fallback

세부 설정은 `config/risk_params.yaml`, `config/strategies.yaml`, `config/settings.yaml`에서 관리합니다.

## 테스트

```bash
pytest tests/ -q
```

외부 API나 웹소켓이 필요한 부분은 모킹해서 테스트합니다.

## 프로젝트 구조

* `config/` — 설정
* `core/` — 데이터, 지표, 신호, 리스크, 주문, 스케줄러, 알림
* `strategies/` — 전략 (scoring, breakout_volume, relative_strength_rotation 등)
* `scripts/` — 검증 스크립트 (C-4 OOS, C-5 sleeve 비교/비중 스윕/필터 테스트/rolling WF/paper 리포트)
* `api/` — KIS REST·웹소켓
* `backtest/` — 백테스트, 검증, 최적화, 비교
* `database/` — 모델·백업
* `monitoring/` — 로깅, 알림, 대시보드, 청산 트리거
* `tests/` — 테스트
* `docs/` — 문서
* `deploy/` — (선택) Oracle Cloud ARM 서버 상시 구동(systemd, logrotate)

## 문서

| 문서 | 내용 |
|------|------|
| [`docs/PROJECT_GUIDE.md`](docs/PROJECT_GUIDE.md) | 파일 역할, 모드별 흐름, 설정 요약, 실전 전 체크리스트 |
| [`quant_trader_design.md`](quant_trader_design.md) | 아키텍처, 지표·전략·리스크 설계, 검증 관점, 로드맵 |
| [`BACKTEST_IMPROVEMENT.md`](BACKTEST_IMPROVEMENT.md) | 백테스트 신뢰성 개선 내역, 알려진 한계, 추가 과제 |
| [`deploy/README.md`](deploy/README.md) | Oracle Cloud Free Tier ARM 배포·systemd 상시 구동 가이드 |

## 주의

실전 투입 전에는 백테스트, 검증, 모의투자를 충분히 거친 뒤 사용하는 것을 권장합니다.  
현재 BV50/R50 Paper Trading이 가동 중이며 (2026-04-01~, 목표 60영업일), 일간 운영 로그(`paper_log.txt`)와 guardrail 모니터링으로 전략 유효성을 검증하고 있습니다.
