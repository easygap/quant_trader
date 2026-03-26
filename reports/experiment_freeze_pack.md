# Experiment Freeze Pack — 60영업일 Paper Trading

> **실험 기간**: 2026-03-27 ~ 2026-06-19 (60영업일)
> **생성일**: 2026-03-26
> **Git Commit**: c182823 (HEAD)
> **Config Hash**: 1366a00b19c4aa58

---

## 1. 공식 Watchlist

| # | 종목코드 | 종목명 | 시장 |
|---|----------|--------|------|
| 1 | 005930 | 삼성전자 | KOSPI |
| 2 | 000660 | SK하이닉스 | KOSPI |
| 3 | 035420 | NAVER | KOSPI |

**Watchlist 모드**: `top_market_cap` (settings.yaml)
- 실험 기간 중 watchlist 변경 **금지**
- watchlist_cache.json 갱신 주기: 20영업일 (팩터 리밸런싱)
- signal-only 모드는 위 3종목 고정, full paper는 top_market_cap 20종목 자동 선정

> **주의**: top_market_cap 모드 사용 시 리밸런싱에 의해 종목이 바뀔 수 있음.
> 실험 순수성을 위해 signal-only는 반드시 `--symbol` 플래그로 종목 고정.

---

## 2. 공식 Benchmark

| Benchmark | 티커 | 용도 |
|-----------|------|------|
| KOSPI 지수 | KS11 | 1차 기준 (시장 대비 초과수익) |
| KOSPI Top 50 등가중 | 자동생성 | 2차 기준 (대형주 대비) |

**비교 지표**: 누적수익률, Sharpe Ratio, MDD, 승률, Profit Factor

---

## 3. 실행 모드별 시작 커맨드

### 3-A. Signal-Only (신호만 관측, 주문 실행 없음)

단일 종목 1회 분석:
```bash
python main.py --mode paper --strategy scoring --symbol 005930
python main.py --mode paper --strategy scoring --symbol 000660
python main.py --mode paper --strategy scoring --symbol 035420
```

**특징**:
- 1회 실행 후 종료
- DB에 신호 기록, 주문 미실행
- cron 또는 수동 실행 가능
- 종목을 `--symbol`로 고정하여 watchlist 변동 차단

### 3-B. Full Paper (24시간 연속 모의매매)

```bash
python main.py --mode schedule --strategy scoring
```

**특징**:
- 무한 루프 (systemd 자동 재시작)
- watchlist 전체 종목 분석 + DB 모의 주문 실행
- 장전(08:50) 데이터 수집 → 장중(09:00-15:30) 10분 간격 모니터링 → 장마감(15:35) 일간 리포트
- 포지션 생성/청산, 손절/익절/트레일링 스탑 모두 동작

**systemd 서비스**:
```bash
sudo systemctl start quant_trader    # 시작
sudo systemctl stop quant_trader     # 정지
sudo systemctl status quant_trader   # 상태 확인
journalctl -u quant_trader -f        # 로그 실시간
```

---

## 4. 환경변수 목록

### 필수 (KIS API)
| 변수 | 설명 | 예시 |
|------|------|------|
| `KIS_APP_KEY` | KIS 앱키 | (비공개) |
| `KIS_APP_SECRET` | KIS 시크릿 | (비공개) |
| `KIS_ACCOUNT_NO` | 계좌번호 | `00000000-00` |

### 선택 (알림/모니터링)
| 변수 | 설명 | 기본값 |
|------|------|--------|
| `DISCORD_WEBHOOK_URL` | 디스코드 알림 | (없으면 비활성) |
| `DART_API_KEY` | 실적 발표일 필터 | (없으면 비활성) |
| `MAX_CALLS_PER_SEC` | API 초당 제한 | 10 |
| `MAX_CALLS_PER_MIN` | API 분당 제한 | 300 |

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
import hashlib
h = hashlib.sha256()
for f in ['config/strategies.yaml', 'config/risk_params.yaml',
          'config/settings.yaml.example', 'config/baskets.yaml']:
    try:
        with open(f, 'rb') as fh: h.update(fh.read())
    except FileNotFoundError: pass
print(h.hexdigest()[:16])
"
# 예: 1366a00b19c4aa58
```

### 실험 중 무결성 검증 (주간 점검 시)
```bash
# 현재 config hash가 실험 시작 시와 동일한지 확인
EXPECTED="1366a00b19c4aa58"
CURRENT=$(python -c "
import hashlib
h = hashlib.sha256()
for f in ['config/strategies.yaml', 'config/risk_params.yaml',
          'config/settings.yaml.example', 'config/baskets.yaml']:
    try:
        with open(f, 'rb') as fh: h.update(fh.read())
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
# uncommitted 변경 없는지 확인
git status --porcelain
# 출력이 비어있어야 정상

# HEAD가 실험 시작 커밋인지 확인
git rev-parse HEAD
# c182823285a2afc53944aba9021d36389dd12df4 와 일치해야 함
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
| 코드 | Git HEAD | `c182823` |
| 설정 | Config Hash | `1366a00b19c4aa58` |

### 변경 가능한 항목 (실험에 영향 없음)

| 항목 | 사유 |
|------|------|
| `logging.level` | 로그 상세도만 변경 |
| `discord.webhook_url` | 알림 채널 변경 |
| `email.*` | 이메일 알림 설정 |
| `dashboard.port` | UI 포트 변경 |
| `config/holidays.yaml` | 공휴일 업데이트 (실험 결과에 영향 없음) |

---

## 9. 실험 중단 조건

→ `reports/experiment_stop_conditions.md` 참조

---

## 10. 재시작 절차

### Case A: 서비스 크래시 후 자동 재시작 (systemd)
- systemd `Restart=always`, `RestartSec=30` 설정으로 30초 후 자동 재시작
- `core/runtime_lock.py` 파일 락으로 중복 실행 방지
- **조치 불필요** — 로그만 확인

### Case B: 수동 재시작 (서버 점검 등)
```bash
# 1. 현재 상태 확인
sudo systemctl status quant_trader

# 2. 안전 정지 (장마감 후 권장)
sudo systemctl stop quant_trader

# 3. 무결성 검증
cd /home/ubuntu/quant_trader  # (또는 배포 경로)
git status --porcelain         # 변경 없어야 함
git rev-parse --short HEAD     # c182823 이어야 함
# config hash 검증 (위 §5 스크립트 실행)

# 4. DB 무결성 확인
python -c "from database.connection import get_engine; print('DB OK')"

# 5. 재시작
sudo systemctl start quant_trader
sudo systemctl status quant_trader

# 6. 정상 동작 확인 (1~2분 후)
tail -20 logs/service.log
```

### Case C: 서버 재부팅
```bash
# systemd WantedBy=multi-user.target → 부팅 시 자동 시작
# 부팅 후 확인만:
sudo systemctl status quant_trader
tail -20 logs/service.log
```

### Case D: 실험 중단 후 재개
```bash
# 1. 중단 사유 기록 (reports/ 에 날짜별)
echo "중단 사유: ..." >> reports/experiment_log.md

# 2. 중단 기간 동안 누락된 데이터 확인
python main.py --mode paper --strategy scoring  # 1회 실행으로 데이터 갭 확인

# 3. 무결성 검증 (§5)
# 4. 60영업일 카운트에서 중단일 제외 → 종료일 연장
# 5. 재시작 (Case B 절차)
```

### Case E: 긴급 — 실매매 차단 확인
```bash
# ENABLE_LIVE_TRADING 이 설정되어 있지 않은지 반드시 확인
grep ENABLE_LIVE_TRADING .env
# 출력이 없거나 false여야 함

# KIS use_mock 확인
grep use_mock config/settings.yaml*
# use_mock: true 여야 함
```

---

## 부록: 파일 체크섬 기록

```
# 실험 시작 시점 고정 파일 목록
config/strategies.yaml
config/risk_params.yaml
config/settings.yaml.example
config/baskets.yaml
config/holidays.yaml
config/us_holidays.yaml
```

**Config Hash (SHA-256 앞 16자리)**: `1366a00b19c4aa58`
**Git Commit (full)**: `c182823285a2afc53944aba9021d36389dd12df4`
