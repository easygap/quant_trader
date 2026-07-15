# QUANT TRADER

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-1%2C800%2B-2EA043)
![Market](https://img.shields.io/badge/market-KR%20stocks-0F766E)
![Mode](https://img.shields.io/badge/default-paper-7C3AED)

한국 주식 바스켓을 **paper 환경에서 먼저 검증**하고, 성과와 리스크를 한 화면에서 확인하는 자동매매 프로젝트입니다.

> [!IMPORTANT]
> 기본값은 `paper`이며 현재 실전 전환 판정은 **NO-GO**입니다. 이 프로그램은 손실을 줄이기 위한 통제 장치를 제공하지만 수익이나 원금을 보장하지 않습니다.

## 한눈에 보기

![paper 운영 대시보드의 자산 요약](docs/images/dashboard-overview.png)

실제 로컬 paper 대시보드 화면입니다. 바스켓별 평가금, 원금 대비 손익, 시간가중수익률(TWR), MDD, 현금, 주식 배치율과 보유 종목을 함께 보여줍니다. 화면의 수치와 진행 상태는 운영 데이터에 따라 달라집니다.

| 주요 기능 | 사용자가 확인할 수 있는 것 |
|---|---|
| 바스켓 paper 운용 | 목표 비중, 리밸런싱 계획, 모의 체결과 보유 현황 |
| 성과 추적 | 입출금을 분리한 TWR, 평가금 추이, MDD |
| 승격 게이트 | 60영업일 트랙레코드, 데이터 커버리지, 운영 차단 사유 |
| 리스크 통제 | 손실 한도, 중복 주문, 유동성, 비정상 가격과 불확실 체결 차단 |
| 운영 대시보드 | 자산·성과·운영 상태 확인과 적립 입금 기록 |

## 빠르게 시작하기

Python 3.11 이상이 필요합니다.

```bash
git clone https://github.com/easygap/quant_trader.git
cd quant_trader

python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
cp config/settings.yaml.example config/settings.yaml
cp .env.example .env
```

`config/settings.yaml`은 아래 안전 기본값을 유지한 채 시작하세요.

```yaml
kis_api:
  use_mock: true
trading:
  mode: "paper"
  auto_entry: false
```

KIS 모의투자나 알림을 사용할 때만 `.env`에 필요한 값을 채웁니다. `.env`와 실제 계좌 설정은 Git에 올리지 마세요.

대시보드를 실행합니다.

```bash
python main.py --mode dashboard
```

기본 바인드는 http://127.0.0.1:8080입니다. 브라우저에서 이 주소를 열면 되며, 인증 없이 외부 주소에 공개하지 마세요.

## 권장 사용 순서

```bash
# 1. 주문 없이 리밸런싱 계획 확인
python main.py --mode rebalance --dry-run

# 2. paper 사이클 1회 실행
python main.py --mode paper

# 3. 운영 상태와 차단 사유 확인
python main.py --mode health
```

상시 paper 운영이 필요하면 `python main.py --mode schedule`, 전체 실행 모드는 `python main.py --mode guide`로 확인할 수 있습니다.

## 성과와 승격 상태

![수익률 차트와 paper 승격 진행률](docs/images/dashboard-performance.png)

대시보드는 평가금과 누적 수익률을 그려 주고, 바스켓별 60영업일 paper 기록과 데이터 커버리지를 함께 표시합니다. `WAIT`는 오류가 아니라 아직 관찰 기간이나 승격 조건을 채우는 중이라는 뜻입니다.

실전 모드는 KIS 연결, 계좌 동기화, 트랙레코드, 운영 상태와 명시적 사용자 확인을 모두 통과해야 열립니다. 우회 플래그는 제공하지 않습니다.

## 적립 입금 기록

![적립 입금 기록 화면](docs/images/dashboard-deposit.png)

대시보드 오른쪽 위의 **+ 적립 입금**에서 바스켓과 금액을 선택할 수 있습니다. 입금은 TWR 계산에서 중화되어 투자 성과를 왜곡하지 않습니다. 이 화면에서 매매나 전략 설정 변경은 할 수 없습니다.

CLI에서도 같은 기록을 남길 수 있습니다.

```bash
python tools/record_deposit.py --basket kr_pocket --amount 100000
```

## 자주 쓰는 명령

| 명령 | 용도 |
|---|---|
| `python main.py --mode guide` | 사용 가능한 실행 모드 확인 |
| `python main.py --mode rebalance --dry-run` | 주문 없이 리밸런싱 계획 확인 |
| `python main.py --mode paper` | paper 사이클 1회 실행 |
| `python main.py --mode dashboard` | 웹 대시보드 실행 |
| `python main.py --mode health` | 운영 상태와 차단 사유 점검 |
| `python main.py --mode weekly_report` | 주간 성과 요약 생성·발송 |

## 안전하게 사용하기

- 가격·잔고·미체결 상태를 확인할 수 없으면 신규 주문을 차단합니다.
- 부분 체결, 체결 상태 불명, 체결 후 장부 저장 실패는 전역 거래 중지(`HALT`)로 이어집니다.
- paper와 live 거래·포지션·현금 장부를 분리해 모의 데이터가 실계좌 판단에 섞이지 않게 합니다.
- 실전 진입에는 환경 설정, 명시적 확인, 하드 게이트 통과가 모두 필요합니다.
- 어떤 안전장치도 시장 급변, 슬리피지, API 장애나 투자 손실을 완전히 제거할 수는 없습니다.

긴급 중지 해제, 체결 불확실 시 복구 순서와 실전 전환 조건은 [SAFETY_MODEL](docs/SAFETY_MODEL.md)에서 확인하세요.

## 설정과 문서

| 파일 | 용도 |
|---|---|
| `config/baskets.yaml` | paper 바스켓과 목표 비중 |
| `config/risk_params.yaml` | 손실·포지션·유동성 한도 |
| `config/settings.yaml` | 로컬 실행 환경과 paper/live 모드 |

- [소액 적립 트랙 설계](docs/POCKET_TRACK_PLAN.md)
- [프로젝트 사용·운영 가이드](docs/PROJECT_GUIDE.md)
- [연구 결과와 한계](docs/PROFITABILITY_FINDINGS.md)

> 학습과 개인 연구를 위한 프로젝트이며 투자 조언이 아닙니다. 실제 자금을 사용하기 전에는 코드, 설정, 증권사 규정과 세금 조건을 직접 검토하세요.
