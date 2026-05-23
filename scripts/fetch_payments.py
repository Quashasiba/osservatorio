"""
Fetch dati pagamenti elettronici Italia da Banca d'Italia.

Banca d'Italia pubblica la statistica "Modalità di pagamento disponibili
per la clientela: dati nazionali" con cadenza trimestrale (talvolta semestrale).

Strategia:
  1. Visita la pagina indice delle statistiche sui pagamenti
  2. Trova l'ultimo report rilasciato
  3. Estrae numero operazioni e valore totale dalle tabelle
  4. Accoda il dato (se è un nuovo trimestre)

Lo script è "tollerante": se il dato è già nel JSON o non c'è ancora un nuovo
trimestre disponibile, esce con successo senza errori. Questo è importante
perché lo script gira mensile ma i dati cambiano solo ogni 3 mesi.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "pagamenti_italia.json"

# Pagina indice statistiche pagamenti Banca d'Italia
BDI_INDEX_URL = "https://www.bancaditalia.it/pubblicazioni/sistema-pagamenti/"
USER_AGENT = "Mozilla/5.0 (compatible; OsservatorioBot/1.0; +github-actions)"
TIMEOUT = 30


def find_latest_report_url() -> str | None:
    """Trova l'URL dell'ultimo report 'Modalità di pagamento - dati nazionali'."""
    resp = requests.get(BDI_INDEX_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Cerca link con titolo che contiene "Modalità di pagamento" e "dati nazionali"
    pattern = re.compile(r"modalit.+pagamento.+dati\s+nazionali", re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if pattern.search(text):
            return urljoin(BDI_INDEX_URL, a["href"])
    return None


def extract_payment_data(report_url: str) -> dict | None:
    """
    Estrae periodo, n. operazioni e valore complessivo dall'ultimo report.

    Il formato preciso delle tabelle Banca d'Italia varia: spesso le serie
    storiche sono fornite come file Excel scaricabili o tabelle HTML.
    Questo è il blocco da adattare al formato corrente — vedi commenti.
    """
    resp = requests.get(report_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # ESEMPIO di parsing — da adattare al layout reale.
    # Tipicamente cerchi:
    #   - Periodo: dal titolo della pagina o testo "dati al <trimestre> <anno>"
    #   - Numero operazioni: tabella con valori in milioni/miliardi
    #   - Valore totale: tabella valori in mld €

    text = soup.get_text(" ", strip=True).lower()

    # Estrai periodo trimestrale
    period = None
    # Pattern: "IV trimestre 2025", "4° trimestre 2025", "Q4 2025", "ottobre-dicembre 2025"
    quarter_map = {
        "i": "Q1", "1": "Q1", "primo": "Q1",
        "ii": "Q2", "2": "Q2", "secondo": "Q2",
        "iii": "Q3", "3": "Q3", "terzo": "Q3",
        "iv": "Q4", "4": "Q4", "quarto": "Q4",
    }
    m = re.search(r"(primo|secondo|terzo|quarto|i{1,3}v?|iv|\d)\s*°?\s*trimestre\s+(20\d{2})", text)
    if m:
        q = quarter_map.get(m.group(1).lower())
        if q:
            period = f"{m.group(2)}-{q}"

    # TODO: estrai operations_billions e value_billions_eur dalle tabelle
    # Per ora ritorniamo None se non riusciamo — il workflow continua senza errore
    # Questo è il punto in cui dovrai adattare quando girerai per la prima volta.
    operations = None
    value = None

    if period and operations:
        return {
            "period": period,
            "operations_billions": operations,
            "value_billions_eur": value,
        }
    return None


def append_observation(new_obs: dict) -> bool:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    existing_periods = {o["period"] for o in data["observations"]}
    if new_obs["period"] in existing_periods:
        print(f"  ⊝ periodo {new_obs['period']} già presente, skip.")
        return False

    data["observations"].append(new_obs)
    data["observations"].sort(key=lambda o: o["period"])
    data["updated_at"] = date.today().isoformat()

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✓ aggiunto: {new_obs}")
    return True


def main() -> int:
    print("→ Cerco l'ultimo report Banca d'Italia sui pagamenti...")
    try:
        report_url = find_latest_report_url()
        if not report_url:
            print("  ⊝ Nessun report trovato (forse la pagina è cambiata).")
            return 0  # Non è un errore bloccante: ritenta il mese prossimo

        print(f"  report: {report_url}")
        obs = extract_payment_data(report_url)
        if not obs:
            print("  ⊝ Nessun nuovo trimestre estraibile (probabilmente i dati non sono ancora aggiornati).")
            return 0

        append_observation(obs)
        return 0
    except requests.RequestException as e:
        print(f"  ✗ Errore di rete: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"  ✗ Errore imprevisto: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
