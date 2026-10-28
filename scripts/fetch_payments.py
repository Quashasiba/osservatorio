"""
Fetch dati cashless vs contante Italia da Osservatorio Innovative Payments
(School of Management - Politecnico di Milano).

L'Osservatorio pubblica i dati annuali a marzo di ogni anno (es. i dati 2025
sono stati presentati il 12 marzo 2026). Lo script:
  1. Cerca i comunicati Innovative Payments con la ricerca WordPress del sito
  2. Tra i comunicati annuali (esclusi i semestrali) tiene l'anno più recente
  3. Estrae la quota % cashless e la quota % contante per l'anno appena chiuso
  4. Appende al JSON se è un nuovo anno (upsert con guardrail se esiste già)

Gira ogni mese ma il dato cambia solo a marzo: negli altri mesi ritrova il
comunicato dell'anno già salvato ed esce senza modificare nulla.

NOTE: il sito osservatori.net mette i comunicati di anno in anno in pagine
diverse; lo scraping può rompersi. In quel caso il dato si aggiunge a mano
al JSON in data/pagamenti_italia.json — è un'operazione fatta una volta
l'anno (a marzo).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "pagamenti_italia.json"

# Ricerca WordPress di osservatori.net: la vecchia pagina indice
# /it/eventi/comunicati-stampa dà 404 dal redesign del sito e lo script
# usciva "senza novità" a ogni run. La ricerca invece restituisce tutti i
# comunicati, filtrati poi per URL /comunicato/innovative-payments/.
SEARCH_URL = "https://www.osservatori.net/wp-json/wp/v2/search"
COMUNICATO_PREFIX = "https://www.osservatori.net/comunicato/innovative-payments/"
USER_AGENT = "Mozilla/5.0 (compatible; OsservatorioBot/1.0; +github-actions)"
TIMEOUT = 30


def find_release_urls() -> list[str]:
    """URL dei comunicati annuali Innovative Payments (esclusi i semestrali)."""
    resp = requests.get(SEARCH_URL, params={"search": "pagamenti digitali", "per_page": 50},
                        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    urls = []
    for item in resp.json():
        url = item.get("url", "")
        if (url.startswith(COMUNICATO_PREFIX)
                and not re.search(r"semestre|sei-mesi", url)
                and url not in urls):
            urls.append(url)
    return urls


def extract_release(text: str) -> dict | None:
    """Estrae anno + quote cashless/contante dal testo di un comunicato.

    Pattern reali:
      2025: "Nel 2025 in Italia ... il 45% dei consumi viene oggi regolato
             tramite strumenti elettronici, mentre banconote e monete si
             fermano a 38%"
      2024: "Nel 2024, infatti, il 43% dei consumi è stato regolato con
             strumenti elettronici, mentre l'uso del contante si è fermato al 41%"
    La quota contante va cercata DOPO "% dei consumi" nella stessa frase: la
    vecchia regex prendeva la prima % dopo una qualsiasi "contante" (es.
    "sorpasso sul contante ... il 45%") e restituiva contante = cashless.
    """
    year_m = re.search(r"Nel\s+(20\d{2})[\s\S]{0,500}?(\d{2})\s*%\s+dei\s+consumi", text)
    if not year_m:
        return None
    pair_m = re.search(
        r"(\d{2})\s*%\s+dei\s+consumi[^.]{0,200}?(?:contante|banconote)[^.%]{0,80}?(\d{2})\s*%",
        text[year_m.start():],
    )
    if not pair_m:
        return None
    value_m = re.search(r"(\d{2,4})\s*(?:mld|miliardi)\s+(?:di\s+)?euro", text, re.IGNORECASE)
    return {
        "year": int(year_m.group(1)),
        "cashless_pct": float(pair_m.group(1)),
        "cash_pct": float(pair_m.group(2)),
        "cashless_value_bn_eur": int(value_m.group(1)) if value_m else None,
    }


def fetch_release_data() -> dict | None:
    """Il comunicato annuale più recente (anno più alto) tra quelli trovati."""
    best = None
    for url in find_release_urls()[:10]:
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            continue
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        obs = extract_release(text)
        if obs and (best is None or obs["year"] > best["year"]):
            best = {**obs, "_source_url": url}
    return best


def upsert_observation(new_obs: dict) -> bool:
    """Aggiunge l'anno se nuovo; se già presente lo aggiorna solo quando il
    dato riestratto sostituisce una stima o è una correzione minore (quote
    entro 2 punti, valore entro il 10%): un errore di parsing non può
    sovrascrivere dati buoni."""
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Range assoluto plausibile, vale sia per anni nuovi che per correzioni.
    for campo in ("cashless_pct", "cash_pct"):
        q = new_obs[campo]
        if not (10 <= q <= 90):
            raise ValueError(f"{campo}={q}% fuori range plausibile (10-90): "
                             "probabile errore di parsing, non salvo.")

    # Non sovrascrivere valori noti con None (es. valore mld non trovato).
    clean = {k: v for k, v in new_obs.items()
             if not k.startswith("_") and v is not None}

    esistente = next((o for o in data["observations"]
                      if o["year"] == clean["year"]), None)
    if esistente is not None:
        if esistente == {**esistente, **clean}:
            print(f"  ⊝ anno {clean['year']} già presente e identico, skip.")
            return False
        quote_vicine = all(abs(esistente[c] - clean[c]) <= 2
                           for c in ("cashless_pct", "cash_pct") if c in esistente)
        val_prev, val_new = esistente.get("cashless_value_bn_eur"), clean.get("cashless_value_bn_eur")
        valore_vicino = (val_prev is None or val_new is None
                         or 0.9 * val_prev <= val_new <= 1.1 * val_prev)
        if not (quote_vicine and valore_vicino) and not esistente.get("estimated"):
            print(f"  ⚠ anno {clean['year']}: riestratto {clean} troppo diverso "
                  f"dall'esistente {esistente}, non sovrascrivo (verificare a mano).",
                  file=sys.stderr)
            return False
        print(f"  ✓ aggiornato {clean['year']}: {esistente} → {clean}")
        esistente.pop("estimated", None)
        esistente.update(clean)
    else:
        # Scostamento contenuto rispetto all'ultimo anno noto (le quote
        # annuali si muovono di pochi punti).
        ultimo = max(data["observations"], key=lambda o: o["year"])
        for campo in ("cashless_pct", "cash_pct"):
            prev = ultimo.get(campo)
            if prev is not None and abs(clean[campo] - prev) > 10:
                raise ValueError(
                    f"{campo}={clean[campo]}% per {clean['year']} si discosta di oltre "
                    f"10 punti da {prev}% del {ultimo['year']}: probabile errore di "
                    "parsing, non salvo.")
        data["observations"].append(clean)
        print(f"  ✓ aggiunto: {clean}")

    data["observations"].sort(key=lambda o: o["year"])
    data["updated_at"] = date.today().isoformat()

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


def main() -> int:
    # Exit code 1 se non si trova/legge alcun comunicato: il dato cambia solo
    # a marzo, ma l'ultimo comunicato annuale resta sempre in linea, quindi
    # "nessun risultato" significa scraper rotto, non "nessuna novità".
    print("→ Cerco ultimo comunicato Osservatorio Innovative Payments...")
    try:
        obs = fetch_release_data()
        if not obs:
            print("  ✗ Nessun comunicato annuale leggibile trovato (sito o testo "
                  "cambiati?).", file=sys.stderr)
            return 1

        print(f"  Trovato: anno={obs['year']} cashless={obs['cashless_pct']}% "
              f"cash={obs['cash_pct']}% valore={obs.get('cashless_value_bn_eur')}mld "
              f"da {obs.get('_source_url')}")

        upsert_observation(obs)
        return 0
    except Exception as e:
        print(f"  ✗ Errore: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
