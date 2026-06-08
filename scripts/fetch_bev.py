"""
Fetch dati BEV (auto full electric) Italia da UNRAE.

Strategia (aggiornata a giugno 2026):
  1. Scarica la pagina-elenco dei comunicati autovetture (`/sala-stampa/autovetture`)
     e ne estrae i link ai singoli comunicati, dal più recente.
     NB: NON si usa più il filtro per tag `immatricolazioni`. UNRAE è
     incoerente nel taggare: il comunicato di maggio 2026 (uscito il 1° giugno)
     non aveva il tag, quindi spariva dalla pagina filtrata e il sito restava
     fermo ad aprile. La pagina-elenco non taggata invece li mostra tutti.
  2. Apre i comunicati uno per uno (dal più recente) e per ciascuno legge il
     testo COMPLETO della pagina del singolo comunicato — l'estratto in elenco
     a volte non contiene il totale del mese.
  3. Tiene solo il comunicato mensile italiano sulle immatricolazioni:
     scarta Europa, auto usata e noleggio, ed estrae:
       - periodo (da "Periodo di riferimento dei dati: <mese> <anno>")
       - quota di mercato BEV (%)
       - totale immatricolazioni del mese
       - calcola le BEV assolute = totale × quota
  4. Prende il primo comunicato valido (= mese più recente) e lo appende al JSON
     se è un nuovo periodo.

Vantaggio rispetto al PDF parsing: i dati sono nel testo HTML del comunicato,
nessun PDF da scaricare/parsare.

Se UNRAE cambia ancora il layout: rimani sotto controllo, lo script logga
chiaramente cosa ha trovato e cosa no, ed esce con codice di errore se
non riesce a estrarre.
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
DATA_FILE = ROOT / "data" / "bev_italia.json"
BASE_URL = "https://unrae.it"
# Pagina-elenco NON filtrata per tag (vedi docstring: i tag UNRAE sono inaffidabili).
LIST_URL = "https://unrae.it/sala-stampa/autovetture"
USER_AGENT = "Mozilla/5.0 (compatible; OsservatorioBot/1.0; +github-actions)"
TIMEOUT = 30
# Quanti comunicati al massimo aprire dall'elenco (dal più recente).
MAX_COMUNICATI = 10
# Range plausibile per il totale immatricolazioni mensile Italia: esclude i dati
# europei (>1 milione) e i progressivi cumulati (es. gennaio-maggio).
TOTALE_MIN, TOTALE_MAX = 50_000, 400_000

MESI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


# Comunicati da scartare perché NON sono il mensile italiano sulle immatricolazioni.
RE_EUROPA = re.compile(r"(mercato\s+(auto\s+)?europ|^\s*europa\s*:)", re.IGNORECASE)
RE_NON_MENSILE = re.compile(r"(usat|noleggio)", re.IGNORECASE)


def comunicato_links(html: str) -> list[str]:
    """Estrae i link ai singoli comunicati dall'elenco, dal più recente, senza duplicati."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"/sala-stampa/autovetture/\d+/", href):
            continue
        if href.startswith("/"):
            href = BASE_URL + href
        if href not in links:
            links.append(href)
    return links


def valuta_comunicato(html: str) -> dict | None:
    """Dato l'HTML di un singolo comunicato, ritorna l'osservazione BEV se è il
    comunicato mensile italiano sulle immatricolazioni, altrimenti None."""
    soup = BeautifulSoup(html, "html.parser")
    titolo_el = soup.find(["h1", "h2"])
    titolo = titolo_el.get_text(" ", strip=True) if titolo_el else ""
    text = soup.get_text(" ", strip=True)

    pm = re.search(r"Periodo di riferimento dei dati:\s*(\w+)\s+(\d{4})", text)
    if not pm:
        return None
    mese_nome = pm.group(1).lower()
    if mese_nome not in MESI:
        return None
    periodo = f"{int(pm.group(2))}-{MESI[mese_nome]:02d}"

    # Scarta Europa, auto usata, noleggio: non sono il mensile italiano nuovo.
    if RE_EUROPA.search(titolo) or RE_NON_MENSILE.search(titolo):
        return None

    # Quota di mercato BEV
    quota_m = re.search(
        r"(?:elettriche pure|BEV)[^%]{0,80}(\d+[,\.]\d+)\s*%",
        text, re.IGNORECASE,
    )
    # Totale immatricolazioni del mese (numero italiano con separatore migliaia)
    tot_m = re.search(
        r"\b(\d{1,3}(?:\.\d{3}){1,2})\s+(?:nuove\s+)?"
        r"(?:immatricolazioni|autovetture|targhe|unità)",
        text, re.IGNORECASE,
    )
    quota = float(quota_m.group(1).replace(",", ".")) if quota_m else None
    totale = int(tot_m.group(1).replace(".", "")) if tot_m else None

    # Serve sia la quota sia un totale mensile plausibile, altrimenti non è il
    # comunicato che cerchiamo (o non è parsabile).
    if quota is None or totale is None or not (TOTALE_MIN <= totale <= TOTALE_MAX):
        return None

    return {
        "period": periodo,
        "registrations": round(totale * quota / 100),
        "market_share_pct": quota,
        "_total_month": totale,  # utile per debug, non viene salvato
    }


def append_observation(new_obs: dict) -> bool:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    existing = {o["period"] for o in data["observations"]}
    if new_obs["period"] in existing:
        print(f"  ⊝ periodo {new_obs['period']} già presente, skip.")
        return False

    # Rimuovi campi interni con underscore prima di salvare
    clean = {k: v for k, v in new_obs.items() if not k.startswith("_")}
    data["observations"].append(clean)
    data["observations"].sort(key=lambda o: o["period"])
    data["updated_at"] = date.today().isoformat()

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✓ aggiunto: {clean}")
    return True


def main() -> int:
    headers = {"User-Agent": USER_AGENT}
    print("→ Scarico l'elenco comunicati UNRAE...")
    try:
        resp = requests.get(LIST_URL, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ✗ Errore di rete sull'elenco: {e}", file=sys.stderr)
        return 1

    links = comunicato_links(resp.text)
    if not links:
        print("  ✗ Nessun link a comunicati trovato (layout cambiato?).")
        return 1

    # Apre i comunicati dal più recente e si ferma al primo mensile italiano valido.
    for href in links[:MAX_COMUNICATI]:
        try:
            page = requests.get(href, headers=headers, timeout=TIMEOUT)
            page.raise_for_status()
        except requests.RequestException as e:
            print(f"  ⚠ Salto comunicato non raggiungibile {href}: {e}", file=sys.stderr)
            continue

        obs = valuta_comunicato(page.text)
        if obs is None:
            continue

        print(f"  Trovato: periodo={obs['period']} "
              f"quota_bev={obs['market_share_pct']}% "
              f"totale_mese={obs.get('_total_month')} "
              f"bev_calcolate={obs['registrations']}")
        append_observation(obs)
        return 0

    print("  ✗ Nessun comunicato mensile italiano sulle immatricolazioni trovato.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
