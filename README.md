# ⚡ Power BI Semantic Model Load Tester + 🗄️ Lakehouse Explorer

Lokal Python/Streamlit-applikation för att:
- **Stresstesta DAX-queries** mot Power BI semantiska modeller och mäta svarstider i realtid
- **Hämta och analysera verkliga queries** från Fabric SemanticModelLogs med lokal AI-beskrivning
- **Analysera Delta-tabeller** i Fabric Lakehouse — kardinalitet, filstruktur och datakvalitet

---

## Installation

### Krav
- Python 3.10 eller senare
- Azure CLI installerat och inloggat (`az login`)
- Åtkomst till ett Microsoft Fabric/Power BI-workspace

### 1. Installera beroenden

```bash
pip install -r requirements.txt
```

Beroenden: `streamlit`, `requests`, `pandas`, `plotly`, `deltalake`, `pyarrow`

### 2. Starta applikationen

```bash
python -m streamlit run app.py
```

Öppnas automatiskt på http://localhost:8501

---

## Autentisering

All autentisering sker under **🔐 Autentisering** i sidopanelen.

### Rekommenderat: Azure CLI

Välj **Azure CLI (rekommenderat)** och klicka **🔑 Hämta alla tokens**.

H�mtar fyra tokens automatiskt med ett klick:

| Token | Audience | Används till |
|-------|----------|-------------|
| Power BI | `analysis.windows.net/powerbi/api` | DAX-körning via executeQueries |
| Fabric REST API | `api.fabric.microsoft.com` | Workspace/Lakehouse-listning |
| OneLake Storage | `storage.azure.com` | Läsa Delta-tabeller direkt |
| Eventhouse/KQL | `kusto.kusto.windows.net` | Hämta queries från SemanticModelLogs |

Token giltiga i ~60 min. Klicka igen när de löper ut.

### Manuell token / Service Principal

Manuell token via:
```bash
az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv
```

---

## Gränssnittet — översikt

```
SIDOPANEL                    HUVUDINNEHÅLL
─────────────────────        ──────────────────────────────────────
🔐 Autentisering             # ⚡ Power BI Semantic Model Load Tester
🎯 Semantisk modell          DAX-editor · Parametrar · Körmotor
👤 Row Level Security        Resultat: P50/P95/P99 · diagram · CSV
⚙️ Lastkonfiguration
📊 Hämta queries från logg   ══════════════════ (turkos linje)

                             # 🗄️ Lakehouse Explorer
                             Scheman · Tabeller · Kardinalitet
```

---

## Lasttestaren — steg för steg

### Steg 1 — Välj semantisk modell

Under **🎯 Semantisk modell**: välj Workspace → välj Semantisk modell.

### Steg 2 — Skriv eller ladda DAX

- **Enkel query** — körs upprepade gånger
- **Flera queries (rotation)** — separera med `---`
- **Ladda profil** — via expandern under editorn
- **Importera från loggar** — via **📊 Hämta queries från logg** i sidopanelen

### Steg 3 — DAX-parametrar (valfritt)

Platshållarsyntax: `{{paramnamn}}`

```dax
-- Text:   TREATAS({"{{fartyg}}"}, 'Fartyg'[Fartygsnamn])
-- Nummer: TREATAS({{{ar}}}, 'Trafikdygn'[År])
-- Datum:  'Trafikdygn'[Trafikdygn] >= {{startdatum}}
```

### Steg 4 — Row Level Security (valfritt)

- **En användare:** `upn@domain.se`
- **Flera (rotation):** en per rad, `upn@domain.se | Roll1, Roll2`

### Steg 5 — Lastkonfiguration

| Inställning | Beskrivning |
|-------------|-------------|
| Körläge | Iterationer eller Tidsbaserat |
| Parallella anrop | 1–50 |
| Rate limiting | Max anrop/min |
| Trafikprofil | Vågor / Kontorstider / Slumpmässig / Konstant |

### Steg 6 — Kör och tolka resultat

Klicka **▶ Starta**. Resultatflikar: 📈 Svarstider · 📊 Histogram · 📋 Rådata (CSV)

---

## Hämta queries från loggar

Konfigureras under **📊 Hämta queries från logg** längst ner i sidopanelen.

### Inställningar

| Inställning | Beskrivning |
|-------------|-------------|
| Antal queries | Max antal att hämta (1–200) |
| Minsta körningar | Filtrera bort sällan körda |
| Sortera efter | Se nedan |
| Från/Till datum | Tidsfönster, default senaste 7 dagarna |
| Exkludera prefix | Filtrera bort lasttests-sessioner (default `test-`) |
| 🤖 AI-beskrivning | Lokal regex-analys av DAX — inga externa API-anrop |

### Sorteringsalternativ

| Val | Optimerar för |
|-----|---------------|
| Vanligast körda | Hög frekvens |
| Längst duration (p95) | Worst-case svarstid |
| Längst duration (avg) | Genomsnittlig belastning |
| Högst CPU-tid | CPU-intensiva queries |
| Senast körda | Nyligen aktiva queries |

### Resultat per mönster

Varje hittat frågemönster visar:
- Körningar · Avg (sek) · p95 (sek)
- Senast kördes av **användare** · tidpunkt
- 🤖 Lokal AI-beskrivning av DAX-strukturen
- Föreslagna parametrar från TREATAS/IN-mönster
- Checkbox → **⬇ Lägg in i DAX-editorn**

> AI-beskrivningar rensas automatiskt vid varje ny hämtning.

---

## Lakehouse Explorer — steg för steg

### Steg 1 — Anslut

I **⚙️ Anslutning**: välj Workspace → välj Lakehouse (kräver Fabric-token från 🔐).

### Steg 2 — Identifiera tabeller

Klicka **🔍 Identifiera scheman och tabeller**. Hanterar automatiskt schema-aktiverade lakehouses.

### Steg 3 — Välj och analysera

1. Schema-multiselect → Tabell-multiselect
2. Konfigurera antal rader i snabbläge (default 200 000)
3. Klicka **📊 Analysera**

**⏹ Avbryt** stoppar pågående analys.

### Samplingsstrategi

Analysen använder en tvåstegsmetod för att undvika minnesproblem:

**Steg 1 — Delta-logg metadata (gratis, noll datainläsning):**
`get_add_actions(flatten=False)` ger exakt `null_count`, `min` och `max` per kolumn för hela tabellen.

**Steg 2 — Proportionellt filurval:**
- Andel filer att läsa = `sample_rader / total_rader`
- Filer väljs jämnt spridda över tabellen för representativt urval
- Dynamiskt tak per fil: **500 000 rader totalt** / antal valda filer

| Valda filer | Max rader/fil |
|-------------|---------------|
| 5 | 100 000 |
| 15 | 33 333 |
| 50 | 10 000 |
| 200 | 2 500 |

Varje fil läses kolumnvis (`to_table(columns=[col])`) och frigörs direkt efter (`del _ft`).

### Läsa analysresultaten

**4 metrics per tabell:** Totalt radantal · Storlek MB · Antal filer · Snittfilstorlek

**Kolumnordning i tabellen:**

| # | Kolumn | Källa |
|---|--------|-------|
| 1 | Kolumn | — |
| 2 | Rekommendation | — |
| 3 | Storlek % | PyArrow nbytes (sample) |
| 4 | Distinkta (sample) | Faktisk räkning |
| 5 | Kard% av sample | distinct / sample_rader |
| 6 | Kard% av totalt | distinct / total_rader |
| 7 | Est. total kardinalitet | Linjär extrap. (eller `≥ X` om mättat) |
| 8 | Exempelvärden | Sample + Delta min/max |
| 9 | Null % | Delta-logg (exakt, alla rader) |
| 10 | Datatyp | Delta-schema |

**Kardinalitetsestimering:**
- `Kard% av sample` = distinct / sample_rader (hur unik i samplet)
- `Kard% av totalt` = distinct / total_rader (hur unik i hela tabellen, adaptiva decimaler)
- `Est. total kardinalitet` = linjär extrapolation om mättnad < 50%; annars `≥ X` (minimum)

**Kardinalitetsvarningar med DAX-prestandatips:**

| Nivå | Gräns |
|------|-------|
| ⚠️ Extrem | > 1 000 000 unika |
| ⚠️ Hög | > 700 000 unika |
| ℹ️ Måttlig | > 75 000 unika |
| ℹ️ | > 50% null (datakvalitet, inte prestandaproblem) |

**Filstrukturrekommendation** (bara om > 100 MB eller > 100k rader):
- Opartitionerad: varnar om > 20 filer
- Partitionerad: analyserar per partition, varnar om > 1 fil AND snitt < 10 MB AND totalt > 10 MB per partition

---

## Kända begränsningar

- Token löper ut efter ~60 min — klicka 🔑 igen
- RLS fungerar inte med Service Principal
- DirectLake stöder inte `INFO.TABLES()` via executeQueries
- Kardinalitetsestimat kan vara missvisande vid hög mättnadsgrad (visas då som `≥ X`)

---

## Felsökning

| Problem | Lösning |
|---------|---------|
| `401` Fabric API | Hämta alla tokens — Power BI-token räcker inte |
| `400 UnsupportedOperation` | Schema-aktiverat lakehouse — hanteras automatiskt |
| `Segmentation fault` | Minska antal rader i snabbläge |
| `deltalake` saknas | `pip install deltalake pyarrow` |
| `az: command not found` | https://aka.ms/installazurecliwindows |