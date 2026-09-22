<img src="monitoring/static/nungum-symbol.svg" alt="" width="40">

# 눈금

**국내 주식·ETF 자동매매 프로그램**

한국투자증권 KIS API를 지원하는 Python 프로그램입니다. 설정한 종목과 비중에 따라 자동으로 매매하고, 웹 화면에서 투자금과 수익률을 확인합니다. 기본 설정은 모의투자입니다.

[설치 방법](#설치-및-실행) · [백테스트 결과](docs/RISK_REVIEW_20260922.md) · [문의](https://github.com/easygap/quant_trader/issues/new/choose)

![계좌에서 날짜별 수익률을 확인하고 차트 모양을 바꾸는 모습](docs/images/readme-walkthrough-20260922.gif)

2026년 9월 22일에 촬영한 모의투자 화면입니다. [이미지로 보기](docs/images/readme-account-20260922.png)

## 주요 기능

- **계좌 조회**: 투자원금, 평가금액, 수익률을 날짜별로 확인하고 CSV로 저장합니다.
- **자동매매**: 정해 둔 종목과 비중에 맞춰 매수·매도합니다. 주문 전에 잔고와 거래 한도를 확인합니다.
- **모의투자**: 가상 자금으로 매매해 보고, 주문 내역과 수익률을 확인합니다.
- **백테스트**: 과거 주가로 매매 규칙을 시험하고 수익률, 손실, 거래 비용을 비교합니다.

계좌에 돈을 추가로 넣어도 수익률이 부풀려지지 않도록 계산합니다. 차트 아래에서 날짜를 고르면 그날의 투자금과 수익률이 함께 바뀝니다.

## 설치 및 실행

**Python 3.11 또는 3.12와 Git**이 필요합니다. 아래는 Windows PowerShell 기준입니다.

```powershell
git clone https://github.com/easygap/quant_trader.git
cd quant_trader
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path config/settings.yaml)) {
    Copy-Item config/settings.yaml.example config/settings.yaml
}
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}

.\.venv\Scripts\python.exe main.py --mode dashboard
```

브라우저에서 **[127.0.0.1:8080](http://127.0.0.1:8080)**을 열면 됩니다. 이 명령은 계좌 화면만 엽니다. 자동매매는 별도로 실행해야 합니다.

처음 설치하면 저장된 거래가 없어 계좌가 비어 있습니다. [설정과 모의투자 실행 방법](docs/GETTING_STARTED.md)을 따라 시작하세요.

## 보유 종목

몇 주를 샀는지, 평균 매수가는 얼마인지, 목표 비중과 얼마나 차이가 나는지 확인할 수 있습니다.

![대형주 계좌의 보유 수량, 평균 매수가, 종목별 비중](docs/images/readme-holdings-20260922.png)

종목별 비중은 **매수한 금액 기준**입니다. 현재 가격으로 계산한 보유 주식의 금액과 손익은 표 아래에 따로 나옵니다.

<details>
<summary>자동매매 상태와 모바일 화면 보기</summary>

### 자동매매 상태

마지막 실행 시각과 거래 중지 여부, 증권사 연결 상태를 확인할 수 있습니다. 문제가 있으면 안내를 눌러 해당 화면으로 이동합니다.

![자동매매의 마지막 실행 시각과 연결 상태](docs/images/readme-operations-20260922.png)

### 모바일 화면

휴대폰 화면에 맞춰 수익률, 계좌 금액, 차트 순서로 표시합니다. 차트의 날짜는 터치로 바꿀 수 있습니다. 휴대폰에서 접속하려면 PC의 기본 접속 설정을 바꿔야 합니다.

<a href="docs/images/dashboard-mobile-20260922.png"><img src="docs/images/readme-mobile-20260922.png" alt="모바일 화면의 ETF 적립 계좌와 수익률 차트" width="300"></a>

[모바일 전체 화면 보기](docs/images/dashboard-mobile-20260922.png)

</details>

## 기본 매매 설정

- **ETF 적립**: KODEX 200과 TIGER CD금리 ETF에 나눠 투자합니다. 시작 금액은 30만원, 월 적립 계획은 10만원입니다.
- **대형주 분산 투자**: 국내 대형주에 나눠 투자합니다. 주식 60%·현금 40%를 목표로 하며, 비중 차이가 커지면 조정합니다.

종목과 비중은 [config/baskets.yaml](config/baskets.yaml)에서 바꿀 수 있습니다. ETF 적립은 현재 모의투자 전용입니다. 추가 투자금은 화면에 직접 기록하며, 자동이체 기능은 없습니다.

백테스트 결과에는 수익률뿐 아니라 **가장 많이 하락한 폭과 거래 비용**도 함께 정리했습니다. [비교 조건과 결과 보기](docs/RISK_REVIEW_20260922.md)

모의투자와 백테스트 결과가 좋아도 실제 투자에서는 손실이 날 수 있습니다. CD금리 ETF도 원금을 보장하지 않습니다. 실제 계좌를 연결하기 전에는 [주문 설정과 확인 사항](docs/PAPER_TO_LIVE_RUNBOOK.md)을 읽어 주세요.

## 사용 안내

- [설치와 첫 모의투자](docs/GETTING_STARTED.md)
- [자동 실행과 설정 파일 설명](docs/PROJECT_GUIDE.md)
- [실제 계좌 연결](docs/PAPER_TO_LIVE_RUNBOOK.md)
- [거래가 중지됐을 때 확인할 것](docs/SAFETY_MODEL.md)

Python · aiohttp · SQLite를 사용합니다. 웹 화면은 HTML/CSS/JavaScript로 만들었으며, 프런트엔드는 따로 설치하거나 빌드할 필요가 없습니다.

## 문의와 의견

잘 안 되는 기능이나 필요한 기능이 있으면 [GitHub Issues](https://github.com/easygap/quant_trader/issues/new/choose)에 남겨 주세요. 오류 화면이나 메시지를 함께 올려 주시면 원인을 찾는 데 도움이 됩니다. 계좌번호와 API 키는 지우고 올려 주세요.

유용하게 쓰셨다면 오른쪽 위 **Star**를 눌러 주세요.
