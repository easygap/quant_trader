**한국어** · [English](README.en.md) · [日本語](README.ja.md) · [简体中文](README.zh-CN.md)

<img src="monitoring/static/nungum-symbol.svg" alt="" width="40">

# 눈금

**국내 주식·ETF 자동매매 프로그램**

매매할 종목과 투자 비중을 정하고, 가상 자금으로 먼저 시험해 볼 수 있습니다. 내 PC에 설치해 쓰며, 수익률과 보유 종목, 자동매매 상태는 브라우저에서 확인할 수 있습니다.

[설치하기](docs/GETTING_STARTED.md) · [화면 사용법](docs/USER_GUIDE.md) · [백테스트](#백테스트) · [오류 제보·기능 제안](https://github.com/easygap/quant_trader/issues/new/choose)

## 계좌와 수익률

![ETF 적립 계좌의 수익률 차트와 날짜 선택, 입체·평면 전환](docs/images/readme-walkthrough-20260922.gif)

이 페이지의 이미지는 모두 **2026년 9월 22일에 촬영한 모의투자 화면**입니다. [정지 이미지 보기](docs/images/readme-account-20260922.png)

투자원금과 평가금액, 월별 수익률을 한 화면에 모았습니다. 추가로 넣은 돈은 수익에 포함하지 않습니다. 차트에서 기간과 날짜를 골라 투자 내역을 살펴볼 수 있습니다.

## 보유 종목

![대형주 계좌의 보유 수량, 평균 매수가, 현재 비중과 목표 비중](docs/images/readme-holdings-20260922.png)

종목별 보유 수량과 평균 매수가, 현재 비중과 목표 비중을 나란히 표시합니다.

비중은 **매수금액 기준**입니다. 현재 주가로 계산한 평가금액과 손익은 표 아래에 따로 표시합니다.

## 투자 내역 저장

![날짜별 평가금액·투자원금·수익률과 고점 대비 하락률](docs/images/history-table-20260922.png)

날짜별 투자 내역은 표로 보거나 CSV 파일로 내려받을 수 있습니다. **기록 내려받기**로 저장한 파일은 엑셀에서도 열립니다. 화면에서는 기록을 100개씩 나눠 보여 주고, 파일에는 조회 기간 전체를 담습니다.

## 매매 설정

두 가지 기본 설정으로 시작할 수 있습니다.

| 설정                 | 매매 방식                                                                 |
| -------------------- | ------------------------------------------------------------------------- |
| **ETF 적립**         | KODEX 200과 TIGER CD금리 ETF에 나눠 투자합니다. 현재 모의투자 전용입니다. |
| **대형주 분산 투자** | 국내 대형주와 현금을 나눠 보유하고, 목표 비중과 차이가 커지면 조정합니다. |

종목과 투자 비중은 직접 바꿀 수 있습니다. [매매 설정](docs/USER_GUIDE.md#매매-설정) · [적립금 입력](docs/USER_GUIDE.md#적립금-기록)

주문 전에는 잔고와 거래 한도를 확인합니다. **자동매매 상태**에서 마지막 실행 시각과 거래가 중지된 이유를 볼 수 있습니다.

<details>
<summary>자동매매 상태 화면</summary>

![자동매매 실행 시각, 거래 중지 여부, 증권사 연결 상태](docs/images/readme-operations-20260922.png)

</details>

<details>
<summary>모바일 화면</summary>

<a href="docs/images/dashboard-mobile-20260922.png"><img src="docs/images/readme-mobile-20260922.png" alt="휴대폰 화면에 맞춘 계좌 조회와 수익률 차트" width="300"></a>

기본 주소는 설치한 PC에서만 열립니다. 휴대폰으로 보려면 [접속 설정](docs/USER_GUIDE.md#모바일-접속)이 필요합니다.

</details>

## 설치

**Python 3.11 또는 3.12와 Git**이 필요합니다. 설치 안내는 Windows PowerShell 기준입니다.

1. [설치 안내](docs/GETTING_STARTED.md)에 따라 프로그램을 설치합니다.
2. 설치한 PC에서 [계좌 화면](http://127.0.0.1:8080)을 엽니다.
3. [모의투자 실행 방법](docs/GETTING_STARTED.md#모의투자)에 따라 첫 매매를 시험해 봅니다.

처음 설치하면 거래 기록이 없는 계좌로 시작합니다.

자동매매는 계좌 화면과 별도로 실행해야 합니다. 사용 중에는 PC와 매매 프로그램을 켜 두세요.

실제 계좌는 **한국투자증권 KIS API**로 연결합니다. [실제 계좌 사용 안내](docs/PAPER_TO_LIVE_RUNBOOK.md)

## 백테스트

과거 주가에 매매 규칙을 적용해 수익률과 고점 대비 하락률을 비교합니다. 수수료 등 거래 비용을 포함한 결과와 계산 조건을 공개하고 있습니다.

[백테스트 실행 방법](docs/GETTING_STARTED.md#백테스트) · [비교 결과](docs/RISK_REVIEW_20260922.md) · [주문 수량·거래 비용 비교](docs/REBALANCE_REVIEW_20260922.md)

모의투자와 백테스트 결과는 실제 수익을 보장하지 않습니다. CD금리 ETF도 원금 손실이 날 수 있습니다.

## 문의

사용 중 문제가 생기거나 필요한 기능이 있으면 [GitHub 이슈](https://github.com/easygap/quant_trader/issues/new/choose)에 남겨 주세요. 오류 메시지나 화면을 첨부할 때는 계좌번호와 API 키를 가려 주세요.

<details>
<summary>설정 파일과 코드가 궁금하다면</summary>

[코드 구조·자동 실행 설정](docs/PROJECT_GUIDE.md) · [거래 중지 조건](docs/SAFETY_MODEL.md)

Python · aiohttp · SQLite · HTML/CSS/JavaScript를 사용합니다.

</details>
