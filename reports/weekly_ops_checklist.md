# 주간 점검 체크리스트 — 60영업일 실험

> 매주 **금요일 장마감 후** 또는 **토요일 오전** 수행
>
> **공식 Watchlist**: 005930, 000660, 035420 + top_market_cap 20종목
> **공식 Benchmark**: KS11 (KOSPI), Top 50 등가중
> **auto_entry 기본값**: `false` — full paper만 `QUANT_AUTO_ENTRY=true`

---

## 1. 무결성 검증 (모든 모드 공통)

- [ ] Git HEAD 일치 확인
  ```bash
  git rev-parse --short HEAD
  ```

- [ ] 듀얼 해시 확인 (YAML + Resolved)
  ```bash
  python -c "
  from config.config_loader import Config
  c = Config.get()
  yaml_ok = c.yaml_hash[:16] == '0d02815a51ea7715'
  print(f'YAML Hash:     {c.yaml_hash[:16]} [{\"OK\" if yaml_ok else \"CHANGED!\"}]')
  print(f'Resolved Hash: {c.resolved_hash[:16]}')
  print(f'auto_entry:    {c.auto_entry} (source={c.auto_entry_source})')
  "
  # YAML Hash 기대값: 0d02815a51ea7715 (모드 무관)
  # Resolved Hash: signal-only=7681f2771efbe6a9 / full paper=92cecd97b49315c0
  ```

- [ ] uncommitted 변경 없음: `git status --porcelain` (비어있어야 함)

- [ ] 실매매 차단 확인
  ```bash
  grep ENABLE_LIVE_TRADING .env        # 출력 없거나 =false
  grep use_mock config/settings.yaml*  # use_mock: true
  ```

---

## 2. Signal-Only 주간 리뷰

- [ ] 주간 신호 생성 건수: BUY __건, SELL __건, HOLD __건
- [ ] 신호 미생성 일수: __일 / 5영업일 (데이터 수집 실패 등)
- [ ] 벤치마크(KS11) 주간 수익률: ___% (참조용)
- [ ] 신호 방향 vs KS11 방향 일치율: ___%
- [ ] 데이터 수집 실패 건수: __건
- [ ] 시장 국면 상태: bullish / caution / bearish

---

## 3. Full Paper 주간 리뷰

- [ ] 주간 P&L: ___%
- [ ] 누적 P&L: ___%
- [ ] 벤치마크(KS11) 대비 초과수익: ___%
- [ ] 누적 MDD: ___% (15% 접근 시 중단 조건 검토)
- [ ] 주간 거래 건수: 매수 __건, 매도 __건
- [ ] 승률 (최근 20거래): ___%
- [ ] Sharpe Ratio (누적): ___
- [ ] 최대 보유일 종목 확인 (30일 접근 종목 유무)
- [ ] 섹터 집중도 확인 (40% 초과 섹터 유무)
- [ ] 상관관계 높은 종목 쌍 확인

---

## 4. 시스템 건강 (모든 모드 공통)

- [ ] 주간 서비스 재시작 횟수: __회 (0이 정상)
- [ ] 에러 로그 총건수: __건
- [ ] 디스크 사용량: __% (80% 미만)
- [ ] DB 파일 크기: __ MB
- [ ] DB 백업 7일치 존재: `ls data/backups/ | wc -l`
- [ ] watchlist 종목 중 데이터 누락 종목: 없음 / 있음 (_____)
- [ ] 공휴일 캘린더 최신 여부

---

## 5. 실험 진행 현황

- [ ] 경과 영업일: __일 / 60일
- [ ] 남은 영업일: __일
- [ ] 현재 실행 모드: signal_only / full_paper
- [ ] 중단 조건 해당 여부: 없음 / 있음 → `experiment_stop_conditions.md` 참조
- [ ] 실험 종료 예정일: 2026-06-19

---

## 6. 주간 기록

```
주차: W__ (____-__-__ ~ ____-__-__)
경과일: __/60 영업일
모드: signal_only / full_paper

--- signal-only ---
주간 신호: BUY __건 / SELL __건 / HOLD __건
신호 방향 일치율: __% (vs KS11)
데이터 실패: __건

--- full paper ---
주간 P&L: ___%
누적 P&L: ___%
누적 MDD: ___%
벤치마크 대비: +/-___%
보유 포지션: __개
주간 거래: 매수 __건, 매도 __건

--- 공통 ---
시스템 이상: 없음 / 있음 (내용: ___)
비고:
```

---

## 주의사항

- 성과가 나빠도 **전략/파라미터를 수정하지 않는다**
- 관측만 하고, 개선 아이디어는 별도 메모 (`reports/research_backlog.md`)
- 중단 조건에 해당하지 않는 한 실험 계속 진행
- 주간 리포트는 `reports/` 에 날짜별 저장 권장
- **모드 전환 시** (signal-only ↔ full paper) `experiment_freeze_pack.md` §10 Case F 참조
