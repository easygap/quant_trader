"""Observe today's paper-basket closing NAV, without orders or external messages."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", "--db", dest="db_path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit only JSON on stdout; diagnostics go to stderr")
    parser.add_argument("--status", action="store_true", help="Read the saved collection report, no DB/provider access")
    args = parser.parse_args(argv)
    directory = args.output_dir or Path(os.environ.get("QUANT_BASKET_CLOSE_DIR", ROOT / "output" / "basket_closes"))
    report_path = directory / "latest.json"
    if args.status:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("invalid report")
        except (OSError, ValueError):
            report = {"status": "unavailable", "reason": "close_collection_report_missing"}
    else:
        if args.db_path:
            os.environ["QUANT_DB_PATH"] = str(args.db_path.resolve())
        try:
            with redirect_stdout(sys.stderr):
                from config.config_loader import Config
                from core.basket_close import collect_basket_closes
                config = Config.get()
                db = Path(config.database.get("sqlite_path", "data/quant_trader.db"))
                db = (ROOT / db).resolve() if not db.is_absolute() else db.resolve()
                if config.database.get("type") == "postgresql" or not db.is_file():
                    report = {"status": "unavailable", "reason": "existing_sqlite_paper_ledger_required", "orders_submitted": 0, "rows_saved": 0}
                else:
                    report = collect_basket_closes(config)
        except Exception as exc:
            report = {"status": "unavailable", "reason": type(exc).__name__, "orders_submitted": 0, "rows_saved": 0}
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            temporary.replace(report_path)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report.get("status") in {"completed", "awaiting_close", "disabled"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
