# Paper Modes

최종 수정: 2026-04-29

## signal_only_paper (기본)

- `trading.auto_entry=false` 또는 `QUANT_AUTO_ENTRY` 미설정
- 신호, benchmark, evidence/finalize만 기록
- 신규 BUY/SELL 주문 제출 없음
- 60영업일 실험의 기본 관측 모드

```bash
python main.py --mode schedule --strategy scoring
```

## full_paper_auto_entry

- `QUANT_AUTO_ENTRY=true`
- 신호 → DB 모의 주문 → 포지션/손절/익절/evidence 전체 lifecycle 실행
- YAML 원본은 그대로 두고 resolved hash만 달라짐

```bash
QUANT_AUTO_ENTRY=true python main.py --mode schedule --strategy scoring
```

## 설정 Drift 확인

실험 전후로 아래 값을 기록한다.

```bash
python - <<'PY'
from config.config_loader import Config
c = Config.get()
print(c.yaml_hash[:16])
print(c.resolved_hash[:16])
print(c.auto_entry, c.auto_entry_source)
PY
```

## 참조

- `reports/experiment_freeze_pack.md`
- `reports/paper_experiment_manifest.json`
- `reports/daily_ops_checklist.md`
