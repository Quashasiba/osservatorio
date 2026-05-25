"""
Fetch dati PM2.5 mensili in Pianura Padana lombarda via ARPA Lombardia.

Fonte: dati.lombardia.it (API Socrata).
  - Metadata stazioni:  ib47-atvt
  - Dati correnti:      nicp-bhqi (dal 2024-01-01 a oggi)

Scarica i dati delle ultime 4 settimane disponibili per 6 stazioni di
background urbano nelle città chiave della Pianura Padana lombarda,
aggrega per mese, calcola la media e aggiorna il JSON solo se il mese
SCORSO non è già presente.

Idsensori PM2.5 verificati al momento dello sviluppo. Se ARPA dovesse
dismettere una stazione, lo script continua con le rimanenti (purché
ne restino almeno 3).
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "inquinamento_padana.json"
DATASET_URL = "https://www.dati.lombardia.it/resource/nicp-bhqi.json"
USER_AGENT = "Mozilla/5.0 (compatible; OsservatorioBot/1.0; +github-actions)"
TIMEOUT = 120

# Stazioni di background urbano: 1 per città chiave della Pianura Padana lombarda.
# Verificate attive su https://www.dati.lombardia.it/resource/ib47-atvt.json
STATIONS = {
    "10283": "Milano",      # Milano Pascal Città Studi
    "9984":  "Brescia",     # Brescia Villaggio Sereno
    "10399": "Bergamo",     # Bergamo v.Meucci
    "10586": "Cremona",     # Cremona Via Fatebenefratelli
    "20157": "Pavia",       # Pavia v. Folperti
    "10347": "Mantova",     # Mantova S.Agnese
}

# Soglie minime per considerare il mese "validato"
MIN_DAYS_PER_STATION = 20    # almeno 20 giorni di misure su una stazione
MIN_STATIONS_PER_MONTH = 3   # almeno 3 città su 6 con dati validi


def fetch_station_year(sid: str, year: int) -> list[dict]:
    """Aggregazione mensile per una stazione in un anno (1 query SoQL)."""
    params = {
        "$select": "date_trunc_ym(data) AS mese, avg(valore) AS pm25, count(valore) AS n",
        "$where": (f"idsensore = '{sid}' AND valore > 0 "
                   f"AND data >= '{year}-01-01' AND data < '{year+1}-01-01'"),
        "$group": "mese",
        "$order": "mese",
        "$limit": 20,
    }
    for attempt in range(3):
        try:
            r = requests.get(DATASET_URL, params=params,
                             headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.HTTPError) as e:
            if attempt == 2:
                print(f"  ERRORE su {sid}/{year}: {e}", file=sys.stderr)
                return []
            time.sleep(2 ** attempt)
    return []


def main() -> int:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    existing = {o["period"] for o in data["observations"]}
    last_existing = max(existing) if existing else None

    # Quale mese serve scaricare? Il mese SCORSO (il mese corrente è incompleto).
    today = date.today()
    end_target = date(today.year, today.month, 1) - timedelta(days=1)
    target_month = end_target.strftime("%Y-%m")

    if last_existing and last_existing >= target_month:
        print(f"Già aggiornato fino a {last_existing}, niente da fare.")
        return 0

    # Per essere robusti, prendiamo gli ultimi 12 mesi disponibili: se ci sono
    # buchi storici nel JSON, vengono riempiti. La pulizia è gestita più sotto.
    print(f"→ Aggiorno fino a {target_month}…")
    years_to_fetch = [end_target.year]
    # Se siamo a inizio anno e dobbiamo recuperare dicembre dell'anno prima
    if end_target.month == 12 or (last_existing and last_existing[:4] != str(end_target.year)):
        years_to_fetch.append(end_target.year - 1)

    by_month: dict[str, dict[str, float]] = defaultdict(dict)
    for sid, city in STATIONS.items():
        print(f"  {city}…", end=" ", flush=True)
        for year in years_to_fetch:
            rows = fetch_station_year(sid, year)
            for row in rows:
                mese = row["mese"][:7]
                n = int(row.get("n", 0))
                if n >= MIN_DAYS_PER_STATION:
                    by_month[mese][city] = round(float(row["pm25"]), 1)
            time.sleep(0.3)
        print(f"({sum(1 for m in by_month if city in by_month[m])} mesi)")

    # Filtra mese corrente (incompleto)
    this_month = today.strftime("%Y-%m")
    by_month = {m: v for m, v in by_month.items() if m < this_month}

    # Build nuove osservazioni: solo mesi non già presenti e con copertura sufficiente
    new_obs = []
    for mese in sorted(by_month):
        if mese in existing:
            continue
        cities_data = by_month[mese]
        if len(cities_data) < MIN_STATIONS_PER_MONTH:
            print(f"  {mese}: solo {len(cities_data)} stazioni, skip")
            continue
        avg = round(sum(cities_data.values()) / len(cities_data), 1)
        new_obs.append({
            "period": mese,
            "pm25_media": avg,
            "stazioni_n": len(cities_data),
            "stazioni": dict(sorted(cities_data.items())),
        })

    if not new_obs:
        print("Nessuna nuova osservazione da aggiungere.")
        return 0

    data["observations"].extend(new_obs)
    data["observations"].sort(key=lambda o: o["period"])
    data["updated_at"] = str(date.today())
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"✓ Aggiunti {len(new_obs)} mesi: {new_obs[0]['period']} → {new_obs[-1]['period']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
