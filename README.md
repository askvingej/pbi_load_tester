# ⚡ Power BI Semantic Model Load Tester

Stresstesta DAX-queries mot din semantiska modell och mät svarstider i realtid.

## Snabbstart

### 1. Installera beroenden
```bash
pip install -r requirements.txt
```

### 2. Starta appen
```bash
streamlit run app.py
```

Appen öppnas automatiskt i din webbläsare på `http://localhost:8501`

---

## Autentisering

### Alternativ A — Manuell token (enklast att komma igång)
Hämta ett token via Azure CLI och klistra in det i appen:

```bash
#  ℹ️ Token som täcker både anrop mot semantiska modeller och KQL-databas
az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv

az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv
```

Token är giltigt i ~60 minuter.

### Alternativ B — Service Principal
Kräver en Azure AD-appregistrering med Power BI-behörigheter:
1. Skapa App Registration i Azure Portal
2. Lägg till `Dataset.ReadWrite.All` (Power BI Service API)
3. Ge Service Principal-åtkomst till Workspace i Power BI Admin
4. Fyll i Tenant ID, Client ID och Client Secret i sidopanelen

---

## Hitta Workspace ID och Dataset ID

**Från Power BI-tjänstens URL:**
```
https://app.powerbi.com/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}
```

**Via Power BI REST API:**
```bash
# Lista workspaces
curl -H "Authorization: Bearer $TOKEN" \
  https://api.powerbi.com/v1.0/myorg/groups

# Lista datasets i ett workspace
curl -H "Authorization: Bearer $TOKEN" \
  https://api.powerbi.com/v1.0/myorg/groups/{workspaceId}/datasets
```

---

## DAX-query-lägen

### Enkel query
Samma DAX-query körs för alla iterationer. Bra för att mäta svarstid för en specifik query under last.

### Flera queries (rotation)
Separera queries med `---`. De körs i rotation (0, 1, 2, 0, 1, 2...).
Bra för att simulera realistisk last med blandade queries.

```dax
EVALUATE ROW("Test", 1)
---
EVALUATE SUMMARIZECOLUMNS("Count", COUNTROWS('FactTable'))
---
EVALUATE TOPN(10, 'DimDate', 'DimDate'[Date], DESC)
```

---

## Lastkonfiguration

| Parameter | Beskrivning |
|---|---|
| **Iterationer** | Totalt antal queries att köra |
| **Parallella anrop** | Hur många queries som skickas samtidigt (concurrency) |
| **Fördröjning (ms)** | Väntetid innan varje anrop — simulerar realistisk last |

---

## Resultat och export

- **P50/P95/P99** — percentiler för svarstider
- **Throughput** — queries per sekund
- **Felrate** — andel misslyckade anrop
- **Realtidsdiagram** — svarstider över tid med percentilreferenslinjer
- **Histogram** — fördelning av svarstider
- **CSV-export** — ladda ner rådata för vidare analys

---

## Tips

- Börja med lågt concurrency (1-3) och öka gradvis
- Använd `EVALUATE ROW("Ping", 1)` som baseline-query
- Hög P99 relativt P95 indikerar sporadiska långsamma queries (cache-missar, cold start)
- Jämför resultat med och utan `ALLSELECTED`/`ALLEXCEPT` för att isolera DAX-overhead