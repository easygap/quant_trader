# Experiment Freeze Pack — 60영업일 Paper Trading

> **실험 기간**: 2026-03-27 ~ 2026-06-19 (60영업일)
> **생성일**: 2026-03-26
> **Git Commit**: c182823 (HEAD)
> **Config Hash**: 1366a00b19c4aa58
> **auto_entry 기본값**: `false` (settings.yaml) — full paper만 환경변수로 `true` 전환

---

## 0. 실험 시작 전 확인 명령어

실험을 시작하기 전에 반드시 아래 5개를 통과해야 한다.

```bash
# (1) Git HEAD 확인
git rev-parse --short HEAD
# 기대: c182823

# (2) Config Hash 확인
python -c "
import hashlib; h = hashlib.sha256()
for f in ['config/strategies.yaml','config/risk_params.yaml',
          'config/settings.yaml.example','config/baskets.yaml']:
    try:
        with open(f,'rb') as fh: h.update(fh.read())
    except FileNotFoundError: pass
print(h.hexdigest()[:16])
"
# 기대: 1366a00b19c4aa58

# (3) 실매매 차단 확인
grep ENABLE_LIVE_TRADING .env        # 출력 없거나 =false
grep use_mock config/settings.yaml*  # use_mock: true

# (4) auto_entry 기본값 확인
grep auto_entry config/settings.yaml*
# auto_entry: false (기본)

# (5) DB 무결성
python -c "from database.connection import get_engine; print('DB OK')"
```

---

## 1. 공식 Watchlist

| # | 종목코드 | 종목명 | 시장 |
|---|----------|--------|------|
| 1 | 005930 | 삼성전자 | KOSPI |
| 2 | 000660 | SK하이닉스 | KOSPI |
| 3 | 035420 | NAVER | KOSPI |

**Watchlist 설정**: `top_market_cap`, `market: KOSPI`, `top_n: 20`

| 모드 | 사용하는 watchlist |
|------|-------------------|
| **spot_check** | `--symbol`로 지정한 단일 종목 (위 3종목 중 택1) |
| **signal_only_schedule** | settings.yaml의 top_market_cap 20종목 (리밸런싱 20영업일 주기) |
| **full_paper_schedule** | 동일 — top_market_cap 20종목 |

- 실험 기간 중 watchlist 모드/top_n/market 변경 **금지**
- watchlist_cache.json 갱신 주기: 20영업일 (자동 팩터 리밸런싱)

---

## 2. 공식 Benchmark

| Benchmark | 티커 | 용도 |
|-----------|------|------|
| KOSPI 지수 | KS11 | 1차 기준 (시장 대비 초과수익) |
| KOSPI Top 50 등가중 | 자동생성 | 2차 기준 (대형주 대비) |

**비교 지표**: 누적수익률, Sharpe Ratio, MDD, 승률, Profit Factor

| 모드 | 벤치마크 비교 대상 |
|------|-------------------|
| **spot_check** | 해당 없음 (스팟 확인용) |
| **signal_only_schedule** | KS11 (신호 정확도 관점) |
| **full_paper_schedule** | KS11 + Top 50 (수익률/MDD 관점) |

---

## 3. 실행 모드 정의 및 시작 커맨드

### 핵심 구분: `auto_entry` 플래그

| 값 | 의미 |
|----|------|
| `auto_entry: false` (기본) | 신호만 생성·기록, 주문 실행 안 함 |
| `auto_entry: true` | 신호 → DB 모의 주문 실행 (포지션 생성/청산/손절/익절) |

settings.yaml의 기본값은 `false`. Full paper에서만 환경변수 `QUANT_AUTO_ENTRY=true`로 오버라이드.

---

### 3-A. Spot Check (스팟 확인, 1회성)

**용도**: 단일 종목 신호 확인, 디버깅, 수동 검증

```bash
python main.py --mode paper --strategy scoring --symbol 005930
python main.py --mode paper --strategy scoring --symbol 000660
python main.py --mode paper --strategy scoring --symbol 035420
```

| 항목 | 값 |
|------|---|
| 실행 방식 | 1회 실행 후 종료 |
| auto_entry | 불필요 (1회 패스) |
| 주문 실행 | 없음 |
| 포지션 관리 | 없음 |
| watchlist | `--symbol`로 고정 |
| 장기 운영 | **불가** — cron/수동 용도 |

---

### 3-B. Signal-Only Schedule (신호 전용 장기 운영)

**용도**: 60영업일 장기 실험 — 신호 생성+기록만, 주문 미실행

```bash
python main.py --mode schedule --strategy scoring
```

| 항목 | 값 |
|------|---|
| 실행 방식 | 24시간 무한 루프 (systemd) |
| auto_entry | `false` (기본값 유지) |
| 주문 실행 | **없음** — 신호만 DB에 기록 |
| 포지션 관리 | 없음 (포지션 생성 안 됨) |
| watchlist | top_market_cap 20종목 |
| 평가 기준 | 신호 정확도, 방향성, 생성 안정성 |

**systemd 서비스** (`deploy/quant_trader.service` 그대로 사용):
```bash
sudo systemctl start quant_trader
sudo systemctl status quant_trader
journalctl -u quant_trader -f
```

---

### 3-C. Full Paper Schedule (전체 모의매매 장기 운영)

**용도**: 60영업일 장기 실험 — 신호 + DB 모의 주문 + 포지션 관리 전체 동작

```bash
QUANT_AUTO_ENTRY=true python main.py --mode schedule --strategy scoring
```

| 항목 | 값 |
|------|---|
| 실행 방식 | 24시간 무한 루프 (systemd) |
| auto_entry | **`true`** (환경변수 오버라이드) |
| 주문 실행 | DB 모의 주문 (실제 KIS 주문 아님) |
| 포지션 관리 | 생성/청산/손절/익절/트레일링 스탑 |
| watchlist | top_market_cap 20종목 |
| 평가 기준 | P&L, MDD, Sharpe, 승률, Profit Factor |

**systemd 서비스** (환경변수 추가 필요):
```bash
# .env 에 추가
echo "QUANT_AUTO_ENTRY=true" >> .env

# 또는 quant_trader.service 의 Environment 라인 추가:
# Environment=QUANT_AUTO_ENTRY=true

sudo systemctl restart quant_trader
```

**장중 루프 동작** (signal-only와의 차이):
- 장전(08:50): 데이터 수집 → 전략 분석 → 매수 후보 선정
- 장중(09:00-15:30): 10분 간격 모니터링 → **재스캔 → 매수/매도 주문 실행**
- 장마감(15:35): **포지션 정산** → 일간 리포트 → DB 백업

---

### 3개 모드 비교표

| | Spot Check | Signal-Only Schedule | Full Paper Schedule |
|---|---|---|---|
| **커맨드** | `--mode paper --symbol X` | `--mode schedule` | `QUANT_AUTO_ENTRY=true --mode schedule` |
| **실행** | 1회 | 24시간 루프 | 24시간 루프 |
| **auto_entry** | N/A | false | **true** |
| **신호 생성** | O | O | O |
| **DB 주문** | X | X | **O** |
| **포지션/P&L** | X | X | **O** |
| **손절/익절** | X | X | **O** |
| **블랙스완 방어** | X | X | **O** |
| **일간 리포트** | X | O (신호 요약) | **O (P&L 포함)** |
| **벤치마크 비교** | X | 신호 정확도 | **수익률/MDD** |

---

## 4. 환경변수 목록

### 공통 (모든 모드)
| 변수 | 설명 | 기본값 |
|------|------|--------|
| `KIS_APP_KEY` | KIS 앱키 | (필수) |
| `KIS_APP_SECRET` | KIS 시크릿 | (필수) |
| `KIS_ACCOUNT_NO` | 계좌번호 | (필수) |
| `DISCORD_WEBHOOK_URL` | 디스코드 알림 | (없으면 비활성) |
| `DART_API_KEY` | 실적 발표일 필터 | (없으면 비활성) |
| `MAX_CALLS_PER_SEC` | API 초당 제한 | 10 |
| `MAX_CALLS_PER_MIN` | API 분당 제한 | 300 |

### Full Paper 전용
| 변수 | 설명 | 기본값 |
|------|------|--------|
| `QUANT_AUTO_ENTRY` | `true`로 설정 시 모의 주문 실행 | `false` |

### 절대 설정하면 안 되는 것
| 변수 | 이유 |
|------|------|
| `ENABLE_LIVE_TRADING=true` | 실험 기간 중 실매매 전환 금지 |

---

## 5. Git Commit / Config Hash 기록 방법

### 실험 시작 시 기록
```bash
# Git commit hash
git rev-parse HEAD
# 예: c182823285a2afc53944aba9021d36389dd12df4

# Config hash (모든 설정 파일 SHA-256 앞 16자리)
python -c "
import hashlib; h = hashlib.sha256()
for f in ['config/strategies.yaml','config/risk_params.yaml',
          'config/settings.yaml.example','config/baskets.yaml']:
    try:
        with open(f,'rb') as fh: h.update(fh.read())
    except FileNotFoundError: pass
print(h.hexdigest()[:16])
"
# 예: 1366a00b19c4aa58
```

### 실험 중 무결성 검증 (주간 점검 시)
```bash
EXPECTED="1366a00b19c4aa58"
CURRENT=$(python -c "
import hashlib; h = hashlib.sha256()
for f in ['config/strategies.yaml','config/risk_params.yaml',
          'config/settings.yaml.example','config/baskets.yaml']:
    try:
        with open(f,'rb') as fh: h.update(fh.read())
    except FileNotFoundError: pass
print(h.hexdigest()[:16])
")
if [ "$CURRENT" = "$EXPECTED" ]; then
    echo "OK: config unchanged ($CURRENT)"
else
    echo "ALERT: config changed! expected=$EXPECTED current=$CURRENT"
fi
```

### 실험 중 코드 무결성 검증
```bash
git status --porcelain    # 출력이 비어있어야 정상
git rev-parse HEAD        # c182823285a2afc53944aba9021d36389dd12df4
```

---

## 6. 일간 점검 체크리스트

→ `reports/daily_ops_checklist.md` 참조

---

## 7. 주간 점검 체크리스트

→ `reports/weekly_ops_checklist.md` 참조

---

## 8. 실험 중 절대 바꾸면 안 되는 항목

### FROZEN — 변경 시 실험 무효

| 카테고리 | 항목 | 고정값 |
|----------|------|--------|
| 전략 | `active_strategy` | `scoring` |
| 전략 | `collinearity_mode` | `representative_only` |
| 전략 | `buy_threshold` | `2` |
| 전략 | `sell_threshold` | `-2` |
| 전략 | `hysteresis.enabled` | `true` |
| 전략 | `scoring.weights.*` | 현재 값 그대로 |
| 전략 | `regime_adaptive.*` | 현재 값 그대로 |
| 전략 | `dynamic_threshold` | `true` |
| 리스크 | `max_risk_per_trade` | `0.01` (1%) |
| 리스크 | `stop_loss.atr_multiplier` | `2.0` |
| 리스크 | `take_profit.fixed_rate` | `0.08` (8%) |
| 리스크 | `trailing_stop.fixed_rate` | `0.05` (5%) |
| 리스크 | `max_positions` | `10` |
| 리스크 | `max_investment_ratio` | `0.70` |
| 리스크 | `min_cash_ratio` | `0.20` |
| 리스크 | `max_portfolio_mdd` | `0.15` |
| 리스크 | `max_daily_loss` | `0.03` |
| 리스크 | `blackswan.*` | 현재 값 그대로 |
| 비용 | `commission_rate` | `0.00015` |
| 비용 | `tax_rate` | `0.0020` |
| 비용 | `slippage` | `0.0005` |
| 유동성 | `min_avg_trading_value_20d_krw` | `5,000,000,000` |
| 보유 | `max_holding_days` | `30` |
| 보유 | `min_holding_days` | `5` |
| watchlist | `mode` | `top_market_cap` |
| watchlist | `top_n` | `20` |
| watchlist | `market` | `KOSPI` |
| 설정 | `trading.auto_entry` | `false` (yaml 원본) |
| 코드 | Git HEAD | `c182823` |
| 설정 | Config Hash | `1366a00b19c4aa58` |

### 변경 가능한 항목 (실험에 영향 없음)

| 항목 | 사유 |
|------|------|
| `logging.level` | 로그 상세도만 변경 |
| `discord.webhook_url` | 알림 채널 변경 |
| `email.*` | 이메일 알림 설정 |
| `dashboard.port` | UI 포트 변경 |
| `config/holidays.yaml` | 공휴일 업데이트 |
| `QUANT_AUTO_ENTRY` 환경변수 | 모드 전환용 (yaml 원본 불변) |

---

## 9. 실험 중단 조건

→ `reports/experiment_stop_conditions.md` 참조 (모드별 분리)

---

## 10. 재시작 절차

### Case A: 서비스 크래시 후 자동 재시작 (systemd)
- systemd `Restart=always`, `RestartSec=30` → 30초 후 자동 재시작
- `core/runtime_lock.py` 파일 락으로 중복 실행 방지
- **조치 불필요** — 로그만 확인
- **주의**: `QUANT_AUTO_ENTRY`는 `.env` 또는 systemd `Environment=`에 설정되어 있으므로 재시작 시 자동 유지

### Case B: 수동 재시작 (서버 점검 등)
```bash
# 1. 현재 상태 확인
sudo systemctl status quant_trader

# 2. 안전 정지 (장마감 후 권장)
sudo systemctl stop quant_trader

# 3. 실험 시작 전 확인 (§0 전체 수행)

# 4. 모드별 재시작
# signal-only:
sudo systemctl start quant_trader

# full paper (.env에 QUANT_AUTO_ENTRY=true 확인):
grep QUANT_AUTO_ENTRY .env    # true 여야 함
sudo systemctl start quant_trader

# 5. 정상 동작 확인 (1~2분 후)
tail -20 logs/service.log
```

### Case C: 서버 재부팅
```bash
# systemd WantedBy=multi-user.target → 부팅 시 자동 시작
# 부팅 후 확인:
sudo systemctl status quant_trader
grep QUANT_AUTO_ENTRY .env    # full paper면 true 확인
tail -20 logs/service.log
```

### Case D: 실험 중단 후 재개
```bash
# 1. 중단 사유 기록
echo "중단 사유: ..." >> reports/experiment_log.md

# 2. 중단 기간 누락 데이터 확인
python main.py --mode paper --strategy scoring --symbol 005930

# 3. §0 전체 확인 수행
# 4. 60영업일 카운트에서 중단일 제외 → 종료일 연장
# 5. Case B 절차로 재시작
```

### Case E: 긴급 — 실매매 차단 확인
```bash
grep ENABLE_LIVE_TRADING .env        # 출력 없거나 =false
grep use_mock config/settings.yaml*  # use_mock: true
```

### Case F: 모드 전환 (signal-only ↔ full paper)
```bash
# signal-only → full paper
sudo systemctl stop quant_trader
echo "QUANT_AUTO_ENTRY=true" >> .env   # 또는 기존 false를 true로 변경
sudo systemctl start quant_trader

# full paper → signal-only
sudo systemctl stop quant_trader
sed -i 's/QUANT_AUTO_ENTRY=true/QUANT_AUTO_ENTRY=false/' .env
sudo systemctl start quant_trader
```

---

## 부록: 파일 체크섬 기록

```
config/strategies.yaml
config/risk_params.yaml
config/settings.yaml.example
config/baskets.yaml
config/holidays.yaml
config/us_holidays.yaml
```

**Config Hash (SHA-256 앞 16자리)**: `1366a00b19c4aa58`
**Git Commit (full)**: `c182823285a2afc53944aba9021d36389dd12df4`
