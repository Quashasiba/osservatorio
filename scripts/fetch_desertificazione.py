"""
Fetch dati desertificazione bancaria Italia da First CISL.

Strategia:
  1. Scarica la pagina tag dell'Osservatorio: /tag/osservatorio-desertificazione-bancaria/
  2. Identifica l'ultimo articolo che riporta il report annuale (titolo contiene "chiusi" + numero + "sportelli" + "nel YYYY")
  3. Visita l'articolo
  4. Estrae dal testo:
       - Anno di riferimento (dal titolo)
       - Numero sportelli totali ("scesi a X" / "totale a X" / "quota X")
       - Numero comuni senza sportello (es. "3.457 comuni" / "44% del totale")
       - Italiani senza accesso (in milioni)
  5. Appende al JSON solo se l'anno è nuovo

First CISL pubblica un report annuale a fine gennaio e report trimestrali ogni 3
mesi. Per semplicità ci basiamo solo sul report annuale (che ha i dati più
completi e narrativamente più forti).

Layout fragility: i numeri sono in prosa italiana. Lo script logga
chiaramente cosa ha trovato e cosa no; se i pattern non matchano, non corrompe
il JSON esistente, esce con errore.
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
DATA_FILE = ROOT / "data" / "desertificazione_bancaria.json"
TAG_URL = "https://www.firstcisl.it/tag/osservatorio-desertificazione-bancaria/"
USER_AGENT = "Mozilla/5.0 (compatible; OsservatorioBot/1.0; +github-actions)"
TIMEOUT = 30


def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def find_annual_report_url(tag_html: str) -> str | None:
    """Trova il link al report annuale più recente.

    Pattern del titolo del report annuale di First CISL:
      "Desertificazione bancaria, chiusi altri NNN sportelli nel YYYY..."

    Da escludere: gli articoli "trimestre" / "primi N mesi" (sono interim) e
    tutti gli articoli di rassegna stampa che NON sono il report originale.
    """
    soup = BeautifulSoup(tag_html, "html.parser")
    for h in soup.find_all(["h2", "h3"]):
        a = h.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True).lower()
        # Pattern stretto: "chiusi NUMERO sportelli nel YYYY" senza "trimestre" o "mesi"
        if (re.search(r"chius[ie]\s+(?:altri\s+)?\d+\s+sportelli\s+nel\s+20\d{2}", title)
                and "trimestre" not in title
                and "mesi" not in title
                and "primo" not in title
                and "primi" not in title):
            return a["href"]
    return None


def find_annual_report_with_pagination(max_pages: int = 4) -> str | None:
    """Scansiona fino a max_pages del tag finché non trova il report annuale."""
    for page in range(1, max_pages + 1):
        url = TAG_URL if page == 1 else f"{TAG_URL}page/{page}/"
        print(f"  scansiono pagina {page}: {url}")
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  pagina {page} fallita ({e}), mi fermo")
            return None
        found = find_annual_report_url(html)
        if found:
            return found
    return None


def extract_numbers(article_html: str) -> dict | None:
    """Estrae numero sportelli, comuni senza sportello, italiani senza accesso.

    Pattern basati sul testo reale del report annuale 2025 di First CISL:
      "...è così sceso a 19.140 al 31 dicembre 2025..."
      "...comuni totalmente privi di sportelli bancari: sono 3.457..."
      "...Circa 5 milioni di italiani non hanno accesso..."
    """
    text = BeautifulSoup(article_html, "html.parser").get_text(" ", strip=True)
    out: dict = {}

    # Anno di riferimento
    m = re.search(r"31\s+dicembre\s+(20\d{2})", text)
    if not m:
        m = re.search(r"chius[ie]\s+altri\s+\d+\s+sportelli\s+nel\s+(20\d{2})", text, re.IGNORECASE)
    if not m:
        return None
    out["year"] = int(m.group(1))

    # Sportelli totali: pattern "sceso a XX.XXX" o "quota XX.XXX" o simili
    # XX.XXX è il formato italiano (punto separatore migliaia)
    patterns_sport = [
        r"sces[oi]\s+(?:a\s+)?(\d{2}\.\d{3})",      # "sceso a 19.140" o "scesi a..."
        r"quota\s+(\d{2}\.\d{3})",                  # "quota 19.140"
        r"(?:totale|complessivo)[^.]{0,40}?(\d{2}\.\d{3})",  # "totale ... 19.140"
        r"(\d{2}\.\d{3})\s+(?:sportelli|filiali)",  # "19.140 sportelli"
    ]
    for pat in patterns_sport:
        m = re.search(pat, text)
        if m:
            n = int(m.group(1).replace(".", ""))
            if 10_000 < n < 40_000:
                out["sportelli"] = n
                break

    # Comuni senza sportello: cerca "X.XXX" vicino a "comuni" + "sportell"
    # Pattern reale: "...comuni totalmente privi di sportelli bancari: sono 3.457"
    #              o "...comuni è priva di sportelli: sono 3.457"
    patterns_com = [
        r"comuni[^.]{0,80}?sportell[^.]{0,40}?sono\s+(\d\.\d{3})",
        r"sono\s+(\d\.\d{3})[^.]{0,40}?comuni",
        r"(\d\.\d{3})\s+comuni[^.]{0,60}?(?:senza|privi)",
    ]
    for pat in patterns_com:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            n = int(m.group(1).replace(".", ""))
            if 1_000 < n < 6_000:
                out["comuni_senza_sportello"] = n
                break

    # Italiani senza accesso: "Circa N milioni di italiani" o "N,N milioni"
    m = re.search(r"(?:circa\s+)?(\d+(?:[,.]\d+)?)\s+milioni\s+di\s+(?:italiani|cittadini)[^.]{0,80}?(?:non\s+hanno|senza)\s+accesso", text, re.IGNORECASE)
    if m:
        out["italiani_senza_accesso_milioni"] = float(m.group(1).replace(",", "."))

    return out if "sportelli" in out else None


def main() -> int:
    print(f"Cerco report annuale su First CISL…")
    article_url = find_annual_report_with_pagination(max_pages=4)
    if not article_url:
        print("WARN: non trovato link al report annuale nelle prime 4 pagine del tag. "
              "Forse non c'è ancora il report annuale di quest'anno (esce a fine gennaio), "
              "oppure il layout è cambiato. Nessuna modifica al JSON.")
        return 0

    print(f"Visito articolo: {article_url}")
    try:
        article_html = fetch(article_url)
    except Exception as e:
        print(f"ERRORE: impossibile scaricare articolo: {e}", file=sys.stderr)
        return 1

    extracted = extract_numbers(article_html)
    if not extracted:
        print("WARN: non sono riuscito a estrarre i numeri dal testo dell'articolo. "
              "Il layout testuale potrebbe essere cambiato. Nessuna modifica al JSON.")
        return 0

    print(f"Estratto: {extracted}")

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Snapshot dei dati prima della modifica per capire se cambiano davvero
    existing_years = {o["year"] for o in data["observations"]}
    year = extracted["year"]

    # Sanity check: oltre ai range assoluti in extract_numbers, il numero di
    # sportelli varia di pochi % l'anno. Uno scostamento oltre il 20%
    # dall'anno precedente più vicino è quasi certamente un errore di parsing.
    precedenti = [o for o in data["observations"] if o["year"] < year]
    if precedenti:
        ultimo = max(precedenti, key=lambda o: o["year"])
        prev_sport = ultimo.get("sportelli")
        if prev_sport and not (0.8 * prev_sport <= extracted["sportelli"] <= 1.2 * prev_sport):
            print(f"ERRORE: sportelli={extracted['sportelli']} per {year} si discosta "
                  f"oltre il 20% da {prev_sport} del {ultimo['year']}: probabile "
                  "errore di parsing, non salvo.", file=sys.stderr)
            return 1
    data_changed = False

    if year in existing_years:
        # Aggiorna l'osservazione esistente solo se qualche valore cambia.
        # Sui dati ufficiali (non stimati) accetta solo correzioni minori
        # (entro il 10%): un errore di parsing non può sovrascrivere dati buoni.
        for o in data["observations"]:
            if o["year"] == year:
                if not o.get("estimated"):
                    divergenti = [
                        k for k, v in extracted.items()
                        if k != "year" and isinstance(o.get(k), (int, float))
                        and not (0.9 * o[k] <= v <= 1.1 * o[k])
                    ]
                    if divergenti:
                        print(f"WARN: {year}, campi {divergenti} riestratti troppo "
                              f"diversi dall'esistente ({extracted} vs {o}), non "
                              "sovrascrivo (verificare a mano).", file=sys.stderr)
                        break
                for k, v in extracted.items():
                    if k != "year" and o.get(k) != v:
                        o[k] = v
                        data_changed = True
                if o.get("estimated") is True:
                    o["estimated"] = False
                    data_changed = True
                break
        if data_changed:
            print(f"Aggiornata osservazione esistente per {year}")
        else:
            print(f"Osservazione {year} già aggiornata, nessuna modifica")
    else:
        # Nuovo anno
        new_obs = {**extracted, "estimated": False}
        data["observations"].append(new_obs)
        data["observations"].sort(key=lambda o: o["year"])
        data_changed = True
        print(f"Aggiunta nuova osservazione per {year}")

    if not data_changed:
        return 0  # nessuna modifica, non tocchiamo il JSON né updated_at

    data["updated_at"] = date.today().isoformat()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✓ JSON salvato in {DATA_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
