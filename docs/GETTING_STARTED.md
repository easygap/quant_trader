# 눈금 시작하기

눈금은 내 PC에서 실행하는 국내 주식·ETF 자동매매 프로그램입니다. 먼저 화면을 열어 설정을 확인하고, 모의투자로 계좌 기록을 쌓을 수 있습니다.

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

`config/settings.yaml`에서 `trading.mode`가 `paper`인지 확인하세요. 예제 파일의 기본값은 모의투자입니다. KIS API 키와 계좌번호 등 개인 정보는 `.env`에 입력하며, 이 파일은 Git에 올리지 않습니다. 실계좌 연결 설정은 [실전 전환 절차](PAPER_TO_LIVE_RUNBOOK.md)에서 따로 다룹니다.

## 계좌 화면 열기

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard
```

브라우저에서 [127.0.0.1:8080](http://127.0.0.1:8080)을 엽니다. 이 명령은 계좌를 보여 주는 웹 서버를 실행합니다. 자동매매는 별도로 실행해야 합니다.

처음 설치한 계좌에는 기록이 없으므로 빈 화면 안내가 나옵니다. README의 캡처는 이미 운용 중인 모의투자 계좌의 실제 기록입니다. 설치만으로 같은 금액과 수익률이 채워지는 것은 아닙니다.

기본 접속 주소는 이 PC에서만 열립니다. 포트를 바꾸려면 다음처럼 실행합니다.

```powershell
.\.venv\Scripts\python.exe main.py --mode dashboard --dashboard-port 8081
```

## 첫 모의투자 실행

계좌 구성은 [config/baskets.yaml](../config/baskets.yaml)에 있습니다. `kr_pocket`은 ETF 적립 계좌, `kr_diversified_hold`는 대형주 분산 계좌입니다.

먼저 주문 계획만 확인합니다. 가격 자료를 조회하므로 인터넷 연결이 필요합니다.

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket --dry-run
```

설정과 계획을 확인한 뒤, **`trading.mode: paper` 상태에서** 같은 계좌의 모의투자를 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe main.py --mode rebalance --basket kr_pocket
```

화면을 새로고침하면 생성된 계좌 기록을 확인할 수 있습니다. 가격 자료나 주문 조건을 확인하지 못하면 실행을 보류할 수 있으며, 이유는 터미널과 운영 기록에 표시됩니다.

계좌 화면의 **적립금 기록**은 추가한 투자금을 원금에 반영하는 기능입니다. 모의투자에서는 가상 자금을 더하며, 실제 은행 계좌에서 돈을 이체하지 않습니다. 계좌·금액·모의/실전 구분을 확인한 뒤 기록하세요.

## 과거 데이터로 비교하기

```powershell
.\.venv\Scripts\python.exe tools/risk_review.py --as-of 2026-09-22
```

결과는 `reports/research/risk_review_20260922.json`, 그래프는 `docs/images/risk-review-20260922.png`에 저장됩니다. 이 명령은 과거 종가로 비교하며 주문을 내지 않습니다. 날짜를 바꾸면 파일 이름도 해당 날짜로 바뀝니다.

자료마다 마지막 제공일이 다르면 모든 자료가 있는 날짜까지만 비교합니다. 9월 22일 조사에서는 지수 자료가 17일까지 제공되어 17일이 비교 종료일입니다. 수익률, 비용 가정과 한계는 [검증 보고서](RISK_REVIEW_20260922.md)에 정리했습니다.

## 계속 운용하려면

- 자동 실행과 설정 구조: [프로젝트 가이드](PROJECT_GUIDE.md)
- 실전 전환과 주문 제한: [실전 전환 절차](PAPER_TO_LIVE_RUNBOOK.md)
- 오류나 거래 중지 시 확인할 것: [거래 안전장치](SAFETY_MODEL.md)

[README로 돌아가기](../README.md)
