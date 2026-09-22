<img src="monitoring/static/nungum-symbol.svg" alt="" width="40">

# 눈금

**국내 주식·ETF 자동매매**

정해 둔 종목과 비중에 맞춰 주식과 ETF를 사고파는 프로그램입니다. 계좌 화면에서 투자금, 수익률, 보유 종목을 볼 수 있습니다.

내 PC에 설치해서 사용하며, 모의투자로 시작합니다.

[설치하기](docs/GETTING_STARTED.md) · [사용법](docs/USER_GUIDE.md) · [백테스트](#백테스트) · [문의](https://github.com/easygap/quant_trader/issues/new/choose)

## 수익률

![날짜별 투자금과 수익률, 입체·평면 차트](docs/images/readme-walkthrough-20260922.gif)

모의투자 화면 · 2026.09.22 · [정지 화면](docs/images/readme-account-20260922.png)

날짜를 고르면 그날의 금액과 수익률이 나옵니다. 추가로 넣은 돈은 원금으로 따로 계산합니다. 차트 아래 **기록 내려받기**를 누르면 날짜별 내역을 CSV 파일로 저장할 수 있습니다.

## 보유 종목

![대형주 계좌의 보유 수량, 평균 매수가, 종목별 비중](docs/images/readme-holdings-20260922.png)

종목별 보유 수량과 평균 매수가, 투자 비중을 보여 줍니다. 지금 비중이 처음 정한 목표와 얼마나 다른지도 바로 비교할 수 있습니다.

비중은 매수한 금액으로 계산합니다. 현재 가격을 반영한 평가금액과 손익은 표 아래에 있습니다.

## 자동매매

**ETF 적립**과 **대형주 분산 투자**, 두 가지 기본 설정이 들어 있습니다. 종목과 투자 비중은 직접 바꿀 수 있습니다. ETF 적립 설정은 현재 모의투자 전용입니다.

주문 전에는 잔고와 거래 한도를 확인합니다. 마지막 실행 시각과 거래 중지 사유는 **자동매매 상태**에 표시합니다.

![마지막 실행 시각, 거래 중지 여부, 증권사 연결 상태](docs/images/readme-operations-20260922.png)

[기본 매매 설정과 사용법](docs/USER_GUIDE.md#매매-설정)

<details>
<summary>모바일 화면</summary>

<a href="docs/images/dashboard-mobile-20260922.png"><img src="docs/images/readme-mobile-20260922.png" alt="휴대폰 크기의 계좌 화면" width="300"></a>

작은 화면에서는 계좌 금액과 차트를 위아래로 배치합니다. 날짜는 터치로 바꿀 수 있습니다. 휴대폰 접속에는 [별도의 연결 설정](docs/USER_GUIDE.md#모바일-접속)이 필요합니다.

</details>

## 백테스트

과거 주가로 매매 조건을 시험해 볼 수 있습니다. 거래 비용을 반영한 수익률과 크게 하락했던 구간을 함께 비교합니다.

[백테스트 결과와 계산 조건](docs/RISK_REVIEW_20260922.md)

모의투자와 백테스트에서 수익이 났더라도 실제 투자에서는 손실이 날 수 있습니다. CD금리 ETF도 원금을 보장하지 않습니다.

## 시작하기

**Python 3.11 또는 3.12와 Git**이 필요합니다. 설치 안내는 Windows PowerShell 기준입니다.

1. [설치 안내](docs/GETTING_STARTED.md)에 따라 프로그램을 설치합니다.
2. [모의투자](docs/GETTING_STARTED.md#모의투자)를 실행합니다.
3. 설치한 PC에서 [계좌 화면](http://127.0.0.1:8080)을 엽니다. 날짜 선택과 파일 저장은 [사용법](docs/USER_GUIDE.md)에 설명했습니다.

처음 실행하면 거래 기록이 없는 계좌가 열립니다. 자동매매는 계좌 화면과 별도로 실행하며, PC와 자동매매 프로그램을 켜 두어야 합니다.

실제 계좌 연결은 한국투자증권 KIS API를 사용합니다. 연결 방법과 주문 설정은 [실제 계좌 사용 안내](docs/PAPER_TO_LIVE_RUNBOOK.md)에 있습니다.

## 문의

오류가 나거나 필요한 기능이 있으면 [오류 제보·기능 제안](https://github.com/easygap/quant_trader/issues/new/choose)에 남겨 주세요. 화면이나 오류 메시지를 올릴 때는 계좌번호와 API 키를 지워 주세요.

<details>
<summary>개발 문서</summary>

Python · aiohttp · SQLite · HTML/CSS/JavaScript를 사용합니다. 코드 구조와 자동 실행 설정은 [개발 문서](docs/PROJECT_GUIDE.md)에 정리했습니다.

</details>
