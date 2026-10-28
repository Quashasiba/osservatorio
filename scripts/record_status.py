"""
Registra l'esito dei fetch in data/fetch_status.json, letto da build_site.py
per mostrare l'etichetta "Errore di lettura" sotto ai singoli topic.

Uso (dal workflow, dopo i fetch):
  python scripts/record_status.py bev=success mobile=failure ...

Gli esiti sono quelli di GitHub Actions (steps.<id>.outcome): si registrano
solo success/failure, skipped/cancelled lasciano lo stato com'è. Per i
fallimenti il messaggio è l'ultima riga non vuota di $RUNNER_TEMP/<key>.err
(lo stderr del fetcher, salvato dallo step).

Il file cambia solo quando un topic passa da ok a errore o viceversa (o
cambia il messaggio d'errore): così i run senza novità non producono commit.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = ROOT / "data" / "fetch_status.json"
MAX_MSG = 200


def last_error_line(key: str) -> str:
    err_file = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{key}.err"
    try:
        lines = [l.strip() for l in err_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return ""
    # Via i simboli di log dei fetcher ("✗ ", "⚠ "...): il messaggio va in un tooltip.
    return lines[-1].lstrip("✗⚠⊝→✓ ")[:MAX_MSG] if lines else ""


def main(args: list[str]) -> int:
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}

    today = date.today().isoformat()
    for arg in args:
        key, _, outcome = arg.partition("=")
        if outcome not in ("success", "failure"):
            continue
        prev = status.get(key, {})
        if outcome == "success":
            new = {"ok": True, "since": prev.get("since", today) if prev.get("ok") else today}
        else:
            new = {"ok": False,
                   "since": prev.get("since", today) if prev.get("ok") is False else today,
                   "message": last_error_line(key)}
        if new != prev:
            print(f"  {key}: {prev or '—'} → {new}")
        status[key] = new

    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
