# Osservatorio Italia — Dati mensili

Pipeline automatica che raccoglie ogni mese dati su:

1. **Immatricolazioni auto full electric (BEV) in Italia** — fonte UNRAE (mensile)
2. **Pagamenti elettronici in Italia** — fonte Banca d'Italia (trimestrale)

I dati vengono accodati a una serie storica in `data/`, viene rigenerato un sito statico con grafici interattivi in `docs/`, e tutto viene committato sul repo. GitHub Pages serve il sito da `docs/`.

## Come funziona

```
┌─────────────────┐
│ GitHub Actions  │  cron mensile (giorno 15, ore 09:00 UTC)
│   update.yml    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐    ┌──────────────────────┐
│ fetch_bev.py    │───▶│ data/bev_italia.json │
└─────────────────┘    └──────────────────────┘
┌─────────────────┐    ┌────────────────────────────┐
│ fetch_payments  │───▶│ data/pagamenti_italia.json │
└─────────────────┘    └────────────────────────────┘
         │
         ▼
┌─────────────────┐    ┌─────────────────┐
│ build_site.py   │───▶│ docs/index.html │
└─────────────────┘    └─────────────────┘
         │
         ▼
┌─────────────────┐
│ git commit+push │ ─▶ GitHub Pages auto-deploy
└─────────────────┘
```

## Setup (una tantum)

1. Crea un nuovo repo su GitHub e fai push di questi file
2. Su GitHub vai in **Settings → Pages**, seleziona:
   - Source: **Deploy from a branch**
   - Branch: **main** / cartella: **/docs**
3. **Settings → Actions → General → Workflow permissions**: seleziona **"Read and write permissions"** (serve all'Action per committare i dati aggiornati)
4. (Opzionale) Per ricevere notifiche di fallimento via email: già attivo se hai le notifiche GitHub abilitate. Per Slack vedi commento in `.github/workflows/update.yml`.

Dopo il primo push il sito sarà online su `https://<tuo-username>.github.io/<nome-repo>/`.

## Lanciare manualmente l'aggiornamento

- Dal browser: tab **Actions** → workflow **"Aggiorna dati mensili"** → **Run workflow**
- In locale:
  ```bash
  pip install -r requirements.txt
  python scripts/fetch_bev.py
  python scripts/fetch_payments.py
  python scripts/build_site.py
  ```

## Aggiungere un nuovo dato manualmente (fallback)

Se uno scraper fallisce (es. UNRAE cambia il formato del PDF), puoi aggiungere il dato a mano:

1. Apri `data/bev_italia.json` (o `pagamenti_italia.json`)
2. Aggiungi una nuova entry nell'array `observations` seguendo il formato esistente
3. Commit e push — il workflow rigenera il sito al prossimo run, oppure lanci `python scripts/build_site.py` localmente

## Struttura dei dati

`data/bev_italia.json`:
```json
{
  "source": "UNRAE",
  "updated_at": "2026-05-04",
  "observations": [
    {
      "period": "2026-04",
      "registrations": 13238,
      "market_share_pct": 8.5
    }
  ]
}
```

`data/pagamenti_italia.json`:
```json
{
  "source": "Banca d'Italia",
  "updated_at": "2026-03-31",
  "observations": [
    {
      "period": "2025-Q4",
      "operations_billions": 3.93,
      "value_billions_eur": 3339
    }
  ]
}
```

## Modificare grafici e sito

- Logica grafici: `scripts/build_site.py`
- Template HTML/CSS: `docs/index.html` (viene rigenerato; modificalo direttamente, lo script lo aggiorna mantenendo struttura ma sostituendo solo i blocchi grafici marcati con commenti `<!-- CHART:bev -->`)

## Aggiungere un nuovo topic

1. Crea `scripts/fetch_NUOVO.py` sulla falsariga degli esistenti
2. Aggiungi una sezione in `scripts/build_site.py` con un nuovo `generate_chart_...()`
3. Aggiungi il job in `.github/workflows/update.yml`
4. Aggiungi un blocco grafico in `docs/index.html`

## Note sulle fonti

- **UNRAE** pubblica i dati mensili nei primi giorni del mese successivo (es. dati di aprile pubblicati a inizio maggio). Cron impostato al 15 per essere sicuri che il dato sia disponibile.
- **Banca d'Italia** pubblica i report sui pagamenti con cadenza trimestrale/semestrale. Lo script controlla se è uscito un nuovo trimestre; se no, no-op.
- Lo scraping di PDF/HTML è fragile: i selettori potrebbero cambiare. Le notifiche di fallimento dell'Action ti avvisano se succede.
