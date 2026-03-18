# QUANT TRADER

국내 주식 자동매매를 공부하고 실험해보려고 만든 개인 프로젝트입니다.
지표 기반으로 매매 신호를 만들고, 백테스트부터 모의투자·실전 매매까지 한 흐름으로 실행할 수 있도록 구성했습니다.

현재는 KIS API를 사용하며, 기본적인 리스크 관리, 디스코드 알림, 실시간 대시보드를 지원합니다.

## 주요 기능

* 백테스트 / 모의투자 / 실전 매매
* RSI, MACD, 볼린저밴드, 스토캐스틱, ATR 기반 신호 생성
* 스코어링 / 평균회귀 / 추세추종 전략 지원
* 손절, 익절, 트레일링 스탑, 포지션 제한 등 기본 리스크 관리
* 디스코드 알림 및 KIS 웹소켓 기반 실시간 처리
* 웹 대시보드로 포트폴리오 상태 확인

## 사용 환경

* Python 3.11 ~ 3.12 권장
* 3.14는 아직 호환성 확인 전

## 설치

```bash
pip install -r requirements.txt
```

## 설정

실행 전 설정 파일과 환경변수를 준비해야 합니다.

* `config/settings.yaml.example` → `config/settings.yaml`
* `.env.example` 참고 후 `.env` 작성
* `config/holidays.yaml`은 없으면 자동 생성되며, 필요 시 갱신 가능

실전 자동 매매를 사용할 경우 아래 설정을 활성화해야 합니다.

```yaml
trading.auto_entry: true
```

## 실행

```bash
# 백테스트
python main.py --mode backtest --strategy scoring --symbol 005930

# 모의투자
python main.py --mode paper --strategy scoring

# 실전 매매
python main.py --mode live --strategy scoring --confirm-live

# 전략 검증
python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5

# 성과 비교
python main.py --mode compare --start 2025-01-01 --end 2025-03-18 --strategy scoring

# 파라미터 최적화
python main.py --mode optimize --strategy scoring

# 웹 대시보드
python main.py --mode dashboard
```

실전 매매는 `ENABLE_LIVE_TRADING=true` 설정이 필요합니다.

## 안정성 관련 메모

실전 사용을 고려해서 몇 가지 안전장치를 넣어두었습니다.

* 백테스트는 lookahead bias를 줄이도록 기본 설정 적용
* 자금 비중 제한과 동시 보유 종목 수 제한 지원
* 미체결 주문 확인 및 중복 주문 방지 로직 포함
* 전략 성과가 일정 기준 이하로 떨어지면 신규 진입 제한 가능
* DB 백업 및 잔고 크로스체크 지원
* 긴급 전체 청산 기능 제공

세부 설정은 `config/risk_params.yaml`과 `docs/PROJECT_GUIDE.md`에서 확인할 수 있습니다.

## 테스트

```bash
pytest tests/ -q
```

외부 API와 웹소켓이 필요한 부분은 모킹해서 테스트합니다.

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
* 상세 내용은 `docs/PROJECT_GUIDE.md`와 `reports/current_project_deep_report.md`에 정리해두었습니다.

## 주의

실전 매매 전에 반드시 백테스트와 모의투자로 충분히 검증한 뒤 사용하는 것을 권장합니다.
사용으로 인한 손실은 본인 책임입니다.
