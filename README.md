# QUANT TRADER

국내 주식 자동매매를 공부하고 실험해보려고 만든 개인 프로젝트입니다.
지표 기반으로 매매 신호를 만들고, 백테스트부터 모의투자/실전 매매까지 한 흐름으로 실행할 수 있게 구성했습니다.

현재는 KIS API를 사용하고, 기본적인 리스크 관리와 디스코드 알림 기능을 포함하고 있습니다.

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
* `config/holidays.yaml.example` → 필요한 경우 복사해서 사용
* `.env.example` 참고해서 `.env` 작성

실전 자동 매매를 사용할 경우에는 설정에서 아래 값을 활성화해야 합니다.

```yaml
trading.auto_entry: true
```

## 실행

```bash
# 백테스트
python main.py --mode backtest --strategy scoring --symbol 005930

# 백테스트 결과 저장
python main.py --mode backtest --strategy scoring --symbol 005930 --output-dir reports

# 모의투자
python main.py --mode paper --strategy scoring

# 실전 매매
python main.py --mode live --strategy scoring --confirm-live
```

실전 매매는 `ENABLE_LIVE_TRADING=true` 설정이 필요합니다.

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
