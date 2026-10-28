"""
Fetch quote di mercato SIM Human Italia da AGCOM.

AGCOM pubblica trimestralmente l'Osservatorio sulle Comunicazioni
(https://www.agcom.it/comunicazione/comunicati-stampa) con quote di mercato
sulle SIM Human per operatore.

Strategia:
  1. Scarica la pagina indice dei comunicati AGCOM
  2. Raccoglie i link "Osservatorio sulle comunicazioni" in pagina (di solito gli ultimi 2)
  3. Apre ciascun comunicato e cerca il blocco "segmento delle sim human"
  4. Estrae le quote dei principali operatori (TIM, Vodafone+Fastweb, WindTre, Iliad)
  5. Determina il trimestre di riferimento dal numero dell'osservatorio
     (1/AAAA → Q4 dell'anno AAAA-1, 2/AAAA → Q1, 3/AAAA → Q2, 4/AAAA → Q3)
  6. Aggiunge al JSON i trimestri nuovi, corregge quelli già presenti (upsert)

Se AGCOM non risponde o il testo non è parsabile lo script esce con codice 1
(il workflow prosegue ma segnala il fallimento) e ritenta al run successivo.
Quando trova nuovi dati sovrascrive eventuali stime ('estimated': true)
presenti per quel trimestre.
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
DATA_FILE = ROOT / "data" / "operatori_mobile.json"

AGCOM_INDEX = "https://www.agcom.it/comunicazione/comunicati-stampa"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}
TIMEOUT = 30

# Mappa: numero osservatorio (N) → trimestre coperto in formato (year_offset, quarter)
# Es. Osservatorio 1/2026 → dati a fine Q4 2025 → (-1, 4)
OSS_TO_QUARTER = {1: (-1, 4), 2: (0, 1), 3: (0, 2), 4: (0, 3)}

OPERATORI = ["TIM", "Vodafone+Fastweb", "WindTre", "Iliad"]


def find_osservatorio_urls() -> list[str]:
    """Link agli Osservatori presenti nella pagina comunicati, dal più recente,
    senza duplicati (ogni comunicato compare due volte: titolo + "Scopri di più")."""
    resp = requests.get(AGCOM_INDEX, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        if "osservatorio sulle comunicazioni" in text:
            url = urljoin(AGCOM_INDEX, a["href"])
            if url not in urls:
                urls.append(url)
    return urls


def determine_quarter(url: str, body_text: str) -> str | None:
    """
    Determina il trimestre di riferimento.
    Prima prova dal URL (es. ...osservatorio-sulle-comunicazioni-n1-2026 → Q4 2025).
    Fallback: prova dal testo (es. "a fine dicembre 2025" → Q4 2025).
    """
    # Pattern URL: -nN-AAAA, -n-N-AAAA o -N-AAAA
    url_m = re.search(r"osservatorio-sulle-comunicazioni-(?:n-?)?(\d)[-_/]?(\d{4})", url)
    if url_m:
        num = int(url_m.group(1))
        year = int(url_m.group(2))
        if num in OSS_TO_QUARTER:
            year_off, q = OSS_TO_QUARTER[num]
            return f"{year + year_off}-Q{q}"

    # Fallback dal testo
    mesi_q = {"marzo": 1, "giugno": 2, "settembre": 3, "dicembre": 4}
    text_m = re.search(r"a\s+fine\s+(marzo|giugno|settembre|dicembre)\s+(20\d{2})",
                       body_text, re.IGNORECASE)
    if text_m:
        return f"{text_m.group(2)}-Q{mesi_q[text_m.group(1).lower()]}"
    return None


def extract_human_shares(body_text: str) -> dict | None:
    """
    Estrae le quote SIM Human dai principali operatori dal blocco di testo
    del comunicato.

    Pattern di esempio nel testo:
        "Considerando il solo segmento delle sim "human", Fastweb+Vodafone è
         il principale operatore con il 25,4%, seguito da Wind Tre (23,7%),
         da Tim (22,9%) e Iliad ... raggiunge il 15,6%"
    """
    # Ancora sulla frase "segmento delle sim “ human ”": un semplice
    # 'sim "human"' agganciava prima una riga della tabella traffico dati
    # ("per sim "human" (GB)"), finendo nella sezione TV senza operatori.
    # Le virgolette sono tipografiche e con spazi interni, quindi regex.
    m = re.search(r"segmento\s+delle\s+sim\s*[\"'“”‘’]?\s*human", body_text, re.IGNORECASE)
    if not m:
        return None
    idx = m.start()

    block = body_text[idx:idx + 2000]  # finestra di 2000 char dopo il marker

    # Cerca pattern "Operatore ... NN,N%" per ogni operatore principale.
    # I nomi variano (TIM/Tim, Wind Tre/WindTre, Fastweb+Vodafone/Vodafone+Fastweb).
    operator_patterns = {
        "TIM": r"\bTIM\b|\bTim\b",
        "Vodafone+Fastweb": r"(?:Fastweb\+Vodafone|Vodafone\+Fastweb|Fastweb e Vodafone)",
        "WindTre": r"Wind\s*Tre|WindTre|WINDTRE",
        "Iliad": r"Iliad",
    }

    out = {}
    for op_name, pat in operator_patterns.items():
        # Trova l'operatore nel blocco e prendi il primo numero N,N% nei 100 char successivi
        m = re.search(rf"({pat})[^%]{{0,100}}?(\d+[,\.]\d+)\s*%", block, re.IGNORECASE)
        if m:
            out[op_name] = float(m.group(2).replace(",", "."))
    return out if len(out) >= 3 else None  # almeno 3 operatori su 4


def update_observation(new_period: str, new_shares: dict) -> bool:
    """Aggiunge o sovrascrive l'osservazione del periodo dato.
    Se è uno stub 'estimated', viene sostituito con i dati ufficiali.
    """
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Sanity check: quote per operatore in range plausibile e scostamento
    # contenuto rispetto all'ultimo trimestre non stimato (le quote trimestrali
    # si muovono di frazioni di punto).
    ufficiali = [o for o in data["observations"]
                 if not o.get("estimated") and o["period"] != new_period]
    ultimo = max(ufficiali, key=lambda o: o["period"]) if ufficiali else None
    for op, q in new_shares.items():
        if not (3 <= q <= 50):
            raise ValueError(f"quota {op}={q}% fuori range plausibile (3-50): "
                             "probabile errore di parsing, non salvo.")
        prev = ultimo.get(op) if ultimo else None
        if prev is not None and abs(q - prev) > 10:
            raise ValueError(
                f"quota {op}={q}% per {new_period} si discosta di oltre 10 punti "
                f"da {prev}% di {ultimo['period']}: probabile errore di parsing, non salvo.")

    found = False
    for obs in data["observations"]:
        if obs["period"] == new_period:
            if not obs.get("estimated"):
                vecchie = {k: v for k, v in obs.items() if k != "period"}
                if vecchie == new_shares:
                    print(f"  ⊝ {new_period} già presente e identico, skip.")
                    return False
                # Upsert: AGCOM a volte rivede le quote. Accetta solo correzioni
                # minori (entro 2 punti per operatore): un errore di parsing non
                # può sovrascrivere dati buoni.
                if any(abs(new_shares[op] - vecchie[op]) > 2
                       for op in new_shares if op in vecchie):
                    print(f"  ⚠ {new_period}: riestratto {new_shares} troppo diverso "
                          f"dall'esistente {vecchie}, non sovrascrivo (verificare a mano).",
                          file=sys.stderr)
                    return False
                print(f"  ✓ aggiornato {new_period}: {vecchie} → {new_shares}")
            else:
                print(f"  ✓ sostituita stima per {new_period} con dati ufficiali: {new_shares}")
            obs.clear()
            obs["period"] = new_period
            obs.update(new_shares)
            found = True
            break

    if not found:
        entry = {"period": new_period, **new_shares}
        data["observations"].append(entry)
        data["observations"].sort(key=lambda o: o["period"])
        print(f"  ✓ aggiunto {new_period}: {new_shares}")

    # updated_at segna l'arrivo di un trimestre più recente di quelli noti
    # (badge "Aggiornato" e ordine del feed), non le correzioni ai passati.
    if new_period == max(o["period"] for o in data["observations"]):
        data["updated_at"] = date.today().isoformat()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


def main() -> int:
    # Exit code 1 su qualsiasi fallimento: il workflow prosegue comunque
    # (continue-on-error) ma il run finisce rosso e arriva la notifica.
    # Prima ogni errore usciva con 0 e lo scraper è rimasto rotto per mesi
    # senza che nessuno se ne accorgesse.
    print("→ Cerco gli Osservatori AGCOM...")
    try:
        urls = find_osservatorio_urls()
        if not urls:
            print("  ✗ Nessun link Osservatorio trovato in pagina comunicati.", file=sys.stderr)
            return 1

        # Tutti quelli in pagina (di solito gli ultimi 2): così anche il
        # trimestre precedente viene riallineato al dato ufficiale.
        errori = 0
        for url in urls:
            print(f"  Osservatorio: {url}")
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            body = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)

            period = determine_quarter(url, body)
            if not period:
                print("  ✗ Impossibile determinare il trimestre di riferimento.", file=sys.stderr)
                errori += 1
                continue

            shares = extract_human_shares(body)
            if not shares:
                print("  ✗ Impossibile estrarre quote SIM Human (pattern cambiato?).", file=sys.stderr)
                errori += 1
                continue

            print(f"  Trovato {period}: {shares}")
            try:
                update_observation(period, shares)
            except ValueError as e:
                print(f"  ✗ {e}", file=sys.stderr)
                errori += 1
        return 1 if errori else 0

    except requests.RequestException as e:
        print(f"  ✗ Errore di rete: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
