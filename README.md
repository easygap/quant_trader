# QUANT TRADER

Python으로 만든 자동 주식 매매 쪽 실험 프로젝트입니다.  
데이터랑 규칙 기반으로 신호를 만들고, KIS(한국투자증권) API로 국내주식 시세·주문을 다루는 구조입니다.

---

## 주요 구성요소

- **기술적 지표**: RSI, MACD, 볼린저 밴드, 이동평균, 스토캐스틱, ADX, ATR, OBV 등
- **전략**: 멀티 지표 스코어링, 평균 회귀, 추세 추종
- **리스크**: 1% 룰, 손절/익절, 트레일링 스탑, MDD 제한
- **백테스트**: 과거 데이터로 전략 검증 (수수료·세금·슬리피지 반영)
- **알림·실시간**: 디스코드 웹훅, KIS 웹소켓 체결가

**Python 3.11 ~ 3.12** 사용을 권장합니다.  
프로젝트 패키지 정책도 `>=3.11,<3.13` 기준입니다. Python 3.14는 공식 지원 범위에 포함하지 않습니다.

### Cursor / VS Code에서 Python 3.12 쓰기

- 이 프로젝트는 `.vscode/settings.json`에서 **Python 3.12** 인터프리터를 지정합니다.  
- 기본 경로: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` (python.org 설치 시).  
- 3.12를 다른 경로에 설치했다면 **Ctrl+Shift+P** → "Python: Select Interpreter" → Python 3.12 선택.  
- 터미널에서도 3.12를 쓰려면: **선택한 인터프리터로 터미널 열기** 또는 PATH에 3.12를 먼저 두기.

---

## 설치

```bash
python -m pip install -r requirements.txt
```

---

## 설정

`config/` 폴더 안 YAML에서 기본 설정을 하고, 민감한 값은 환경 변수로 두는 방식을 쓰면 됩니다.  
예시: `config/settings.yaml.example`, `config/holidays.yaml.example`를 복사해 `config/settings.yaml` 등으로 두고 값을 채우면 됩니다. (`config/settings.yaml`은 `.gitignore` 대상이라 커밋되지 않습니다.)  
KIS API 키·계좌번호·디스코드 웹훅 등 비밀은 `.env`에 두고, `.env.example` 또는 `.env.template`을 참고하세요.  
실전 신규 진입을 자동으로 켜려면 `trading.auto_entry: true`를 명시적으로 설정하세요.

---

## 실행 예시

```bash
# 백테스트만
python main.py --mode backtest --strategy scoring --symbol 005930

# 백테스트 + 리포트 저장 경로 지정
python main.py --mode backtest --strategy scoring --symbol 005930 --output-dir reports

# 모의 매매
python main.py --mode paper --strategy scoring

# 실전 (이중 확인 필수: ENABLE_LIVE_TRADING=true + --confirm-live)
# 신규 진입 자동화는 config/settings.yaml 의 trading.auto_entry=true 일 때만 시도
python main.py --mode live --strategy scoring --confirm-live
```

---

## 테스트

```bash
pytest tests/ -q
```

`pyproject.toml`에 `testpaths = ["tests"]`와 서드파티 경고 필터가 설정되어 있습니다. KIS/WebSocket 등 일부 테스트는 모의 객체를 사용합니다.

---

## 폴더 구조

```
config/       설정 YAML
core/         데이터 수집, 지표, 신호, 리스크, 주문
strategies/   전략 구현
api/          KIS API·웹소켓
backtest/     백테스트·리포트
monitoring/   로그, 디스코드, 대시보드
database/     DB
tests/        단위·통합·모의 E2E 테스트
main.py       진입점
```

---

## 문제 해결

- **백테스트 시 데이터 수집 실패**: `config/settings.yaml`·`.env` 설정 후, `pip install FinanceDataReader` 또는 `yfinance` 설치되어 있는지 확인하세요. 실행 시 로그에 안내 메시지가 출력됩니다.

---

## 관련

- 이 저장소 루트에 **fintics** 폴더가 있으면, KIS 미국 주식 세금 보고서 PDF→홈텍스 엑셀 변환 등 Spring Shell CLI(`fintics-shell`)를 사용할 수 있습니다.  
- fintics는 `.gitignore`로 제외되어 있어, 기본 clone에는 포함되지 않을 수 있습니다. 별도 클론/서브모듈로 두고 사용하면 됩니다.  
- 프로젝트 구조·설정·알고리즘 상세 분석은 `reports/current_project_deep_report.md`를 참고하면 됩니다.

---

⚠ 실전에 쓰기 전에 모의로 충분히 돌려보시고, 투자 손실은 전적으로 사용자 책임입니다.
