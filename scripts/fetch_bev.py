"""
Fetch dati BEV (auto full electric) Italia da UNRAE.

UNRAE pubblica un comunicato stampa mensile con i dati di immatricolazione.
Lo script:
  1. Trova l'URL del comunicato mensile più recente
  2. Scarica il PDF
  3. Estrae numero immatricolazioni BEV e market share
  4. Accoda il dato a data/bev_italia.json (se è un nuovo periodo)

ATTENZIONE: Lo scraping di PDF di terze parti è fragile. Se UNRAE cambia
il formato del comunicato lo script può rompersi. In quel caso:
  - Il workflow ti notificherà il fallimento
  - Puoi aggiungere manualmente il dato a data/bev_italia.json
  - Aggiorna i selettori/regex sotto al nuovo formato
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "bev_italia.json"
UNRAE_NOTIZIE_URL = "https://unrae.it/notizie/comunicati-stampa/"
USER_AGENT = "Mozilla/5.0 (compatible; OsservatorioBot/1.0; +github-actions)"
TIMEOUT = 30

# Mappa mesi italiano → numero (per parsing comunicati)
MESI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


def find_latest_press_release_url() -> str | None:
    """Trova l'URL del comunicato stampa mensile più recente sulla home UNRAE."""
    resp = requests.get(UNRAE_NOTIZIE_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # I comunicati hanno titoli tipo "Immatricolazioni autovetture - <Mese> 2026"
    # Cerchiamo il primo link che matcha questo pattern.
    pattern = re.compile(r"immatricolazion.*autovetture", re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if pattern.search(text):
            return urljoin(UNRAE_NOTIZIE_URL, a["href"])
    return None


def find_pdf_url_in_press_page(press_url: str) -> str | None:
    """Estrae l'URL del PDF allegato dalla pagina del comunicato."""
    resp = requests.get(press_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            return urljoin(press_url, a["href"])
    return None


def download_pdf(url: str) -> bytes:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.content


def extract_bev_data(pdf_bytes: bytes) -> dict | None:
    """
    Estrae periodo, immatricolazioni BEV e market share dal PDF UNRAE.

    Il formato tipico contiene frasi come:
      "Ad aprile 2026 in Italia sono state immatricolate 13.238 auto elettriche pure (BEV)"
      "quota di mercato è salita all'8,5%"

    Adatta queste regex se il formato cambia.
    """
    import io
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:5]:  # Le info chiave sono nelle prime pagine
            t = page.extract_text() or ""
            text_parts.append(t)
    full_text = "\n".join(text_parts).lower()

    # Estrai periodo: cerca "<mese> <anno>" vicino a "immatricolat"
    period = None
    for mese_nome, mese_num in MESI.items():
        m = re.search(rf"{mese_nome}\s+(20\d{{2}})", full_text)
        if m:
            period = f"{m.group(1)}-{mese_num:02d}"
            break

    # Estrai immatricolazioni BEV: cerca "BEV" o "elettriche pure" + numero
    # I numeri italiani usano "." come separatore migliaia, "," per decimali
    registrations = None
    patterns = [
        r"immatricolate\s+([\d\.]+)\s+(?:auto\s+)?elettriche\s+pure",
        r"bev[^\d]{0,30}([\d\.]+)",
        r"elettriche\s+pure[^\d]{0,30}([\d\.]+)",
    ]
    for pat in patterns:
        m = re.search(pat, full_text)
        if m:
            registrations = int(m.group(1).replace(".", ""))
            break

    # Estrai market share BEV: cerca "<numero>,<numero>%"
    share = None
    share_patterns = [
        r"bev.{0,80}?quota[^\d]+(\d+[,\.]\d+)\s*%",
        r"elettriche\s+pure.{0,80}?(\d+[,\.]\d+)\s*%",
        r"quota.{0,80}?(\d+[,\.]\d+)\s*%.{0,80}?bev",
    ]
    for pat in share_patterns:
        m = re.search(pat, full_text)
        if m:
            share = float(m.group(1).replace(",", "."))
            break

    if period and registrations:
        return {
            "period": period,
            "registrations": registrations,
            "market_share_pct": share,
        }
    return None


def append_observation(new_obs: dict) -> bool:
    """Aggiunge una nuova osservazione al JSON, evitando duplicati. Ritorna True se aggiunta."""
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
    print("→ Cerco l'ultimo comunicato UNRAE...")
    try:
        press_url = find_latest_press_release_url()
        if not press_url:
            print("  ✗ Nessun comunicato trovato. Forse la pagina UNRAE è cambiata.")
            return 1
        print(f"  comunicato: {press_url}")

        pdf_url = find_pdf_url_in_press_page(press_url)
        if not pdf_url:
            print("  ✗ Nessun PDF trovato sulla pagina del comunicato.")
            return 1
        print(f"  pdf: {pdf_url}")

        pdf_bytes = download_pdf(pdf_url)
        obs = extract_bev_data(pdf_bytes)
        if not obs:
            print("  ✗ Impossibile estrarre i dati BEV dal PDF (formato cambiato?).")
            return 1

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
