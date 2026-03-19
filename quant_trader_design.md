# 🏗️ QUANT TRADER - 자동 주식 매매 시스템 설계서

> **문서 버전**: v2.2  
> **작성일**: 2026-03-11  
> **최종 수정**: 2026-03-19  
> **목적**: 데이터 기반 알고리즘 트레이딩 시스템의 전체 아키텍처, **실제 파일/구조/알고리즘** 및 구현 가이드

---

## 목차

1. [시스템 개요](#1-시스템-개요)
2. [기술 스택](#2-기술-스택)
3. [핵심 기술 지표](#3-핵심-기술-지표)
4. [매매 전략 로직](#4-매매-전략-로직)
5. [리스크 관리](#5-리스크-관리)
6. [시스템 아키텍처 및 프로젝트 구조](#6-시스템-아키텍처-및-프로젝트-구조)
7. [실행 모드 및 CLI](#7-실행-모드-및-cli)
8. [백테스팅 & 검증](#8-백테스팅--검증)
9. [예외 처리 및 안정성](#9-예외-처리-및-안정성)
10. [개발 로드맵 & 현재 구현 상태](#10-개발-로드맵--현재-구현-상태)
11. [주의사항](#11-주의사항)
12. [부록: 용어 정리](#부록-용어-정리)

---

## 1. 시스템 개요

자동 주식 매매 시스템(알고리즘 트레이딩)은 **사람의 감정 없이** 데이터와 수학적 로직으로 매매 결정을 내리는 프로그램입니다.

### 1.1 핵심 처리 흐름

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ 01       │   │ 02       │   │ 03       │   │ 04       │   │ 05       │   │ 06       │
│ 데이터   │──▸│ 지표     │──▸│ 신호     │──▸│ 리스크   │──▸│ 주문     │──▸│ 모니터링 │
│ 수집     │   │ 계산     │   │ 생성     │   │ 관리     │   │ 실행     │   │          │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
  실시간 주가     기술적 지표로    매수/매도       손절/익절      증권사 API로    실시간 성과
  거래량, 뉴스    시장 상태 분석   신호 판단       라인 설정      자동 주문       추적 및 로깅
```

### 1.2 시스템 목표

| 항목 | 목표 |
|------|------|
| **자동화** | 24시간 무인 매매 가능 (장 시간 내 자동 실행) |
| **감정 제거** | 공포/탐욕 없는 규칙 기반 매매 |
| **리스크 제한** | 최대 낙폭(MDD) 15% 이내 제어 |
| **수익 목표** | 연 20% 이상 (백테스팅 기준) |
| **대응 속도** | 실시간 시세 반영 (1초 이내 분석·주문) |

---

## 2. 기술 스택

### 2.1 언어 & 런타임

| 기술 | 선정 사유 |
|------|----------|
| **Python 3.11~3.12** | 금융 라이브러리 생태계 풍부. pandas, numpy, pandas-ta 등 핵심 패키지 완비 (`pyproject.toml`: `>=3.11,<3.13`) |
| **asyncio** | 비동기 처리로 실시간 데이터 스트리밍과 주문 처리를 동시 수행 |

### 2.2 데이터 수집

| 기술 | 선정 사유 |
|------|----------|
| **KIS Developers API** | 한국투자증권 공식 API — 국내주식 실시간 시세 및 주문 실행 |
| **yfinance** | 미국/한국 주식 무료 데이터 (백테스팅·일봉 보조, auto_adjust=True 로 수정주가) |
| **FinanceDataReader** | 한국 주식 무료 데이터 (KRX 전 종목, watchlist 자동 선정). **수정주가 기본 제공** — 백테스트·실전 동일 소스 권장 |
| **websocket-client** | KIS 실시간 호가/체결가 스트리밍 수신 |

**⚠️ 데이터 소스·수정주가 일관성 (§2.2)**

- **문제**: 한국 일봉은 **FinanceDataReader → yfinance → KIS** 순으로 fallback합니다. 세 소스의 **수정주가(배당·액면분할 반영)** 처리 방식이 다릅니다. FDR·yfinance는 수정주가를 기본/옵션으로 제공하지만, **KIS API는 비수정(원시) 데이터**를 반환하는 경우가 많습니다. 백테스트에는 수정주가를 썼는데 실전 신호 계산에 비수정 데이터를 쓰면 **지표값이 완전히 달라집니다**.

| 소스 | 수정주가 | 비고 |
|------|----------|------|
| **FinanceDataReader** | ✅ 기본 제공 | 한국 주식 **우선 권장** |
| **yfinance** | ✅ auto_adjust=True | 한국 종목 `.KS` 지원, 폴백용 |
| **KIS API** | ❌ 비수정 가능 | 주문 실행 전용 권장, 일봉 폴백은 위험 |

- **대응**:
  1. **소스 추적**: `DataCollector`가 매 수집 시 사용 소스와 수정주가 여부를 기록합니다 (`_last_source`, `_last_adjusted`, `_source_history`). 수집 로그에 `소스=FinanceDataReader, 수정주가=Yes` 형태로 명시합니다.
  2. **KIS 폴백 차단 옵션**: `settings.yaml`의 `data_source.allow_kis_fallback: false`로 설정하면 FDR/yfinance 모두 실패 시 **KIS 폴백을 차단**합니다. 수정주가 불일치를 원천 방지합니다.
  3. **소스 불일치 자동 감지**: `Scheduler` 장전 분석 후 `check_source_consistency()`로 FinanceDataReader 이외 소스를 사용한 종목을 감지하고, **경고 로그 + 디스코드 critical 알림**을 발송합니다.
  4. **우선 소스 지정**: `data_source.preferred: "fdr"` 로 설정하면 FinanceDataReader만 사용하고 다른 소스로 폴백하지 않습니다.
- **설정**: `config/settings.yaml` → `data_source` (preferred, allow_kis_fallback, warn_on_source_mismatch).
- **권장**: FDR 설치·우선 사용. KIS는 **주문 실행 전용**으로 두고, 일봉 수집에는 FDR 고정.

### 2.3 데이터 처리 & 분석

| 기술 | 선정 사유 |
|------|----------|
| **pandas** | 시계열 OHLCV 데이터프레임 처리 |
| **numpy** | 수치 계산 가속화 |
| **pandas-ta** | 기술적 지표 계산 (RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV) |

### 2.4 백테스팅 & 검증

| 기술 | 선정 사유 |
|------|----------|
| **자체 Backtester** | `backtest/backtester.py` — 수수료·세금·슬리피지·손절/익절/트레일링 스탑 반영, **strict-lookahead 기본** |
| **strategy_validator** | 최소 3~5년 데이터, 샤프·MDD·벤치마크(KS11·코스피 상위 50 동일비중) 비교, in/out-of-sample 분리 검증, **손익비 자동 경고(추세 추종 ≥ 2.0) + 디스코드 알림** |
| **param_optimizer** | Grid Search / Bayesian(scikit-optimize) 파라미터 최적화 |

### 2.5 데이터베이스

| 기술 | 선정 사유 |
|------|----------|
| **SQLite** | 기본. WAL + busy_timeout(30s) + scoped_session + @with_retry(읽기·쓰기 전체) + Online Backup API. 실전 안정화 후 PostgreSQL 전환 권장 |
| **SQLAlchemy** | ORM — DB 전환(PostgreSQL 등) 시 마이그레이션 용이 |

**ORM 모델** (`database/models.py`):

| 모델 | 설명 |
|------|------|
| **StockPrice** | 종목별 OHLCV 시계열 저장 |
| **TradeHistory** | 매매 기록 (종목, 방향, 수량, 가격, 수수료, 전략, 사유) |
| **Position** | 현재 보유 포지션 (종목, 수량, 평균가, 손절/익절/트레일링) |
| **PortfolioSnapshot** | 일별 포트폴리오 스냅샷 (총자산, 수익률, MDD) |
| **DailyReport** | 일일 리포트 데이터 |

### 2.6 모니터링 & 알림

| 기술 | 선정 사유 |
|------|----------|
| **Discord Webhook** | 매수/매도·일일 리포트·블랙스완·동기화 불일치 알림 |
| **loguru** | 구조화된 로그 (파일 로테이션·콘솔 출력) |
| **웹 대시보드** | aiohttp 기반 실시간 포트폴리오·스냅샷 (기본 8080) |

---

## 3. 핵심 기술 지표

구현 위치: **`core/indicator_engine.py`** (pandas-ta 기반, 설정: `config/strategies.yaml` → `indicators`)

### 3.1 RSI (상대강도지수)

- **설명**: 가격 모멘텀. 0~100. 과매도/과매수 구간 판별.
- **공식**: `RSI = 100 - (100 / (1 + RS))`, RS = 평균 상승폭 / 평균 하락폭
- **설정**: `indicators.rsi.period` (기본 14), `oversold` 30, `overbought` 70
- **신호**: RSI < 30 → 과매도(매수 후보), RSI > 70 → 과매수(매도 후보)

### 3.2 MACD (이동평균 수렴·확산)

- **설명**: 추세 방향·강도. 골든크로스/데드크로스.
- **설정**: `fast_period` 12, `slow_period` 26, `signal_period` 9
- **신호**: MACD선이 Signal선 상향 돌파 → 매수, 하향 돌파 → 매도

### 3.3 볼린저 밴드

- **설명**: 변동성 밴드. 하단 터치 후 반등 → 매수, 상단 터치 후 하락 → 매도.
- **설정**: `period` 20, `std_dev` 2.0

### 3.4 이동평균 (MA)

- **설명**: 추세 방향. 단기/장기 골든크로스·데드크로스.
- **설정**: `short_period` 5, `mid_period` 20, `long_period` 60, `trend_period` 200

### 3.5 거래량 & OBV

- **설정**: `volume.avg_period` 20, `surge_ratio` 1.5 (평균 대비 거래량 급증 기준)
- **OBV**: 상승일 +거래량, 하락일 -거래량 누적. `indicator_engine.add_obv`, `add_volume_ratio`

### 3.6 스토캐스틱

- **설정**: `k_period` 5, `d_period` 3, `smooth` 3, `oversold` 20, `overbought` 80

### 3.7 ADX (평균 방향 지수)

- **설명**: 추세 **강도** (방향 아님). ADX < 20 횡보, > 25 추세 강함.
- **설정**: `period` 14, `trend_threshold` 25

### 3.8 ATR (평균 실질 범위)

- **설명**: 변동성 크기. 손절/트레일링 스탑 배수 설정에 사용.
- **설정**: `period` 14. 리스크: `risk_params.yaml` → `stop_loss.atr_multiplier`, `trailing_stop.atr_multiplier`

---

## 4. 매매 전략 로직

### ⚠️ 시장 비효율성과 전략의 이론적 근거 (근본 원칙)

퀀트 전략이 **지속적으로 수익**을 내려면 **시장 비효율성(Market Inefficiency)** 을 이용해야 합니다. "왜 이 전략이 돈을 벌 수 있는가"에 대한 **이론적·실증적 근거**가 있어야 하며, 현재 설계에서 **각 전략이 이용하려는 비효율성이 무엇인지** 명시하는 것이 중요합니다.

- **학술적으로 검증된 팩터 예**: (1) **단기 과반응 후 되돌림** → 평균 회귀 전략, (2) **모멘텀 효과**(좋은 주식이 일정 기간 계속 좋음) → 추세 추종 전략, (3) 요일/월 효과, 실적 발표 전후 패턴 등. 이런 **명시된 비효율성**을 기반으로 전략을 설계하면 "왜 돈이 될 수 있는가"에 대한 근거가 생깁니다.
- **현재 한계**: "RSI가 30 이하면 반등할 것 같다", "볼린저 하단이면 매수" 같은 **직관 수준**에 머물러 있으면, **어떤 시장 비효율성을 이용하는지** 불명확합니다. 아래 각 전략에 **이용(가정)하는 비효율성**을 적어 두었으므로, 전략 선택·개선 시 참고하세요.

---

### 4.1 멀티 지표 스코어링 전략 (초급 ⭐)

- **구현**: `strategies/scoring_strategy.py`, `core/signal_generator.py`
- **설정**: `config/strategies.yaml` → `scoring` (buy_threshold, sell_threshold, weights)
- **이용(가정)하는 시장 비효율성**: **명시되지 않음**. 여러 기술지표(RSI, MACD, 볼린저, MA, 거래량)를 조합해 신호를 내는 구조이며, "RSI 30 이하면 반등할 것 같다"는 **직관**에 가깝고, **학술적으로 검증된 단일 팩터(비효율성)** 에 기반을 두지 않습니다. 노이즈 완화·다수 지표 합의를 노리는 **실용적 조합** 수준이므로, 실전 사용 시 **어떤 비효율성을 노리는지** 별도 가정을 두거나, 평균 회귀·모멘텀 등 명시된 팩터와 결합하는 것을 권장합니다.

**⚠️ 지표 간 다중공선성 (Multicollinearity)**

- **문제**: 현재 스코어링에 RSI, MACD, 볼린저, MA, 거래량이 들어가 있으며, 스토캐스틱(계산되지만 스코어링에는 미사용)을 추가하면 RSI와 동일한 오실레이터 성격으로 **높은 상관관계**를 가집니다. 이 지표들 대부분은 **가격과 이동평균의 변형**입니다. RSI·스토캐스틱은 둘 다 "과매수/과매도" 오실레이터로 정보가 중복되고, MACD와 MA 골든크로스도 **같은 정보(가격 추세)**를 다르게 표현할 뿐입니다. 결과적으로 **스코어의 대부분이 "가격이 최근 올랐냐 내렸냐" 한 가지 정보를 여러 번 세는 형태**가 되어, 실질적으로 1~2개 지표에 스코어가 지배당할 수 있습니다.
- **필수 조치**: `--mode check_correlation`을 **반드시** 실행하고 리포트를 확인하세요. 상관계수 |r| ≥ 0.7인 쌍은 **둘 중 하나 제거**(가중치 0) 또는 가중치 축소를 적용해야 합니다.
- **권장 구성**: 실질적으로 **독립적인 정보**는 다음 **3그룹**으로 나눌 수 있습니다. **그룹당 대표 지표 하나씩만** 남기는 것을 권장합니다.

| 그룹 | 대표 지표 (택 1) | 비고 |
|------|------------------|------|
| **가격 모멘텀** | MACD (권장) 또는 MA | 같은 추세 정보. RSI도 가격 변형. 둘 이상 쓰면 다중공선성 |
| **변동성** | 볼린저 (또는 ATR) | 밴드/범위 정보. 스코어링에는 현재 볼린저만 사용 |
| **거래량** | volume_surge (OBV/volume_ratio) | 가격 외 독립 정보 |

**다중공선성 완화 모드 (`collinearity_mode`)**

`strategies.yaml`의 `scoring.collinearity_mode` 설정으로 두 가지 모드를 제공합니다:

| 모드 | 동작 | 완화 수준 | 권장 대상 |
|------|------|-----------|-----------|
| `max_per_direction` | 가격 그룹(RSI/MACD/볼린저/MA) 점수를 방향별 최대 1개만 반영. 매수=양수 max, 매도=음수 min | 중간 | 기존 호환 필요 시 |
| `representative_only` **(권장)** | 3그룹에서 **대표 지표 1개**씩만 사용 (MACD + 볼린저 + 거래량). 나머지 점수를 0 강제 | 강력 | 신규 설정, 실전 투입 전 |

`representative_only` 모드에서의 스코어 구성:
```
total_score = score_macd + score_bollinger + score_volume
               (가격모멘텀)   (변동성)          (거래량)
```
RSI·MA 점수는 **계산은 되지만** 총점에 반영되지 않습니다 (check_correlation 등 분석 용도로 유지).

**⚠️ 런타임 경고**: `SignalGenerator` 초기화 시 가격 모멘텀 그룹에서 **2개 이상 지표가 활성 가중치**를 가지면 경고 로그가 자동 출력됩니다. `--mode optimize --auto-correlation`으로 자동 정리하거나 `collinearity_mode: representative_only`로 설정하세요.

**스코어링 가중치 (weights):**

| 조건 | 가중치 키 | 예시 점수 | 비고 |
|------|-----------|----------|------|
| RSI 과매도 | rsi_oversold | +2 | 예시값, 검증 없음 |
| RSI 과매수 | rsi_overbought | -2 | 예시값, 검증 없음 |
| MACD 골든크로스 | macd_golden_cross | +2 | 예시값, 검증 없음 |
| MACD 데드크로스 | macd_dead_cross | -2 | 예시값, 검증 없음 |
| 볼린저 하단 이탈 후 반등 | bollinger_lower | +1 | 예시값, 검증 없음 |
| 볼린저 상단 이탈 후 하락 | bollinger_upper | -1 | 예시값, 검증 없음 |
| 거래량 급증 | volume_surge | +1 | 예시값, 검증 없음 |
| 5일선 > 20일선 (골든크로스) | ma_golden_cross | +1 | 예시값, 검증 없음 |
| 5일선 < 20일선 (데드크로스) | ma_dead_cross | -1 | 예시값, 검증 없음 |

**⚠️ 가중치 설정 유의사항**

- **근거**: 위 점수는 **직관·예시용**이며, 한국 주식 시장 데이터로 검증된 값이 **아닙니다**. RSI에 +2, 볼린저에 +1인 이유에 대한 통계적·실증적 근거는 없습니다. **이 가중치로 실제 거래를 발생시키면 신호가 노이즈에 가까울 수 있습니다**.
- **영향**: 가중치를 바꾸면 신호 빈도·방향이 달라지므로, "현재 값이 최적"이라는 보장이 없습니다. 아무리 리스크 관리·시장 국면 필터 등 인프라가 잘 되어 있어도 **신호 자체가 노이즈라면 결과는 무작위 또는 손실**입니다.
- **최적화 시 오버피팅**: `--mode optimize --include-weights`로 가중치를 과거 데이터로 탐색하면 **과적합** 가능성이 있습니다. 반드시 **OOS 샤프 ≥ 1.0 게이트**(자동 적용)를 통과해야 하며, walk-forward 추가 검증이 필수입니다.

**가중치 최적화 파이프라인 (필수 3단계)**

실전 투입 전 아래 순서를 반드시 따르세요:

```
┌─────────────────────────────────┐
│ STEP 1: 지표 독립성 검증         │
│ --mode check_correlation        │
│ → |r| ≥ 0.7 쌍 확인·제거 결정    │
└────────────┬────────────────────┘
             ▼
┌─────────────────────────────────┐
│ STEP 2: 가중치+임계값 최적화     │
│ --mode optimize --include-weights│
│ → 대칭 Grid Search              │
│ → OOS 샤프 ≥ 1.0 게이트 통과?   │
│   YES → YAML 스니펫 채택         │
│   NO  → 채택 불가 (과적합)       │
└────────────┬────────────────────┘
             ▼
┌─────────────────────────────────┐
│ STEP 3: 워크포워드 안정성 검증   │
│ --mode validate --walk-forward  │
│ → 여러 기간에서 안정적?          │
│   YES → 실전 투입 고려           │
│   NO  → 다시 STEP 1로           │
└─────────────────────────────────┘
```

**STEP 1** — 지표 독립성 검증:
```bash
python main.py --mode check_correlation --symbol 005930 --validation-years 5
```
`core/indicator_correlation.py`가 스코어 시리즈 상관계수 행렬을 계산하고, |r| ≥ 0.7인 쌍에 대해 **하나 제거 또는 가중치 축소**를 권고합니다. 리포트는 `reports/indicator_correlation_*.txt`에 저장됩니다. RSI와 스토캐스틱은 높은 확률로 중복됩니다. 리포트 하단에 **자동 비활성화 대상 가중치 키**와 **다음 단계 CLI 명령어**가 출력됩니다.

**STEP 2** — 가중치+임계값 최적화:
```bash
# 방법 A: 원스텝 (상관 분석 + 자동 비활성화 + 최적화를 한 번에)
python main.py --mode optimize --strategy scoring --include-weights --auto-correlation --symbol 005930

# 방법 B: 수동 (STEP 1 결과를 보고 직접 지정)
python main.py --mode optimize --strategy scoring --include-weights --disable-weights w_rsi,w_ma --symbol 005930
```
`--auto-correlation` 사용 시 STEP 1의 상관 분석이 자동 실행되어 고상관 지표(가격 모멘텀 그룹에서 MACD를 대표로 남기고 RSI·MA 비활성화)를 자동으로 `disabled_weights`에 추가합니다. `backtest/param_optimizer.py`의 `grid_search_scoring_weights()`가 가중치(대칭: 매수=+w, 매도=-w) × 임계값 조합을 탐색합니다. Train 70%에서 최적화한 뒤 OOS 30%에서 **샤프 ≥ 1.0 게이트**를 자동 검증합니다. 게이트 통과 시 `strategies.yaml`에 붙여넣을 YAML 스니펫을 출력합니다.

**STEP 3** — 워크포워드 안정성 검증:
```bash
python main.py --mode validate --walk-forward --strategy scoring --symbol 005930 --validation-years 5
```
STEP 2에서 찾은 가중치를 `strategies.yaml`에 반영한 뒤 실행합니다. 대부분의 창(80% 이상)에서 통과해야 실전 투입을 고려할 수 있습니다.

**실행 기준**: 총점 ≥ `buy_threshold`(다중공선성 완화 후 권장 2~3) → 매수, 총점 ≤ `sell_threshold`(권장 -2~-3) → 매도.  
(임계값 근처에서 신호가 자주 바뀌면 **과매매** 위험 → 거래 빈도·수수료 §8.3 참고.)

**⚠️ 매수/매도 임계값 대칭**

- **권장**: `buy_threshold`와 `sell_threshold`는 **절댓값을 같게** 두는 것을 권장합니다 (예: 3과 -3). 비대칭(예: 매수 5점, 매도 -4점)이 의도된 것이 아니라면, 대칭으로 설정해 두는 것이 안전합니다.
- **비대칭 시 문제**: 매도 쪽 임계값이 완화되면(예: -4만 있어도 매도) 매도가 **늦어져** 수익을 반납하기 쉽고, 반대로 매도 임계값이 엄격하면(예: -6 이상일 때만 매도) 매도가 **너무 일찍** 나와 보유 기간이 짧아질 수 있습니다. 의도 없는 비대칭은 진입·청산 타이밍이 한쪽으로 치우친 패턴을 만듭니다.
- **설정**: `strategies.yaml`의 `buy_threshold`, `sell_threshold`를 동일 절댓값으로 맞추고, `--mode optimize` 사용 시에도 대칭 쌍만 탐색하도록 하는 것을 권장합니다.

### 4.2 평균 회귀 전략 (중급 ⭐⭐)

- **구현**: `strategies/mean_reversion.py`, `core/fundamental_loader.py` (펀더멘털 필터)
- **설정**: `strategies.yaml` → `mean_reversion` (z_score_buy, z_score_sell, lookback_period, adx_filter, **exclude_52w_low_near**, **max_drawdown_from_52w_high**, **near_52w_low_pct**, window_52w, **restrict_to_kospi200**, **fundamental_filter**)
- **이용(가정)하는 시장 비효율성**: **단기 과반응 후 되돌림(Short-term overreaction then reversal)**. 가격이 단기적으로 평균에서 크게 이탈했다가 다시 평균으로 돌아오는 현상을 이용합니다. 학술적으로 **평균 회귀·되돌림(mean reversion)** 효과로 알려진 팩터에 해당하며, **한국 시장**에서는 펀더멘털 악화로 인한 하락이 많아 해당 비효율성이 제한적으로만 성립할 수 있습니다(아래 "한국 시장 한계" 참고).

**로직**: Z-Score = (현재가 - 평균) / 표준편차. Z < -2 매수, Z > 2 매도. ADX < adx_filter 일 때만 활성화(횡보장 강조).

**52주 고점/저점 이중 필터**: 실전 적용 시 장기 하락 구간 종목은 매수 제외하는 것을 권장합니다. `exclude_52w_low_near: true` 시 두 가지 조건을 검사합니다:
1. **52주 고점 대비 하락률**: `max_drawdown_from_52w_high`(기본 0.30) — 52주 고점에서 30% 이상 하락한 종목은 매수 제외. 이것이 주된 필터이며, "깊은 하락 = 실적 악화 가능성"을 간접적으로 포착합니다.
2. **52주 저점 근방**: `near_52w_low_pct`(기본 0.05) — 현재가가 52주 저점 대비 5% 이내이면 신저가 구간으로 판단하여 매수 제외.
`window_52w`(기본 252 거래일)로 52주 기간을 조정할 수 있습니다.

**코스피200 대형주 제한**: `restrict_to_kospi200: true` 시 **코스피200 구성 종목만** 평균 회귀 매수 가능합니다. 대형주는 실적 악화로 인한 영구 하락이 소형주보다 적어, 평균 회귀 가정이 상대적으로 잘 성립합니다. `pykrx`가 필요하며, 로드 실패 시 제한이 비활성화됩니다 (로그 경고).

**펀더멘털 필터**: Z-Score 매수 조건이 충족되어도, **매수 전** 해당 종목의 기본 재무 지표가 정상 범위인지 확인합니다. `mean_reversion.fundamental_filter.enabled: true` 시 **PER**(적자 제외·상한 설정 가능), **부채비율(%)** 상한을 검사하며, 범위를 벗어나면 매수 신호를 HOLD로 보류합니다. 데이터는 **pykrx(우선) → yfinance(폴백)** 순서로 조회합니다. pykrx는 한국 종목 PER 정확도가 높고, yfinance는 부채비율 등 추가 항목을 보충합니다. `per_min`·`per_max`·`debt_ratio_max`는 `strategies.yaml`에서 설정할 수 있으며, 백테스트 시 symbol이 전달되지 않으면 펀더멘털 필터를 수행하지 않습니다.

**⚠️ "평균"의 정의와 lookback_period**

- **핵심**: Z-Score에서 쓰는 **"평균"**은 **최근 lookback_period 일의 종가 이동평균**입니다. 표준편차도 같은 구간으로 계산됩니다. 즉 "어느 기간 기준으로 벗어났는가"를 정하는 것이 lookback_period 입니다.
- **영향**: 이 기간을 **20일**로 하느냐 **60일**로 하느냐에 따라 신호가 **완전히** 달라집니다. 20일은 단기 이탈, 60일은 중기 추세 이탈에 가깝습니다. 현재 설정은 **최적화·실증 없이 쓰는 고정값(기본 20일)** 이므로, 종목·시장에 맞게 조정하거나 `--mode optimize` 로 탐색하는 것을 권장합니다.
- **최적화**: `param_optimizer` 의 mean_reversion 검색 공간에 lookback_period가 포함되어 있습니다 (Grid: 15/20/25 등, Bayesian: 10~40). 다른 기간(예: 60)을 쓰려면 `strategies.yaml` 에서 직접 설정하거나, 검색 공간을 확장해 사용하세요.

**⚠️ 한국 주식 시장에서의 한계**

- **가정과 현실**: 평균 회귀는 "많이 떨어진 주가는 결국 평균으로 돌아온다"는 가정에 기반합니다. 그러나 **한국 시장**에서는 크게 하락한 종목 상당수가 **실적 악화, 분식회계, 대주주 지분 매도** 등 **펀더멘털 이유**로 하락하며, 이런 종목은 평균으로 회귀하지 않고 **추가 하락**하는 경우가 많습니다.
- **Z-Score만으로는 구분 불가**: Z-Score < -2 조건만으로는 **"기술적 과매도(일시적 반등 가능)"**와 **"펀더멘털 악화로 망해가는 기업"**을 구분할 수 없습니다.
- **ADX 필터의 불완전성**: ADX < adx_filter 로 "횡보장만 매수"하려 해도, **실적 악화 등으로 꾸준히 우하향하는 구간**에서도 ADX가 낮게 나올 수 있어, **하락 추세를 횡보로 오판**할 수 있습니다. 즉 필터만으로는 "진짜 횡보"와 "하락 추세의 일부 구간"을 완전히 나누기 어렵습니다.
- **권장 (실전 적용 시)**:
  1. **52주 고점/저점 이중 필터**: `exclude_52w_low_near: true` + `max_drawdown_from_52w_high: 0.30` + `near_52w_low_pct: 0.05`. 52주 고점 대비 30% 이상 하락 또는 저점 대비 5% 이내인 종목을 매수에서 제외합니다.
  2. **코스피200 제한**: `restrict_to_kospi200: true` — 대형주만 매수 허용. 소형주의 영구 하락 위험 회피.
  3. **펀더멘털 데이터**: `fundamental_loader.py`가 **pykrx(우선) → yfinance(폴백)** 순서로 자동 조회합니다. pykrx는 한국 종목 PER 정확도가 높고, yfinance는 부채비율 등 추가 항목 보충.
  4. **손절·포지션 사이징**: 위 필터만으로도 리스크가 남으므로 **손절·포지션 사이징을 엄격히** 적용하세요.

### 4.3 추세 추종 전략 (중급 ⭐⭐)

- **구현**: `strategies/trend_following.py`
- **설정**: `trend_following` (adx_threshold, trend_ma_period, atr_stop_multiplier, trailing_atr_multiplier)
- **이용(가정)하는 시장 비효율성**: **모멘텀 효과(Momentum)** — "좋은 주식이 일정 기간 계속 좋다"는 현상. 상대적으로 강한 추세가 지속되는 구간에서 추세를 따라가는 방식으로, 미국(나스닥) 등에서 **모멘텀 팩터**로 실증된 비효율성에 기반합니다. 한국 시장에서는 추세 지속성이 약해 해당 비효율성이 weaker할 수 있습니다(아래 "한국 시장 추세 지속성" 참고).

**로직**: ADX > adx_threshold, 가격 > trend_ma(200일), MACD 골든크로스(히스토그램 양수 전환) 시 매수. ATR 기반 손절·트레일링 스탑.

**⚠️ 늦은 진입 구조와 수익 구조**

- **진입이 늦는 이유**: 세 조건이 **동시에** 충족되는 시점은 (1) ADX > threshold → 이미 추세가 강해진 뒤, (2) 가격 > 200일선 → 이미 장기 상승 구간, (3) MACD 양수 전환 → 이미 단기 상승이 확인된 뒤입니다. 즉 **상당히 올라간 이후**에야 매수 신호가 나옵니다.
- **반복 패턴**: 뒤늦게 진입 → 조정 시 ATR 손절로 매도 → 다시 상승 시 또 늦게 진입. 그래서 **손실 거래가 잦고 승률이 40% 이하**로 낮을 수 있습니다. 이 자체가 나쁜 것은 아니지만, **수익 거래가 크게 나와야** 전략이 유효합니다.
- **손익비(Profit Factor) 검증 필수**: 이런 구조에서는 **손익비(Profit Factor) ≥ 2.0** 이어야 전략이 의미 있습니다. 백테스트·검증(`--mode backtest`, `--mode validate`) 결과에서 **손익비가 실제로 2.0 이상인지 반드시 확인**하세요. 달성되지 않으면 진입 조건 완화(예: 200일선 근접 허용) 또는 다른 전략 검토를 권장합니다.

**⚠️ 한국 시장의 추세 지속성**

- **미국 vs 한국**: 추세 추종 전략은 **미국 주식(특히 나스닥)** 시장에서 잘 동작한다는 실증·문헌이 많습니다. 반면 **코스피/코스닥**은 **박스권 등락이 길고**, 추세가 **빠르게 꺾이는** 특성이 있어, 추세 추종이 한국 시장에서도 동일하게 유효하다는 **실증 근거는 상대적으로 약합니다**.
- **권장**: 한국 시장에 이 전략을 적용할 때는 (1) **백테스트·검증으로 해당 종목/기간에서의 성과를 반드시 확인**하고, (2) 미국 시장용 파라미터(예: 200일선, ADX 25)를 그대로 쓰지 말고 **기간·임계값을 조정**하거나, (3) 앙상블에서 비중을 낮추는 것을 고려하세요.

### 4.4 전략 앙상블 (정보 소스 분리)

- **구현**: `core/strategy_ensemble.py`
- **설정**: `strategies.yaml` → `ensemble` (mode, confidence_weight), `momentum_factor`, `volatility_condition`
- **사용**: `--strategy ensemble` 시 **서로 다른 정보 소스** 세 가지 신호를 통합.

앙상블은 **기술적 지표 / 모멘텀 팩터 / 변동성 조건**으로 정보 소스를 나누어, 다수결·가중합·보수적 모드가 독립적인 근거의 합의에 가깝게 동작하도록 구성되어 있습니다.

| 구성 전략 | 정보 소스 | 구현 | 설명 |
|-----------|-----------|------|------|
| **technical** | 기술적 지표 | ScoringStrategy | RSI, MACD, 볼린저, 거래량, 이동평균 스코어링 |
| **momentum_factor** | 가격 수익률 | MomentumFactorStrategy | N일 수익률만 사용. 모멘텀 효과(과거 수익률 지속) 기반 |
| **volatility_condition** | 실현변동성 | VolatilityConditionStrategy | N일 실현변동성(연율화)만 사용. 저변동성=매수, 고변동성=매도 |

- **모드**: `majority_vote`(다수결), `weighted_sum`(전략별 가중 후 임계값), `conservative`(세 전략 모두 동일 신호일 때만 매매).
- **추가 개선 여지**: 펀더멘털(실적·재무)·뉴스/센티먼트 등 다른 소스가 추가되면 앙상블의 독립성이 더 높아질 수 있습니다.

**⚠️ 앙상블의 실질적 독립성 문제**

- **문제**: technical(스코어링)은 RSI·MACD·MA 등을 포함하고, momentum_factor는 N일 수익률을 사용합니다. **N일 수익률이 좋은 구간은 이동평균 골든크로스도 발생했을 가능성이 높아**, technical과 momentum_factor가 **같은 상황에서 동시에 BUY**를 내는 경향이 있습니다. 그러면 다수결 의미가 퇴색합니다.
- **대응 (3단계 자동 방어)**:
  1. **런타임 자동 검사**: `StrategyEnsemble.analyze()` 첫 호출 시 세 전략 신호의 Pearson 상관계수를 계산. **|r| ≥ `independence_threshold`** (기본 0.6)인 쌍이 있으면 경고 로그 출력.
  2. **자동 모드 다운그레이드**: `auto_downgrade: true`(기본) + 고상관 감지 시, `majority_vote`/`weighted_sum` → `conservative`로 **자동 전환**. 세 전략 모두 동의해야만 BUY/SELL 실행. `auto_downgrade: false`로 비활성화 가능.
  3. **validate 모드 통합**: `--mode validate --strategy ensemble`로 검증 시, 검증 완료 후 **앙상블 독립성 리포트**가 자동 생성·출력. 고상관 감지 시 Discord 알림.
- **수동 검증**: `python main.py --mode check_ensemble_correlation --symbol 005930 --validation-years 5`
  `core/ensemble_correlation.py`가 앙상블 analyze 결과에서 `signal_technical`, `signal_momentum_factor`, `signal_volatility_condition` 을 수치화해 일별 상관계수 행렬을 계산합니다. 기준값은 `--ensemble-correlation-threshold` 로 변경 가능(기본 0.6).
- **BUY/SELL 동시 발생률**: Pearson 상관계수 외에 "두 전략이 같은 날 BUY한 비율"도 리포트에 포함. 상관계수는 직관적이지 않으므로 동시 발생률로 실전 위험을 체감할 수 있습니다.
- **구체적 대안 전략 권고**: 고상관 쌍이 감지되면 정보 소스가 겹치지 않는 **대안 전략**(예: technical-momentum_factor 고상관 시 → momentum_factor를 mean_reversion 또는 fundamental_factor로 교체)을 리포트에 제시합니다.
- **설정**: `strategies.yaml` → `ensemble`:
  - `auto_downgrade: true` — 고상관 시 conservative 자동 전환
  - `independence_threshold: 0.6` — 상관계수 기준
- **권고**: **0.6 이상**인 쌍이 있으면 다수결만으로는 독립성이 보장되지 않습니다. conservative 전환은 응급 조치이며, **근본적으로는 전략 구성을 재검토**하세요.

### 4.5 팩터 기반 종목 선정 (워치리스트)

- **구현**: `core/watchlist_manager.py`
- **설정**: `config/settings.yaml` → `watchlist.mode`, `watchlist.market`, `watchlist.top_n`, **`watchlist.rebalance_interval_days`**

**학술적으로 검증된 팩터**를 이용해 관심 종목 리스트를 구성할 수 있습니다. 기존 `manual` / `top_market_cap` / `kospi200` 외에 아래 모드를 사용하면 **12개월 수익률(모멘텀)** 및 **저변동성** 팩터로 종목을 선정한 뒤, 기존 전략(scoring, mean_reversion, trend_following)으로 매매 신호를 생성합니다.

| mode | 설명 | 이용 팩터 |
|------|------|-----------|
| **momentum_top** | 12개월 수익률 상위 종목 매수 | 모멘텀(12개월 수익률) |
| **low_vol_top** | 60일 실현변동성 하위 = 저변동성 상위 종목 | 저변동성(60일 실현변동성, 연율화) |
| **momentum_lowvol** | 저변동성 필터 통과 종목 중 12개월 수익률 상위 | 모멘텀 + 저변동성 복합 |

- **모멘텀 팩터**: 과거 12개월(약 252 거래일) 수익률이 높은 종목을 선정. "좋은 주식이 일정 기간 계속 좋다"는 모멘텀 효과에 기반합니다.
- **저변동성 팩터**: 최근 60일 일일 수익률의 표준편차를 연율화(×√252)한 **60일 실현변동성**이 낮은 종목을 선정. 저변동성 주식의 위험 대비 수익이 높다는 실증 연구에 기반합니다.
- **momentum_lowvol**: 후보 풀에서 60일 변동성 중앙값 이하만 남긴 뒤, 12개월 수익률 순으로 상위 `top_n`개를 반환합니다.

**사용 예**: `watchlist.mode: momentum_top`, `watchlist.top_n: 20` 으로 설정하면 시가총액 상위 풀(기본 80여 종목)에서 12개월 수익률을 계산해 상위 20종목을 관심 종목으로 사용합니다. paper/live 모드에서 이 리스트에 대해 기존 전략으로 신호를 생성·실행합니다.

**⚠️ 리밸런싱 주기**

- **문제**: 팩터 기반 모드(momentum_top, low_vol_top, momentum_lowvol)는 매일 재계산하면 **종목 교체가 잦아져 불필요한 거래비용**이 발생합니다. 반대로 너무 드물게 갱신하면 **팩터 효과가 희석**됩니다.
- **대응**: Jegadeesh & Titman(1993) 등 모멘텀 팩터 학술 연구에 따르면 **월 1회 리밸런싱**이 일반적입니다. `watchlist.rebalance_interval_days`(기본 20)를 설정하면, `WatchlistManager`가 마지막 갱신 날짜를 `data/watchlist_cache.json`에 기록하고 **주기가 되었을 때만 재계산**합니다. 주기 내에는 캐시된 종목 리스트를 그대로 사용합니다.
- **설정**: `settings.yaml` → `watchlist.rebalance_interval_days: 20` (캘린더 일수 기준, 20일 ≈ 1개월 거래일). manual / top_market_cap / kospi200 모드에는 적용되지 않습니다.
- **캐시 강제 갱신**: `data/watchlist_cache.json` 파일을 삭제하면 다음 `resolve()` 호출 시 즉시 재계산됩니다.

**⚠️ 유동성 필터 (저유동 종목 진입 제외)**

- **문제**: 시가총액 필터만 있으면 **일평균 거래대금**이 매우 낮은 종목(예: 하루 거래량 1억 원 미만)이 watchlist에 포함될 수 있습니다. 이런 종목은 실전에서 포지션 진입/청산 시 **슬리피지**가 백테스트 가정(0.05%)보다 훨씬 커져, **백테스트 수익이 실전에서 손실**로 바뀌는 대표 원인입니다. `dynamic_slippage`로 일부 보정은 가능하지만, 아예 **진입 대상에서 제외**하는 것이 더 안전합니다.
- **대응 (2단계 필터)**:
  1. **Watchlist 구축 시점**: `WatchlistManager.resolve()` 시 20일 평균 거래대금(`close × volume`) 하한 미만 종목을 제외합니다.
     - **strict 모드** (기본 true): 거래대금 데이터를 조회할 수 없는 종목도 제외. 데이터 없는 종목이 자동 포함되는 위험을 방지합니다.
     - strict=false: 데이터 없으면 통과 (수동 watchlist에서 직접 지정한 종목 유지 용도).
  2. **주문 직전 재검증**: `OrderExecutor._execute_buy_impl()`에서 매수 직전에 `avg_daily_volume × price`로 추정 일평균 거래대금을 재확인합니다. watchlist 구축 이후 유동성이 변했을 수 있으므로, 하한 미만이면 매수를 거부합니다. `check_on_entry: true`(기본)로 활성화.
- **설정**: `config/risk_params.yaml` → `liquidity_filter`:
  - `enabled: true` (권장)
  - `min_avg_trading_value_20d_krw: 5e9` (50억 원)
  - `strict: true` (데이터 없는 종목도 제외)
  - `check_on_entry: true` (매수 직전 재검증)
- **보조**: `dynamic_slippage`가 주문 비중(일평균 거래량의 1%/3%) 기준으로 슬리피지를 동적 상향합니다. 유동성 필터와 함께 사용하면 이중 안전장치.

---

## 5. 리스크 관리

구현: **`core/risk_manager.py`**, 설정: **`config/risk_params.yaml`**

### 5.1 포지션 사이징 — 1% 룰

- **규칙**: 1회 거래 최대 손실 = 자본의 1%
- **설정**: `position_sizing.max_risk_per_trade: 0.01`, `initial_capital`

### 5.2 손절매 (Stop Loss)

- **타입**: `fixed`(고정 비율) 또는 `atr`(변동성 기반). `stop_loss.type`, `fixed_rate`, `atr_multiplier`

### 5.3 익절매 (Take Profit)

- **설정**: `take_profit.fixed_rate`, `partial_exit`, `partial_ratio`, `partial_target` (부분 익절)

### 5.4 트레일링 스탑

- **설정**: `trailing_stop.enabled`, `type`, `fixed_rate`, `atr_multiplier`

### 5.5 분산 투자 (업종별 비중 제한 포함)

- **설정**: `diversification.max_position_ratio`, `max_investment_ratio`, `max_positions`, `min_cash_ratio`, **`max_sector_ratio`**

**⚠️ 포지션 간 상관관계 — 분산 투자가 실제 분산이 아닐 수 있음**

- **문제**: `max_positions`·`max_position_ratio`로 종목 수·비중을 제한해도, momentum_top 등으로 코스피 상위 20종목을 선정하면 **반도체·IT·금융주**가 함께 들어가 시장 하락 시 동시에 급락합니다. 종목이 20개라도 **실질적 리스크는 1~2개 업종에 집중**될 수 있습니다.
- **대응**: `diversification.max_sector_ratio`(기본 0.40 = 40%)를 설정하면, 매수 시 **해당 종목의 업종(KRX Sector)**이 기존 보유 포지션 중 동일 업종 총 투자금과 합산해 총자산 대비 상한을 초과하면 매수를 차단합니다. 업종 정보는 `DataCollector.get_sector_map()`으로 FDR `StockListing('KRX')`의 `Sector` 컬럼을 사용합니다.
- **동작**: `OrderExecutor._execute_buy_impl()` → `RiskManager.check_diversification(symbol=, sector_map=, positions=)` 에서 업종 비중 초과 시 `{"can_buy": False, "reason": "업종 'XXX' 비중 N% > 상한 40%"}` 반환.
- **FDR 미설치·조회 실패**: 업종 매핑이 빈 dict이면 업종 체크는 자동 스킵되어 기존처럼 동작합니다.

### 5.6 최대 보유 기간

- **설정**: `position_limits.max_holding_days` (N일 초과 시 강제 매도, 0이면 비활성)

### 5.7 MDD 제한

- **설정**: `drawdown.max_portfolio_mdd`, `max_daily_loss`, `recovery_scale`

### 5.8 전략 성과 열화 감지

- **설정**: `performance_degradation` (recent_trades, min_win_rate). 최근 N거래 승률이 임계값 미만이면 **신규 매수만** 중단.

### 5.9 거래 비용

- **설정**: `transaction_costs` (commission_rate, tax_rate 0.20% 증권거래세+농특세(2026년~), slippage, capital_gains_tax, dynamic_slippage). 백테스트·실거래 일치를 위해 반드시 반영.

### 5.10 블랙스완 대응 (긴급 청산 + 재진입)

- **구현**: `core/blackswan_detector.py`. 급락 감지 시 전량 매도·디스코드 경고·쿨다운 동안 신규 매수 차단.
- **설정**: `trading.blackswan_recovery_minutes`(기본 120), `blackswan_recovery_scale`(기본 0.5)

**⚠️ 쿨다운 이후 재진입 로직**

- **문제**: 블랙스완 전량 매도 → 쿨다운 만료 후, 시장이 회복되었을 때 다음 모니터링 사이클까지 대기하면 급락 직후 반등 구간을 놓칠 수 있습니다. 또한 곧바로 100% 사이징으로 재진입하면 하락이 더 이어질 때 추가 손실 위험이 있습니다.
- **대응**:
  1. **즉시 신호 재평가**: 쿨다운이 해제되는 순간 `BlackSwanDetector.consume_cooldown_ended_flag()`가 `True`를 반환하고, `Scheduler._run_monitoring()`이 이를 감지해 **워치리스트 전 종목을 즉시 재스캔**(`_run_post_cooldown_rescan`)합니다. 매수 신호가 나오면 진입 후보에 추가되어 같은 사이클에서 실행됩니다.
  2. **점진적 사이징 복구 (recovery)**: 쿨다운 해제 시 `blackswan_recovery_minutes`(기본 120분) 동안 **recovery 기간**에 진입합니다. 이 기간 중 `get_recovery_scale()`이 `blackswan_recovery_scale`(기본 0.5)을 반환하여, 포지션 사이징이 **시장 국면 scale × recovery scale**로 곱연산됩니다. 예: 시장 국면 caution(50%) + recovery(50%) → 사이징 25%.
  3. **recovery 종료 후**: `_recovery_until` 경과 시 자동으로 `1.0` 복귀, 정상 사이징으로 운영됩니다.

### 5.11 실적 발표일(어닝) 필터

- **구현**: `core/earnings_filter.py` → `is_near_earnings(symbol, skip_days)`
- **설정**: `config/settings.yaml` → `trading.skip_earnings_days`(기본 3, 0이면 비활성)

**⚠️ 공시·이벤트 리스크**

- **문제**: 실적 발표일, 유상증자 공시, 주요 계약 공시 등이 발생하면 주가가 단기에 급변합니다. 현재 시스템은 기술적 지표만으로 신호를 내므로, **실적 발표 전날 매수 → 어닝 쇼크로 -10% 갭 하락** 같은 상황에 무방비입니다.
- **대응**: `skip_earnings_days: 3` 으로 설정하면, 매수 주문 실행 전 해당 종목의 다음 실적 발표 예정일을 조회해 **전후 3일 이내**이면 신규 매수를 금지합니다. 기존 포지션의 매도·손절은 정상 동작합니다.
- **데이터 소스**: yfinance `Ticker.calendar`의 `earningsDate`를 사용합니다. **한국 종목은 yfinance에 실적 발표일이 누락되는 경우가 많습니다.** 조회 실패·날짜 미제공 시에는 필터를 통과(매수 허용)시켜, 데이터 부재로 인한 진입 기회 상실을 방지합니다.
- **향후 개선**: pykrx 또는 **KRX 공식 OPEN API(전자공시 DART)** 연동으로 실적 발표 예정일·유상증자 공시 등을 정확하게 가져오면 필터 정확도를 높일 수 있습니다.
- **동작 위치**: `OrderExecutor._execute_buy_impl()` 에서 분산 투자 체크 직전에 실행됩니다.

### 5.12 시장 국면 필터 (단계적 대응 — 3중 신호)

- **구현**: `core/market_regime.py` → `check_market_regime()` (하위 호환: `allow_new_buys_by_market_regime()`)
- **설정**: `config/settings.yaml` → `trading.market_regime_*`

**200일선 단독의 한계**: 200일선은 정의상 매우 느려서, 시장이 본격 하락한 뒤 한참 지나서야 필터가 작동합니다(예: 2020-03 코로나 급락 시 200일선 이탈은 급락 후 수 주 후). 이를 보완하기 위해 **단기 모멘텀 + 단기 MA 크로스를 병행한 3중 신호 단계적 대응**을 적용합니다.

**3가지 독립 신호**:

| 신호 | 조건 | 반응 속도 | 용도 |
|---|---|---|---|
| **A. 200일선 이탈** | 종가 < MA(200) | 느림 (수 주~수 개월) | 장기 추세 확인 |
| **B. 단기 모멘텀 하락** | N일 수익률 ≤ threshold (기본 -5%) | 중간 (수 일) | 급락 즉시 감지 |
| **C. 단기 MA 데드크로스** | MA(20) < MA(60) | 빠름 (1~2주) | 200일선 이탈 전에 추세 전환 포착 |

**국면 판별 로직** — 신호 개수 기준:

| 충족 신호 수 | 예시 | 결과 |
|---|---|---|
| **2개 이상** | A+B, A+C, B+C, A+B+C | **bearish** — 신규 매수 전면 중단 (position_scale=0.0) |
| **1개** | A만, B만, C만 | **caution** — 포지션 사이징 축소 (기본 50%) |
| **0개** | — | **bullish** — 정상 (position_scale=1.0) |

**왜 3중 신호인가?**: 2020년 3월 코로나 급락 사례에서 신호 C(20/60일선 데드크로스)는 200일선 이탈보다 **2~3주 먼저** 트리거됩니다. 신호 B(20일 수익률 -5%)는 급락 당일~수일 내에 트리거됩니다. 이 두 빠른 신호가 조합되면, 200일선이 아직 이탈하지 않아도 **bearish 판정**이 가능하여 조기 방어가 됩니다.

**파라미터** (`settings.yaml` → `trading`):

| 키 | 기본값 | 설명 |
|---|---|---|
| `market_regime_filter` | true | 필터 활성화 여부 |
| `market_regime_index` | KS11 | 기준 지수 (코스피) |
| `market_regime_ma_days` | 200 | 신호 A: 장기 이동평균 일수 |
| `market_regime_short_momentum_days` | 20 | 신호 B: 단기 모멘텀 산출 기간 (거래일) |
| `market_regime_short_momentum_threshold` | -5.0 | 신호 B: 해당 기간 수익률(%) 이하 시 하락 판단 |
| `market_regime_caution_scale` | 0.5 | caution 국면에서 포지션 사이징 배수 |
| `market_regime_ma_cross_enabled` | true | 신호 C: 단기 MA 크로스 활성화 여부 |
| `market_regime_ma_short` | 20 | 신호 C: 단기 이동평균 일수 |
| `market_regime_ma_mid` | 60 | 신호 C: 중기 이동평균 일수 (short < mid 이면 데드크로스) |

**하위 호환**: 신호 C를 비활성화(`market_regime_ma_cross_enabled: false`)하면 기존 2-신호(A+B) 로직과 동일하게 동작합니다. 기존 포지션의 매도·손절·익절·트레일링 스탑은 국면과 무관하게 그대로 동작합니다. paper/live 모드 및 스케줄러 장전·장중 진입 시 지수 데이터를 조회해 국면을 판별하며, 조회 실패 시 보수적으로 신규 매수를 허용합니다(API 장애로 인한 진입 기회 상실 방지). 비활성화하려면 `market_regime_filter: false` 로 설정하면 됩니다.

---

## 6. 시스템 아키텍처 및 프로젝트 구조

### 6.1 계층별 구조

```
┌─────────────────────────────────────────────────────────────────┐
│                      📊 모니터링 레이어                          │
│ 통합 알림(Discord→Telegram→Email) │ 수익률 로깅 │ 웹 대시보드(aiohttp) │
├─────────────────────────────────────────────────────────────────┤
│                      ⚡ 실행 레이어                              │
│     주문 생성 │ OrderGuard·미체결 확인 │ 재시도(지수 백오프)       │
├─────────────────────────────────────────────────────────────────┤
│                      🛡️ 리스크 관리 레이어                       │
│     손절/익절/트레일링 스탑 │ 포지션 사이징 │ MDD·성과열화·시장 국면 필터 │
├─────────────────────────────────────────────────────────────────┤
│                      🎯 전략 레이어                              │
│     스코어링/평균회귀/추세추종/앙상블(기술+모멘텀+변동성) │ generate_signal │
├─────────────────────────────────────────────────────────────────┤
│                      🔬 분석 엔진                                │
│     IndicatorEngine │ SignalGenerator │ strategies.yaml 가중치   │
├─────────────────────────────────────────────────────────────────┤
│                      💾 데이터 레이어                             │
│     DataCollector(FinanceDataReader/yfinance/KIS) │ SQLAlchemy    │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 실제 프로젝트 디렉토리 및 파일 역할

```
quant_trader/
├── main.py                      # CLI 진입점. --mode 로 backtest/validate/paper/live/liquidate/compare/optimize/dashboard/check_correlation/check_ensemble_correlation 분기
├── test_integration.py          # 통합 검증 스크립트 (설정·DB·지표·백테스트·디스코드 등 일괄 점검, 단일 실행)
├── pyproject.toml               # 프로젝트 메타데이터 (Python >=3.11,<3.13, 패키지 구성, pytest 설정)
├── requirements.txt             # pip 의존성 목록 (pandas, numpy, pandas-ta, pykrx, yfinance, sqlalchemy 등)
├── .env.example                 # 환경변수 템플릿 (KIS API 키, 디스코드, 텔레그램, 이메일, 긴급청산 토큰)
├── .gitignore                   # .env, settings.yaml, data/, logs/, *.db, reports/*, fintics/ 등 제외
├── README.md                    # 프로젝트 소개·빠른 시작·실행 예시
├── quant_trader_design.md       # 전체 아키텍처·지표·전략·리스크 설계서 (본 문서)
├── config/
│   ├── __init__.py
│   ├── config_loader.py         # YAML 통합 로더. settings/strategies/risk_params 로드, .env 덮어쓰기, Config.get() 싱글톤
│   ├── settings.yaml.example    # 설정 예시 (실제 settings.yaml 은 .gitignore)
│   ├── settings.yaml            # KIS API, database, logging, data_source, trading, discord, telegram, dashboard, watchlist
│   ├── strategies.yaml          # indicators, scoring, mean_reversion, trend_following, momentum_factor, volatility_condition, ensemble 파라미터
│   ├── risk_params.yaml         # backtest_universe, liquidity_filter, 포지션/손절/익절/트레일링/분산/MDD/성과열화/거래비용
│   ├── holidays.yaml.example    # 휴장일 예시
│   └── holidays.yaml            # 휴장일 (--update-holidays 로 pykrx+fallback 자동 갱신)
├── core/
│   ├── __init__.py
│   ├── data_collector.py        # 한국/미국 주가 수집. FDR→yfinance→KIS 폴백, 소스 추적·수정주가 일치 검증. get_krx_stock_list(universe_mode=current|historical|kospi200), get_sector_map()
│   ├── watchlist_manager.py     # 관심 종목: manual/top_market_cap/kospi200/momentum_top/low_vol_top/momentum_lowvol + 유동성 필터 + 리밸런싱 주기(캐시) + as_of_date 지원(백테스트 시 과거 유니버스)
│   ├── indicator_engine.py      # pandas-ta: RSI, MACD, 볼린저, MA(SMA/EMA), 스토캐스틱, ADX, ATR, OBV, volume_ratio. calculate_all(df)
│   ├── signal_generator.py      # 멀티 지표 스코어링 신호 (BUY/SELL/HOLD, score, score_details). collinearity_mode(representative_only 권장)
│   ├── risk_manager.py          # 포지션 사이징(1% 룰), check_diversification(업종 비중 포함), check_recent_performance, 손절/익절/트레일링, 거래비용
│   ├── order_executor.py        # 매수/매도 실행. paper: DB만, live: KIS API. PositionLock, OrderGuard, 미체결 확인, 유동성·어닝 필터
│   ├── portfolio_manager.py     # 보유 포지션·잔고·수익률. sync_with_broker(KIS 잔고↔DB 크로스체크), save_daily_snapshot()
│   ├── scheduler.py             # 실전 무한 루프: 장전/장중(10분 간격)/장마감. 시장 국면 필터, 블랙스완 recovery, paper 모드 실전 전환 자동 평가
│   ├── trading_hours.py         # 장 시간·휴장일 판별 (holidays.yaml → pykrx → fallback)
│   ├── holidays_updater.py      # 휴장일 YAML 자동 갱신 (pykrx 또는 fallback)
│   ├── blackswan_detector.py    # 급락 감지 → 전량 매도·쿨다운·recovery(점진적 재진입, recovery_scale)
│   ├── market_regime.py         # 시장 국면 필터: 3중 신호(200일선 + 단기 모멘텀 + MA 크로스) → bearish/caution/bullish
│   ├── fundamental_loader.py    # 펀더멘털(PER·부채비율) 조회 — pykrx(우선) → yfinance(폴백). 평균회귀 필터용
│   ├── earnings_filter.py       # 실적 발표일 필터 (전후 N일 신규 매수 금지, yfinance earningsDate)
│   ├── indicator_correlation.py # 스코어링 지표 상관계수 분석·고상관 쌍 제거 권고 (check_correlation 모드)
│   ├── ensemble_correlation.py  # 앙상블 전략 신호 상관계수 + BUY 동시 발생률 + 대안 전략 권고 + auto_downgrade
│   ├── strategy_ensemble.py     # 앙상블: technical + momentum_factor + volatility_condition (정보 소스 분리, auto_downgrade)
│   ├── data_validator.py        # OHLCV 정합성 검사 (Null, NaN, 음수 주가, 타임스탬프 역전 등)
│   ├── notifier.py              # 통합 알림 이중화 (1차 디스코드 → 2차 텔레그램 → 3차 이메일, critical 시 전채널 동시 발송)
│   ├── position_lock.py         # threading.RLock (포지션/주문 동시 접근 제어)
│   └── order_guard.py           # 동일 종목 TTL(기본 600초) 동안 중복 주문 차단
├── strategies/
│   ├── __init__.py
│   ├── base_strategy.py         # 추상 클래스. analyze(df), generate_signal(df, **kwargs)
│   ├── scoring_strategy.py      # IndicatorEngine + SignalGenerator, 멀티 지표 스코어링 전략
│   ├── mean_reversion.py        # Z-Score·ADX·52주 이중 필터·코스피200 제한·펀더멘털 필터 평균 회귀
│   ├── trend_following.py       # ADX·200일선·MACD·ATR 추세 추종
│   ├── momentum_factor.py       # 모멘텀 팩터 (N일 수익률만 사용, 앙상블용)
│   └── volatility_condition.py  # 변동성 조건 (N일 실현변동성만 사용, 앙상블용)
├── api/
│   ├── __init__.py
│   ├── kis_api.py               # KIS REST API: 토큰·시세·주문·잔고·일봉. 이중 Rate Limiter(Token Bucket 초당 + 슬라이딩 윈도우 분당) + 사용량 모니터링 + Circuit Breaker
│   ├── websocket_handler.py     # KIS 웹소켓 실시간 체결/호가 (asyncio, Heartbeat 45초, 자동 재연결)
│   └── circuit_breaker.py       # CLOSED → OPEN → HALF_OPEN. API 연속 5회 실패 시 60초 차단, Notifier 알림
├── backtest/
│   ├── __init__.py
│   ├── backtester.py            # 시뮬레이션: strict_lookahead 기본, 수수료·세금·슬리피지·동적 슬리피지·손절/익절/트레일링, 과매매 분석
│   ├── report_generator.py      # txt·html 리포트 (거래 내역, 성과 지표, 자본 곡선, 과매매 분석)
│   ├── strategy_validator.py    # validate: 3~5년 데이터, 샤프·MDD·벤치마크(KS11·코스피 상위 50 동일비중), in/out-of-sample, 손익비 자동 경고+디스코드
│   ├── paper_compare.py         # 모의투자 vs 백테스트 비교, divergence 경고, 실전 전환 준비 자동 평가(check_live_readiness)
│   └── param_optimizer.py       # Grid / Bayesian(scikit-optimize) 파라미터 최적화, train_ratio·OOS 보고, 가중치 대칭 Grid Search
├── database/
│   ├── __init__.py
│   ├── models.py                # ORM 모델 5종(StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport). SQLite WAL/PostgreSQL 지원, scoped_session, @with_retry, db_session()
│   ├── repositories.py          # CRUD — 읽기·쓰기 전체 함수 @with_retry 적용, get_paper_performance_metrics (compare 모드)
│   └── backup.py                # SQLite Online Backup API로 WAL 안전 백업 (실패 시 shutil 폴백 + -wal/-shm 포함), 보관 일수 자동 삭제
├── monitoring/
│   ├── __init__.py
│   ├── logger.py                # loguru 초기화 (파일 로테이션·콘솔 출력), log_trade(), log_signal()
│   ├── discord_bot.py           # 디스코드 웹훅 전송 (매매·일일 리포트·블랙스완·동기화 불일치). Notifier를 통해 호출 권장
│   ├── liquidate_trigger.py     # HTTP POST /liquidate 로 긴급 전량 매도 트리거 (X-Token 또는 ?token= 인증)
│   ├── dashboard.py             # 콘솔 대시보드 (선택, show_summary_line)
│   └── web_dashboard.py         # aiohttp 웹 대시보드 (포트폴리오·스냅샷 JSON/HTML, 10초 폴링)
├── tests/
│   ├── __init__.py
│   ├── test_backtester_strategies.py    # 백테스터 전략별 시뮬레이션 검증
│   ├── test_backtester_trailing_stop.py # 트레일링 스탑 로직 검증
│   ├── test_blackswan_detector.py       # 블랙스완 감지·쿨다운 로직 검증
│   ├── test_discord_bot.py              # 디스코드 알림 모킹·콘솔 fallback
│   ├── test_integration_smoke.py        # 설정·DB·지표·신호 등 연동 스모크 테스트
│   ├── test_kis_websocket_e2e.py        # KIS API·웹소켓 모의 E2E 테스트
│   ├── test_order_executor_paper.py     # OrderExecutor paper 모드 검증
│   ├── test_portfolio_manager.py        # 포트폴리오·sync 검증
│   ├── test_risk_manager.py             # 리스크 매니저 (포지션·손절·동적 슬리피지 등)
│   ├── test_scheduler.py                # 스케줄러 구간·동작 검증
│   ├── test_signal_generator.py         # 신호 생성·스코어링 검증
│   ├── test_strategy_validator.py       # 전략 검증(validate) 로직 검증
│   ├── test_trading_hours.py            # 장 시간·휴장일 검증
│   └── test_watchlist_manager.py        # watchlist 모드별 resolve 검증
├── docs/
│   └── PROJECT_GUIDE.md         # 파일별 역할·실행 모드·데이터 흐름 상세
└── reports/                     # 백테스트 txt/html 출력 (.gitignore로 제외)
```

### 6.3 저장소 관리 (Git)

- **커밋 대상**: Python 소스(`*.py`), 설정 예시(`*.example`), `requirements.txt`, `pyproject.toml`, `README.md`, 문서(`*.md`).
- **커밋 제외 (.gitignore)**:
  - `.env`, `config/settings.yaml` — 비밀·환경 정보
  - `__pycache__/`, `.venv/`, `.pytest_cache/` — Python 런타임
  - `data/`, `logs/`, `*.db` — 데이터·로그
  - `reports/backtest_*.html`, `reports/backtest_*.txt`, `reports/*.md` — 백테스트 산출물
  - `fintics/` — 외부 프로젝트 (본 저장소는 quant_trader 소스만 관리)
  - `.idea/`, `.vscode/` — IDE 설정

### 6.4 핵심 설계 원칙

| 원칙 | 설명 |
|------|------|
| **모듈화** | 계층별 독립 교체·테스트 가능 |
| **설정 외부화** | YAML(config) + .env. 코드 수정 없이 전략·리스크 조정 |
| **Look-Ahead Bias 방지** | 백테스트 strict_lookahead 기본 True (시점별 슬라이싱) |
| **장애 복구** | 재시도, Circuit Breaker, OrderGuard, 미체결 확인, KIS↔DB 크로스체크 |
| **로깅 필수** | 신호 점수·주문 사유·손절 사유 등 상세 로그 |

---

## 7. 실행 모드 및 CLI

진입점: **`main.py`**. 인자: `--mode`, `--strategy`, `--symbol`, `--start`, `--end` 등.

| 모드 | 설명 | 핵심 호출 |
|------|------|-----------|
| **backtest** | 백테스트 실행 | `run_backtest()` → DataCollector → Backtester.run(strict_lookahead 기본) → ReportGenerator |
| **validate** | 전략 검증 (3~5년, 샤프·MDD·벤치마크·in/out-of-sample). `--walk-forward` 시 워크포워드 | `run_strategy_validation()` → StrategyValidator.run / run_walk_forward |
| **paper** | 모의투자 (DB 기록 + 디스코드, 실제 주문 없음) | `run_paper_trading()` → WatchlistManager, 전략.generate_signal, OrderExecutor(paper) |
| **live** | 실전 매매 (ENABLE_LIVE_TRADING=true + --confirm-live 필수) | `run_live_trading()` → KIS 인증 → Scheduler.run() |
| **liquidate** | 긴급 전 종목 매도 | `run_emergency_liquidate()` → DB 포지션 조회 → 종목별 매도 |
| **compare** | 모의투자 vs 백테스트 비교 + **실전 전환 준비 평가** | `run_compare_paper_backtest()` → paper_compare.run_compare + check_live_readiness |
| **optimize** | 전략 파라미터 최적화 (grid / bayesian / 가중치 대칭 Grid) | `run_param_optimize()` → param_optimizer, train_ratio·OOS |
| **dashboard** | 웹 대시보드 기동 | `run_dashboard()` → monitoring.web_dashboard (aiohttp, 기본 8080) |
| **check_correlation** | 스코어링 지표 간 상관계수·독립성 검증 (0.7 이상 쌍 제거/가중치 축소 권고) | `run_check_indicator_correlation()` → core.indicator_correlation |
| **check_ensemble_correlation** | 앙상블 전략 신호 상관계수 + BUY 동시 발생률 검증. 0.6 이상이면 conservative 전환 또는 재구성 권고 | `run_check_ensemble_correlation()` → core.ensemble_correlation |

**기타 CLI 옵션**:
- `--update-holidays` → 휴장일 YAML 갱신 후 종료
- `--allow-lookahead` → strict-lookahead 해제 (경고 출력, 권장하지 않음)
- `--include-weights` → optimize 모드에서 스코어링 가중치도 탐색
- `--auto-correlation` → optimize 전 상관 분석 자동 실행, 고상관 지표 자동 비활성화
- `--disable-weights w_rsi,w_ma` → 특정 가중치 키를 0으로 고정
- `--walk-forward` → validate 모드에서 슬라이딩 윈도우 워크포워드 검증
- `--no-benchmark-top50` → validate 모드에서 코스피 Top50 벤치마크 비활성화
- `--confirm-live` → live 모드 진입 시 필수 확인 플래그

---

## 8. 백테스팅 & 검증

### 8.1 핵심 성과 지표 (KPI)

| 지표 | 설명 | 목표치 |
|------|------|--------|
| 총 수익률 | 누적 수익 | > 20% / 년 |
| 샤프 지수 | 위험 대비 수익 | > 1.5 (검증 시 최소 1.0) |
| MDD | 고점 대비 최대 하락 | < 20% |
| 승률 | 수익 거래 비율 | > 50% |
| 손익비 (Profit Factor) | 평균 수익/평균 손실 | > 2.0 |
| 칼마 비율 | 연 수익률/MDD | > 1.0 |
| 평균 보유 기간 | 매수→매도 일수 평균 | 전략별·과매매 점검용 |
| 총 수수료 | 전체 거래 수수료 합계 | 총 거래 횟수 대비 과매매 점검 |

### 8.2 검증 절차

- 과거 3~5년 데이터 → 훈련/검증 분리 → 파라미터 최적화 → OOS 검증 → 거래비용·슬리피지 반영 → 페이퍼 트레이딩 → 소액 실전.
- **벤치마크 비교**: 코스피 지수(KS11) 대비 초과 수익 여부에 더해, **코스피 상위 50종목 동일비중 매수·홀딩** 대비 out-of-sample 초과 수익 여부를 검증합니다. Top50 벤치마크는 `--mode validate` 시 기본 사용하며, `--no-benchmark-top50` 으로 비활성화할 수 있습니다. 벤치마크·유니버스 종목 리스트는 **검증 시작일(as_of_date)** 기준으로 가져오며, `risk_params.backtest_universe` 설정에 따라 **생존자 편향**을 완화할 수 있습니다(아래 §8.2.1 참고).

**⚠️ 생존자 편향 (Survivorship Bias) — §8.2.1**

- **문제**: 현재 상장 종목만으로 백테스트/벤치마크를 구성하면, 기간 중 **상장폐지·관리종목**이 제외되어 수익률이 **과대평가**될 수 있습니다. 코스닥 소형주·top_market_cap·momentum_top 등 자동 선정 모드에서 특히 치명적입니다. 실전에서는 그 망한 종목에도 투자했을 것이므로, 살아남은 종목만의 성과는 허구일 수 있습니다.
- **대응**:
  1. **관리종목 제외**: `risk_params.backtest_universe.exclude_administrative: true`(기본)로 FDR `KRX-ADMINISTRATIVE` 목록을 제외합니다.
  2. **과거 시점 전체 종목 유니버스 (권장)**: `backtest_universe.mode: historical`로 설정하면 백테스트 시작일(as_of_date) 기준으로 **당시 상장되어 있던 KOSPI+KOSDAQ 전체 종목**을 pykrx `get_market_ticker_list(date)` 로 가져옵니다. 상장폐지된 종목도 포함되어 **생존자 편향을 실질적으로 제거**합니다. `WatchlistManager`에 `as_of_date`가 주어지고 mode가 `current`이면 자동으로 `historical`로 전환됩니다.
  3. **코스피200 유니버스**: `backtest_universe.mode: kospi200` 으로 설정하면 해당 일자 **코스피200 구성종목**(pykrx)만 사용합니다. 대형주 위주라 상장폐지가 적어 편향을 줄일 수 있습니다.
  4. **시점 기준 벤치마크**: 전략 검증(`--mode validate`) 시 **검증 시작일(as_of_date)** 기준으로 종목 리스트를 가져와 Top50/벤치마크를 구성합니다.
- **설정**: `config/risk_params.yaml` → `backtest_universe.mode` (`current` | `historical` | `kospi200`), `exclude_administrative` (true 권장). historical/kospi200 사용 시 pykrx 설치 필요.
- **경고**: `mode: current` 상태로 백테스트를 실행하면 콘솔에 생존자 편향 경고가 출력됩니다.

**⚠️ 검증 방법 자체의 한계**

- **기준이 통과해도 실전 수익이 안 날 수 있음**: `--mode validate` 조건(샤프 ≥ 1.0, MDD 기준, 벤치마크 초과 수익)을 만족해도, 아래 상황에서는 **실전에서 손실**이 날 수 있습니다.
  1. **검증 기간(3~5년)이 해당 전략에 유리한 시장 국면이었던 경우**: 그 기간이 우연히 상승장·특정 변동성 구간이었다면, 검증 통과는 **국면 편향**일 수 있습니다. 이후 국면이 바뀌면 성과가 반전될 수 있습니다.
  2. **파라미터 최적화 후 검증한 경우**: 학습 구간에서 최적화한 뒤 OOS로 검증해도, **OOS 구간이 같은 시대(같은 시장 환경)** 이면 OOS에서도 성과가 높게 나오도록 **간접적으로 과적합**되었을 수 있습니다. 진정한 "미래" 구간이 아니므로 실전 이탈 가능성이 남습니다.
- **권장**: 검증 통과를 **필요 조건**으로 두되 **충분 조건으로 해석하지 말 것**. 가능하면 **여러 시장 국면(상승·하락·횡보)** 이 포함된 기간으로 검증하거나, **walk-forward**·롤링 검증을 고려하고, 실전은 **소액·보수적**으로 시작하는 것을 권장합니다.

**손익비(Profit Factor) 자동 경고**

- 설계서에서 추세 추종 전략은 **손익비 ≥ 2.0**을 검증하라고 명시하고 있습니다. `StrategyValidator`가 검증 완료 시 자동으로 확인합니다.
- **추세 추종(`trend_following`)**: FULL 또는 OOS 기간 `profit_factor < 2.0`이면 `WARN: 추세 추종 전략 손익비 미달` 경고 발생.
- **기타 전략**: `profit_factor < 1.0`이면 순손실 구조 경고 발생.
- **워크포워드 검증**: 각 테스트 창별로 손익비가 기준 미달 시 창별 경고 발생.
- 경고는 (1) 콘솔 로그(`loguru.warning`), (2) 리포트 텍스트 파일 하단, (3) **디스코드 알림**으로 자동 전송됩니다.
- 리포트에는 `손익비(Profit Factor): FULL X.XX | OOS X.XX` 행과 `⚠️ 경고` 섹션이 표시됩니다.

**워크포워드(Walk-Forward) 검증**

- **기본 검증**: `--mode validate` (옵션 없음)는 전체 구간을 **한 번만** train(기본 70%) / test(30%) 로 나눕니다.
- **워크포워드 검증**: `--mode validate --walk-forward` 로 **슬라이딩 윈도우** 반복 검증을 수행합니다. `strategy_validator.run_walk_forward()`: train 2년(504일) → test 1년(252일), 1년(252일) 스텝으로 슬라이드해 여러 구간에서 테스트합니다. 예: 2019~2020 훈련 → 2021 테스트, 2020~2021 훈련 → 2022 테스트, … 각 테스트 구간에서 샤프·MDD 기준 통과 여부를 보고, **전체 통과** 또는 **80% 이상 창 통과** 시 검증 성공으로 볼 수 있습니다. 리포트는 `reports/validation_walkforward_*.txt` 에 저장됩니다.
- **권장**: 검증 신뢰도를 높이려면 **워크포워드** (`--walk-forward`) 를 사용하고, 대부분의 창에서 통과하는지 확인하세요.

### 8.3 거래 비용 반영

- 수수료 0.015%, 증권거래세+농특세 0.20%(매도, 2026년~ 코스피·코스닥 동일), 슬리피지(기본 0.05%, 거래량 기반 동적 배수). `risk_params.yaml` → `transaction_costs`.

**⚠️ 거래 빈도와 수수료의 관계 (공통 문제)**

- **왕복 비용**: 매수·매도 합쳐 **약 0.23%**(수수료 0.015%×2 + 증권거래세 0.20%) 수준입니다(2026년 기준). 이를 상회하려면 **매 거래마다 평균 0.23% 이상의 초과 수익**이 나와야 합니다. 일봉 기반 전략에서 매번 달성하기는 쉽지 않습니다.
- **10분마다 신호 확인**: 실전 스케줄러는 장중 **10분 간격**으로 신호를 확인하고 매매를 실행합니다. 신호가 자주 바뀌는 전략은 **과매매(Over-trading)** 가 되어, 수수료만 나가는 상황이 될 수 있습니다.
- **스코어링 전략**: 임계값 근처에서 신호가 BUY ↔ HOLD ↔ SELL 로 자주 바뀌기 쉽습니다. 백테스트에서 **거래 횟수·연간 왕복 수**를 확인하고, 수수료를 감안한 후 **순수익이 양수**인지 반드시 점검하세요. 필요 시 임계값을 완화해 진입/청산 빈도를 낮추는 것을 고려하세요.
- **권장**: 전략별로 "거래 1회당 기대 초과 수익 > 왕복 비용"이 성립하도록 **진입/청산 조건을 보수적으로** 두거나, **최소 보유 기간·신호 안정화(히스터리시스)** 등을 도입해 불필요한 왕복을 줄이는 설계를 권장합니다.

### 8.4 Paper → Live 전환 준비 자동 평가

현재 "1~2개월 paper 후 실전" 전환은 수동 판단에 의존합니다. `paper_compare.check_live_readiness()`가 이를 자동화합니다.

**평가 기준** (`risk_params.yaml` → `paper_backtest_compare.live_readiness`):

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| `min_direction_agreement_pct` | 70 | paper와 backtest 일별 수익률 방향성 일치율 ≥ 70% |
| `max_return_diff_pct` | 5 | 누적 수익률 차이 ≤ 5%p |
| `min_trading_days` | 20 | 최소 평가 거래일수 (약 1개월) |
| `min_trades` | 5 | 최소 매도 거래 건수 |
| `notify_on_ready` | true | 준비 완료 시 디스코드 알림 |
| `auto_check_in_scheduler` | true | paper 모드 장마감 시 자동 체크 (최근 30일) |

**방향성 일치율**: 동일 날짜의 paper 포트폴리오 일별 수익률과 backtest equity 일별 수익률이 같은 방향(둘 다 +, 둘 다 -)인 비율. 70% 이상이면 전략 실행 로직이 백테스트와 충분히 일치한다고 판단합니다.

**동작 방식**:
1. **수동**: `--mode compare` 실행 시 divergence 비교 후 자동으로 readiness 평가도 수행. 결과를 콘솔에 출력하고, 준비 완료 시 디스코드 Embed 알림 전송.
2. **자동**: paper 모드 Scheduler의 장마감(`_run_post_market`) 시 `_check_live_readiness()`가 최근 30일 기준으로 자동 평가. 준비 완료 시 디스코드 알림.
3. 모든 기준이 충족되면 `"✅ 실전 전환 준비 완료"` 신호가 발생하며, 미달 시 어떤 기준이 부족한지 상세 사유를 제공합니다.

**주의**: 이 신호는 의사결정 보조 도구이며, 최종 실전 전환은 사용자가 직접 판단해야 합니다. 특히 paper 기간이 특정 시장 국면에만 해당하는 경우 실전에서 결과가 달라질 수 있습니다.

---

## 9. 예외 처리 및 안정성

- **API**: Circuit Breaker (`api/circuit_breaker.py`), 지수 백오프 재시도, 토큰 만료 시 알림.
- **웹소켓**: 자동 재연결·Heartbeat (구현 시).
- **데이터**: `core/data_validator.py` 로 Null/NaN/음수 주가 필터링.
- **알림 이중화**: `core/notifier.py` — 1차 디스코드 → 2차 텔레그램 Bot API → 3차 이메일(SMTP). `critical=True` 이벤트(블랙스완, 서킷브레이커)는 **가용한 모든 채널에 동시 발송**. 디스코드 웹훅 장애 시에도 텔레그램 또는 이메일로 알림 수신 보장.
- **비밀**: `.env` + `os.environ`, 설정 파일에 하드코딩 금지.
- **주문**: OrderGuard(TTL)·KIS 미체결 조회로 중복 주문 방지; 루프 10분 초과 시 다음 사이클 스킵.

**⚠️ 알림 이중화 — 디스코드 장애 대비**

- **문제**: 디스코드 웹훅은 무료이지만 가끔 장애가 발생합니다. 블랙스완이 발생했는데 알림을 못 받으면 치명적입니다.
- **대응**: `core/notifier.py`의 `Notifier` 클래스가 모든 알림을 관리합니다. `Scheduler`, `CircuitBreaker`, `main.py` 등 주요 모듈은 `DiscordBot` 대신 `Notifier`를 사용합니다.
  - **일반 알림**: 디스코드 발송 → 실패 시 텔레그램 → 실패 시 이메일 순서 fallback.
  - **치명적 알림** (`critical=True`): 블랙스완 발동, 서킷브레이커 오픈, 큰 손절(-5% 이하) 등은 디스코드·텔레그램·이메일 **모두 동시 발송**.
  - **실패 누적 감시**: 알림 실패가 5회 이상 누적되면 "알림 경로 점검 필요" 경고를 이메일로 발송.
- **설정**:
  - 텔레그램: `settings.yaml` → `telegram.enabled`, `bot_token`, `chat_id` (또는 환경변수 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
  - 이메일: 환경변수 `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`
  - 세 채널 모두 선택적이며, 설정된 채널만 사용됩니다.

**⚠️ SQLite 동시성 (실전 다중 접근)**

- **문제**: 실전 모드에서 Scheduler(장중 10분 루프), LiquidateTrigger(HTTP), web_dashboard(aiohttp 10초 폴링)가 **동시에** SQLite에 접근합니다. SQLite는 **파일 단위 write lock**이라 동시 쓰기 시 `database is locked` 오류가 발생할 수 있습니다. PositionLock(threading.RLock)은 Python 스레드 내에서만 보호하며, aiohttp는 별도 이벤트 루프에서 동작할 수 있어 보호 범위 밖입니다.
- **대응 (7단계 방어)**:
  1. **WAL 모드**: `PRAGMA journal_mode=WAL` — 읽기와 쓰기가 동시에 가능. `PRAGMA synchronous=NORMAL` — WAL에서 안전하면서 쓰기 성능 향상.
  2. **busy_timeout=30s**: 다른 연결이 write lock을 잡고 있으면 최대 30초 대기 후 예외.
  3. **scoped_session**: SQLAlchemy `scoped_session`으로 **스레드별 독립 세션** 보장. Scheduler·aiohttp·LiquidateTrigger가 각각 자기 세션을 받아 세션 충돌 방지.
  4. **`@with_retry` 데코레이터**: **읽기·쓰기 전체 함수**에 적용. busy_timeout 초과 시 **최대 3회 지수 백오프 재시도** (1초→2초→4초). WAL 체크포인트 중 일시적 locked에도 읽기 함수가 안전.
  5. **커넥션 풀**: `check_same_thread=False` + `pool_pre_ping=True`로 커넥션 상태를 재사용 전 확인.
  6. **컨텍스트 매니저**: `db_session()` 제공 — `with db_session() as session:` 으로 commit/rollback/close 자동 처리.
  7. **안전 백업**: `backup.py`가 **SQLite Online Backup API** (`sqlite3.Connection.backup()`)를 사용해 WAL 모드에서도 일관된 스냅샷을 보장하는 백업 수행. 실패 시 `-wal`/`-shm` 파일을 포함한 `shutil.copy2` 폴백.
- **초기화 검증**: `init_database()` 시 WAL 모드 활성화 여부를 검증하고, **WAL이 아니면 ERROR 로그**를 출력합니다 (네트워크 드라이브 등에서 WAL이 지원되지 않을 수 있음).
- **중기 검토**: 실전 운영이 안정화되면 **PostgreSQL** 전환 권장. `settings.yaml` → `database.type: "postgresql"` + `postgresql:` 섹션 주석 해제만으로 전환 가능 (SQLAlchemy ORM 동일, `pool_size=5`, `pool_pre_ping=True`).

**⚠️ KIS API 요청 한도 (Rate Limit)**

- **문제**: KIS API는 **초당/분당/일당** 요청 수 제한이 있습니다(예: 초당 20건). momentum_top·kospi200 모드로 20~50종목을 관리할 경우, 장중 10분마다 **종목별 데이터 수집 + 포지션 조회 + 잔고 조회** 등을 한꺼번에 실행하면 한도를 초과할 수 있습니다. 한도 초과로 API 키가 일시 차단되면 Circuit Breaker가 열려 그 시간 동안 모든 주문이 불가능해집니다.
- **대응 (이중 Rate Limiter + 모니터링)**:
  1. **Token Bucket (초당)**: `_wait_for_token()`으로 초당 허용 건수(`max_calls_per_sec`, 기본 10)를 넘지 않도록 버스트 제어.
  2. **슬라이딩 윈도우 (분당)**: `_wait_for_minute_window()`로 최근 60초 내 요청 수가 `max_calls_per_min` (기본 300)을 초과하면 가장 오래된 요청이 윈도우를 벗어날 때까지 대기. Token Bucket만으로는 분당 한도 위반 가능(10건/초 × 60초 = 600건 > 분당 한도).
  3. **429 재시도**: `Retry-After` 헤더만큼 대기 후 자동 재시도. 429 누적 횟수 추적.
  4. **사용량 모니터링**: `get_rate_limit_stats()` — 최근 60초 요청 수, 분당 활용률(%), 누적 요청·429 횟수, 평균 초당 요청.
  5. **Scheduler 사전 예측**: 장전/장중 분석 시작 전 `종목 수 × 2(예상 요청)`을 계산하여 예상 소요 시간과 분당 한도 초과 여부를 로그. 분석 후 실제 사용량 출력.
- **설정**: `settings.yaml` → `kis_api.max_calls_per_sec` (기본 10), `kis_api.max_calls_per_min` (기본 300). 환경변수 `MAX_CALLS_PER_SEC`, `MAX_CALLS_PER_MIN`으로 덮어쓰기 가능.
- **종목 수가 많을 때**: 초당 10건이면 50종목은 약 5~10초, 200종목은 약 20~40초에 걸쳐 자동 분산. 분당 한도 300건 초과 시 자동 대기 발생 후 계속 진행.

---

## 10. 개발 로드맵 & 현재 구현 상태

### 현재 구현 완료

- [x] Python 프로젝트 구조, Config(YAML+.env), SQLite·SQLAlchemy
- [x] KIS API 인증·시세·주문·잔고, 웹소켓 핸들러, Circuit Breaker, **이중 Rate Limiter(초당 Token Bucket + 분당 슬라이딩 윈도우) + 사용량 모니터링**
- [x] DataCollector (한국/미국, KRX 리스트), WatchlistManager (manual/top_market_cap/kospi200/momentum_top/low_vol_top/momentum_lowvol)
- [x] IndicatorEngine (RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV)
- [x] SignalGenerator, ScoringStrategy, MeanReversion, TrendFollowing, MomentumFactor, VolatilityCondition, StrategyEnsemble(기술+모멘텀+변동성)
- [x] RiskManager (포지션 사이징, 분산, 성과 열화, 손절/익절/트레일링, 거래 비용)
- [x] Backtester (strict-lookahead, 수수료·세금·슬리피지·동적 슬리피지)
- [x] StrategyValidator, ReportGenerator, PaperCompare(**실전 전환 준비 자동 평가 포함**), ParamOptimizer
- [x] OrderExecutor (paper/live), PositionLock, OrderGuard, 미체결 확인
- [x] PortfolioManager, sync_with_broker, Scheduler (장전/장중/장마감)
- [x] BlackSwanDetector, MarketRegime(시장 국면 필터, 단계적 대응), **EarningsFilter(실적 발표일 필터)**, TradingHours, HolidaysUpdater, DataValidator, FundamentalLoader
- [x] **통합 알림 이중화(Notifier: Discord→Telegram→Email, critical 동시 발송)**, 웹 대시보드, LiquidateTrigger, DB 백업
- [x] test_integration.py, pytest 테스트 suite

### 향후 개선 (선택)

- [x] **워크포워드(슬라이딩 윈도우) 검증**: `--mode validate --walk-forward` 및 `StrategyValidator.run_walk_forward()` (§8.2).
- [x] **벤치마크 강화**: KS11 + 코스피 상위 50종목 동일비중 대비 OOS 초과 수익 검증 (§8.2).
- [x] **과매매 분석**: 백테스트 리포트에 평균 보유 기간·총 수수료(총 거래 횟수) 항목 (§8.1).
- [x] **시장 국면 필터**: 3중 신호(200일선 이탈 + 단기 모멘텀 + 20/60일선 데드크로스) 단계적 대응 (§5.12).
- [x] **SQLite 동시성 안전**: WAL + busy_timeout(30s) + scoped_session + @with_retry(읽기·쓰기 전체) + synchronous=NORMAL + Online Backup API (§9).
- [ ] 정적 자산 배분(EMP) 리밸런싱
- [ ] 다중 오실레이터 과매수/과매도 필터 강화
- [ ] ML/딥러닝 예측 모델 연동
- [ ] 멀티 증권사(Kiwoom 등) 지원
- [ ] Grafana 등 고급 대시보드
- [ ] **실전 안정화 후**: PostgreSQL 전환 검토 (SQLAlchemy 연동 완비)

---

## 11. 주의사항

### 🚨 치명적 주의

- **과적합**: OOS 검증·파라미터 수 최소화·단순 전략 우선.
- **블랙스완**: 비상 손절·현금 비중 유지.

### ⚠️ 경고

- **소액 시작**: 페이퍼 → 소액 실전 → 점진적 증액.
- **수수료·과매매**: 왕복 약 0.23%(수수료 0.015%×2 + 거래세 0.20%, 2026년 기준). 매 거래당 0.23% 이상 초과 수익이 나와야 손익분기. 10분마다 신호 확인 구조에서 신호가 자주 바뀌면 **과매매**로 수수료만 나갈 수 있음 → 거래 빈도·연간 왕복 수 점검, §8.3 참고.
- **생존자 편향**: 백테스트/검증 시 **현재 상장 종목만** 쓰면 수익률이 수십 %p 과대평가될 수 있음. `backtest_universe.mode: historical`(전체 과거 종목)·`exclude_administrative: true` 권장. 최소한 `kospi200` 사용, §8.2.1 참고.

### ℹ️ 참고

- **법적**: 개인 계좌만 자동매매 허용. 타인 자금 대리 운용 불법.
- **세금**: 양도소득세·증권거래세 등 신고 의무 확인.
- **운영 환경**: 장 시간 무중단 필요 시 클라우드·NAS 등 권장.
- **데이터 소스 불일치**: 백테스트와 실전에서 **동일 데이터 소스**(FDR 권장) 사용 필수. KIS만 쓰면 수정주가 미반영으로 지표·신호가 달라짐. `data_source.allow_kis_fallback: false`로 비수정주가 폴백 차단 가능 (§2.2). 장전 분석 후 소스 불일치 시 자동 경고 발송.

---

## 부록: 용어 정리

| 용어 | 설명 |
|------|------|
| **시장 비효율성** | 가격이 정보를 완전 반영하지 않아 수익 기회가 생기는 현상. 퀀트 전략은 특정 비효율성(과반응 후 되돌림, 모멘텀 등)을 이용해 수익을 노린다. |
| **모멘텀 효과** | 좋은(나쁜) 성과가 일정 기간 지속되는 현상. 추세 추종 전략이 이용하는 팩터. |
| **과반응 후 되돌림** | 단기적으로 가격이 과하게 움직였다가 평균으로 돌아오는 현상. 평균 회귀 전략이 이용하는 팩터. |
| EMA/SMA | 지수/단순 이동평균 |
| 골든크로스/데드크로스 | 단기선이 장기선 상향/하향 돌파 |
| 슬리피지 | 주문 예상가와 실제 체결가 차이 |
| MDD | Maximum Drawdown |
| 샤프 지수 | 위험 대비 수익 효율 |
| 워크포워드 | 슬라이딩 윈도우 반복 검증 |
| Z-Score | 평균 대비 표준편차 배수 |
| OBV | On Balance Volume |
| ATR | Average True Range |
| ADX | Average Directional Index |
| VWAP | Volume Weighted Average Price |

---

> 📌 **이 문서는 개발 진행에 따라 지속적으로 업데이트됩니다.**  
> 상세 파일별 역할·데이터 흐름은 `docs/PROJECT_GUIDE.md` 참고.  
> **최종 수정**: 2026-03-19 (v2.2: 프로젝트 구조 정확화, CLI 옵션 상세화, ORM 모델 명세, 테스트 파일 개별 명시, 불필요 파일 정리)
