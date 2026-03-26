# 주간 점검 체크리스트 — 60영업일 실험

> 매주 **금요일 장마감 후** 또는 **토요일 오전** 수행

---

## 1. 무결성 검증

- [ ] Git HEAD 일치 확인
  ```bash
  git rev-parse --short HEAD
  # 기대값: c182823
  ```

- [ ] Config Hash 일치 확인
  ```bash
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
  # 기대값: 1366a00b19c4aa58
  ```

- [ ] uncommitted 변경 없음 확인
  ```bash
  git status --porcelain
  # 출력 비어있어야 함 (data/, logs/ 는 .gitignore)
  ```

- [ ] ENABLE_LIVE_TRADING 미설정 확인
  ```bash
  grep ENABLE_LIVE_TRADING .env
  # 출력 없거나 =false
  ```

## 2. 성과 리뷰

- [ ] 주간 P&L 확인: ___%
- [ ] 누적 P&L 확인: ___%
- [ ] 벤치마크(KS11) 대비 초과수익: ___%
- [ ] 누적 MDD: ___% (15% 접근 시 중단 조건 검토)
- [ ] 주간 거래 건수: 매수 __건, 매도 __건
- [ ] 승률 (최근 20거래): ___%
- [ ] Sharpe Ratio (누적): ___

## 3. 포지션 리뷰

- [ ] 현재 보유 종목 리스트 & 비중 확인
- [ ] 최대 보유일 종목 확인 (30일 접근 종목 유무)
- [ ] 섹터 집중도 확인 (40% 초과 섹터 유무)
- [ ] 상관관계 높은 종목 쌍 확인

## 4. 시스템 건강

- [ ] 주간 서비스 재시작 횟수: __회 (0이 정상)
- [ ] 에러 로그 총건수: __건
- [ ] 디스크 사용량: __% (80% 미만)
- [ ] DB 파일 크기: __ MB
- [ ] DB 백업 7일치 존재 확인: `ls data/backups/ | wc -l`

## 5. 데이터 품질

- [ ] 데이터 수집 실패 건수 (주간): __건
- [ ] watchlist 종목 중 데이터 누락 종목: 없음 / 있음 (_____)
- [ ] 공휴일 캘린더 최신 여부 확인

## 6. 실험 진행 현황

- [ ] 경과 영업일: __일 / 60일
- [ ] 남은 영업일: __일
- [ ] 중단 조건 해당 여부: 없음 / 있음 → `experiment_stop_conditions.md` 참조
- [ ] 실험 종료 예정일: 2026-06-19

## 7. 주간 기록

```
주차: W__ (____-__-__ ~ ____-__-__)
경과일: __/60 영업일
주간 P&L: ___%
누적 P&L: ___%
누적 MDD: ___%
벤치마크 대비: +/-___%
보유 포지션: __개
주간 거래: 매수 __건, 매도 __건
시스템 이상: 없음 / 있음 (내용: ___)
비고:
```

---

## 주의사항

- 성과가 나빠도 **전략/파라미터를 수정하지 않는다**
- 관측만 하고, 개선 아이디어는 별도 메모 (`reports/research_backlog.md`)
- 중단 조건에 해당하지 않는 한 실험 계속 진행
- 주간 리포트는 `reports/` 에 날짜별 저장 권장
