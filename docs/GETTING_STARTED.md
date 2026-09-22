**한국어** · [English](en/GETTING_STARTED.md) · [日本語](ja/GETTING_STARTED.md) · [简体中文](zh-CN/GETTING_STARTED.md)

# 설치와 모의투자

프로그램을 설치하고, 가상 자금으로 첫 매매를 해 보는 방법입니다.

아래 명령은 **Windows PowerShell, Python 3.11 또는 3.12** 기준입니다. Python과 Git이 설치되어 있어야 합니다.

## 설치

```powershell
git clone https://github.com/easygap/quant_trader.git
cd quant_trader
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

개인 설정 파일을 만듭니다. 이미 파일이 있으면 기존 설정을 유지합니다.

```powershell
if (-not (Test-Path config/settings.yaml)) {
    Copy-Item config/settings.yaml.example config/settings.yaml
}
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

`config/settings.yaml`에서 `trading.mode`가 `paper`인지 확인하세요. 예제 파일의 기본값은 모의투자입니다. 이 모드의 매매는 프로그램 안에서 가상 자금으로 처리하며, 증권사에서 제공하는 모의투자 서비스와는 별개입니다.

한국투자증권 KIS API를 연결할 때는 키와 계좌번호를 `.env`에 입력합니다. 이 파일은 Git에 올리지 마세요. 실제 돈으로 주문하려면 [실제 계좌 연결 안내](PAPER_TO_LIVE_RUNBOOK.md)를 먼저 읽어 주세요.

## 계좌 화면 열기

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard
```

PowerShell 창을 켜 둔 채, 같은 PC의 브라우저에서 [127.0.0.1:8080](http://127.0.0.1:8080)을 엽니다. 이 명령으로 계좌 화면을 실행합니다. 자동매매는 별도로 실행해야 합니다.

처음 설치하면 저장된 거래가 없어 계좌가 비어 있습니다. README의 캡처는 기존 모의투자 계좌의 기록입니다. 설치 직후에는 캡처에 나온 금액과 수익률이 표시되지 않습니다.

계좌 선택, 날짜별 수익률, 적립금 입력은 [계좌 화면 사용법](USER_GUIDE.md)에 설명했습니다.

기본 접속 주소는 이 PC에서만 열립니다. 포트를 바꾸려면 다음처럼 실행합니다.

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard --dashboard-port 8081
```

이때는 [127.0.0.1:8081](http://127.0.0.1:8081)로 접속합니다.

## 모의투자

계좌 화면을 켜 둔 채 진행하려면 PowerShell 창을 새로 열고, 설치한 `quant_trader` 폴더로 이동하세요.

매매할 종목과 비중은 [config/baskets.yaml](../config/baskets.yaml)에서 정합니다. `kr_pocket`은 ETF 적립 계좌, `kr_diversified_hold`는 대형주 분산 계좌입니다.

먼저 어떤 주문을 낼지 확인합니다. 주가를 불러오므로 인터넷 연결이 필요합니다.

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket --dry-run
```

종목과 주문 수량을 확인한 뒤, **`trading.mode: paper` 상태에서** 모의투자를 실행합니다.

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket
```

실행 후 화면을 새로고침하면 계좌에 결과가 반영됩니다. 주가를 불러오지 못하거나 주문 조건에 맞지 않으면 매매하지 않습니다. 이유는 터미널과 실행 기록에서 확인할 수 있습니다.

계좌 화면의 **적립금 기록**으로 투자금을 추가할 수 있습니다. 모의투자에서는 가상 자금이 늘어납니다. 은행 계좌에서 돈이 이체되는 기능은 아닙니다. 기록하기 전에 계좌, 금액, 모의투자 여부를 확인하세요.

## 백테스트

과거 주가에 매매 규칙을 적용해 결과를 비교합니다.

```powershell
.\.venv\Scripts\python.exe tools/risk_review.py --as-of 2026-09-22
```

결과는 `reports/research/risk_review_20260922.json`, 그래프는 `docs/images/risk-review-20260922.png`에 저장됩니다. 이 명령으로 실제 주문이 나가지는 않습니다. 날짜를 바꾸면 파일 이름도 바뀝니다.

자료마다 최신 날짜가 다르면 모두 비교할 수 있는 날짜까지만 계산합니다. 9월 22일에 확인한 지수 자료는 17일까지 있어, 이 결과도 17일까지의 주가로 계산했습니다. 수익률과 거래 비용의 계산 조건은 [백테스트 결과](RISK_REVIEW_20260922.md)에 정리했습니다.

## 자동 실행과 실제 계좌 연결

- [자동 실행과 설정 파일 설명](PROJECT_GUIDE.md)
- [실제 계좌 연결과 주문 제한](PAPER_TO_LIVE_RUNBOOK.md)
- [거래가 중지됐을 때 확인할 것](SAFETY_MODEL.md)

[README로 돌아가기](../README.md)
