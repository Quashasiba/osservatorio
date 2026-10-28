"""
Fetch dati parco auto provincia di Milano da ACI Autoritratto.

ACI pubblica l'Autoritratto con i dati del parco circolante al 31 dicembre
dell'anno precedente. La data non è fissa: il 2023 a giugno 2024, il 2024 a
novembre 2025, il 2025 a giugno 2026.

Strategia:
  1. Scarica la pagina indice https://www.aci.it/.../open-data.html
  2. Trova il link all'archivio "Autoritratto-YYYY-OD" più recente (.zip o
     .rar: il 2025 è uscito come RAR e lo script, che cercava solo .zip, non
     l'aveva visto)
  3. Estrai l'anno dal nome del file
  4. Scarica e scompatta l'archivio, poi trova il file Parco_veicolare_YYYY.ods
     (ricorsivamente negli archivi annidati, la struttura varia anno per anno).
     I RAR si estraggono con bsdtar (libarchive): su macOS c'è già, su
     Ubuntu è il pacchetto libarchive-tools (installato dal workflow).
  6. Legge i 3 fogli per alimentazione × provincia:
       - AVBenz_Provincia (benzina pura)
       - AVGasolio_Prov (gasolio puro)
       - AVAltre_Provincia (GPL + metano + ibridi + elettrici)
  7. Estrae la riga MILANO da ciascuno
  8. Aggiunge l'osservazione al JSON

Nota: ACI a livello provinciale non disaggrega "altre" in sub-categorie
(ibrido vs elettrico vs GPL). Per quel dettaglio servirebbe la dashboard
interattiva opv.aci.it, di fatto non scrappabile.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "auto_milano.json"
INDEX_URL = "https://www.aci.it/laci/studi-e-ricerche/dati-e-statistiche/open-data.html"
USER_AGENT = "Mozilla/5.0 (compatible; OsservatorioBot/1.0; +github-actions)"
TIMEOUT = 120


def find_latest_autoritratto() -> tuple[int, str] | None:
    """Scrappa la pagina indice ACI e ritorna (anno, url) dell'Autoritratto più recente."""
    r = requests.get(INDEX_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    # I link sono nella forma:
    #   https://aci.gov.it/app/uploads/YYYY/MM/Autoritratto-YYYY-OD.zip
    #   https://aci.gov.it/app/uploads/YYYY/MM/Autoritratto_YYYY_OD-1.zip
    #   https://aci.gov.it/app/uploads/YYYY/MM/Autoritratto-YYYY-OD.rar
    pattern = r'(https?://aci\.gov\.it/[^"\']*Autoritratto[_\-]?(\d{4})[_\-][^"\']*\.(?:zip|rar))'
    matches = re.findall(pattern, r.text, re.IGNORECASE)
    if not matches:
        return None
    # Tieni il match con anno più alto
    latest = max(matches, key=lambda m: int(m[1]))
    return int(latest[1]), latest[0]


def extract_archive(archive: Path, dest: Path) -> None:
    """Scompatta uno .zip (zipfile) o .rar (bsdtar) in `dest`."""
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return
    bsdtar = shutil.which("bsdtar")
    if not bsdtar:
        raise RuntimeError("bsdtar non trovato: serve per i RAR "
                           "(Ubuntu: apt-get install libarchive-tools)")
    subprocess.run([bsdtar, "-xf", str(archive), "-C", str(dest)], check=True)


def find_parco_ods(root: Path, year: int, depth: int = 0) -> bytes | None:
    """Trova ricorsivamente il file Parco_veicolare_YYYY.ods (o variante) sotto `root`.

    A seconda dell'anno il file ODS sta direttamente nell'archivio esterno o
    dentro un sotto-archivio: quelli annidati vengono scompattati a loro volta.
    """
    if depth > 3:
        return None
    files = sorted(f for f in root.rglob("*") if f.is_file())

    # Pattern preferiti (basename only, no path)
    preferred_patterns = [
        rf"^Parco_veicolare_{year}\.ods$",
        rf"^Autoritratto{year}_Parco[_\s]veicolare\.ods$",
    ]
    for f in files:
        for p in preferred_patterns:
            if re.match(p, f.name, re.IGNORECASE):
                return f.read_bytes()

    # Fallback: qualsiasi ods con "parco" + "veicolare" nel basename ma
    # senza "vendite" o "mondo" (per evitare il file globale "Vendite nel Mondo")
    for f in files:
        base = f.name.lower()
        if (base.endswith(".ods") and "parco" in base and "veicolare" in base
                and "vendite" not in base and "mondo" not in base):
            return f.read_bytes()

    # Scendi negli archivi annidati
    for f in files:
        if f.suffix.lower() in (".zip", ".rar"):
            sub = Path(tempfile.mkdtemp(dir=root))
            extract_archive(f, sub)
            found = find_parco_ods(sub, year, depth + 1)
            if found:
                return found
    return None


def extract_milano_data(ods_bytes: bytes) -> dict[str, int] | None:
    """Estrae i 3 valori (benzina, gasolio, altre) per la provincia di Milano."""
    import pandas as pd
    with tempfile.NamedTemporaryFile(suffix=".ods", delete=False) as tmp:
        tmp.write(ods_bytes)
        path = tmp.name
    try:
        xl = pd.ExcelFile(path, engine="odf")
        sheets = xl.sheet_names
        benz = next((s for s in sheets if "Benz" in s and "Prov" in s), None)
        gaso = next((s for s in sheets if ("Gasolio" in s or "Gasol" in s)
                     and "Prov" in s and "Benz" not in s), None)
        altre = next((s for s in sheets if "Altre" in s and "Prov" in s), None)
        if not all([benz, gaso, altre]):
            print(f"  Fogli mancanti: benz={benz}, gaso={gaso}, altre={altre}", file=sys.stderr)
            return None

        res = {}
        for label, sheet in [("benzina", benz), ("gasolio", gaso), ("altre", altre)]:
            df = pd.read_excel(path, engine="odf", sheet_name=sheet, header=None)
            for i in range(len(df)):
                row_vals = df.iloc[i].tolist()
                # Cerca cella con valore esatto "MILANO" (per evitare false positive
                # come "MILANO COMUNE" o "MILANO MARITTIMA")
                if any(isinstance(v, str) and v.strip().upper() == "MILANO"
                       for v in row_vals):
                    import pandas as _pd
                    nums = [x for x in row_vals
                            if isinstance(x, (int, float)) and not _pd.isna(x)]
                    if nums:
                        res[label] = int(nums[-1])  # ultima colonna = totale
                    break

        return res if len(res) == 3 else None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main() -> int:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("→ Cerco l'Autoritratto più recente su ACI…")
    latest = find_latest_autoritratto()
    if not latest:
        print("Nessun link trovato. La pagina indice ACI potrebbe essere cambiata.", file=sys.stderr)
        return 1
    year, url = latest
    print(f"  Trovato: Autoritratto {year} → {url}")

    # Anche se l'anno è già nel JSON si riestrae comunque (upsert): ACI a
    # volte ripubblica il file con correzioni. Costa un download in più al
    # mese, ma elimina ogni intervento manuale.
    print(f"→ Scarico archivio per anno {year}…")
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    print(f"  Scaricati {len(r.content) // 1024} KB")

    print("→ Estraggo il file Parco_veicolare ODS…")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / Path(url).name
        archive.write_bytes(r.content)
        extracted = Path(tmp) / "x"
        extracted.mkdir()
        extract_archive(archive, extracted)
        ods = find_parco_ods(extracted, year)
    if not ods:
        print("Non sono riuscito a trovare il file ODS atteso nell'archivio.", file=sys.stderr)
        return 1

    print("→ Estraggo i dati Milano provincia…")
    milano = extract_milano_data(ods)
    if not milano:
        print("Non sono riuscito a estrarre i dati Milano dai fogli.", file=sys.stderr)
        return 1

    new_obs = {
        "year": year,
        "benzina": milano["benzina"],
        "gasolio": milano["gasolio"],
        "altre": milano["altre"],
        "totale": milano["benzina"] + milano["gasolio"] + milano["altre"],
    }

    # Sanity check: il parco circolante cambia di pochi % l'anno. Un totale che
    # si discosta oltre il 20% dall'ultimo anno noto (o una categoria a zero) è
    # quasi certamente un errore di estrazione (riga/colonna sbagliata).
    if any(v <= 0 for v in milano.values()):
        print(f"ERRORE: categoria a zero in {milano}: probabile errore di "
              "estrazione, non salvo.", file=sys.stderr)
        return 1

    esistente = next((o for o in data["observations"] if o["year"] == year), None)
    if esistente is not None:
        if esistente == new_obs:
            print(f"Anno {year} già presente e identico, niente da fare.")
            return 0
        # Upsert: accetta solo correzioni minori (entro il 10% per categoria),
        # un errore di estrazione non può sovrascrivere dati buoni.
        if any(not (0.9 * esistente[c] <= new_obs[c] <= 1.1 * esistente[c])
               for c in ("benzina", "gasolio", "altre")):
            print(f"ERRORE: riestratto {new_obs} troppo diverso dall'esistente "
                  f"{esistente}, non sovrascrivo (verificare a mano).", file=sys.stderr)
            return 1
        print(f"✓ Aggiornata osservazione {year}: {esistente} → {new_obs}")
        esistente.clear()
        esistente.update(new_obs)
    else:
        precedenti = [o for o in data["observations"] if o["year"] < year]
        if precedenti:
            ultimo = max(precedenti, key=lambda o: o["year"])
            prev_tot = ultimo["totale"]
            if not (0.8 * prev_tot <= new_obs["totale"] <= 1.2 * prev_tot):
                print(f"ERRORE: totale {new_obs['totale']} per {year} si discosta oltre "
                      f"il 20% da {prev_tot} del {ultimo['year']}: probabile errore di "
                      "estrazione, non salvo.", file=sys.stderr)
                return 1
        data["observations"].append(new_obs)
        print(f"✓ Aggiunta osservazione: {new_obs}")

    data["observations"].sort(key=lambda o: o["year"])
    data["updated_at"] = date.today().isoformat()

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
