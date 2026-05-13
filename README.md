# ⚡ Power BI Semantic Model Load Tester + 🗄️ Lakehouse Explorer

Lokal Python/Streamlit-applikation för att:
- **Stresstesta DAX-queries** mot Power BI semantiska modeller och mäta svarstider i realtid
- **Analysera Delta-tabeller** i Fabric Lakehouse — kardinalitet, filstruktur och datakvalitet

---

## Installation

### Krav
- Python 3.10 eller senare
- Azure CLI installerat och inloggat (`az login`)
- Åtkomst till ett Microsoft Fabric/Power BI-workspace

### 1. Klona eller ladda ner filerna

Placera `app.py`, `requirements.txt` och övriga filer i en mapp, t.ex. `pbi_load_tester/`.

### 2. Installera Python-beroenden

```bash
pip install -r requirements.txt
```

Beroenden: `streamlit`, `requests`, `pandas`, `plotly`, `deltalake`, `pyarrow`.

### 3. Starta applikationen

```bash
python -m streamlit run app.py
```

Öppnas automatiskt på http://localhost:8501

---

## Autentisering

All autentisering sker i **sidopanelens översta avsnitt (🔐 Autentisering)**.

### Rekommenderat: Azure CLI

Välj **Azure CLI (rekommenderat)** och klicka **🔑 Hämta alla tokens**.

Det hämtar automatiskt fyra tokens på en gång:

| Token             | Används till                          |
|-------------------|---------------------------------------|
| Power BI          | DAX-körning via executeQueries API    |
| Fabric REST API   | Workspace/Lakehouse-listning          |
| OneLake Storage   | Läsa Delta-tabeller direkt            |
| Eventhouse/KQL    | Hämta queries från SemanticModelLogs  |

Kräver inloggning: `az login`. Token giltiga i ~60 min.

### Manuell token

Klistra in ett Bearer-token. Hämtas via:

```bash
az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv
```

### Service Principal

Fyll i Tenant ID, Client ID och Client Secret från en Azure App Registration med `Dataset.ReadWrite.All`.

---

## Gränssnittet — översikt

Applikationen har en **sidopanel** (vänster) och ett **huvudinnehåll** (höger) med två sektioner separerade av en turkos linje.

Sidopanelens ordning:
1. 🔐 Autentisering
2. 🎯 Semantisk modell
3. 👤 Row Level Security
4. ⚙️ Lastkonfiguration
5. 📊 Hämta queries från logg

Huvudinnehållet:
1. **# ⚡ Power BI Semantic Model Load Tester** — DAX-editor och lasttestning
2. **# 🗄️ Lakehouse Explorer** — Delta-tabellanalys

---

## Lasttestaren — steg för steg

### Steg 1 — Välj semantisk modell

Under **🎯 Semantisk modell** i sidopanelen:
1. Välj Workspace i dropdown
2. Välj Semantisk modell i nästa dropdown
3. GUID visas som caption

### Steg 2 — Skriv eller ladda DAX

I DAX-editorn:
- **Enkel query** — en query körs upprepade gånger
- **Flera queries (rotation)** — separera med `---`

Ladda sparad profil via expandern under editorn, eller importera från loggar via sidopanelen.

### Steg 3 — DAX-parametrar (valfritt)

Platshållarsyntax: `{{paramnamn}}`

```dax
-- Text (citattecken ingår redan i DAX-texten):
TREATAS({"{{fartyg}}"}, 'Fartyg'[Fartygsnamn])

-- Nummer:
TREATAS({{{ar}}}, 'Trafikdygn'[År])

-- Datum (YYYY-MM-DD):
'Trafikdygn'[Trafikdygn] >= {{startdatum}}
```

Ange värden, välj typ (text/number/date) och läge (Slumpa/Rotation/Fast).

### Steg 4 — Row Level Security (valfritt)

Aktivera **👤 RLS** i sidopanelen:
- **En användare:** UPN + valfria roller
- **Flera användare:** en per rad, `upn@domain.se` eller `upn@domain.se | Roll1`

> OBS: RLS kräver användarkonto — fungerar inte med Service Principal.

### Steg 5 — Lastkonfiguration

| Inställning      | Beskrivning                                   |
|------------------|-----------------------------------------------|
| Session-etikett  | Identifierar körningen i loggar               |
| Körläge          | Iterationer (fast antal) eller Tidsbaserat    |
| Parallella anrop | Antal samtidiga requests (1–50)               |
| Rate limiting    | Max anrop/min, undviker HTTP 429              |
| Trafikprofil     | Vågor / Kontorstider / Slumpmässig / Konstant |

### Steg 6 — Kör och tolka resultat

Klicka **▶ Starta**. Realtidslogg och diagram uppdateras under körningen.

Resultatflikar efter körning:

| Flik               | Innehåll                              |
|--------------------|---------------------------------------|
| 📈 Svarstider      | Scatter med P50/P95-linjer            |
| 📊 Histogram       | Fördelning av svarstider              |
| 📋 Rådata          | Alla anrop med CSV-export             |

Nyckelmetriker: P50, P95, P99, genomsnitt, throughput (q/s), felrate.

---

## Lakehouse Explorer — steg för steg

### Steg 1 — Anslut

I **⚙️ Anslutning**:
1. Se till att tokens hämtats via 🔐 Autentisering
2. Välj Workspace i dropdown
3. Välj Lakehouse i nästa dropdown

### Steg 2 — Identifiera tabeller

Klicka **🔍 Identifiera scheman och tabeller**. Appen navigerar automatiskt rätt oavsett om lakehouset är schema-aktiverat eller inte.

### Steg 3 — Välj och analysera

1. Välj scheman i **multiselect**
2. Välj tabeller i **multiselect** (uppdateras baserat på schema-val)
3. Konfigurera snabbläge (default 200 000 rader) eller fullständig analys
4. Klicka **📊 Analysera**

Klicka **⏹ Avbryt** för att stoppa pågående analys.

### Läsa resultaten

Per tabell:

**4 metrics:** totalt radantal · storlek MB · antal filer · snittfilstorlek

**Filstrukturrekommendation:**
- ✅ OK — väloptimerad eller för liten för att optimering ska spela roll
- ℹ️ Kan förbättras — periodisk OPTIMIZE hjälper
- 🟡 OPTIMIZE rekommenderas — många små filer i stor tabell
- 🔴 Kritiskt — micro-filer, kör OPTIMIZE + VACUUM omedelbart

Partitionerade tabeller analyseras per partition — en fil per partition är alltid OK.

**Kolumnstatistik** sorterad på kardinalitet:

| Kardinalitet         | Rekommendation                                                 |
|----------------------|----------------------------------------------------------------|
| > 1 000 000 unika    | ⚠️ Extrem — undvik DISTINCTCOUNT, MEDIAN, relationer          |
| > 700 000 unika      | ⚠️ Hög — överväg APPROXIMATEDISTINCTCOUNT()                   |
| > 75 000 unika       | ℹ️ Måttlig — DISTINCTCOUNT och MEDIAN fungerar men märks      |
| Hög null-frekvens    | ℹ️ Datakvalitetsinformation                                    |

Täckningsprocent visas för snabbläge — låg täckning ger osäkrare estimat.

---

## Hämta queries från loggar

Konfigurera under **📊 Hämta queries från logg** i sidopanelen:
1. Ange Eventhouse Cluster-URL: `https://xxxx.kusto.fabric.microsoft.com`
2. Ange databas
3. Klicka **🔍 Hämta och analysera**

Appen importerar verkliga queries, grupperar liknande, och identifierar TREATAS/IN-parametrar automatiskt.

---

## Kända begränsningar

- Rate limit: ~120 anrop/min för Premium/Fabric
- Token löper ut efter ~60 min — klicka 🔑 igen
- RLS fungerar inte med Service Principal
- DirectLake stöder inte `INFO.TABLES()` eller `COLUMNSTATISTICS()` via executeQueries
- Kardinalitetsestimat vid < 10% täckning kan vara missvisande

---

## Felsökning

| Problem                         | Lösning                                                  |
|---------------------------------|----------------------------------------------------------|
| `401 Unauthorized` Fabric API   | Hämta alla tokens — Power BI-token räcker inte           |
| `400 UnsupportedOperation`      | Schema-aktiverat lakehouse — hanteras automatiskt        |
| Tom workspace-dropdown          | Kontrollera att Power BI-token hämtats                   |
| `deltalake` saknas              | `pip install deltalake pyarrow`                          |
| `az: command not found`         | https://aka.ms/installazurecliwindows                    |