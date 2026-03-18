# 🏗️ QUANT TRADER - 자동 주식 매매 시스템 설계서

> **문서 버전**: v2.1  
> **작성일**: 2026-03-11  
> **최종 수정**: 2026-03-18  
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
| **Python 3.11~3.12** | 금융 라이브러리 생태계 풍부. pandas, numpy, pandas-ta 등 핵심 패키지 완비 (3.14는 호환 미검증) |
| **asyncio** | 비동기 처리로 실시간 데이터 스트리밍과 주문 처리를 동시 수행 |

### 2.2 데이터 수집

| 기술 | 선정 사유 |
|------|----------|
| **KIS Developers API** | 한국투자증권 공식 API — 국내주식 실시간 시세 및 주문 실행 |
| **yfinance** | 미국/한국 주식 무료 데이터 (백테스팅·일봉 보조) |
| **FinanceDataReader** | 한국 주식 무료 데이터 (KRX 전 종목 지원, watchlist 자동 선정에 사용) |
| **websocket-client** | KIS 실시간 호가/체결가 스트리밍 수신 |

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
| **strategy_validator** | 최소 3~5년 데이터, 샤프·MDD·벤치마크(KS11·코스피 상위 50 동일비중) 비교, in/out-of-sample 분리 검증 |
| **param_optimizer** | Grid Search / Bayesian(scikit-optimize) 파라미터 최적화 |

### 2.5 데이터베이스

| 기술 | 선정 사유 |
|------|----------|
| **SQLite** | 기본. 설치 불필요, 로컬 개발·테스트·단일 인스턴스 운영에 적합 |
| **SQLAlchemy** | ORM — DB 전환(PostgreSQL 등) 시 마이그레이션 용이 |

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

- **근거**: 위 점수는 **직관·예시용**이며, 한국 주식 시장 데이터로 검증된 값이 **아닙니다**. RSI에 +2, 볼린저에 +1인 이유에 대한 통계적·실증적 근거는 없습니다.
- **영향**: 가중치를 바꾸면 신호 빈도·방향이 달라지므로, "현재 값이 최적"이라는 보장이 없습니다.
- **최적화 시 오버피팅**: `--mode optimize`로 가중치를 포함해 탐색하면 **과거 데이터에만 맞는 값**을 찾기 쉽고, 실전에서는 백테스트와 결과가 달라질 수 있습니다. 가중치 최적화를 쓸 경우 **반드시 OOS(Out-of-Sample) 검증**과 walk-forward 등으로 과적합 여부를 확인하세요.
- **권장**: 실전 투입 전 (1) OOS 구간 성과 확인, (2) 가능하면 한국 시장·대상 종목에 맞는 별도 검증 또는 보수적 가중치 사용을 권장합니다.

**실행 기준**: 총점 ≥ `buy_threshold`(다중공선성 완화 후 권장 2~3) → 매수, 총점 ≤ `sell_threshold`(권장 -2~-3) → 매도.  
(임계값 근처에서 신호가 자주 바뀌면 **과매매** 위험 → 거래 빈도·수수료 §8.3 참고.)

**지표 독립성 검증**

- 스코어링에 사용 중인 지표(RSI, MACD, 볼린저, 거래량, 이동평균) **점수 시리즈** 간 **상관계수**를 계산할 수 있습니다. **상관계수 0.7 이상**인 지표 쌍은 정보가 중복되므로 **둘 중 하나 제거** 또는 **가중치 축소**를 권장합니다.
- 실행: `python main.py --mode check_correlation --symbol 005930 --validation-years 5`. `core/indicator_correlation.py` 가 스코어 시리즈 상관계수 행렬을 계산하고, 고상관 쌍에 대한 권고를 `reports/indicator_correlation_*.txt` 에 저장합니다. 기준값(기본 0.7)은 `--correlation-threshold` 로 변경 가능합니다.

**⚠️ 매수/매도 임계값 대칭**

- **권장**: `buy_threshold`와 `sell_threshold`는 **절댓값을 같게** 두는 것을 권장합니다 (예: 3과 -3). 비대칭(예: 매수 5점, 매도 -4점)이 의도된 것이 아니라면, 대칭으로 설정해 두는 것이 안전합니다.
- **비대칭 시 문제**: 매도 쪽 임계값이 완화되면(예: -4만 있어도 매도) 매도가 **늦어져** 수익을 반납하기 쉽고, 반대로 매도 임계값이 엄격하면(예: -6 이상일 때만 매도) 매도가 **너무 일찍** 나와 보유 기간이 짧아질 수 있습니다. 의도 없는 비대칭은 진입·청산 타이밍이 한쪽으로 치우친 패턴을 만듭니다.
- **설정**: `strategies.yaml`의 `buy_threshold`, `sell_threshold`를 동일 절댓값으로 맞추고, `--mode optimize` 사용 시에도 대칭 쌍만 탐색하도록 하는 것을 권장합니다.

### 4.2 평균 회귀 전략 (중급 ⭐⭐)

- **구현**: `strategies/mean_reversion.py`, `core/fundamental_loader.py` (펀더멘털 필터)
- **설정**: `strategies.yaml` → `mean_reversion` (z_score_buy, z_score_sell, lookback_period, adx_filter, **fundamental_filter**)
- **이용(가정)하는 시장 비효율성**: **단기 과반응 후 되돌림(Short-term overreaction then reversal)**. 가격이 단기적으로 평균에서 크게 이탈했다가 다시 평균으로 돌아오는 현상을 이용합니다. 학술적으로 **평균 회귀·되돌림(mean reversion)** 효과로 알려진 팩터에 해당하며, **한국 시장**에서는 펀더멘털 악화로 인한 하락이 많아 해당 비효율성이 제한적으로만 성립할 수 있습니다(아래 "한국 시장 한계" 참고).

**로직**: Z-Score = (현재가 - 평균) / 표준편차. Z < -2 매수, Z > 2 매도. ADX < adx_filter 일 때만 활성화(횡보장 강조).

**펀더멘털 필터**: Z-Score 매수 조건이 충족되어도, **매수 전** 해당 종목의 기본 재무 지표가 정상 범위인지 확인합니다. `mean_reversion.fundamental_filter.enabled: true` 시 **PER**(적자 제외·상한 설정 가능), **부채비율(%)** 상한을 검사하며, 범위를 벗어나면 매수 신호를 HOLD로 보류합니다. 데이터는 yfinance(Yahoo Finance) 기반으로 조회하며, `per_min`·`per_max`·`debt_ratio_max`를 `strategies.yaml`에서 설정할 수 있습니다. 백테스트 시에는 symbol이 전달되지 않으면 펀더멘털 필터를 수행하지 않습니다.

**⚠️ "평균"의 정의와 lookback_period**

- **핵심**: Z-Score에서 쓰는 **"평균"**은 **최근 lookback_period 일의 종가 이동평균**입니다. 표준편차도 같은 구간으로 계산됩니다. 즉 "어느 기간 기준으로 벗어났는가"를 정하는 것이 lookback_period 입니다.
- **영향**: 이 기간을 **20일**로 하느냐 **60일**로 하느냐에 따라 신호가 **완전히** 달라집니다. 20일은 단기 이탈, 60일은 중기 추세 이탈에 가깝습니다. 현재 설정은 **최적화·실증 없이 쓰는 고정값(기본 20일)** 이므로, 종목·시장에 맞게 조정하거나 `--mode optimize` 로 탐색하는 것을 권장합니다.
- **최적화**: `param_optimizer` 의 mean_reversion 검색 공간에 lookback_period가 포함되어 있습니다 (Grid: 15/20/25 등, Bayesian: 10~40). 다른 기간(예: 60)을 쓰려면 `strategies.yaml` 에서 직접 설정하거나, 검색 공간을 확장해 사용하세요.

**⚠️ 한국 주식 시장에서의 한계**

- **가정과 현실**: 평균 회귀는 "많이 떨어진 주가는 결국 평균으로 돌아온다"는 가정에 기반합니다. 그러나 **한국 시장**에서는 크게 하락한 종목 상당수가 **실적 악화, 분식회계, 대주주 지분 매도** 등 **펀더멘털 이유**로 하락하며, 이런 종목은 평균으로 회귀하지 않고 **추가 하락**하는 경우가 많습니다.
- **Z-Score만으로는 구분 불가**: Z-Score < -2 조건만으로는 **"기술적 과매도(일시적 반등 가능)"**와 **"펀더멘털 악화로 망해가는 기업"**을 구분할 수 없습니다.
- **ADX 필터의 불완전성**: ADX < adx_filter 로 "횡보장만 매수"하려 해도, **실적 악화 등으로 꾸준히 하락하는 구간**에서도 ADX가 낮게 나올 수 있어, **하락 추세를 횡보로 오판**할 수 있습니다. 즉 필터만으로는 "진짜 횡보"와 "하락 추세의 일부 구간"을 완전히 나누기 어렵습니다.
- **권장**: (1) 이 전략을 쓸 경우 **유동성·퀄리티가 검증된 종목** 또는 **펀더멘털 스크리닝(실적·재무)과 병행**하는 것을 권장하고, (2) 단순 Z-Score·ADX만으로는 한국 시장 리스크가 남으므로 **손절·포지션 사이징을 엄격히** 적용하세요.

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

### 4.5 팩터 기반 종목 선정 (워치리스트)

- **구현**: `core/watchlist_manager.py`
- **설정**: `config/settings.yaml` → `watchlist.mode`, `watchlist.market`, `watchlist.top_n`

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

### 5.5 분산 투자

- **설정**: `diversification.max_position_ratio`, `max_investment_ratio`, `max_positions`, `min_cash_ratio`

### 5.6 최대 보유 기간

- **설정**: `position_limits.max_holding_days` (N일 초과 시 강제 매도, 0이면 비활성)

### 5.7 MDD 제한

- **설정**: `drawdown.max_portfolio_mdd`, `max_daily_loss`, `recovery_scale`

### 5.8 전략 성과 열화 감지

- **설정**: `performance_degradation` (recent_trades, min_win_rate). 최근 N거래 승률이 임계값 미만이면 **신규 매수만** 중단.

### 5.9 거래 비용

- **설정**: `transaction_costs` (commission_rate, tax_rate 0.18% 증권거래세, slippage, capital_gains_tax, dynamic_slippage). 백테스트·실거래 일치를 위해 반드시 반영.

### 5.10 블랙스완 대응

- **구현**: `core/blackswan_detector.py`. 급락 감지 시 전량 매도·디스코드 경고·쿨다운 동안 신규 매수 차단.

### 5.11 시장 국면 필터 (하락장 신규 매수 중단)

- **구현**: `core/market_regime.py`
- **설정**: `config/settings.yaml` → `trading.market_regime_filter`, `market_regime_index`(기본 KS11), `market_regime_ma_days`(기본 200)

**코스피 지수(KS11)가 200일 이동평균선 아래에 있을 때(하락장)** 는 신규 매수를 전면 중단합니다. 기존 포지션의 매도·손절·익절·트레일링 스탑은 그대로 동작합니다. paper/live 모드 및 스케줄러 장전·장중 진입 시 한 번씩 지수 데이터를 조회해 200일선 대비 위치를 확인하며, 조회 실패 시 보수적으로 신규 매수를 허용합니다(API 장애로 인한 진입 기회 상실 방지). 비활성화하려면 `market_regime_filter: false` 로 설정하면 됩니다.

---

## 6. 시스템 아키텍처 및 프로젝트 구조

### 6.1 계층별 구조

```
┌─────────────────────────────────────────────────────────────────┐
│                      📊 모니터링 레이어                          │
│     디스코드 알림 │ 수익률 로깅 │ 오류 알림 │ 웹 대시보드(aiohttp) │
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
├── main.py                      # CLI 진입점. --mode 로 backtest/validate/paper/live/liquidate/compare/optimize/dashboard 분기
├── test_integration.py          # 통합 검증 스크립트 (설정·DB·지표·백테스트·디스코드 등 일괄 점검, 단일 실행)
├── requirements.txt
├── README.md
├── config/
│   ├── config_loader.py         # YAML 통합 로더. settings/strategies/risk_params 로드, .env 덮어쓰기, Config.get() 싱글톤
│   ├── settings.yaml.example   # 설정 예시 (실제 settings.yaml 은 .gitignore)
│   ├── settings.yaml            # KIS API, database, logging, trading(mode/auto_entry/market_regime_filter), discord, dashboard, watchlist
│   ├── strategies.yaml         # indicators, scoring, mean_reversion, trend_following, momentum_factor, volatility_condition, ensemble 파라미터
│   ├── risk_params.yaml        # 포지션/손절/익절/트레일링/분산/MDD/성과열화/거래비용/paper_backtest_compare
│   ├── holidays.yaml.example
│   └── holidays.yaml            # 휴장일 (--update-holidays 로 pykrx+fallback 자동 갱신)
├── core/
│   ├── __init__.py
│   ├── data_collector.py        # 한국/미국 주가 수집 (FinanceDataReader → yfinance → KIS 일봉), get_krx_stock_list()
│   ├── watchlist_manager.py    # 관심 종목: manual / top_market_cap / kospi200 / momentum_top / low_vol_top / momentum_lowvol
│   ├── indicator_engine.py     # RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV, volume_ratio
│   ├── signal_generator.py      # 멀티 지표 스코어링 신호 (BUY/SELL/HOLD, score, score_details)
│   ├── risk_manager.py         # 포지션 사이징, check_diversification, check_recent_performance, 손절/익절/트레일링, 거래비용
│   ├── order_executor.py       # 매수/매도 실행. paper: DB만, live: KIS API. PositionLock, OrderGuard, 미체결 확인
│   ├── portfolio_manager.py    # 보유 포지션·잔고·수익률. sync_with_broker(KIS 잔고↔DB 크로스체크)
│   ├── scheduler.py            # 실전 무한 루프: 장전/장중(10분 간격)/장마감. 최대 보유 기간 정리, 신호·손절·익절 실행
│   ├── trading_hours.py        # 장 시간·휴장일 판별 (holidays.yaml → pykrx → fallback)
│   ├── holidays_updater.py     # 휴장일 YAML 자동 갱신
│   ├── blackswan_detector.py   # 급락 감지 → 전량 매도·쿨다운
│   ├── position_lock.py        # threading.RLock (포지션/주문 동시 접근 제어)
│   ├── order_guard.py          # 동일 종목 TTL 동안 중복 주문 차단
│   ├── strategy_ensemble.py    # 앙상블: technical + momentum_factor + volatility_condition (정보 소스 분리)
│   ├── market_regime.py        # 시장 국면 필터 (코스피 200일선 이하 시 신규 매수 중단)
│   ├── fundamental_loader.py   # 펀더멘털(PER·부채비율) 조회 (yfinance, 평균회귀 필터용)
│   ├── indicator_correlation.py # 스코어링 지표 상관계수 검증 (check_correlation 모드)
│   ├── data_validator.py       # OHLCV 정합성 검사 (Null, NaN, 음수 주가 등)
│   └── notifier.py             # 알림 추상화 (디스코드 실패 시 이메일 fallback 등)
├── strategies/
│   ├── __init__.py
│   ├── base_strategy.py        # 추상 클래스. analyze(df), generate_signal(df, **kwargs)
│   ├── scoring_strategy.py    # IndicatorEngine + SignalGenerator, 스코어링 전략
│   ├── mean_reversion.py      # Z-Score·ADX·펀더멘털 필터 평균 회귀
│   ├── trend_following.py     # ADX·200일선·MACD·ATR 추세 추종
│   ├── momentum_factor.py     # 모멘텀 팩터 (N일 수익률만 사용, 앙상블용)
│   └── volatility_condition.py # 변동성 조건 (N일 실현변동성만 사용, 앙상블용)
├── api/
│   ├── __init__.py
│   ├── kis_api.py             # KIS REST API: 토큰·시세·주문·잔고. 토큰 만료 시 디스코드 알림, Circuit Breaker 연동
│   ├── websocket_handler.py   # KIS 웹소켓 실시간 체결/호가
│   └── circuit_breaker.py     # CLOSED → OPEN → HALF_OPEN. API 연속 실패 시 차단
├── backtest/
│   ├── __init__.py
│   ├── backtester.py         # 시뮬레이션: strict_lookahead 기본, 수수료·세금·슬리피지·손절/익절/트레일링, 과매매 분석(평균 보유기간·총 수수료)
│   ├── report_generator.py    # txt·html 리포트 (과매매 분석: 평균 보유 기간, 총 수수료)
│   ├── strategy_validator.py # validate: 3~5년 데이터, 샤프·MDD·벤치마크(KS11·코스피 상위 50 동일비중), in/out-of-sample
│   ├── paper_compare.py       # 모의투자 vs 백테스트 기간 비교, divergence 시 경고/알림
│   └── param_optimizer.py    # Grid / Bayesian 파라미터 최적화, train_ratio·OOS 보고
├── database/
│   ├── __init__.py
│   ├── models.py              # StockPrice, TradeHistory, Position, PortfolioSnapshot, DailyReport (SQLAlchemy)
│   ├── repositories.py        # CRUD, get_paper_performance_metrics (compare 모드)
│   └── backup.py              # SQLite 일일 백업 (backup_path 설정 시 장마감 후)
├── monitoring/
│   ├── __init__.py
│   ├── logger.py              # loguru 초기화
│   ├── discord_bot.py         # 웹훅 알림 (매매·일일 리포트·블랙스완·동기화 불일치)
│   ├── liquidate_trigger.py   # HTTP POST /liquidate 로 긴급 전량 매도 트리거 (X-Token)
│   ├── dashboard.py           # 콘솔 대시보드 (선택)
│   └── web_dashboard.py       # aiohttp 웹 대시보드 (포트폴리오·스냅샷, 10초 폴링)
├── tests/
│   ├── test_*.py              # 단위·통합: 지표, 신호, 리스크, 스케줄러, 거래시간, 블랙스완, OrderExecutor(paper), KIS 웹소켓 등
│   └── ...
├── docs/
│   └── PROJECT_GUIDE.md       # 파일별 역할·실행 모드·데이터 흐름 상세
└── reports/                   # 백테스트 txt/html 출력 (reports/backtest_* 는 .gitignore)
```

### 6.3 저장소 관리 (Git)

- **커밋 대상**: 위 `quant_trader` 소스 및 설정 예시. `config/settings.yaml`(비밀 포함)은 **커밋 제외** (.gitignore).
- **제외**: `fintics/` 폴더는 본 설계서 저장소에서 제외 (.gitignore). `reports/backtest_*.html`, `reports/backtest_*.txt` 는 생성물로 제외.
- **데이터/로그**: `data/`, `logs/`, `*.db`, `.env` 등은 .gitignore로 관리.

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
| **backtest** | 백테스트 실행 | DataCollector → Backtester.run(strict_lookahead 기본) → ReportGenerator |
| **validate** | 전략 검증 (3~5년, 샤프·MDD·벤치마크·in/out-of-sample). `--walk-forward` 시 슬라이딩 윈도우 워크포워드 | StrategyValidator.run / run_walk_forward |
| **paper** | 모의투자 (DB 기록 + 디스코드, 실제 주문 없음) | WatchlistManager, 전략.generate_signal, OrderExecutor(paper) |
| **live** | 실전 매매 | ENABLE_LIVE_TRADING=true + --confirm-live 필요, KIS 인증 → Scheduler.run() |
| **liquidate** | 긴급 전 종목 매도 | DB 포지션 조회 → 종목별 매도 (실전 시 KIS 현재가 주문) |
| **compare** | 모의투자 vs 백테스트 기간 비교 | paper_compare.run_compare, divergence 시 경고 |
| **optimize** | 전략 파라미터 최적화 | param_optimizer (grid / bayesian), train_ratio·OOS |
| **dashboard** | 웹 대시보드 기동 | monitoring.web_dashboard (aiohttp, 기본 8080) |
| **check_correlation** | 스코어링 지표 간 상관계수·독립성 검증 (0.7 이상 쌍 제거/가중치 축소 권고) | core.indicator_correlation, SignalGenerator 스코어 기반 |

**기타**: `--update-holidays` → 휴장일 YAML 갱신 후 종료. `--allow-lookahead` 사용 시 strict-lookahead 해제(경고 출력).

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
- **벤치마크 비교**: 코스피 지수(KS11) 대비 초과 수익 여부에 더해, **코스피 상위 50종목 동일비중 매수·홀딩** 대비 out-of-sample 초과 수익 여부를 검증합니다. Top50 벤치마크는 `--mode validate` 시 기본 사용하며, `--no-benchmark-top50` 으로 비활성화할 수 있습니다.

**⚠️ 검증 방법 자체의 한계**

- **기준이 통과해도 실전 수익이 안 날 수 있음**: `--mode validate` 조건(샤프 ≥ 1.0, MDD 기준, 벤치마크 초과 수익)을 만족해도, 아래 상황에서는 **실전에서 손실**이 날 수 있습니다.
  1. **검증 기간(3~5년)이 해당 전략에 유리한 시장 국면이었던 경우**: 그 기간이 우연히 상승장·특정 변동성 구간이었다면, 검증 통과는 **국면 편향**일 수 있습니다. 이후 국면이 바뀌면 성과가 반전될 수 있습니다.
  2. **파라미터 최적화 후 검증한 경우**: 학습 구간에서 최적화한 뒤 OOS로 검증해도, **OOS 구간이 같은 시대(같은 시장 환경)** 이면 OOS에서도 성과가 높게 나오도록 **간접적으로 과적합**되었을 수 있습니다. 진정한 "미래" 구간이 아니므로 실전 이탈 가능성이 남습니다.
- **권장**: 검증 통과를 **필요 조건**으로 두되 **충분 조건으로 해석하지 말 것**. 가능하면 **여러 시장 국면(상승·하락·횡보)** 이 포함된 기간으로 검증하거나, **walk-forward**·롤링 검증을 고려하고, 실전은 **소액·보수적**으로 시작하는 것을 권장합니다.

**워크포워드(Walk-Forward) 검증**

- **기본 검증**: `--mode validate` (옵션 없음)는 전체 구간을 **한 번만** train(기본 70%) / test(30%) 로 나눕니다.
- **워크포워드 검증**: `--mode validate --walk-forward` 로 **슬라이딩 윈도우** 반복 검증을 수행합니다. `strategy_validator.run_walk_forward()`: train 2년(504일) → test 1년(252일), 1년(252일) 스텝으로 슬라이드해 여러 구간에서 테스트합니다. 예: 2019~2020 훈련 → 2021 테스트, 2020~2021 훈련 → 2022 테스트, … 각 테스트 구간에서 샤프·MDD 기준 통과 여부를 보고, **전체 통과** 또는 **80% 이상 창 통과** 시 검증 성공으로 볼 수 있습니다. 리포트는 `reports/validation_walkforward_*.txt` 에 저장됩니다.
- **권장**: 검증 신뢰도를 높이려면 **워크포워드** (`--walk-forward`) 를 사용하고, 대부분의 창에서 통과하는지 확인하세요.

### 8.3 거래 비용 반영

- 수수료 0.015%, 증권거래세 0.18%(매도), 슬리피지(기본 0.05%, 거래량 기반 동적 배수). `risk_params.yaml` → `transaction_costs`.

**⚠️ 거래 빈도와 수수료의 관계 (공통 문제)**

- **왕복 비용**: 매수·매도 합쳐 **약 0.23%**(수수료 + 증권거래세) 수준입니다. 이를 상회하려면 **매 거래마다 평균 0.23% 이상의 초과 수익**이 나와야 합니다. 일봉 기반 전략에서 매번 달성하기는 쉽지 않습니다.
- **10분마다 신호 확인**: 실전 스케줄러는 장중 **10분 간격**으로 신호를 확인하고 매매를 실행합니다. 신호가 자주 바뀌는 전략은 **과매매(Over-trading)** 가 되어, 수수료만 나가는 상황이 될 수 있습니다.
- **스코어링 전략**: 임계값 근처에서 신호가 BUY ↔ HOLD ↔ SELL 로 자주 바뀌기 쉽습니다. 백테스트에서 **거래 횟수·연간 왕복 수**를 확인하고, 수수료를 감안한 후 **순수익이 양수**인지 반드시 점검하세요. 필요 시 임계값을 완화해 진입/청산 빈도를 낮추는 것을 고려하세요.
- **권장**: 전략별로 "거래 1회당 기대 초과 수익 > 왕복 비용"이 성립하도록 **진입/청산 조건을 보수적으로** 두거나, **최소 보유 기간·신호 안정화(히스터리시스)** 등을 도입해 불필요한 왕복을 줄이는 설계를 권장합니다.

---

## 9. 예외 처리 및 안정성

- **API**: Circuit Breaker (`api/circuit_breaker.py`), 지수 백오프 재시도, 토큰 만료 시 디스코드 알림.
- **웹소켓**: 자동 재연결·Heartbeat (구현 시).
- **데이터**: `core/data_validator.py` 로 Null/NaN/음수 주가 필터링.
- **알림**: 디스코드 + notifier fallback(이메일 등).
- **비밀**: `.env` + `os.environ`, 설정 파일에 하드코딩 금지.
- **주문**: OrderGuard(TTL)·KIS 미체결 조회로 중복 주문 방지; 루프 10분 초과 시 다음 사이클 스킵.

---

## 10. 개발 로드맵 & 현재 구현 상태

### 현재 구현 완료

- [x] Python 프로젝트 구조, Config(YAML+.env), SQLite·SQLAlchemy
- [x] KIS API 인증·시세·주문·잔고, 웹소켓 핸들러, Circuit Breaker
- [x] DataCollector (한국/미국, KRX 리스트), WatchlistManager (manual/top_market_cap/kospi200/momentum_top/low_vol_top/momentum_lowvol)
- [x] IndicatorEngine (RSI, MACD, 볼린저, MA, 스토캐스틱, ADX, ATR, OBV)
- [x] SignalGenerator, ScoringStrategy, MeanReversion, TrendFollowing, MomentumFactor, VolatilityCondition, StrategyEnsemble(기술+모멘텀+변동성)
- [x] RiskManager (포지션 사이징, 분산, 성과 열화, 손절/익절/트레일링, 거래 비용)
- [x] Backtester (strict-lookahead, 수수료·세금·슬리피지·동적 슬리피지)
- [x] StrategyValidator, ReportGenerator, PaperCompare, ParamOptimizer
- [x] OrderExecutor (paper/live), PositionLock, OrderGuard, 미체결 확인
- [x] PortfolioManager, sync_with_broker, Scheduler (장전/장중/장마감)
- [x] BlackSwanDetector, MarketRegime(시장 국면 필터), TradingHours, HolidaysUpdater, DataValidator, FundamentalLoader
- [x] Discord 알림, 웹 대시보드, LiquidateTrigger, DB 백업
- [x] test_integration.py, pytest 테스트 suite

### 향후 개선 (선택)

- [x] **워크포워드(슬라이딩 윈도우) 검증**: `--mode validate --walk-forward` 및 `StrategyValidator.run_walk_forward()` (§8.2).
- [x] **벤치마크 강화**: KS11 + 코스피 상위 50종목 동일비중 대비 OOS 초과 수익 검증 (§8.2).
- [x] **과매매 분석**: 백테스트 리포트에 평균 보유 기간·총 수수료(총 거래 횟수) 항목 (§8.1).
- [x] **시장 국면 필터**: 코스피 200일선 이하 시 신규 매수 전면 중단 (§5.11).
- [ ] 정적 자산 배분(EMP) 리밸런싱
- [ ] 다중 오실레이터 과매수/과매도 필터 강화
- [ ] ML/딥러닝 예측 모델 연동
- [ ] 멀티 증권사(Kiwoom 등) 지원
- [ ] Grafana 등 고급 대시보드

---

## 11. 주의사항

### 🚨 치명적 주의

- **과적합**: OOS 검증·파라미터 수 최소화·단순 전략 우선.
- **블랙스완**: 비상 손절·현금 비중 유지.

### ⚠️ 경고

- **소액 시작**: 페이퍼 → 소액 실전 → 점진적 증액.
- **수수료·과매매**: 왕복 약 0.23%(수수료+거래세). 매 거래당 0.23% 이상 초과 수익이 나와야 손익분기. 10분마다 신호 확인 구조에서 신호가 자주 바뀌면 **과매매**로 수수료만 나갈 수 있음 → 거래 빈도·연간 왕복 수 점검, §8.3 참고.

### ℹ️ 참고

- **법적**: 개인 계좌만 자동매매 허용. 타인 자금 대리 운용 불법.
- **세금**: 양도소득세·증권거래세 등 신고 의무 확인.
- **운영 환경**: 장 시간 무중단 필요 시 클라우드·NAS 등 권장.

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
> **최종 수정**: 2026-03-18 (v2.1: 앙상블 정보 소스 분리, 시장 국면 필터, 펀더멘털 필터, Top50 벤치마크, 과매매 분석, 팩터 워치리스트 반영)
