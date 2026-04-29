# 일간 점검 체크리스트 — 60영업일 실험

> 매 거래일 **장마감 후 (15:35~16:00)** 수행
>
> **공식 Watchlist**: 005930, 000660, 035420 + top_market_cap 20종목
> **공식 Benchmark**: KS11 (KOSPI), Top 50 등가중
> **auto_entry 기본값**: `false` — full paper만 `QUANT_AUTO_ENTRY=true`

---

## 장전 확인 (08:45~08:55)

- [ ] 서비스 실행 중 확인: `sudo systemctl status quant_trader`
- [ ] 에러 로그 확인: `tail -50 logs/service_error.log`
- [ ] 디스크 여유 확인: `df -h /home` (80% 미만)
- [ ] (full paper만) `QUANT_AUTO_ENTRY` 환경변수 확인: `grep QUANT_AUTO_ENTRY .env`

## 장마감 확인 (15:35~16:00)

### 공통 (모든 모드)

- [ ] 서비스 정상 실행 중: `sudo systemctl status quant_trader`
- [ ] 오늘 에러 로그 없음: `grep -c ERROR logs/quant_trader_*.log | tail -1`
- [ ] runtime lock 정상: `ls -la data/*.lock` (1개만 존재)
- [ ] 데이터 수집 정상 (FDR/yfinance 에러 없음)
- [ ] DB 백업 생성 확인: `ls -la data/backups/ | tail -1`

### Signal-Only 전용

- [ ] 오늘 생성된 신호 확인 (DB 또는 로그)
- [ ] BUY/SELL/HOLD 신호 건수 기록: BUY __건 / SELL __건 / HOLD __건
- [ ] 신호 미생성 종목 유무: 없음 / 있음 (_____)
- [ ] 데이터 누락으로 분석 실패한 종목: 없음 / 있음 (_____)

### Full Paper 전용

- [ ] 오늘 생성된 신호 확인 (DB 또는 로그)
- [ ] BUY/SELL 신호 건수 기록: BUY __건 / SELL __건
- [ ] 현재 보유 포지션 수: ____개 / 최대 10개
- [ ] 전체 투자 비중: ___% / 최대 70%
- [ ] 일일 손실 확인: ___% (3% 초과 시 주의)
- [ ] 블랙스완 발동 여부: 없음 / 발동 (시간: ____)
- [ ] MDD 확인: 누적 ___% (15% 접근 시 주의)
- [ ] 손절/익절/트레일링 스탑 발동 건수: __건

### 기록

```
날짜: 2026-__-__
모드: signal_only / full_paper
--- 공통 ---
에러: 없음 / 있음 (내용: ___)
신호: BUY __건 / SELL __건 / HOLD __건
--- full paper 전용 ---
포지션: __개 (투자비중 __%)
일일 P&L: __%
누적 P&L: __%
MDD: __%
비고:
```

---

## 비정상 상황 대응

| 상황 | 조치 |
|------|------|
| 서비스 다운 | `sudo systemctl restart quant_trader` → 로그 확인 |
| DB 에러 | `data/backups/` 에서 최근 백업 복원 |
| 데이터 수집 실패 | FDR/yfinance 서버 상태 확인, 1시간 후 재시도 |
| 디스크 90%+ | `logs/` 오래된 로그 삭제, DB vacuum |
| 블랙스완 발동 (full paper) | 기록만 하고 개입하지 않음 (실험 관측) |
| MDD 15% 도달 (full paper) | `experiment_stop_conditions.md` 참조 |
| 신호 전량 HOLD (signal-only) | 시장 국면(bearish) 확인 — 정상일 수 있음 |
