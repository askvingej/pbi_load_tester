"""
Power BI Semantic Model Load Tester
====================================
Kör med: streamlit run app.py
"""

import time
import uuid
import json
import math
import random
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px

# ─── Sidkonfiguration ────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PBI Load Tester",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── CSS ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&display=swap');

    .stApp { background: #0d1117; }

    .metric-card {
        background: #161b22;
        border: 1px solid #21262d;
        border-radius: 6px;
        padding: 16px 20px;
        text-align: center;
    }
    .metric-value {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 28px;
        font-weight: 600;
        color: #00d4aa;
        line-height: 1;
    }
    .metric-label {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: #8b949e;
        margin-top: 6px;
    }
    .metric-unit { font-size: 13px; color: #8b949e; margin-left: 3px; }

    .log-box {
        background: #0d1117;
        border: 1px solid #21262d;
        border-radius: 6px;
        padding: 12px 14px;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 12px;
        height: 280px;
        overflow-y: auto;
        color: #c9d1d9;
        white-space: pre-wrap;
    }

    div[data-testid="stSidebar"] { background: #0d1117; border-right: 1px solid #21262d; }
    .stButton button {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 12px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
    }
</style>
""", unsafe_allow_html=True)

# ─── Session State ────────────────────────────────────────────────────────────
for key, default in [
    ("results", []),
    ("log_lines", []),
    ("token_cache", None),
    ("stop_flag", False),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ─── Auth ─────────────────────────────────────────────────────────────────────
def fetch_sp_token(tenant_id, client_id, client_secret):
    url  = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "https://analysis.windows.net/powerbi/api/.default",
    }
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

def resolve_token(auth_mode, tenant_id, client_id, client_secret, manual_token):
    if auth_mode == "Azure CLI (rekommenderat)":
        tok = st.session_state.get("_last_token", "")
        if not tok:
            raise ValueError("Token saknas — klicka 🔑 Hämta alla tokens i autentiseringssektionen.")
        return tok
    if auth_mode == "Manuell token":
        if not manual_token.strip():
            raise ValueError("Token saknas — klistra in ett Bearer Token.")
        return manual_token.strip()
    cache = st.session_state.token_cache
    if cache and cache["exp"] > time.time():
        return cache["tok"]
    tok = fetch_sp_token(tenant_id, client_id, client_secret)
    st.session_state.token_cache = {"tok": tok, "exp": time.time() + 55 * 60}
    return tok

# ─── DAX-bibliotek (JSON-fil) ─────────────────────────────────────────────────
DAX_LIBRARY_FILE = Path("dax_library.json")

def load_dax_library() -> dict:
    if DAX_LIBRARY_FILE.exists():
        try:
            return json.loads(DAX_LIBRARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_dax_library(lib: dict):
    DAX_LIBRARY_FILE.write_text(
        json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def dax_library_key(workspace_id: str, dataset_id: str) -> str:
    return f"{workspace_id}::{dataset_id}"

def workspace_config_key(workspace_id: str) -> str:
    return f"__ws_config__{workspace_id}"

def load_workspace_config(workspace_id: str) -> dict:
    """Hämtar sparad Eventhouse-konfiguration för ett workspace."""
    lib = load_dax_library()
    return lib.get(workspace_config_key(workspace_id), {})

def save_workspace_config(workspace_id: str, config: dict):
    """Sparar Eventhouse-konfiguration för ett workspace."""
    lib = load_dax_library()
    lib[workspace_config_key(workspace_id)] = config
    save_dax_library(lib)


# ─── DAX-parameterisering ─────────────────────────────────────────────────────
def apply_dax_params(dax: str, params: list[dict], iteration: int) -> str:
    """
    Ersätter {{param_namn}} i DAX-queryn med värden från params-listan.

    Varje param är ett dict med:
      name:   str   — parameternamn, matchar {{name}} i DAX
      mode:   str   — "Slumpa", "Rotation" eller "Fast"
      values: list  — lista med möjliga värden (strängar)
      type:   str   — "text", "number" eller "date"

    type styr hur värdet formateras i DAX:
      text   → "värde"  (med citattecken)
      number → värde    (utan citattecken)
      date   → DATE(yyyy, mm, dd)
    """
    import re

    def format_value(val: str, typ: str) -> str:
        import unicodedata
        val = unicodedata.normalize("NFC", val).strip()
        val = "".join(c for c in val if unicodedata.category(c) != "Cc")
        if typ == "date":
            try:
                from datetime import datetime as dt
                d = dt.strptime(val, "%Y-%m-%d")
                return f"DATE({d.year}, {d.month}, {d.day})"
            except Exception:
                return val
        if typ == "number":
            # Auto-detektera: om värdet inte är ett rent tal behandla det som text
            import re as _re
            if _re.match(r'^-?\d+(\.\d+)?$', val):
                return val
            else:
                escaped = val.replace('"', '""')
                return f'"{escaped}"'
        # text
        escaped = val.replace('"', '""')
        return f'"{escaped}"'

    for param in params:
        name = param.get("name", "").strip()
        mode = param.get("mode", "Slumpa")
        typ  = param.get("type", "text")

        raw_values = param.get("values", [])
        values = []
        for v in raw_values:
            v = v.strip()
            v = v.replace("\u201c", '"').replace("\u201d", '"')
            v = v.replace("\u2018", "'").replace("\u2019", "'")
            if v:
                values.append(v)

        if not name or not values:
            continue

        if mode == "Rotation":
            val = values[iteration % len(values)]
        elif mode == "Fast":
            val = values[0]
        else:
            val = random.choice(values)

        placeholder = "{{" + name + "}}"

        # Normalisera värdet
        import unicodedata as _ud2
        clean_val = _ud2.normalize("NFC", val).strip()
        clean_val = "".join(c for c in clean_val if _ud2.category(c) != "Cc")

        if typ == "number":
            formatted = clean_val
        elif typ == "date":
            try:
                from datetime import datetime as dt
                d = dt.strptime(clean_val, "%Y-%m-%d")
                formatted = f"DATE({d.year}, {d.month}, {d.day})"
            except Exception:
                formatted = clean_val
        else:
            # text — escapa inbyggda citattecken (DAX-standard: "" = escaped ")
            formatted = clean_val.replace('"', '""')

        # Enkel str.replace — byt ut bara platshållaren, lämna omgivande citattecken i fred
        dax = dax.replace(placeholder, formatted)

    return dax

# ─── Rate Limiter ─────────────────────────────────────────────────────────────
class RateLimiter:
    """
    Token bucket-baserad rate limiter.
    max_per_minute=0 innebär ingen begränsning.
    Trådsäker via threading.Lock.
    """
    def __init__(self, max_per_minute: int = 0):
        self.max_per_minute  = max_per_minute
        self._lock           = threading.Lock()
        self._tokens         = float(max_per_minute) if max_per_minute else 0.0
        self._last_refill    = time.perf_counter()
        self.throttle_count  = 0   # antal gånger vi väntade

    def acquire(self):
        """Vänta tills ett token finns tillgängligt."""
        if not self.max_per_minute:
            return
        while True:
            with self._lock:
                now     = time.perf_counter()
                elapsed = now - self._last_refill
                # Fyll på tokens proportionellt mot förfluten tid
                self._tokens = min(
                    float(self.max_per_minute),
                    self._tokens + elapsed * (self.max_per_minute / 60.0)
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                self.throttle_count += 1
            time.sleep(0.1)

# Global rate limiter — skapas om vid varje testkörning
_rate_limiter = RateLimiter(0)

# ─── DAX-körning via REST API ─────────────────────────────────────────────────
def run_dax(token, workspace_id, dataset_id, dax, identity=None, session_tag="", timeout=60):
    """
    Kör DAX via Power BI REST API executeQueries.
    Hanterar HTTP 429 med exponential backoff (upp till 3 försök).
    Returnerar (duration_ms, rows, error, correlation_id).
    """
    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
        f"/datasets/{dataset_id}/executeQueries"
    )

    username    = identity["username"] if identity and identity.get("username") else None
    custom_data = username or session_tag or "pbi-load-tester"
    corr_id     = f"{session_tag}|{custom_data}|{uuid.uuid4()}".replace(" ", "_")[:128]

    # Normalisera hela DAX-strängen till NFC Unicode innan bearbetning
    import unicodedata as _ud
    dax_stripped = _ud.normalize("NFC", dax.strip())
    dax_upper    = dax_stripped.upper()

    tracking_vars = (
        "VAR _Session          = \"" + session_tag + "\"\n"
        "VAR _ImpersonatedUser = \"" + (username or "anon") + "\"\n"
        "VAR _CorrelationId    = \"" + corr_id + "\"\n"
    )

    if dax_upper.startswith("EVALUATE"):
        # Enkel EVALUATE-query — wrappa med VAR-block
        dax_body = dax_stripped[len("EVALUATE"):].strip()
        dax = f"EVALUATE\n{tracking_vars}RETURN\n{dax_body}"

    elif dax_upper.startswith("DEFINE"):
        # DEFINE-block — injicera VAR:ar direkt efter DEFINE-nyckelordet
        # Hitta positionen efter "DEFINE" och eventuellt whitespace
        define_end = dax_stripped.index("DEFINE") + len("DEFINE")
        before     = dax_stripped[:define_end]
        after      = dax_stripped[define_end:]
        dax = f"{before}\n{tracking_vars}{after}"

    else:
        # Okänt format — lägg som kommentar (bästa möjliga)
        dax = f"// session:{session_tag} user:{username or 'anon'}\n" + dax_stripped

    # Debug: logga exakt DAX som skickas (första 500 tecken)
    dax_preview = dax[:500].replace("\n", "↵")

    headers = {
        "Authorization":           f"Bearer {token}",
        "Content-Type":            "application/json; charset=utf-8",
        "X-PowerBI-CorrelationId": corr_id,
    }
    body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}

    eff = {
        "username":   username or "pbi-load-tester",
        "datasets":   [{"id": dataset_id}],
        "customData": custom_data,
    }
    if identity and identity.get("roles"):
        eff["roles"] = identity["roles"]
    if username:
        body["impersonatedUserName"] = username
    body["effectiveIdentity"] = [eff]

    # Hämta token från rate limiter
    st.session_state.get("_rate_limiter", _rate_limiter).acquire()

    t0          = time.perf_counter()
    max_retries = 3
    backoff     = 5.0   # startvärde sekunder

    for attempt in range(max_retries):
        try:
            json_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            # Debug: sök efter trunkerade svenska tecken i JSON
            json_str = json_bytes.decode("utf-8")
            r = requests.post(
                url,
                data=json_bytes,
                headers=headers,
                timeout=timeout,
            )
            ms = (time.perf_counter() - t0) * 1000

            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", backoff))
                if attempt < max_retries - 1:
                    time.sleep(retry_after)
                    backoff *= 2
                    continue
                return ms, 0, f"HTTP 429: Rate limit, uppgav efter {max_retries} försök", corr_id

            if not r.ok:
                err_body = r.text[:400]
                if r.status_code == 400:
                    # Hitta kontexten runt 'Djurg' eller annat trunkerat värde i JSON
                    for probe in ["Djurg", "Sk\\u00e4", "Vax", "erg\\u00e5", "rden"]:
                        idx = json_str.find(probe)
                        if idx >= 0:
                            ctx = json_str[max(0,idx-20):idx+40]
                            err_body += f"\n\nJSON-kontext runt '{probe}': ...{ctx}..."
                            break
                    return ms, 0, f"HTTP 400: {err_body}\n\nDAX-förhandsvisning: {dax_preview}", corr_id
                return ms, 0, f"HTTP {r.status_code}: {err_body}", corr_id

            rows = len(
                r.json().get("results", [{}])[0]
                  .get("tables",  [{}])[0]
                  .get("rows",    [])
            )
            return ms, rows, None, corr_id

        except requests.exceptions.Timeout:
            return (time.perf_counter() - t0) * 1000, 0, "Timeout", corr_id
        except Exception as e:
            return (time.perf_counter() - t0) * 1000, 0, str(e), corr_id

    return (time.perf_counter() - t0) * 1000, 0, "Max retries uppnådda", corr_id


# ─── Statistik ────────────────────────────────────────────────────────────────
def compute_stats(results):
    if not results:
        return {}
    ok  = [r["duration_ms"] for r in results if r["error"] is None]
    err = [r for r in results if r["error"] is not None]
    if not ok:
        return {"total": len(results), "errors": len(err), "error_rate": 100.0,
                "p50": 0, "p95": 0, "p99": 0, "mean": 0, "throughput": 0}
    s = pd.Series(ok)
    return {
        "total":      len(results),
        "success":    len(ok),
        "errors":     len(err),
        "error_rate": len(err) / len(results) * 100,
        "min":        min(ok),
        "max":        max(ok),
        "mean":       s.mean(),
        "p50":        s.quantile(0.50),
        "p95":        s.quantile(0.95),
        "p99":        s.quantile(0.99),
        "throughput": len(ok) / (sum(ok) / 1000) if ok else 0,
    }

# ─── Resultatvy ───────────────────────────────────────────────────────────────
def render_results(results):
    if not results:
        return
    stats = compute_stats(results)
    st.markdown("#### 📊 Statistik")

    cols = st.columns(6)
    defs = [
        ("P50",        "p50",        "ms",  "#00d4aa"),
        ("P95",        "p95",        "ms",  "#f0b429"),
        ("P99",        "p99",        "ms",  "#ef4444"),
        ("Medel",      "mean",       "ms",  "#00d4aa"),
        ("Throughput", "throughput", "q/s", "#00d4aa"),
        ("Felrate",    "error_rate", "%",   "#ef4444" if stats.get("error_rate", 0) > 0 else "#00d4aa"),
    ]
    for col, (label, key, unit, color) in zip(cols, defs):
        val = stats.get(key, 0)
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-value" style="color:{color}">
                {val:.1f}<span class="metric-unit">{unit}</span>
            </div>
            <div class="metric-label">{label}</div>
        </div>""", unsafe_allow_html=True)

    st.markdown(" ")
    tab1, tab2, tab3 = st.tabs(["📈 Svarstider över tid", "📊 Histogram", "📋 Rådata"])
    df    = pd.DataFrame(results)
    df_ok = df[df["error"].isna()].copy()

    with tab1:
        if not df_ok.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df_ok["i"], y=df_ok["duration_ms"],
                mode="lines+markers", name="Svarstid",
                line=dict(color="#00d4aa", width=1.5),
                marker=dict(size=4),
            ))
            if len(df_ok) >= 5:
                roll = df_ok["duration_ms"].rolling(5, min_periods=1).mean()
                fig.add_trace(go.Scatter(
                    x=df_ok["i"], y=roll, mode="lines",
                    name="Medel (5)", line=dict(color="#f0b429", width=2, dash="dot"),
                ))
            for pct, val, c in [
                ("p50", stats["p50"], "#8b949e"),
                ("p95", stats["p95"], "#f0b429"),
                ("p99", stats["p99"], "#ef4444"),
            ]:
                fig.add_hline(y=val, line_dash="dash", line_color=c, opacity=0.5,
                              annotation_text=f" {pct}: {val:.0f} ms",
                              annotation_position="right")
            df_err = df[df["error"].notna()]
            if not df_err.empty:
                fig.add_trace(go.Scatter(
                    x=df_err["i"], y=df_err["duration_ms"],
                    mode="markers", name="Fel",
                    marker=dict(color="#ef4444", size=9, symbol="x"),
                ))
            fig.update_layout(
                template="plotly_dark", paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                xaxis=dict(title="Iteration", gridcolor="#21262d"),
                yaxis=dict(title="ms", gridcolor="#21262d"),
                legend=dict(bgcolor="#161b22"),
                margin=dict(l=0, r=100, t=10, b=0), height=340,
            )
            st.plotly_chart(fig, use_container_width=True)

    with tab2:
        if not df_ok.empty:
            fig2 = px.histogram(df_ok, x="duration_ms", nbins=30,
                                color_discrete_sequence=["#00d4aa"],
                                labels={"duration_ms": "Svarstid (ms)"})
            for pct, val, c in [
                ("p50", stats["p50"], "#8b949e"),
                ("p95", stats["p95"], "#f0b429"),
                ("p99", stats["p99"], "#ef4444"),
            ]:
                fig2.add_vline(x=val, line_dash="dash", line_color=c,
                               annotation_text=f" {pct}", annotation_position="top right")
            fig2.update_layout(
                template="plotly_dark", paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                xaxis=dict(gridcolor="#21262d"),
                yaxis=dict(title="Antal", gridcolor="#21262d"),
                margin=dict(l=0, r=0, t=10, b=0), height=340, bargap=0.05,
            )
            st.plotly_chart(fig2, use_container_width=True)

    with tab3:
        cols_show = ["i", "ts", "duration_ms", "rows", "query_idx", "user", "correlation_id", "error"]
        df_show = df.reindex(columns=cols_show).copy()
        df_show.columns = ["#", "Tid", "ms", "Rader", "Query", "Användare", "CorrelationId", "Fel"]
        df_show["ms"] = df_show["ms"].round(1)
        st.dataframe(df_show, use_container_width=True, height=300)
        csv = df_show.to_csv(index=False).encode("utf-8")
        st.download_button("⬇ Ladda ner CSV", csv, "lasttest.csv", "text/csv")


# ─── Performance Advisor ─────────────────────────────────────────────────────

def _run_dmv(token: str, workspace_id: str, dataset_id: str, dmv_query: str) -> list[dict]:
    """Kör en INFO DAX-query via executeQueries och returnerar rader som list[dict]."""
    # INFO-funktioner behöver EVALUATE prefix
    q = dmv_query.strip()
    if not q.upper().startswith("EVALUATE"):
        q = "EVALUATE " + q
    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
        f"/datasets/{dataset_id}/executeQueries"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json; charset=utf-8",
    }
    body = {"queries": [{"query": q}], "serializerSettings": {"includeNulls": True}}
    r = requests.post(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        timeout=30,
    )
    if not r.ok:
        raise ValueError(f"DMV-fel {r.status_code}: {r.text[:300]}")
    data  = r.json()
    rows  = data.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])
    return rows


def fetch_performance_data(token: str, workspace_id: str, dataset_id: str) -> dict:
    """
    Hämtar metadata om modellen via DAX TOPN(0,...) discovery.
    Fungerar för alla modelltyper inkl DirectLake.
    """
    result   = {}
    pbi_base = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}"
    dax_url  = f"{pbi_base}/executeQueries"
    headers  = {"Authorization": f"Bearer {token}"}
    dax_hdrs = {**headers, "Content-Type": "application/json; charset=utf-8"}

    def dax(query):
        body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
        r = requests.post(dax_url,
                          data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers=dax_hdrs, timeout=60)
        if not r.ok:
            raise ValueError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json().get("results",[{}])[0].get("tables",[{}])[0].get("rows",[])

    # ── Dataset-info ──────────────────────────────────────────────────────────
    try:
        r = requests.get(pbi_base, headers=headers, timeout=15)
        if r.ok:
            info         = r.json()
            storage_mode = info.get("targetStorageMode", "")
            dataset_mode = info.get("defaultMode", "")
            display_mode = (
                "DirectLake" if storage_mode == "PremiumFiles" or "directlake" in dataset_mode.lower()
                else dataset_mode or storage_mode or "Unknown"
            )
            result["dataset_info"] = {
                "Namn":            info.get("name",""),
                "Typ":             display_mode,
                "Konfigurerad av": info.get("configuredBy",""),
            }
            result["raw_dataset_api"] = {k: v for k, v in info.items()
                                          if k in ("name","defaultMode","targetStorageMode","isRefreshable")}
            result["is_directlake"] = (storage_mode == "PremiumFiles" or
                                        "directlake" in dataset_mode.lower())
    except Exception as e:
        result["dataset_info_error"] = str(e)

    # ── Steg 1: Hitta tabellnamn via COLUMNSTATISTICS() ───────────────────────
    # COLUMNSTATISTICS() fungerar för Import — returnerar tabeller+kolumner
    # För DirectLake returnerar den bara beräknade tabeller, men ger oss en startpunkt
    known_tables = set()
    columns_list = []

    try:
        cs_rows = dax("EVALUATE COLUMNSTATISTICS()")
        for row in cs_rows:
            tbl = row.get("[Table Name]","")
            col = row.get("[Column Name]","")
            if tbl and col and "RowNumber" not in col:
                known_tables.add(tbl)
                columns_list.append({
                    "Tabell": tbl, "Kolumn": col,
                    "Distinkta värden": int(row.get("[Cardinality]",0) or 0),
                    "Min": str(row.get("[Min]","") or ""),
                    "Max": str(row.get("[Max]","") or ""),
                    "Datatyp": "", "Dold": False,
                    "Källa": "COLUMNSTATISTICS",
                })
        result["source"] = f"COLUMNSTATISTICS — {len(known_tables)} tabeller, {len(columns_list)} kolumner"
    except Exception as e:
        result["columnstatistics_error"] = str(e)

    # ── Steg 2: DirectLake — extrahera tabellnamn ur DAX-queries + manuell input ─
    if result.get("is_directlake"):
        debug_info = {}

        # Extrahera tabellnamn ur DAX-queries i session (lasttest + Eventhouse-loggar)
        import re as _re2
        _TABLE_RE = _re2.compile(r"'([^']+)'\[")
        extracted_tables = set()

        # Hämta från körda queries i session
        for r_item in (
            list(st.session_state.get("results", [])) +
            [{"query": q.get("sample","")} for q in st.session_state.get("eh_clustered",[])]
        ):
            qtext = r_item.get("query","") if isinstance(r_item, dict) else ""
            for m in _TABLE_RE.finditer(qtext):
                extracted_tables.add(m.group(1))

        # Slå ihop med redan kända tabeller från COLUMNSTATISTICS
        all_table_names = set(known_tables) | extracted_tables
        debug_info["extracted_from_queries"] = sorted(extracted_tables)
        debug_info["all_tables_to_try"]      = sorted(all_table_names)

        # Kör TOPN(1) per tabell för att få kolumnnamn
        dl_cols_found = 0
        topn_results  = {}

        for tbl in sorted(all_table_names):
            if not tbl:
                continue
            try:
                body = {"queries": [{"query": f"EVALUATE TOPN(1, '{tbl}')"}],
                        "serializerSettings": {"includeNulls": True}}
                r2 = requests.post(dax_url,
                                   data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                   headers=dax_hdrs, timeout=15)
                if r2.ok:
                    rows = r2.json().get("results",[{}])[0].get("tables",[{}])[0].get("rows",[])
                    if rows:
                        col_keys = list(rows[0].keys())
                        topn_results[tbl] = {"ok": True, "kolumner": len(col_keys)}
                        for ck in col_keys:
                            col_name = ck.split("[",1)[1].rstrip("]") if "[" in ck else ck
                            if "RowNumber" in col_name:
                                continue
                            if not any(c["Tabell"] == tbl and c["Kolumn"] == col_name
                                       for c in columns_list):
                                columns_list.append({
                                    "Tabell": tbl, "Kolumn": col_name,
                                    "Distinkta värden": 0,
                                    "Min": "", "Max": "", "Datatyp": "", "Dold": False,
                                    "Källa": "TOPN(1)",
                                })
                                dl_cols_found += 1
                    else:
                        topn_results[tbl] = {"ok": True, "kolumner": 0, "note": "Tom tabell"}
                else:
                    err = r2.json().get("error",{}).get("pbi.error",{}).get("details",[{}])
                    msg = err[0].get("detail",{}).get("value","") if err else r2.text[:100]
                    topn_results[tbl] = {"ok": False, "error": msg[:120]}
            except Exception as e:
                topn_results[tbl] = {"ok": False, "error": str(e)[:80]}

        debug_info["topn1_results"]  = topn_results
        debug_info["dl_cols_found"]  = dl_cols_found
        result["directlake_debug"]   = debug_info

        if dl_cols_found > 0:
            result["source"] = (result.get("source","") +
                                f" + TOPN(1) — {dl_cols_found} DirectLake-kolumner")


    # ── Relationer ────────────────────────────────────────────────────────────
    try:
        r = requests.get(f"{pbi_base}/relationships", headers=headers, timeout=15)
        if r.ok:
            result["relationships"] = [
                {
                    "Från tabell":  rel.get("fromTable",""),
                    "Från kolumn":  rel.get("fromColumn",""),
                    "Till tabell":  rel.get("toTable",""),
                    "Till kolumn":  rel.get("toColumn",""),
                    "Riktning":     rel.get("crossFilteringBehavior",""),
                }
                for rel in r.json().get("value",[])
            ]
    except Exception as e:
        result["relationships_error"] = str(e)

    return result


def fetch_directlake_cardinality(
    token: str,
    workspace_id: str,
    dataset_id: str,
    table_columns: list[dict],
) -> dict:
    """
    Hämtar kardinalitet för DirectLake-modeller via DAX DISTINCTCOUNT().
    Kör batchad DAX-query med SUMMARIZECOLUMNS per tabell.
    Returnerar uppdaterad all_columns-lista.
    """
    dax_url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
        f"/datasets/{dataset_id}/executeQueries"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json; charset=utf-8",
    }

    # Gruppera kolumner per tabell
    by_table: dict[str, list[str]] = {}
    for c in table_columns:
        tbl = c.get("Tabell", "")
        col = c.get("Kolumn", "")
        if tbl and col:
            by_table.setdefault(tbl, []).append(col)

    cardinality_map: dict[tuple, int] = {}

    for tbl, cols in by_table.items():
        # Bygg en EVALUATE-query med DISTINCTCOUNT per kolumn
        # Max 20 kolumner per query för att undvika timeout
        for batch_start in range(0, len(cols), 20):
            batch = cols[batch_start:batch_start + 20]
            measures = ", ".join(
                f'"DISTINCTCOUNT_{c.replace(" ","_").replace("[","").replace("]","")}", '
                f'DISTINCTCOUNT(\'{tbl}\'[{c}])'
                for c in batch
            )
            dax = f"EVALUATE ROW({measures})"

            try:
                body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
                r = requests.post(
                    dax_url,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    timeout=30,
                )
                if not r.ok:
                    continue
                rows = r.json().get("results",[{}])[0].get("tables",[{}])[0].get("rows",[])
                if not rows:
                    continue
                row = rows[0]
                for c in batch:
                    key_suffix = c.replace(" ","_").replace("[","").replace("]","")
                    val = next(
                        (v for k, v in row.items() if key_suffix in k),
                        None
                    )
                    if val is not None:
                        try:
                            cardinality_map[(tbl, c)] = int(val)
                        except Exception:
                            pass
            except Exception:
                continue

    # Uppdatera all_columns med faktisk kardinalitet
    updated = []
    for c in table_columns:
        c = c.copy()
        key = (c.get("Tabell",""), c.get("Kolumn",""))
        if key in cardinality_map:
            c["Distinkta värden"] = cardinality_map[key]
        updated.append(c)

    return updated


def get_dataset_mode(token: str, workspace_id: str, dataset_id: str) -> str:
    """Returnerar defaultMode för datasetet: DirectLake, Import, DirectQuery etc."""
    try:
        r = requests.get(
            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.ok:
            return r.json().get("defaultMode", "Unknown")
    except Exception:
        pass
    return "Unknown"


def fetch_slow_queries(
    token: str,
    cluster_url: str,
    database: str,
    workspace_id: str,
    dataset_id: str,
    p95_threshold_ms: float = 2000,
    top_n: int = 10,
) -> list[dict]:
    """Hämtar långsamma queries från SemanticModelLogs (p95 > tröskel)."""
    kusto_token = resolve_kusto_token()
    resolved_db = resolve_kusto_database(cluster_url, database, kusto_token)
    ws_clean    = workspace_id.strip().lower()
    ds_clean    = dataset_id.strip().lower()

    kql = (
        "SemanticModelLogs\n"
        "| where OperationName == \"QueryEnd\"\n"
        "| where tolower(WorkspaceId) == \"" + ws_clean + "\"\n"
        "| where tolower(ItemId) == \"" + ds_clean + "\"\n"
        "| where isnotempty(EventText)\n"
        "| extend CleanQuery = replace_regex(EventText, "
        "@'VAR _Session[^\\n]*\\n(VAR _ImpersonatedUser[^\\n]*\\n)?(VAR _CorrelationId[^\\n]*\\n)?', '')\n"
        "| extend CleanQuery = replace_regex(CleanQuery, @'\\s*\\[[A-Za-z]+Time:[^\\]]*\\]\\s*$', '')\n"
        "| extend CleanQuery = trim(' \\t\\n\\r', CleanQuery)\n"
        "| where CleanQuery matches regex @'^\\s*(EVALUATE|DEFINE)'\n"
        "| where CleanQuery !contains \"COLUMNSTATISTICS\"\n"
        "| summarize\n"
        "    ExecCount  = count(),\n"
        "    p50_ms     = percentile(DurationMs, 50),\n"
        "    p95_ms     = percentile(DurationMs, 95),\n"
        "    p99_ms     = percentile(DurationMs, 99),\n"
        "    AvgMs      = round(avg(DurationMs), 0)\n"
        "  by CleanQuery\n"
        "| where p95_ms > " + str(p95_threshold_ms) + "\n"
        "| top " + str(top_n) + " by p95_ms desc\n"
    )

    url     = f"{cluster_url.rstrip('/')}/v1/rest/query"
    headers = {
        "Authorization":           f"Bearer {kusto_token}",
        "Content-Type":            "application/json; charset=utf-8",
        "Accept":                  "application/json",
        "x-ms-client-request-id": str(uuid.uuid4()),
    }
    body = {
        "db":  resolved_db,
        "csl": kql,
        "properties": {"Options": {"queryconsistency": "strongconsistency"}},
    }
    r = requests.post(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        timeout=30,
    )
    if not r.ok:
        raise ValueError(f"KQL-fel {r.status_code}: {r.text[:300]}")

    data      = r.json()
    tables    = data.get("Tables", [])
    if not tables:
        return []
    rows      = tables[0].get("Rows", [])
    cols      = [c.get("ColumnName", "") for c in tables[0].get("Columns", [])]
    return [dict(zip(cols, row)) for row in rows]


def _analyze_dax_patterns(queries: list[str]) -> list[dict]:
    """
    Analyserar DAX-queries efter kända ineffektivitetsmönster.
    Returnerar lista av fynd: [{query_preview, issue, severity, suggestion}]
    """
    findings = []

    patterns = [
        (
            r'\bFILTER\s*\(\s*ALL\b',
            "FILTER(ALL(...))",
            "hög",
            "Ersätt FILTER(ALL(T), ...) med CALCULATETABLE(..., ALL(T)) för bättre optimering.",
        ),
        (
            r'\bFILTER\s*\(\s*(?!KEEPFILTERS|VALUES|ALLSELECTED)',
            "FILTER() på hel tabell",
            "medel",
            "Undvik FILTER(Tabell, ...) — använd CALCULATETABLE(Tabell, ...) istället.",
        ),
        (
            r'\bISFILTERED\b|\bHASCROSSFILTERED\b',
            "ISFILTERED/HASCROSSFILTERED",
            "medel",
            "Dessa funktioner kan sakta ner queries i DirectLake/DirectQuery-läge.",
        ),
        (
            r'\bCALCULATE\s*\([^)]+,\s*FILTER\s*\(',
            "CALCULATE med inbäddad FILTER",
            "medel",
            "Flytta filtervillkor direkt till CALCULATE-argumentet istället för FILTER().",
        ),
        (
            r'\bCOUNTROWS\s*\(\s*FILTER\s*\(',
            "COUNTROWS(FILTER(...))",
            "hög",
            "Ersätt med CALCULATE(COUNTROWS(T), filtervillkor) för markant bättre prestanda.",
        ),
        (
            r'\bSUMX\s*\(\s*(?!VALUES|SUMMARIZE)',
            "SUMX på hel tabell",
            "hög",
            "SUMX itererar hela tabellen — lägg till filter eller använd SUM om möjligt.",
        ),
        (
            r'\bCROSSJOIN\b',
            "CROSSJOIN",
            "hög",
            "CROSSJOIN skapar kartesisk produkt — kan bli extremt stor. Överväg SUMMARIZECOLUMNS.",
        ),
        (
            r'\bEARLIER\b',
            "EARLIER()",
            "medel",
            "EARLIER är långsam vid stora tabeller. Ersätt med VAR för bättre prestanda.",
        ),
    ]

    import re as _re
    for q in queries:
        q_upper = q.upper()
        preview = q[:120].replace("\n", " ").strip()
        if len(q) > 120:
            preview += "..."
        for pattern, name, severity, suggestion in patterns:
            if _re.search(pattern, q, _re.IGNORECASE):
                findings.append({
                    "query_preview": preview,
                    "issue":         name,
                    "severity":      severity,
                    "suggestion":    suggestion,
                })
    return findings



def get_storage_token() -> str:
    """Hämtar token med storage.azure.com scope för OneLake-åtkomst."""
    import subprocess, shutil
    resource = "https://storage.azure.com/"
    az_cmd   = shutil.which("az") or shutil.which("az.cmd")
    args     = [az_cmd or "az", "account", "get-access-token",
                "--resource", resource, "--query", "accessToken", "-o", "tsv"]
    result   = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise ValueError(
        f"Kunde inte hämta storage-token via Azure CLI.\n{result.stderr.strip()}\n"
        "Kontrollera att du är inloggad med 'az login'."
    )


def list_onelake_tables(workspace_id: str, lakehouse_id: str, token: str,
                        schema: str = "") -> tuple[list[str], str]:
    """
    Listar Delta-tabeller via OneLake ADLS Gen2 filesystem API.
    Hämtar oneLakeTablesPath från lakehouse-info och listar kataloger direkt.
    """
    headers     = {"Authorization": f"Bearer {token}"}
    fabric_base = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/lakehouses/{lakehouse_id}"
    storage_token = st.session_state.get("le_storage_token", "")

    # Steg 1: Hämta oneLakeTablesPath från lakehouse-info
    tables_path = None
    try:
        r_info = requests.get(fabric_base, headers=headers, timeout=15)
        if r_info.ok:
            props = r_info.json().get("properties", {})
            tables_path = props.get("oneLakeTablesPath", "")
    except Exception:
        pass

    # Steg 2: Lista via OneLake ADLS Gen2 om vi har sökvägen
    if tables_path and storage_token:
        list_url = tables_path.rstrip("/") + "?resource=filesystem&recursive=false"
        storage_headers = {
            "Authorization": f"Bearer {storage_token}",
            "x-ms-version":  "2020-10-02",
        }
        r_list = requests.get(list_url, headers=storage_headers, timeout=15)
        if r_list.ok:
            paths = r_list.json().get("paths", [])
            dirs  = [
                p["name"].split("/")[-1]
                for p in paths
                if str(p.get("isDirectory","false")).lower() == "true"
                and not p["name"].endswith("_delta_log")
            ]

            # Kolla om toppnivån är scheman eller faktiska tabeller
            # Faktiska Delta-tabeller har en _delta_log-undermapp
            # Schemakataloger innehåller tabeller som underkataloger
            # Enkel heuristik: om _delta_log inte finns i någon katalog → det är scheman
            actual_tables = []
            schema_dirs   = []

            for d in dirs:
                # Kolla om det finns _delta_log direkt under denna katalog
                _check_url = (
                    tables_path.rstrip("/") + f"/{d}/_delta_log"
                    "?resource=filesystem&recursive=false"
                )
                _cr = requests.get(_check_url, headers=storage_headers, timeout=5)
                if _cr.ok:
                    actual_tables.append(d)  # Det är en Delta-tabell
                else:
                    schema_dirs.append(d)    # Det är troligen ett schema

            if actual_tables:
                return sorted(actual_tables), "OneLake ADLS (Tables/)"

            # Ingen tabell på toppnivån — lista ett nivå djupare (scheman)
            all_tables = []
            used_schema = "OneLake ADLS"
            for sd in schema_dirs:
                sub_url = (
                    tables_path.rstrip("/") + f"/{sd}"
                    "?resource=filesystem&recursive=false"
                )
                r_sub = requests.get(sub_url, headers=storage_headers, timeout=15)
                if r_sub.ok:
                    sub_paths = r_sub.json().get("paths", [])
                    for sp in sub_paths:
                        tname = sp["name"].split("/")[-1]
                        if (str(sp.get("isDirectory","false")).lower() == "true"
                                and not tname.endswith("_delta_log")):
                            all_tables.append(f"{sd}/{tname}")

            if all_tables:
                return sorted(all_tables), "OneLake ADLS (schema/tabell)"

    # Steg 3: Fallback — prova Fabric API med scheman från /schemas
    fabric_token = st.session_state.get("le_fabric_token", token)
    fab_headers  = {"Authorization": f"Bearer {fabric_token}"}

    # Hämta scheman
    schemas_found = []
    r_s = requests.get(f"{fabric_base}/schemas", headers=fab_headers, timeout=15)
    if r_s.ok:
        schemas_found = [s.get("name","") for s in r_s.json().get("value",[]) if s.get("name")]

    if not schemas_found and schema:
        schemas_found = [schema]
    if not schemas_found:
        schemas_found = ["dbo", "semantic_contract", "lh", "default"]

    errors = []
    for s in schemas_found:
        r = requests.get(f"{fabric_base}/schemas/{s}/tables", headers=fab_headers, timeout=15)
        if r.ok:
            tables = [t.get("name","") for t in r.json().get("value",[]) if t.get("name")]
            if tables:
                return sorted(tables), s
        errors.append(f"  schema='{s}': HTTP {r.status_code}")

    r2 = requests.get(f"{fabric_base}/tables", headers=fab_headers, timeout=15)
    if r2.ok:
        tables = [t.get("name","") for t in r2.json().get("value",[]) if t.get("name")]
        if tables:
            return sorted(tables), "(ingen schema)"

    errors.append(f"  plain: HTTP {r2.status_code}: {r2.text[:150]}")

    if not storage_token:
        errors.append(
            "\n💡 Tips: Hämta storage-token via 🔑 Hämta alla tokens för att "
            "lista tabeller direkt från OneLake."
        )
    raise ValueError("Alla endpoints misslyckades:\n" + "\n".join(errors))


def read_delta_stats(workspace_id: str, lakehouse_id: str,
                     table_name: str, token: str) -> dict:
    """
    Läser Delta-tabellstatistik från OneLake via deltalake-biblioteket.
    Returnerar radantal, schema, kardinalitet per kolumn och null-frekvens.
    """
    try:
        from deltalake import DeltaTable
        import pyarrow.compute as pc
    except ImportError:
        raise ValueError("Kör: pip install deltalake pyarrow")

    table_uri = (
        f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/"
        f"{lakehouse_id}/Tables/{table_name}"
    )
    storage_options = {
        "bearer_token":          token,
        "use_fabric_endpoint":   "true",
        "account_name":          "onelake",
    }

    dt     = DeltaTable(table_uri, storage_options=storage_options)
    schema = dt.schema()
    ds     = dt.to_pyarrow_dataset()
    tbl    = ds.to_table()

    total_rows = len(tbl)
    col_stats  = []

    for field in schema.fields:
        col_name  = field.name
        if col_name.startswith("__"):
            continue
        try:
            arr        = tbl.column(col_name)
            null_count = arr.null_count
            null_pct   = round(null_count / max(total_rows, 1) * 100, 1)
            distinct   = len(pc.unique(arr))
            col_stats.append({
                "Kolumn":           col_name,
                "Datatyp":          str(field.type),
                "Distinkta värden": distinct,
                "Null-värden":      null_count,
                "Null %":           null_pct,
                "Rekommendation":   (
                    "⚠️ Extrem kardinalitet" if distinct > 1_000_000 else
                    "⚠️ Hög kardinalitet"    if distinct > 100_000  else
                    "⚠️ Hög null-frekvens"   if null_pct > 50       else
                    "✅ OK"
                ),
            })
        except Exception:
            col_stats.append({
                "Kolumn": col_name, "Datatyp": str(field.type),
                "Distinkta värden": "?", "Null-värden": "?", "Null %": "?",
                "Rekommendation": "⚠️ Kunde inte läsas",
            })

    return {
        "table":      table_name,
        "total_rows": total_rows,
        "columns":    col_stats,
        "schema":     [(f.name, str(f.type)) for f in schema.fields],
    }


# ─── Trafikprofiler ───────────────────────────────────────────────────────────

def _profile_description(profile):
    return {
        "Vågor (burst + paus)":        "Korta burstar med 3–8 s paus emellan — som användare som öppnar rapportsidor.",
        "Kontorstider (trappa upp/ner)": "Låg last i början och slutet, hög last i mitten — miniatyr av en arbetsdag.",
        "Slumpmässig":                  "Oregelbunden last, ingen fördröjning följer ett fast mönster.",
        "Konstant":                     "Jämn last under hela perioden, klassiskt lasttestmönster.",
    }.get(profile, "")

def next_delay_seconds(profile, elapsed, duration_sec, concurrency):
    """
    Returnerar hur många sekunder körloopen ska vänta innan nästa anrop skickas.
    elapsed     = sekunder sedan testet startade
    duration_sec = total körtid
    concurrency  = max parallella anrop (används för att skala burst-storlek)
    """
    progress = elapsed / max(duration_sec, 1)   # 0.0 → 1.0

    if profile == "Konstant":
        return 0.05   # minimal throttle så vi inte hammrar lokalt

    if profile == "Vågor (burst + paus)":
        # Sinusvåg med period ~30 s — hög last när sin > 0.3, paus annars
        wave = math.sin(elapsed * (2 * math.pi / 30))
        if wave > 0.3:
            return random.uniform(0.05, 0.3)    # burst: snabba anrop
        else:
            return random.uniform(2.0, 6.0)     # paus mellan vågor

    if profile == "Kontorstider (trappa upp/ner)":
        # Triangulär profil: upp första tredjedelen, platt i mitten, ner sista
        if progress < 0.2:
            intensity = progress / 0.2           # 0 → 1
        elif progress < 0.8:
            intensity = 1.0                      # full last
        else:
            intensity = (1.0 - progress) / 0.2  # 1 → 0
        base = 2.0 - (intensity * 1.8)           # 2.0 s → 0.2 s
        return max(0.05, base + random.uniform(-0.1, 0.1))

    if profile == "Slumpmässig":
        return random.choice([
            random.uniform(0.05, 0.3),   # snabb
            random.uniform(0.5,  2.0),   # mellantempo
            random.uniform(3.0,  8.0),   # lång paus
        ])

    return 0.1

# ─── PBI REST — lista workspaces & datasets ───────────────────────────────────
@st.cache_data(ttl=120, show_spinner=False)
def fetch_workspaces(token):
    """Hämtar alla workspaces som token-ägaren har tillgång till."""
    r = requests.get(
        "https://api.powerbi.com/v1.0/myorg/groups",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    items = r.json().get("value", [])
    return sorted(items, key=lambda x: x.get("name", "").lower())

@st.cache_data(ttl=120, show_spinner=False)
def fetch_datasets(token, workspace_id):
    """Hämtar alla semantiska modeller i ett workspace."""
    r = requests.get(
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    items = r.json().get("value", [])
    return sorted(items, key=lambda x: x.get("name", "").lower())


# ─── Eventhouse / KQL-logg ────────────────────────────────────────────────────
def get_kusto_token_via_cli() -> str:
    """
    Hämtar ett Kusto-token automatiskt via Azure CLI (az).
    Provar flera varianter för att hantera Windows PATH-problem.
    """
    import subprocess, shutil

    resource = "https://kusto.kusto.windows.net"
    args_variants = [
        ["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
        ["cmd", "/c", "az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
    ]

    # Försök också hitta az.cmd explicit på Windows
    az_cmd = shutil.which("az") or shutil.which("az.cmd")
    if az_cmd:
        args_variants.insert(0, [az_cmd, "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"])

    last_err = ""
    for args in args_variants:
        try:
            result = subprocess.run(
                args,
                capture_output=True, text=True, timeout=15,
                shell=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            last_err = result.stderr.strip() or f"returncode={result.returncode}"
        except FileNotFoundError:
            last_err = f"Kommando ej hittat: {args[0]}"
            continue
        except Exception as e:
            last_err = str(e)
            continue

    raise ValueError(
        f"Kunde inte hämta Kusto-token via Azure CLI.\n"
        f"Sista fel: {last_err}\n\n"
        f"Kontrollera att Azure CLI är installerat och att du kört 'az login'.\n"
        f"Ladda ner: https://aka.ms/installazurecliwindows"
    )


def resolve_kusto_token() -> str:
    """
    Returnerar ett giltigt Kusto-token.
    Hämtar via Azure CLI och cachar i session_state i 55 minuter.
    """
    cache = st.session_state.get("kusto_token_cache")
    if cache and cache["exp"] > time.time():
        return cache["tok"]
    # Hämta nytt token — låt undantag propagera upp till UI
    tok = get_kusto_token_via_cli()
    st.session_state["kusto_token_cache"] = {
        "tok": tok,
        "exp": time.time() + 55 * 60,
    }
    return tok
    """
    Slår upp databas-GUID via PrettyName. Kör .show databases och matchar läsbart namn.
    Returnerar GUID om hittat, annars friendly_name som fallback.
    """
    url = f"{cluster_url.rstrip('/')}/v1/rest/query"
    headers = {
        "Authorization": f"Bearer {resolve_kusto_token()}",
        "Content-Type":  "application/json; charset=utf-8",
        "Accept":        "application/json",
    }
    body = {
        "db":  "NetDefaultDB",
        "csl": ".show databases",
        "properties": {"Options": {"queryconsistency": "strongconsistency"}},
    }
    try:
        r = requests.post(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        if not r.ok:
            return friendly_name  # fallback

        data    = r.json()
        tables  = data.get("Tables", [])
        if not tables:
            return friendly_name

        rows    = tables[0].get("Rows", [])
        columns = tables[0].get("Columns", [])
        col_names = [c.get("ColumnName", "") for c in columns]

        # Hitta index för DatabaseName och PrettyName
        try:
            db_idx     = col_names.index("DatabaseName")
            pretty_idx = col_names.index("PrettyName") if "PrettyName" in col_names else -1
        except ValueError:
            return friendly_name

        needle = friendly_name.lower().strip()
        for row in rows:
            db_name     = str(row[db_idx]) if row[db_idx] else ""
            pretty_name = str(row[pretty_idx]) if pretty_idx >= 0 and row[pretty_idx] else ""
            if (db_name.lower() == needle or pretty_name.lower() == needle):
                return db_name  # returnera det exakta namnet/GUID:et som Kusto förstår

        # Inget exakt match — prova partiell matchning
        for row in rows:
            db_name     = str(row[db_idx]) if row[db_idx] else ""
            pretty_name = str(row[pretty_idx]) if pretty_idx >= 0 and row[pretty_idx] else ""
            if (needle in db_name.lower() or needle in pretty_name.lower()):
                return db_name

        return friendly_name  # inget match — returnera originalnamnet
    except Exception:
        return friendly_name


def resolve_kusto_database(cluster_url: str, friendly_name: str, token: str) -> str:
    """
    Slår upp databas-GUID via PrettyName. Kör .show databases och matchar läsbart namn.
    Returnerar GUID om hittat, annars friendly_name som fallback.
    """
    url = f"{cluster_url.rstrip('/')}/v1/rest/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json; charset=utf-8",
        "Accept":        "application/json",
    }
    body = {
        "db":  "NetDefaultDB",
        "csl": ".show databases",
        "properties": {"Options": {"queryconsistency": "strongconsistency"}},
    }
    try:
        r = requests.post(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        if not r.ok:
            return friendly_name

        tables = r.json().get("Tables", [])
        if not tables:
            return friendly_name

        rows      = tables[0].get("Rows", [])
        columns   = tables[0].get("Columns", [])
        col_names = [c.get("ColumnName", "") for c in columns]

        try:
            db_idx = col_names.index("DatabaseName")
        except ValueError:
            return friendly_name
        pt_idx = col_names.index("PrettyName") if "PrettyName" in col_names else -1

        needle = friendly_name.strip().lower()

        # Exakt match på PrettyName → returnera DatabaseName (GUID)
        for row in rows:
            db_name = str(row[db_idx]) if row[db_idx] else ""
            pretty  = str(row[pt_idx]) if pt_idx >= 0 and row[pt_idx] else ""
            if pretty.lower() == needle:
                return db_name

        # Exakt match på DatabaseName
        for row in rows:
            db_name = str(row[db_idx]) if row[db_idx] else ""
            if db_name.lower() == needle:
                return db_name

        # Partiell matchning som fallback
        for row in rows:
            db_name = str(row[db_idx]) if row[db_idx] else ""
            pretty  = str(row[pt_idx]) if pt_idx >= 0 and row[pt_idx] else ""
            if needle in pretty.lower() or needle in db_name.lower():
                return db_name

        return friendly_name
    except Exception:
        return friendly_name


def fetch_top_queries_from_logs(
    token: str,
    cluster_url: str,
    database: str,
    workspace_id: str,
    dataset_id: str,
    top_n: int = 20,
    min_count: int = 2,
) -> list[dict]:
    """
    Hämtar de N vanligast körda DAX-queries från SemanticModelLogs.
    Hämtar Kusto-token automatiskt via Azure CLI.
    """
    auth_token = resolve_kusto_token()

    # Validera att workspace_id och dataset_id är rena GUIDs
    import re as _re_guid
    def _is_guid(s):
        return bool(_re_guid.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            s.strip().lower()
        ))

    if not _is_guid(workspace_id):
        raise ValueError(f"workspace_id är inte ett giltigt GUID: '{workspace_id}'")
    if not _is_guid(dataset_id):
        raise ValueError(f"dataset_id är inte ett giltigt GUID: '{dataset_id}'")

    # Bygg KQL med separata variabler för att undvika f-strängsproblem
    ws_id_clean = workspace_id.strip().lower()
    ds_id_clean = dataset_id.strip().lower()

    # Slå upp databas-GUID och bygg gemensamma headers
    resolved_db  = resolve_kusto_database(cluster_url, database, auth_token)
    url          = f"{cluster_url.rstrip('/')}/v1/rest/query"
    headers = {
        "Authorization":           f"Bearer {auth_token}",
        "Content-Type":            "application/json; charset=utf-8",
        "Accept":                  "application/json",
        "x-ms-client-request-id": str(uuid.uuid4()),
    }

    # Undersök vilka kolumner som finns i SemanticModelLogs
    _schema_kql = "SemanticModelLogs | getschema | project ColumnName"
    _schema_body = {
        "db":  resolved_db,
        "csl": _schema_kql,
        "properties": {"Options": {"queryconsistency": "strongconsistency"}},
    }
    _schema_r = requests.post(
        url,
        data=json.dumps(_schema_body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    available_cols = []
    if _schema_r.ok:
        _tbl = _schema_r.json().get("Tables", [{}])[0]
        available_cols = [row[0] for row in _tbl.get("Rows", []) if row]

    # Välj rätt kolumnnamn baserat på vad som faktiskt finns
    def _col(candidates):
        for c in candidates:
            if c in available_cols:
                return c
        return candidates[0]

    col_workspace = _col(["WorkspaceId", "CapacityId", "TenantId", "CustomerTenantId"])
    col_time      = _col(["TimeGenerated", "Timestamp", "EventTime", "StartTime"])
    col_duration  = _col(["DurationMs", "Duration", "ExecutionTime"])
    col_operation = _col(["OperationName", "Operation", "EventType"])

    # Kontrollera tillgängliga kolumner för query-text
    has_query_text = "QueryText" in available_cols
    has_event_text = "EventText" in available_cols
    has_app_ctx    = "ApplicationContext" in available_cols
    has_item_id    = "ItemId" in available_cols

    # Bygg dataset-filter
    if has_item_id:
        dataset_filter = "| where tolower(ItemId) == \"" + ds_id_clean + "\"\n"
    else:
        dataset_filter = ""

    # Bygg query-extraktion — EventText är primärt för DAX-queries
    if has_query_text:
        query_col    = "QueryText"
        query_extend = ""
    elif has_event_text:
        query_col    = "EventText"
        query_extend = ""
    elif has_app_ctx:
        query_col    = "ExtractedQuery"
        query_extend = "| extend ExtractedQuery = tostring(parse_json(ApplicationContext)[\"QueryText\"])\n"
    else:
        raise ValueError(
            "Ingen lämplig kolumn för query-text hittades.\n"
            f"Tillgängliga kolumner: {available_cols}"
        )
    kql = (
        "SemanticModelLogs\n"
        "| where " + col_operation + " == \"QueryEnd\"\n"
        "| where tolower(WorkspaceId) == \"" + ws_id_clean + "\"\n"
        + dataset_filter +
        query_extend +
        "| where isnotempty(" + query_col + ")\n"
        "| extend CleanQuery = replace_regex(\n"
        "    " + query_col + ",\n"
        "    @'VAR _Session[^\\n]*\\n(VAR _ImpersonatedUser[^\\n]*\\n)?(VAR _CorrelationId[^\\n]*\\n)?',\n"
        "    ''\n"
        "  )\n"
        "| extend CleanQuery = replace_regex(CleanQuery, @'\\s*\\[[A-Za-z]+Time:[^\\]]*\\]\\s*$', '')\n"
        "| extend CleanQuery = trim(' \\t\\n\\r', CleanQuery)\n"
        "| where strlen(CleanQuery) > 10\n"
        "| where CleanQuery matches regex @'^\\s*(EVALUATE|DEFINE)'\n"
        "| where CleanQuery !contains \"COLUMNSTATISTICS\"\n"
        "| where CleanQuery !contains \"COARSECOLUMNTRAITS\"\n"
        "| where CleanQuery !contains \"SYSTEMRESTRICTSCHEMA\"\n"
        "| where CleanQuery !contains \"$SYSTEM\"\n"
        "| where CleanQuery !contains \"__XL_\"\n"
        "| where CleanQuery !contains \"COLUMNTRAITS\"\n"
        "| summarize\n"
        "    Count    = count(),\n"
        "    AvgMs    = round(avg(toreal(" + col_duration + ")), 0),\n"
        "    LastSeen = max(" + col_time + ")\n"
        "  by CleanQuery\n"
        "| where Count >= " + str(min_count) + "\n"
        "| top " + str(top_n) + " by Count desc\n"
        "| project CleanQuery, Count, AvgMs, LastSeen\n"
    )

    body = {
        "db":  resolved_db,
        "csl": kql,
        "properties": {
            "Options": {
                "queryconsistency": "strongconsistency",
                "request_readonly": True,
            }
        },
    }
    r = requests.post(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        timeout=30,
    )
    if not r.ok:
        col_info = f"\nHittade kolumner: {available_cols[:20]}" if available_cols else ""
        raise ValueError(f"KQL-anrop misslyckades: HTTP {r.status_code}: {r.text[:400]}{col_info}")

    data   = r.json()
    tables = data.get("Tables", data.get("tables", []))
    if not tables:
        return [], kql

    rows    = tables[0].get("Rows", tables[0].get("rows", []))
    columns = tables[0].get("Columns", tables[0].get("columns", []))
    col_names = [c.get("ColumnName", c.get("name", f"col{i}"))
                 for i, c in enumerate(columns)]

    result = []
    for row in rows:
        rec = dict(zip(col_names, row))
        result.append({
            "query":     rec.get("CleanQuery", ""),
            "count":     int(rec.get("Count", 0)),
            "avg_ms":    float(rec.get("AvgMs", 0)),
            "last_seen": str(rec.get("LastSeen", "")),
        })
    return result, kql



# ─── Mönsterigenkänning för DAX-queries ──────────────────────────────────────
import re as _re_dax

# Regex som matchar DAX-strängliteraler och numeriska värden
_STR_RE  = _re_dax.compile(r'"([^"]*)"')
_NUM_RE  = _re_dax.compile(r'\b(\d{4,}|\d+\.\d+)\b')
_DATE_RE = _re_dax.compile(r'DATE\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)')

# Regex för TREATAS({val1, val2, ...}, 'Tabell'[Kolumn])
_TREATAS_RE = _re_dax.compile(
    r"TREATAS\s*\(\s*\{([^}]+)\}\s*,\s*'?([^'\[]+)'?\s*\[([^\]]+)\]\s*\)",
    _re_dax.IGNORECASE,
)


def _sanitize_param_name(s: str) -> str:
    """Konverterar ett kolumnnamn till ett snake_case parameternamn."""
    s = s.lower().strip()
    for a, b in [("å","a"),("ä","a"),("ö","o"),(" ","_"),("-","_")]:
        s = s.replace(a, b)
    s = _re_dax.sub(r"[^a-z0-9_]", "_", s)
    s = _re_dax.sub(r"_+", "_", s).strip("_")
    return s[:25] or "param"


def extract_treatas_params(dax: str) -> tuple[str, list[dict]]:
    """
    Hittar två mönster i en DAX-query och ersätter värdelistor med {{param}}-platshållare:

    1. TREATAS({val1, val2}, 'Tabell'[Kolumn])
    2. 'Tabell'[Kolumn] IN {"val1", "val2", ...}
       eller NOT('Tabell'[Kolumn] IN {"val1", ...})

    Returnerar (template_dax, params).
    """
    # Regex för 'Tabell'[Kolumn] IN {"val1", "val2"} eller {num1, num2}
    _IN_RE = _re_dax.compile(
        r"'?([^'\[]+)'?\s*\[([^\]]+)\]\s+IN\s+\{([^}]+)\}",
        _re_dax.IGNORECASE,
    )

    params     = []
    seen_names = {}
    template   = dax

    def _add_param(values_raw, col_name, original_expr, replacement_fn):
        str_vals = _re_dax.findall(r'"([^"]*)"', values_raw)
        num_vals = _re_dax.findall(r'\b(\d+(?:\.\d+)?)\b', values_raw) if not str_vals else []
        values   = str_vals if str_vals else num_vals
        typ      = "text" if str_vals else "number"
        if not values:
            return

        base_name = _sanitize_param_name(col_name)
        if base_name in seen_names:
            seen_names[base_name] += 1
            param_name = f"{base_name}_{seen_names[base_name]}"
        else:
            seen_names[base_name] = 0
            param_name = base_name

        nonlocal template
        new_expr = replacement_fn(original_expr, values_raw, param_name, typ)
        template = template.replace(original_expr, new_expr, 1)

        params.append({
            "name":   param_name,
            "mode":   "Slumpa",
            "values": list(dict.fromkeys(values)),
            "type":   typ,
        })

    # Mönster 1: TREATAS({...}, 'Tabell'[Kolumn])
    for m in _TREATAS_RE.finditer(dax):
        values_raw   = m.group(1)
        col_name     = m.group(3).strip()
        original_set = m.group(0)

        def treatas_replace(orig, vraw, pname, typ):
            if typ == "text":
                return orig.replace("{" + vraw + "}", '{"{{' + pname + '}}"}')
            else:
                return orig.replace("{" + vraw + "}", "{{{" + pname + "}}}")

        _add_param(values_raw, col_name, original_set, treatas_replace)

    # Mönster 2: 'Tabell'[Kolumn] IN {"val1", "val2"}
    for m in _IN_RE.finditer(dax):
        values_raw   = m.group(3)
        col_name     = m.group(2).strip()
        original_set = m.group(0)

        # Hoppa över om detta redan ersatts av TREATAS
        if "{{" in original_set:
            continue

        def in_replace(orig, vraw, pname, typ):
            col_part = orig[:orig.index(" IN ") + 4]  # t.ex. "'Diverse'[Fartyg År] IN "
            if typ == "text":
                return col_part + '{"{{' + pname + '}}"}'
            else:
                return col_part + "{{{" + pname + "}}}"

        _add_param(values_raw, col_name, original_set, in_replace)

    return template, params

def _extract_column_hint(dax: str, value: str, placeholder: str) -> str:
    """
    Försöker hitta ett bra parameternamn baserat på kontexten runt värdet i queryn.
    Letar efter kolumnnamn i närheten: 'Tabell'[Kolumn] eller [Kolumn].
    Returnerar ett sanerat snake_case-namn.
    """
    # Hitta positionen av värdet i queryn
    pos = dax.find(f'"{value}"')
    if pos < 0:
        pos = dax.find(str(value))
    if pos < 0:
        return placeholder

    # Ta ett fönster på 150 tecken runt värdet och leta efter [Kolumnnamn]
    window = dax[max(0, pos-150):pos+150]
    cols   = _re_dax.findall(r"\[([^\]]+)\]", window)
    if not cols:
        return placeholder

    # Välj det kolumnnamn som är närmast, sanera till snake_case
    best = cols[0]
    name = best.lower()
    name = _re_dax.sub(r"[åä]", "a", name)
    name = _re_dax.sub(r"ö", "o", name)
    name = _re_dax.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")[:20]
    return name or placeholder


def _tokenize(dax: str) -> list[tuple[str, str]]:
    """
    Tokeniserar DAX-queryn till lista av (typ, värde):
      "lit_str"  — strängliteral
      "lit_num"  — numeriskt literal
      "lit_date" — DATE()-uttryck
      "text"     — övrig text
    """
    tokens = []
    pos    = 0
    for m in _re_dax.finditer(
        r'DATE\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)|"[^"]*"|\b(?:\d{4,}|\d+\.\d+)\b',
        dax
    ):
        if m.start() > pos:
            tokens.append(("text", dax[pos:m.start()]))
        raw = m.group()
        if raw.startswith("DATE("):
            tokens.append(("lit_date", raw))
        elif raw.startswith('"'):
            tokens.append(("lit_str", raw[1:-1]))  # utan citattecken
        else:
            tokens.append(("lit_num", raw))
        pos = m.end()
    if pos < len(dax):
        tokens.append(("text", dax[pos:]))
    return tokens


def _queries_to_template(queries: list[str]) -> tuple[str, list[dict]]:
    """
    Tar en lista av strukturellt liknande DAX-queries och returnerar:
      - template: DAX-text med {{paramN}} platshållare
      - params: lista av parameterförslag [{name, mode, values, type}]

    Algoritm:
      1. Tokenisera alla queries.
      2. Jämför token-för-token — om alla queries har samma token är det fast text,
         annars är det en variabel position → platshållare.
      3. Samla alla unika värden per platshållarposition som parameterförslag.
    """
    if not queries:
        return "", []

    if len(queries) == 1:
        return queries[0], []

    tokenized = [_tokenize(q) for q in queries]

    # Normalisera längd — använd den vanligaste längden
    lengths = [len(t) for t in tokenized]
    target_len = max(set(lengths), key=lengths.count)
    tokenized  = [t for t in tokenized if len(t) == target_len]
    if not tokenized:
        return queries[0], []

    template_parts = []
    params         = []
    param_counter  = {}  # namn → räknare för suffix

    for i in range(target_len):
        types  = [t[i][0] for t in tokenized]
        values = [t[i][1] for t in tokenized]
        unique_vals = list(dict.fromkeys(values))  # ordnad deduplicering

        first_type = types[0]
        all_same_type = all(tp == first_type for tp in types)
        all_same_val  = len(unique_vals) == 1

        if all_same_val or first_type == "text":
            # Fast position — lägg till som text
            if first_type == "lit_str":
                template_parts.append(f'"{values[0]}"')
            else:
                template_parts.append(values[0])
        else:
            # Variabel position — skapa platshållare
            # Hitta ett bra namn baserat på kontext i första queryn
            if first_type == "lit_str":
                base_name = _extract_column_hint(queries[0], values[0], f"param{len(params)+1}")
                typ       = "text"
                # Lägg tillbaka citattecken i template om strängen var citerad
                placeholder_in_dax = True
            elif first_type == "lit_date":
                base_name = "datum"
                typ       = "date"
                placeholder_in_dax = False
            else:
                base_name = _extract_column_hint(queries[0], values[0], f"varde{len(params)+1}")
                typ       = "number"
                placeholder_in_dax = False

            # Unikt paramnamn
            if base_name in param_counter:
                param_counter[base_name] += 1
                param_name = f"{base_name}_{param_counter[base_name]}"
            else:
                param_counter[base_name] = 0
                param_name = base_name

            placeholder = "{{" + param_name + "}}"
            if placeholder_in_dax and first_type == "lit_str":
                template_parts.append(f'"{placeholder}"')
            else:
                template_parts.append(placeholder)

            # Normalisera datumvärden
            if typ == "date":
                norm_vals = []
                for v in unique_vals:
                    m = _re_dax.search(r'DATE\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', v)
                    if m:
                        norm_vals.append(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
                    else:
                        norm_vals.append(v)
                unique_vals = norm_vals

            params.append({
                "name":   param_name,
                "mode":   "Slumpa",
                "values": unique_vals,
                "type":   typ,
            })

    return "".join(template_parts), params


def cluster_queries_by_pattern(
    query_records: list[dict],
    similarity_threshold: float = 0.75,
) -> list[dict]:
    """
    Grupperar queries med liknande struktur och extraherar parametrar.
    Returnerar lista av {template, params, queries, total_count, avg_ms}.

    Likhetsmått: andel token-positioner som är identiska (strukturell likhet).
    """
    if not query_records:
        return []

    def structural_similarity(a: str, b: str) -> float:
        ta, tb = _tokenize(a), _tokenize(b)
        if len(ta) != len(tb):
            # Olika längd — ge poäng baserat på längdöverlapp
            shorter = min(len(ta), len(tb))
            longer  = max(len(ta), len(tb))
            return shorter / longer * 0.5  # max 50% likhet om längderna skiljer
        matches = sum(1 for x, y in zip(ta, tb) if x[0] == y[0] and x[1] == y[1])
        return matches / len(ta) if ta else 0.0

    # Klustrera med greedy nearest-neighbour
    used     = [False] * len(query_records)
    clusters = []

    for i, rec in enumerate(query_records):
        if used[i]:
            continue
        cluster = [i]
        used[i] = True
        for j in range(i + 1, len(query_records)):
            if used[j]:
                continue
            if structural_similarity(rec["query"], query_records[j]["query"]) >= similarity_threshold:
                cluster.append(j)
                used[j] = True
        clusters.append(cluster)

    result = []
    for cluster_idxs in clusters:
        recs     = [query_records[i] for i in cluster_idxs]
        qs       = [r["query"] for r in recs]
        template, params = _queries_to_template(qs)

        # Om token-baserad klustring inte hittade parametrar, prova TREATAS-extraktion
        if not params:
            treatas_template, treatas_params = extract_treatas_params(qs[0])
            if treatas_params:
                template = treatas_template
                params   = treatas_params

        result.append({
            "template":    template,
            "params":      params,
            "query_count": len(recs),
            "total_count": sum(r["count"] for r in recs),
            "avg_ms":      sum(r["avg_ms"] * r["count"] for r in recs) / max(sum(r["count"] for r in recs), 1),
            "sample":      qs[0],
        })

    result.sort(key=lambda x: x["total_count"], reverse=True)
    return result
with st.sidebar:
    st.markdown("### ⚡ PBI Load Tester")
    st.divider()

    st.markdown("### 🔐 Autentisering")
    auth_mode = st.selectbox(
        "Metod",
        ["Azure CLI (rekommenderat)", "Manuell token", "Service Principal"],
    )

    manual_token = tenant_id = client_id = client_secret = ""

    if auth_mode == "Azure CLI (rekommenderat)":
        st.caption(
            "Hämtar automatiskt alla nödvändiga tokens via `az account get-access-token`. "
            "Kräver att Azure CLI är installerat och att du kört `az login`."
        )
        if st.button("🔑 Hämta alla tokens", width="stretch", key="fetch_all_tokens"):
            import subprocess, shutil as _sh
            _az = _sh.which("az") or _sh.which("az.cmd") or "az"
            _token_configs = [
                ("_last_token",       "https://analysis.windows.net/powerbi/api", "Power BI"),
                ("le_fabric_token",   "https://api.fabric.microsoft.com",         "Fabric REST API"),
                ("le_storage_token",  "https://storage.azure.com/",               "OneLake Storage"),
                ("kusto_token_cache", "https://kusto.kusto.windows.net",           "Eventhouse/KQL"),
            ]
            _results = []
            for _key, _resource, _label in _token_configs:
                try:
                    _res = subprocess.run(
                        [_az, "account", "get-access-token",
                         "--resource", _resource,
                         "--query", "accessToken", "-o", "tsv"],
                        capture_output=True, text=True, timeout=15
                    )
                    if _res.returncode == 0 and _res.stdout.strip():
                        _tok = _res.stdout.strip()
                        if _key == "kusto_token_cache":
                            st.session_state[_key] = {"tok": _tok, "exp": time.time() + 55*60}
                        else:
                            st.session_state[_key] = _tok
                        _results.append(f"✅ {_label}")
                    else:
                        _results.append(f"⚠️ {_label}: {_res.stderr.strip()[:60]}")
                except Exception as _e:
                    _results.append(f"❌ {_label}: {str(_e)[:60]}")
            for _r in _results:
                st.caption(_r)
            # Sätt manual_token från det hämtade Power BI-tokenet
            if st.session_state.get("_last_token"):
                st.rerun()

        # Visa status för alla tokens
        _tok_status = [
            ("Power BI",        "_last_token"),
            ("Fabric REST API", "le_fabric_token"),
            ("OneLake Storage", "le_storage_token"),
            ("Eventhouse/KQL",  "kusto_token_cache"),
        ]
        _any_token = False
        for _lbl, _key in _tok_status:
            _has = bool(st.session_state.get(_key))
            if _has:
                _any_token = True
            st.caption(f"{'✅' if _has else '⬜'} {_lbl}")

        # Använd Power BI-token som manual_token för resten av appen
        manual_token = st.session_state.get("_last_token", "")

    elif auth_mode == "Manuell token":
        manual_token = st.text_area(
            "Bearer Token",
            height=90,
            placeholder="eyJ0eXAiOiJKV1Qi...",
            help="az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv",
        )
        st.caption("Fabric-token täcker Power BI REST API. Giltigt ~60 min.")
    else:
        tenant_id     = st.text_input("Tenant ID",     placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        client_id     = st.text_input("Client ID",     placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        client_secret = st.text_input("Client Secret", type="password")

    st.divider()
    st.markdown("### 🎯 Semantisk modell")

    # Bygg ett tillfälligt token för att kunna hämta listor
    _preview_token = None
    if auth_mode == "Azure CLI (rekommenderat)":
        _preview_token = st.session_state.get("_last_token", "") or manual_token.strip() or None
    elif auth_mode == "Manuell token" and manual_token.strip():
        _preview_token = manual_token.strip()
    elif auth_mode == "Service Principal" and all([tenant_id, client_id, client_secret]):
        try:
            _preview_token = resolve_token(auth_mode, tenant_id, client_id, client_secret, "")
        except Exception:
            _preview_token = None

    workspace_id   = ""
    workspace_name = ""
    dataset_id     = ""

    if _preview_token:
        # ── Workspace-dropdown ──────────────────────────────────────────
        with st.spinner("Hämtar workspaces..."):
            try:
                workspaces = fetch_workspaces(_preview_token)
            except Exception as exc:
                st.error(f"Kunde inte hämta workspaces: {exc}")
                workspaces = []

        if workspaces:
            ws_labels = [f"{w['name']}" for w in workspaces]
            ws_ids    = [w["id"]        for w in workspaces]
            ws_idx = st.selectbox(
                "Workspace",
                options=range(len(ws_labels)),
                format_func=lambda i: ws_labels[i],
                key="ws_select",
            )
            workspace_id   = ws_ids[ws_idx]
            workspace_name = ws_labels[ws_idx]
            st.caption(f"ID: `{workspace_id}`")

            # Auto-ladda sparad Eventhouse-konfiguration för detta workspace
            _ws_cfg = load_workspace_config(workspace_id)
            if _ws_cfg:
                # Skriv till session_state bara om workspace byttes
                if st.session_state.get("_last_ws_id") != workspace_id:
                    st.session_state["eh_cluster"]  = _ws_cfg.get("cluster", "")
                    st.session_state["eh_database"] = _ws_cfg.get("database", "")
                    st.session_state["_last_ws_id"] = workspace_id
            else:
                if st.session_state.get("_last_ws_id") != workspace_id:
                    st.session_state["_last_ws_id"] = workspace_id

            # ── Dataset-dropdown ────────────────────────────────────────
            with st.spinner("Hämtar semantiska modeller..."):
                try:
                    datasets = fetch_datasets(_preview_token, workspace_id)
                except Exception as exc:
                    st.error(f"Kunde inte hämta datasets: {exc}")
                    datasets = []

            if datasets:
                ds_labels = [d["name"]                          for d in datasets]
                ds_ids    = [d["id"]                            for d in datasets]
                ds_modes  = [d.get("defaultMode", "")           for d in datasets]
                ds_idx = st.selectbox(
                    "Semantisk modell",
                    options=range(len(ds_labels)),
                    format_func=lambda i: f"{ds_labels[i]}  ({ds_modes[i]})" if ds_modes[i] else ds_labels[i],
                    key="ds_select",
                )
                dataset_id = ds_ids[ds_idx]
                st.caption(f"ID: `{dataset_id}`")
            elif workspaces:
                st.warning("Inga dataset hittades i valt workspace.")
        else:
            st.warning("Inga workspaces hittades.")
    else:
        # Fallback — manuell inmatning om token inte finns än
        st.caption("Fyll i token ovan för att välja via dropdowns.")
        workspace_id   = st.text_input("Workspace ID (manuell)", placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        workspace_name = ""
        dataset_id     = st.text_input("Dataset ID (manuell)",   placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

    st.divider()
    st.markdown("### 👤 Row Level Security (RLS)")

    # När en profil laddas skriver vi prefill-värdena direkt in i
    # widgetarnas session_state-nycklar INNAN de renderas — det är
    # det enda sättet att få Streamlit att faktiskt visa nya värden.
    _rls_pre = st.session_state.pop("rls_prefill", None)
    if _rls_pre is not None:
        st.session_state["rls_toggle"]      = _rls_pre.get("enabled", False)
        st.session_state["rls_mode_radio"]  = _rls_pre.get("mode", "En användare")
        pre_users = _rls_pre.get("users", [])
        if _rls_pre.get("mode") == "En användare":
            pre_u = pre_users[0] if pre_users else {}
            st.session_state["rls_single_user"]  = pre_u.get("username", "")
            st.session_state["rls_single_roles"] = ", ".join(pre_u.get("roles", []))
        else:
            st.session_state["rls_multi_text"] = "\n".join(
                f"{u['username']} | {', '.join(u['roles'])}" if u.get("roles")
                else u["username"]
                for u in pre_users
            )

    rls_enabled = st.toggle(
        "Aktivera RLS-impersonering",
        key="rls_toggle",
    )

    rls_users = []
    rls_mode  = "Ingen"

    if rls_enabled:
        rls_mode = st.radio(
            "Läge",
            ["En användare", "Flera användare (rotation)"],
            horizontal=True,
            key="rls_mode_radio",
            help="Rotation: användarna körs i turordning precis som queries",
        )

        if rls_mode == "En användare":
            single_user = st.text_input(
                "UPN / e-post",
                placeholder="anna.svensson@foretaget.se",
                key="rls_single_user",
                help="Användarens UPN i Azure AD",
            )
            single_roles = st.text_input(
                "RLS-roller (valfritt)",
                placeholder="SalesRegion_North, Manager",
                key="rls_single_roles",
                help="Kommaseparerade rollnamn — lämna tomt för dynamisk RLS",
            )
            if single_user.strip():
                roles = [r.strip() for r in single_roles.split(",") if r.strip()]
                rls_users = [{"username": single_user.strip(), "roles": roles}]
        else:
            multi_users_raw = st.text_area(
                "Användare — en per rad, valfritt följt av | roller",
                placeholder=(
                    "anna.svensson@foretaget.se\n"
                    "erik.lindqvist@foretaget.se | SalesRegion_North\n"
                    "maria.berg@foretaget.se | Manager, Viewer"
                ),
                height=130,
                key="rls_multi_text",
                help="Format: upn@domain.se  eller  upn@domain.se | Roll1, Roll2",
            )
            for line in multi_users_raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    upn, roles_str = line.split("|", 1)
                    roles = [r.strip() for r in roles_str.split(",") if r.strip()]
                else:
                    upn, roles = line, []
                if upn.strip():
                    rls_users.append({"username": upn.strip(), "roles": roles})

            if rls_users:
                st.caption(f"👥 {len(rls_users)} användare laddade")

        if rls_users:
            st.caption("⚠️ Kräver Service Principal med _Execute Queries As_ eller admin-behörighet.")

    st.divider()
    st.markdown("### ⚙️ Lastkonfiguration")
    session_tag = st.text_input(
        "Session-etikett",
        value=datetime.now().strftime("test-%Y%m%d"),
        help="Prefixas i DAX VAR _Session → sökbar i SemanticModelLogs.QueryText",
    )

    # ── Rate limiting ─────────────────────────────────────────────────────────
    with st.expander("🚦 Rate limiting", expanded=False):
        rate_limit_enabled = st.toggle(
            "Begränsa anrop per minut",
            value=False,
            help="Aktivera för att undvika HTTP 429 från Power BI REST API",
        )
        if rate_limit_enabled:
            max_per_minute = st.slider(
                "Max anrop / minut",
                min_value=10, max_value=200, value=60, step=10,
                help="Power BI REST API tillåter ~120/min för Premium, lägre för delad kapacitet",
            )
            st.caption(f"≈ ett anrop var {60/max_per_minute:.1f} s i genomsnitt")
        else:
            max_per_minute = 0
        st.caption("REST API: Vid HTTP 429 görs upp till 3 automatiska retry-försök med exponential backoff oavsett denna inställning.")

    run_mode = st.radio("Körläge", ["Iterationer", "Tidsbaserat"], horizontal=True)

    if run_mode == "Iterationer":
        iterations  = st.number_input("Antal iterationer", min_value=1, max_value=5000, value=20)
        concurrency = st.number_input("Parallella anrop",  min_value=1, max_value=50,   value=3)
        delay_ms    = st.number_input("Fast fördröjning (ms)", min_value=0, max_value=10000, value=0,
                                      help="Väntetid innan varje anrop")
        traffic_profile = "Konstant"
        duration_sec    = 0
    else:
        # Tidsbaserat
        dur_col1, dur_col2 = st.columns([2, 1])
        with dur_col1:
            duration_val = st.number_input("Körtid", min_value=1, max_value=1440, value=5)
        with dur_col2:
            dur_unit = st.selectbox("Enhet", ["sekunder", "minuter", "timmar"], index=1)
        duration_sec = int(duration_val * {"sekunder": 1, "minuter": 60, "timmar": 3600}[dur_unit])

        concurrency = st.number_input("Max parallella anrop", min_value=1, max_value=50, value=5)

        traffic_profile = st.selectbox(
            "Trafikprofil",
            ["Vågor (burst + paus)", "Kontorstider (trappa upp/ner)", "Slumpmässig", "Konstant"],
            help=(
                "Vågor: simulerar användare som öppnar rapporter i klunkar\n"
                "Kontorstider: låg last → hög → låg, som en arbetsdag i miniatyr\n"
                "Slumpmässig: oregelbunden last utan mönster\n"
                "Konstant: jämn last under hela perioden"
            ),
        )
        st.caption(_profile_description(traffic_profile))
        iterations = 0
        delay_ms   = 0

    st.divider()
    st.markdown("### 📊 Hämta queries från logg")

    with st.expander("⚙️ Eventhouse-konfiguration", expanded=not st.session_state.get("eh_cluster")):
        if workspace_id:
            st.caption(f"Konfiguration sparas per workspace: `{workspace_name}`")
        eh_cluster = st.text_input(
            "Eventhouse cluster-URL",
            placeholder="https://xxxxxxxx.kusto.fabric.microsoft.com",
            help="Hitta i Fabric: Eventhouse → System overview → Query URI",
            key="eh_cluster",
        )
        # Applicera väntande databasval INNAN text_input renderas
        _pending_db = st.session_state.pop("eh_database_pending", None)
        if _pending_db is not None:
            st.session_state["eh_database"] = _pending_db

        eh_database = st.text_input(
            "KQL-databas (läsbart namn)",
            placeholder="Monitoring KQL database",
            key="eh_database",
            help="Skriv det läsbara namnet — appen slår upp GUID:et automatiskt",
        )

        # Knapp för att lista tillgängliga databaser
        if st.button("🔍 Visa tillgängliga databaser", width="stretch",
                     disabled=not (st.session_state.get("eh_cluster","").strip() and
                                   _preview_token)):
            with st.spinner("Hämtar databaser..."):
                try:
                    _url = f"{st.session_state['eh_cluster'].rstrip('/')}/v1/rest/query"
                    _hdrs = {
                        "Authorization": f"Bearer {resolve_kusto_token()}",
                        "Content-Type":  "application/json; charset=utf-8",
                        "Accept":        "application/json",
                    }
                    _bod = {
                        "db": "NetDefaultDB", "csl": ".show databases",
                        "properties": {"Options": {"queryconsistency": "strongconsistency"}},
                    }
                    _resp = requests.post(_url, data=json.dumps(_bod).encode("utf-8"),
                                          headers=_hdrs, timeout=15)
                    if _resp.ok:
                        _tbl  = _resp.json().get("Tables", [{}])[0]
                        _rows = _tbl.get("Rows", [])
                        _cols = [c.get("ColumnName","") for c in _tbl.get("Columns",[])]
                        _db_i = _cols.index("DatabaseName") if "DatabaseName" in _cols else 0
                        _pt_i = _cols.index("PrettyName")   if "PrettyName"   in _cols else -1

                        # Bygg lista med läsbara namn — visa PrettyName om det finns, annars DatabaseName
                        _db_names = []
                        for _row in _rows:
                            _db_guid   = str(_row[_db_i]) if _row[_db_i] else ""
                            _pretty    = str(_row[_pt_i]) if _pt_i >= 0 and _row[_pt_i] else ""
                            _display   = _pretty if _pretty and _pretty != _db_guid else _db_guid
                            _db_names.append(_display)

                        st.session_state["eh_db_list"] = _db_names
                        if not _db_names:
                            st.warning("Inga databaser hittades.")
                    else:
                        st.error(f"Fel {_resp.status_code}: {_resp.text[:200]}")
                except Exception as _ex:
                    st.error(str(_ex))

        # Visa databaserna som klickbara val
        _db_list = st.session_state.get("eh_db_list", [])
        if _db_list:
            st.caption("Välj databas:")
            for _db_name in _db_list:
                if st.button(f"  {_db_name}", key=f"eh_db_pick_{_db_name}",
                             use_container_width=True):
                    st.session_state["eh_database_pending"] = _db_name
                    st.rerun()

        st.caption(
            "Kusto-token hämtas automatiskt via Azure CLI (`az`). "
            "Kontrollera att du är inloggad med `az login`."
        )
        if st.button("🔑 Testa Kusto-token", width="stretch"):
            try:
                tok = get_kusto_token_via_cli()
                import base64 as _b64, json as _j
                payload = tok.split(".")[1]
                payload += "=" * ((4 - len(payload) % 4) % 4)
                claims  = _j.loads(_b64.b64decode(payload))
                st.success(f"✅ Token OK — aud={claims.get('aud','?')}, upn={claims.get('upn', claims.get('unique_name','?'))}")
            except Exception as ex:
                st.error(f"❌ {ex}")

        save_col, status_col = st.columns([2, 3])
        with save_col:
            if st.button("💾 Spara konfiguration", width="stretch",
                         disabled=not (workspace_id and eh_cluster and eh_database)):
                save_workspace_config(workspace_id, {
                    "cluster":  eh_cluster.strip(),
                    "database": eh_database.strip(),
                })
                with status_col:
                    st.success("Sparad!")

        if workspace_id and st.session_state.get("eh_cluster"):
            st.caption("✅ Eventhouse konfigurerat för detta workspace")

    eh_top_n = st.number_input(
        "Antal queries att hämta",
        min_value=1, max_value=200, value=7,
        key="eh_top_n",
    )
    eh_min_count = st.number_input(
        "Minsta antal körningar",
        min_value=1, max_value=1000, value=2,
        help="Filtrera bort queries som körts färre gånger",
        key="eh_min_count",
    )

    # Visa kontext för vad som ska hämtas
    if workspace_id and dataset_id:
        _ds_name = ds_labels[ds_idx] if "ds_labels" in dir() and ds_idx is not None else dataset_id[:8]
        st.caption(f"Hämtar från: **{workspace_name}** → **{_ds_name}**")
    elif workspace_id:
        st.caption("Välj en semantisk modell ovan för att aktivera hämtning")

    fetch_btn = st.button(
        "🔍 Hämta vanligaste queries",
        width="stretch",
        disabled=not (
            _preview_token and
            workspace_id and
            dataset_id and
            st.session_state.get("eh_cluster", "").strip() and
            st.session_state.get("eh_database", "").strip()
        ),
        help="Hämtar och grupperar de mest körda DAX-queries för vald modell",
    )

    if fetch_btn:
        with st.spinner("Hämtar och analyserar queries från SemanticModelLogs..."):
            try:
                fetched, generated_kql = fetch_top_queries_from_logs(
                    token        = _preview_token,
                    cluster_url  = st.session_state["eh_cluster"].strip(),
                    database     = st.session_state["eh_database"].strip(),
                    workspace_id = workspace_id,
                    dataset_id   = dataset_id,
                    top_n        = int(st.session_state["eh_top_n"]),
                    min_count    = int(st.session_state["eh_min_count"]),
                )
                st.session_state["eh_last_kql"] = generated_kql
                if fetched:
                    clustered = cluster_queries_by_pattern(fetched)
                    st.session_state["eh_clustered"] = clustered
                    st.success(
                        f"✅ Hittade {len(fetched)} queries → "
                        f"{len(clustered)} unika mönster"
                    )
                else:
                    st.session_state["eh_clustered"] = []
                    st.warning("Inga queries hittades — prova lägre 'Minsta antal körningar'")
            except Exception as exc:
                st.error(f"❌ {exc}")

    if st.session_state.get("eh_last_kql"):
        with st.expander("🔎 Visa genererad KQL-query", expanded=False):
            st.code(st.session_state["eh_last_kql"], language="sql")
            st.caption("Kopiera och kör direkt i Fabric Eventhouse för att felsöka.")
    clustered = st.session_state.get("eh_clustered", [])
    if clustered:
        st.markdown(f"**Välj mönster att använda** ({len(clustered)} hittade):")

        selected_clusters = []
        for idx, cl in enumerate(clustered):
            has_params = bool(cl["params"])
            param_info = (
                f" · {len(cl['params'])} param{'etrar' if len(cl['params'])>1 else 'eter'}"
                if has_params else ""
            )
            label = (
                f"Mönster #{idx+1} · {cl['total_count']}× körningar · "
                f"{cl['avg_ms']:.0f}ms snitt · "
                f"{cl['query_count']} varianter{param_info}"
            )

            with st.expander(label, expanded=False):
                # Förhandsvisning av template
                preview = cl["template"][:300].replace("\n", "\n")
                st.code(preview + ("..." if len(cl["template"]) > 300 else ""), language="sql")

                # Visa föreslagna parametrar
                if cl["params"]:
                    st.markdown("**Föreslagna parametrar:**")
                    for p in cl["params"]:
                        vals_str = ", ".join(p["values"][:5])
                        if len(p["values"]) > 5:
                            vals_str += f" ... (+{len(p['values'])-5})"
                        st.caption(
                            f"`{{{{{p['name']}}}}}` · typ={p['type']} · "
                            f"{len(p['values'])} värden: {vals_str}"
                        )
                else:
                    st.caption("Inga variabla delar hittades — mönstret är fast.")

                if st.checkbox("Välj detta mönster", key=f"eh_cl_{idx}"):
                    selected_clusters.append(idx)

        if selected_clusters:
            col_load, col_clear = st.columns(2)
            with col_load:
                if st.button("⬇ Lägg in i DAX-editorn", width="stretch", key="eh_load_btn"):
                    templates = [clustered[i]["template"] for i in selected_clusters]
                    combined  = "\n---\n".join(templates)

                    # Slå ihop parametrar från alla valda mönster
                    merged_params = {}
                    for i in selected_clusters:
                        for p in clustered[i]["params"]:
                            if p["name"] not in merged_params:
                                merged_params[p["name"]] = p.copy()
                            else:
                                existing = set(merged_params[p["name"]]["values"])
                                merged_params[p["name"]]["values"] = list(
                                    existing | set(p["values"])
                                )

                    # Använd pending-mönstret så att allt appliceras INNAN widgets renderas
                    st.session_state["profile_load"] = {
                        "dax":      combined,
                        "mode":     "Enkel query" if len(templates) == 1 else "Flera queries (rotation)",
                        "params":   list(merged_params.values()),
                        "rls":      None,  # None = bevara befintlig RLS
                    }
                    st.rerun()

            with col_clear:
                if st.button("✕ Rensa lista", width="stretch", key="eh_clear_btn"):
                    st.session_state["eh_clustered"] = []
                    st.rerun()

# ─── Huvudvy ──────────────────────────────────────────────────────────────────
st.markdown("# ⚡ Power BI Semantic Model Load Tester")
st.caption("Stresstesta DAX-queries mot din semantiska modell och mät svarstider i realtid.")

_prefill = st.session_state.pop("profile_load", None)
if _prefill is not None:
    st.session_state["dax_single_input"] = _prefill.get("dax", 'EVALUATE ROW("Ping", 1)')
    st.session_state["dax_multi_input"]  = _prefill.get("dax", 'EVALUATE ROW("Ping", 1)')
    st.session_state["query_mode_radio"] = _prefill.get("mode", "Enkel query")
    st.session_state["dax_params"]       = _prefill.get("params", [])
    # Sätt rls_prefill bara om profilen har RLS (None = bevara befintlig)
    if _prefill.get("rls") is not None:
        st.session_state["rls_prefill"] = _prefill["rls"]

query_mode = st.radio(
    "Query-läge",
    ["Enkel query", "Flera queries (rotation)"],
    horizontal=True,
    key="query_mode_radio",
)

if query_mode == "Enkel query":
    dax_single = st.text_area(
        "DAX Query",
        key="dax_single_input",
        height=130,
    )
    queries  = [dax_single.strip()]
    dax_text = dax_single
else:
    dax_multi = st.text_area(
        "DAX Queries — separera med ---",
        key="dax_multi_input",
        height=160,
    )
    queries  = [q.strip() for q in dax_multi.split("---") if q.strip()]
    dax_text = dax_multi
    st.caption(f"📋 {len(queries)} queries laddade")

# ─── DAX-parametrar ───────────────────────────────────────────────────────────
if "dax_params" not in st.session_state:
    st.session_state["dax_params"] = []

# Hitta automatiskt alla {{param}} i aktuell DAX
import re as _re
detected = sorted(set(_re.findall(r"\{\{(\w+)\}\}", dax_text)))

with st.expander(
    "🔀 DAX-parametrar" +
    (f" · {len(detected)} platshållare" if detected else " · inga {{platshållare}} hittade"),
    expanded=bool(detected),
):
    if not detected:
        st.caption(
            "Lägg in platshållare i DAX-queryn med dubbla klamrar, t.ex. `{{fartyg}}` eller `{{år}}`.\n"
            "Appen ersätter dem dynamiskt vid varje anrop."
        )
    else:
        st.caption(
            f"Hittade {len(detected)} platshållare. Definiera värden nedan — "
            "varje anrop ersätter platshållarna enligt valt läge."
        )

        # Synka session_state-listan mot detekterade platshållare
        existing = {p["name"]: p for p in st.session_state["dax_params"]}
        synced   = []
        for pname in detected:
            synced.append(existing.get(pname, {
                "name":   pname,
                "mode":   "Slumpa",
                "values": [],
                "type":   "text",
            }))
        # Ta bort parametrar som inte längre finns i queryn
        st.session_state["dax_params"] = synced

        updated_params = []
        for idx, param in enumerate(st.session_state["dax_params"]):
            st.markdown(f"**`{{{{{param['name']}}}}}`**")
            pc1, pc2, pc3 = st.columns([2, 2, 5])
            with pc1:
                typ = st.selectbox(
                    "Typ",
                    ["text", "number", "date"],
                    index=["text", "number", "date"].index(param.get("type", "text")),
                    key=f"param_type_{idx}",
                    label_visibility="collapsed",
                    help="text → \"värde\", number → värde, date → DATE(yyyy,mm,dd)",
                )
            with pc2:
                mode = st.selectbox(
                    "Läge",
                    ["Slumpa", "Rotation", "Fast"],
                    index=["Slumpa", "Rotation", "Fast"].index(param.get("mode", "Slumpa")),
                    key=f"param_mode_{idx}",
                    label_visibility="collapsed",
                    help="Slumpa: slumpmässigt värde · Rotation: i turordning · Fast: alltid första",
                )
            with pc3:
                vals_str = st.text_input(
                    "Värden (kommaseparerade)",
                    value=", ".join(param.get("values", [])),
                    key=f"param_vals_{idx}",
                    label_visibility="collapsed",
                    placeholder=(
                        "Dalarö, Cinderella, Djurgården 3" if typ == "text" else
                        "2024, 2025, 2026" if typ == "number" else
                        "2024-01-01, 2025-01-01, 2026-01-01"
                    ),
                )

            values = [v.strip() for v in vals_str.split(",") if v.strip()]
            updated_params.append({
                "name":   param["name"],
                "mode":   mode,
                "values": values,
                "type":   typ,
            })

            if values:
                if mode == "Slumpa":
                    st.caption(f"Slumpar bland: {values}")
                elif mode == "Rotation":
                    st.caption(f"Rotation: {' → '.join(values)}")
                else:
                    st.caption(f"Fast värde: {values[0]}")
            else:
                st.warning(f"⚠️ Inga värden definierade för `{{{{{param['name']}}}}}`")

            if idx < len(st.session_state["dax_params"]) - 1:
                st.markdown("---")

        st.session_state["dax_params"] = updated_params

# ─── Spara / ladda profil ─────────────────────────────────────────────────────
dax_lib  = load_dax_library()
lib_key  = dax_library_key(workspace_id, dataset_id) if workspace_id and dataset_id else None
profiles = dax_lib.get(lib_key, {}) if lib_key else {}

with st.expander(
    "💾 Profiler (DAX + RLS)" +
    (f" · {len(profiles)} sparade" if profiles else " · inga sparade ännu"),
    expanded=bool(_prefill),
):
    if not lib_key:
        st.caption("Välj workspace och semantisk modell för att kunna spara/ladda profiler.")
    else:
        # ── Ladda ────────────────────────────────────────────────────────
        if profiles:
            st.markdown("**Ladda sparad profil**")
            lc1, lc2, lc3 = st.columns([4, 2, 2])
            with lc1:
                sel = st.selectbox(
                    "Profil",
                    options=list(profiles.keys()),
                    key="profile_select",
                    label_visibility="collapsed",
                )
            with lc2:
                if st.button("⬇ Ladda", key="load_profile", width="stretch"):
                    st.session_state["profile_load"] = profiles[sel]
                    # Fyll även in RLS i session så sidopanelen kan läsa det
                    st.session_state["rls_prefill"] = profiles[sel].get("rls", {})
                    st.rerun()
            with lc3:
                if st.button("🗑 Ta bort", key="del_profile", width="stretch"):
                    del dax_lib[lib_key][sel]
                    if not dax_lib[lib_key]:
                        del dax_lib[lib_key]
                    save_dax_library(dax_lib)
                    st.success(f"Tog bort '{sel}'")
                    st.rerun()

            # Visa vad profilen innehåller
            with st.container():
                p = profiles[sel]
                st.caption(
                    f"DAX: {len([q for q in p.get('dax','').split('---') if q.strip()])} query(s)"
                    + (f" · RLS: {len(p.get('rls',{}).get('users',[]))} användare"
                       if p.get('rls',{}).get('users') else " · Ingen RLS")
                )
            st.divider()

        # ── Spara ────────────────────────────────────────────────────────
        st.markdown("**Spara nuvarande profil**")
        save_name = st.text_input(
            "Namn",
            placeholder="t.ex. Fartygsdata RLS-test",
            key="profile_save_name",
            label_visibility="collapsed",
        )

        # Bygg RLS-snapshot från nuvarande sidebar-värden
        rls_snapshot = {
            "enabled":  rls_enabled,
            "mode":     rls_mode if rls_enabled else "Ingen",
            "users":    [
                {"username": u["username"], "roles": u["roles"]}
                for u in rls_users
            ],
        }

        col_save, col_preview = st.columns([2, 3])
        with col_preview:
            st.caption(
                f"Sparar: {len(queries)} DAX-query(s)"
                + (f" + {len(rls_users)} RLS-användare" if rls_users else " + ingen RLS")
            )
        with col_save:
            if st.button(
                "💾 Spara profil",
                key="save_profile",
                disabled=not save_name.strip(),
                width="stretch",
                type="primary",
            ):
                if lib_key not in dax_lib:
                    dax_lib[lib_key] = {}
                dax_lib[lib_key][save_name.strip()] = {
                    "dax":    dax_text,
                    "rls":    rls_snapshot,
                    "mode":   query_mode,
                    "params": st.session_state.get("dax_params", []),
                }
                save_dax_library(dax_lib)
                st.success(f"✅ Sparade profilen '{save_name.strip()}'")
                st.rerun()

st.divider()

c1, c2, c3, _ = st.columns([2, 2, 2, 6])
start_btn = c1.button("▶ Starta",  type="primary", width="stretch")
stop_btn  = c2.button("⏹ Stoppa",                  width="stretch")
clear_btn = c3.button("🗑 Rensa",                   width="stretch")

if stop_btn:
    st.session_state.stop_flag = True
    st.info("⏹ Stoppar efter pågående anrop...")

if clear_btn:
    st.session_state.results   = []
    st.session_state.log_lines = []
    st.rerun()

# ─── Live-körning ─────────────────────────────────────────────────────────────
if start_btn:
    # Validering
    val_errors = []
    if auth_mode == "Azure CLI (rekommenderat)" and not st.session_state.get("_last_token",""):
        val_errors.append("Token saknas — klicka 🔑 Hämta alla tokens i autentiseringssektionen.")
    elif auth_mode == "Manuell token" and not manual_token.strip():
        val_errors.append("Bearer Token saknas.")
    if auth_mode == "Service Principal" and not all([tenant_id, client_id, client_secret]):
        val_errors.append("Tenant ID, Client ID och Client Secret krävs.")
    if not workspace_id.strip():
        val_errors.append("Workspace ID saknas.")
    if not dataset_id.strip():
        val_errors.append("Dataset ID saknas.")
    if not queries:
        val_errors.append("Minst en DAX-query krävs.")

    if val_errors:
        for e in val_errors:
            st.error(f"⚠️ {e}")
        st.stop()

    # Hämta token
    with st.spinner("Hämtar token..."):
        try:
            token = resolve_token(auth_mode, tenant_id, client_id, client_secret, manual_token)
        except Exception as exc:
            st.error(f"❌ Token-fel: {exc}")
            st.stop()

    st.success("✅ Token OK")

    # Spara för Performance Advisor
    st.session_state["_last_token"]        = token
    st.session_state["_last_workspace_id"] = workspace_id
    st.session_state["_last_dataset_id"]   = dataset_id

    # Initiera rate limiter för denna session
    st.session_state["_rate_limiter"] = RateLimiter(max_per_minute if rate_limit_enabled else 0)
    if rate_limit_enabled:
        st.info(f"🚦 Rate limiting aktiverat: max {max_per_minute} anrop/min")

    st.session_state.results   = []
    st.session_state.log_lines = []
    st.session_state.stop_flag = False

    results_local = []
    log_local     = []
    query_count   = len(queries)
    total         = int(iterations)
    i_counter     = [0]  # lista för att kunna muteras inuti nästlad funktion

    st.markdown("---")

    if run_mode == "Tidsbaserat":
        dur_str = f"{duration_val} {dur_unit}"
        prog_bar = st.progress(0, text=f"0 / {dur_str}")
    else:
        prog_bar = st.progress(0, text="Startar...")

    status_text  = st.empty()
    log_holder   = st.empty()
    chart_holder = st.empty()

    def add_log(msg):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_local.append(f"[{ts}]  {msg}")
        log_holder.markdown(
            '<div class="log-box">' + "\n".join(log_local[-40:]) + "</div>",
            unsafe_allow_html=True,
        )

    def refresh_chart():
        if len(results_local) < 2:
            return
        df_live = pd.DataFrame(results_local)
        df_ok   = df_live[df_live["error"].isna()]
        if df_ok.empty:
            return
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_ok["i"], y=df_ok["duration_ms"],
            mode="lines+markers", name="Svarstid",
            line=dict(color="#00d4aa", width=1.5), marker=dict(size=4),
        ))
        df_err = df_live[df_live["error"].notna()]
        if not df_err.empty:
            fig.add_trace(go.Scatter(
                x=df_err["i"], y=df_err["duration_ms"],
                mode="markers", name="Fel",
                marker=dict(color="#ef4444", size=9, symbol="x"),
            ))
        fig.update_layout(
            template="plotly_dark", paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
            xaxis=dict(title="Anrop #" if run_mode == "Iterationer" else "Tid (s)",
                       gridcolor="#21262d"),
            yaxis=dict(title="ms", gridcolor="#21262d"),
            margin=dict(l=0, r=20, t=10, b=0), height=240, showlegend=False,
        )
        chart_holder.plotly_chart(fig, use_container_width=True)

    def handle_result(fut, i, identity, done, total_known):
        """Packa upp ett future-resultat och uppdatera UI."""
        ms, rows, err, corr_id = fut.result()
        ts         = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        user_label = identity["username"] if identity else "—"
        i_counter[0] += 1
        ic = i_counter[0]

        results_local.append({
            "i":             ic,
            "ts":            ts,
            "duration_ms":   ms,
            "rows":          rows,
            "error":         err,
            "query_idx":     i % query_count,
            "user":          user_label,
            "correlation_id": corr_id,
        })

        if total_known:
            prog_bar.progress(done / total_known, text=f"{done}/{total_known} klara")
        status_text.markdown(
            f"**#{ic}** &nbsp;{'✅' if not err else '❌'}&nbsp; "
            f"`{ms:.0f} ms` · {rows} rader"
            + (f" · 👤 `{user_label}`" if identity else "")
            + (f" · ⚠️ `{err}`" if err else "")
        )
        user_part = f"  [{user_label}]" if identity else ""
        if err:
            add_log(f"✗ #{ic:03d}  {ms:.0f} ms{user_part}  —  {err}")
        else:
            add_log(f"✓ #{ic:03d}  {ms:.0f} ms{user_part}  —  {rows} rader")

        if ic % 5 == 0:
            refresh_chart()

    # ── Iterationsläge ────────────────────────────────────────────────────────
    if run_mode == "Iterationer":
        add_log(f"▶ Iterationer: {total} st · concurrency={concurrency} · {query_count} query(s)" +
                (f" · {len(rls_users)} RLS-användare" if rls_users else ""))

        with ThreadPoolExecutor(max_workers=int(concurrency)) as pool:
            futures = {}
            for i in range(total):
                if st.session_state.stop_flag:
                    break
                dax      = queries[i % query_count]
                dax      = apply_dax_params(dax, st.session_state.get("dax_params", []), i)
                identity = rls_users[i % len(rls_users)] if rls_users else None
                if int(delay_ms) > 0:
                    time.sleep(int(delay_ms) / 1000)
                fut = pool.submit(run_dax, token, workspace_id, dataset_id, dax, identity, session_tag)
                futures[fut] = (i, identity)

            add_log(f"→ {len(futures)} anrop skickade till trådpool")
            done = 0
            for fut in as_completed(futures):
                if st.session_state.stop_flag:
                    add_log("⏹ Stoppad av användare")
                    break
                done += 1
                i, identity = futures[fut]
                handle_result(fut, i, identity, done, len(futures))

    # ── Tidsbaserat läge ──────────────────────────────────────────────────────
    else:
        add_log(f"▶ Tidsbaserat: {dur_str} · profil={traffic_profile} · max concurrency={concurrency}" +
                (f" · {len(rls_users)} RLS-användare" if rls_users else ""))

        t_start  = time.perf_counter()
        seq      = 0          # sekventiell räknare för rotation
        pending  = {}         # future → (seq, identity)
        last_chart = 0

        with ThreadPoolExecutor(max_workers=int(concurrency)) as pool:
            while True:
                elapsed = time.perf_counter() - t_start

                # Uppdatera tidsprogress
                pct = min(elapsed / duration_sec, 1.0)
                elapsed_fmt = f"{int(elapsed)}s / {dur_str}"
                prog_bar.progress(pct, text=elapsed_fmt)

                # Skörda klara futures utan att blockera
                done_futs = [f for f in list(pending) if f.done()]
                for fut in done_futs:
                    if not st.session_state.stop_flag:
                        s_idx, ident = pending.pop(fut)
                        handle_result(fut, s_idx, ident, 0, None)
                    else:
                        pending.pop(fut, None)

                if st.session_state.stop_flag:
                    add_log("⏹ Stoppad av användare")
                    break

                if elapsed >= duration_sec:
                    # Vänta på att pågående anrop avslutas
                    add_log(f"⏱ Körtid slut — väntar på {len(pending)} pågående anrop...")
                    for fut in as_completed(list(pending)):
                        s_idx, ident = pending.pop(fut)
                        handle_result(fut, s_idx, ident, 0, None)
                    prog_bar.progress(1.0, text=f"✅ {dur_str} klara")
                    break

                # Skicka nytt anrop om vi inte nått max concurrency
                if len(pending) < int(concurrency):
                    dax      = queries[seq % query_count]
                    dax      = apply_dax_params(dax, st.session_state.get("dax_params", []), seq)
                    identity = rls_users[seq % len(rls_users)] if rls_users else None
                    fut = pool.submit(run_dax, token, workspace_id, dataset_id, dax, identity, session_tag)
                    pending[fut] = (seq, identity)
                    seq += 1

                # Trafikprofilens fördröjning
                wait = next_delay_seconds(traffic_profile, elapsed, duration_sec, int(concurrency))
                time.sleep(wait)

    st.session_state.results   = results_local
    st.session_state.log_lines = log_local
    st.session_state.running   = False
    st.rerun()

    s = compute_stats(results_local)
    rl = st.session_state.get("_rate_limiter", _rate_limiter)
    throttle_info = f" · throttled {rl.throttle_count}×" if rl.throttle_count else ""
    add_log(
        f"✅ Klar!  {s.get('success',0)} OK · {s.get('errors',0)} fel · "
        f"p50={s.get('p50',0):.0f}ms · p95={s.get('p95',0):.0f}ms · p99={s.get('p99',0):.0f}ms"
        + throttle_info
    )
    prog_bar.progress(1.0, text="✅ Klar!")

# ─── Permanenta resultat ──────────────────────────────────────────────────────
if st.session_state.results and not start_btn:
    st.divider()
    render_results(st.session_state.results)
    if st.session_state.log_lines:
        st.divider()
        st.markdown("#### 🖥 Logg")
        st.markdown(
            '<div class="log-box">' + "\n".join(st.session_state.log_lines) + "</div>",
            unsafe_allow_html=True,
        )

# ─── Lakehouse Explorer ───────────────────────────────────────────────────────
st.markdown("""
<div style="border-top: 3px solid #00d4aa; margin: 2.5rem 0 1.5rem 0;"></div>
""", unsafe_allow_html=True)
st.markdown("# 🗄️ Lakehouse Explorer")
st.caption(
    "Läser Delta-tabeller direkt från OneLake via `deltalake`-biblioteket. "
    "Kräver: `pip install deltalake pyarrow`"
)

with st.expander("⚙️ Anslutning", expanded=not st.session_state.get("le_lakehouse_id","")):

    # Kontrollera deltalake
    try:
        import deltalake as _dl
        st.success(f"✅ deltalake {_dl.__version__}")
    except ImportError:
        st.error("❌ pip install deltalake pyarrow")

    st.caption("Välj workspace och lakehouse via dropdowns. Kräver Fabric-token (hämtas automatiskt).")

    # ── Hämta Fabric-token för dropdown-API-anrop ─────────────────────────────
    _le_ft = st.session_state.get("le_fabric_token","")
    if _le_ft:
        st.caption("✅ Fabric-token finns (hämtat via Autentisering)")
    else:
        st.warning("⚠️ Hämta tokens via **🔐 Autentisering** → 🔑 Hämta alla tokens")

    # ── Workspace-dropdown ────────────────────────────────────────────────────
    if _le_ft:
        @st.cache_data(ttl=120)
        def _fetch_le_workspaces(token):
            r = requests.get(
                "https://api.powerbi.com/v1.0/myorg/groups",
                headers={"Authorization": f"Bearer {token}"}, timeout=15
            )
            if r.ok:
                items = sorted(r.json().get("value",[]), key=lambda x: x.get("name","").lower())
                return [(w["id"], w["name"]) for w in items]
            return []

        _le_ws_options = _fetch_le_workspaces(st.session_state.get("_last_token","") or _le_ft)
        if _le_ws_options:
            _le_ws_labels = [f"{name}" for _, name in _le_ws_options]
            _le_ws_ids    = [wid for wid, _ in _le_ws_options]

            # Sätt default till samma workspace som semantisk modell om möjligt
            _le_ws_default = 0
            _cur_ws = st.session_state.get("le_workspace_id","")
            if _cur_ws and _cur_ws in _le_ws_ids:
                _le_ws_default = _le_ws_ids.index(_cur_ws)

            _le_ws_sel = st.selectbox(
                "Workspace", _le_ws_labels,
                index=_le_ws_default, key="le_ws_dropdown"
            )
            _le_sel_ws_id = _le_ws_ids[_le_ws_labels.index(_le_ws_sel)]
            st.session_state["le_workspace_id"] = _le_sel_ws_id
            st.caption(f"`{_le_sel_ws_id}`")

            # ── Lakehouse-dropdown ────────────────────────────────────────────
            @st.cache_data(ttl=120)
            def _fetch_le_lakehouses(workspace_id, token):
                # Prova Fabric API för lakehouses
                r = requests.get(
                    f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/lakehouses",
                    headers={"Authorization": f"Bearer {token}"}, timeout=15
                )
                if r.ok:
                    items = r.json().get("value",[])
                    return sorted([(i["id"], i.get("displayName", i.get("name",""))) for i in items],
                                   key=lambda x: x[1].lower())
                # Fallback: /items?type=Lakehouse
                r2 = requests.get(
                    f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items?type=Lakehouse",
                    headers={"Authorization": f"Bearer {token}"}, timeout=15
                )
                if r2.ok:
                    items = r2.json().get("value",[])
                    return sorted([(i["id"], i.get("displayName", i.get("name",""))) for i in items],
                                   key=lambda x: x[1].lower())
                return []

            _le_lh_options = _fetch_le_lakehouses(_le_sel_ws_id, _le_ft)
            if _le_lh_options:
                _le_lh_labels = [name for _, name in _le_lh_options]
                _le_lh_ids    = [lid for lid, _ in _le_lh_options]

                _le_lh_default = 0
                _cur_lh = st.session_state.get("le_lakehouse_id","")
                if _cur_lh and _cur_lh in _le_lh_ids:
                    _le_lh_default = _le_lh_ids.index(_cur_lh)

                _le_lh_sel = st.selectbox(
                    "Lakehouse", _le_lh_labels,
                    index=_le_lh_default, key="le_lh_dropdown"
                )
                _le_sel_lh_id = _le_lh_ids[_le_lh_labels.index(_le_lh_sel)]
                st.session_state["le_lakehouse_id"] = _le_sel_lh_id
                st.caption(f"`{_le_sel_lh_id}`")
            else:
                st.warning("Inga lakehouses hittades i detta workspace.")
        else:
            st.warning("Inga workspaces hittades — kontrollera token.")


_le_token        = st.session_state.get("le_storage_token", "")
_le_fabric_token = st.session_state.get("le_fabric_token", "")
_le_ws           = st.session_state.get("le_workspace_id", "").strip()
_le_lh           = st.session_state.get("le_lakehouse_id", "").strip()
_le_ready        = bool(_le_fabric_token and _le_ws and _le_lh)

if not _le_ready:
    missing = []
    if not _le_fabric_token: missing.append("Fabric-token (hämta via 🔐 Autentisering)")
    if not _le_ws:           missing.append("Workspace (välj i ⚙️ Anslutning)")
    if not _le_lh:           missing.append("Lakehouse (välj i ⚙️ Anslutning)")
    st.info(f"Saknas: {', '.join(missing)}")
else:
    # ── Steg 1: Hämta scheman ─────────────────────────────────────────────────
    if st.button("🔍 Identifiera scheman och tabeller", width="stretch", key="le_discover_btn"):
        with st.spinner("Undersöker lakehouse-struktur..."):
            try:
                _pbi_token = _le_fabric_token
                _base_url  = f"https://api.fabric.microsoft.com/v1/workspaces/{_le_ws}/lakehouses/{_le_lh}"
                _stor_hdrs = {"Authorization": f"Bearer {_le_token}", "x-ms-version": "2020-10-02"}
                _fab_hdrs  = {"Authorization": f"Bearer {_pbi_token}"}

                # Hämta oneLakeTablesPath
                _r_info = requests.get(_base_url, headers=_fab_hdrs, timeout=15)
                _tables_path = ""
                if _r_info.ok:
                    _tables_path = _r_info.json().get("properties",{}).get("oneLakeTablesPath","")

                # Lista toppnivån via ADLS
                _schemas_found = {}  # {schema_name: [table_name, ...]}
                if _tables_path and _le_token:
                    _list_url = _tables_path.rstrip("/") + "?resource=filesystem&recursive=false"
                    _r_top = requests.get(_list_url, headers=_stor_hdrs, timeout=15)
                    if _r_top.ok:
                        _top_dirs = [
                            p["name"].split("/")[-1]
                            for p in _r_top.json().get("paths",[])
                            if str(p.get("isDirectory","false")).lower() == "true"
                            and not p["name"].endswith("_delta_log")
                        ]
                        # Kolla om toppnivån är scheman eller tabeller
                        for _d in _top_dirs:
                            _delta_url = _tables_path.rstrip("/") + f"/{_d}/_delta_log?resource=filesystem&recursive=false"
                            _cr = requests.get(_delta_url, headers=_stor_hdrs, timeout=5)
                            if _cr.ok:
                                # Det är en tabell — lägg under schema "(root)"
                                _schemas_found.setdefault("(root)", []).append(_d)
                            else:
                                # Det är ett schema — lista dess tabeller
                                _sub_url = _tables_path.rstrip("/") + f"/{_d}?resource=filesystem&recursive=false"
                                _r_sub = requests.get(_sub_url, headers=_stor_hdrs, timeout=15)
                                if _r_sub.ok:
                                    for _sp in _r_sub.json().get("paths",[]):
                                        _tname = _sp["name"].split("/")[-1]
                                        if (str(_sp.get("isDirectory","false")).lower() == "true"
                                                and not _tname.endswith("_delta_log")):
                                            _schemas_found.setdefault(_d, []).append(_tname)

                if _schemas_found:
                    st.session_state["le_schemas_found"] = _schemas_found
                    _total = sum(len(v) for v in _schemas_found.values())
                    st.success(f"✅ {len(_schemas_found)} scheman, {_total} tabeller hittade")
                else:
                    st.warning("Inga tabeller hittades. Kontrollera att storage-token finns (🔑 Hämta alla tokens).")
            except Exception as e:
                st.error(f"❌ {e}")

    # ── Steg 2: Schema-multiselect ────────────────────────────────────────────
    _schemas_data = st.session_state.get("le_schemas_found", {})
    if _schemas_data:
        _all_schema_names = sorted(_schemas_data.keys())
        _sel_schemas = st.multiselect(
            "Välj scheman",
            _all_schema_names,
            default=_all_schema_names,
            key="le_sel_schemas",
        )

        # ── Steg 3: Tabell-multiselect baserat på valda scheman ───────────────
        _available_tables = []
        for _s in _sel_schemas:
            for _t in _schemas_data.get(_s, []):
                _available_tables.append(f"{_s}/{_t}" if _s != "(root)" else _t)
        _available_tables = sorted(_available_tables)

        if _available_tables:
            _sel_tables = st.multiselect(
                "Välj tabeller att analysera",
                _available_tables,
                default=[],
                key="le_sel_tables",
                help="Välj en eller flera tabeller. Varje tabell analyseras separat."
            )

            _sample_only = st.checkbox(
                "Snabbläge (samplar exakt N rader med dt.head())",
                value=True, key="le_sample",
            )
            _sample_rows = st.number_input(
                "Antal rader i snabbläge",
                min_value=1_000, max_value=500_000,
                value=50_000, step=10_000,
                key="le_sample_rows",
                disabled=not st.session_state.get("le_sample", True),
            ) if st.session_state.get("le_sample", True) else 0

            # ── Steg 4: Analysera ─────────────────────────────────────────────
            if _sel_tables:
                _btn_col, _abort_col = st.columns([3, 1])
                with _btn_col:
                    _do_analyze = st.button(
                        f"📊 Analysera {len(_sel_tables)} tabell(er)",
                        width="stretch", key="le_analyze_btn"
                    )
                with _abort_col:
                    if st.button("⏹ Avbryt", width="stretch", key="le_abort_btn"):
                        st.session_state["le_abort"] = True
                        st.rerun()

                if _do_analyze:
                    if not _le_token:
                        st.error("Storage-token saknas — klicka 🔑 Hämta alla tokens.")
                    else:
                        st.session_state["le_abort"]    = False
                        st.session_state["le_analyses"] = {}
                        _all_analyses = {}

                        _tbl_progress  = st.progress(0.0)
                        _status_text   = st.empty()
                        _col_progress  = st.empty()

                        from deltalake import DeltaTable
                        import pyarrow.compute as pc
                        import pyarrow as pa

                        for _idx, _tbl_full in enumerate(_sel_tables):
                            if st.session_state.get("le_abort"):
                                _status_text.warning("⏹ Avbrutet av användaren.")
                                break

                            _tbl_progress.progress(
                                _idx / len(_sel_tables),
                                text=f"Tabell {_idx+1}/{len(_sel_tables)}: {_tbl_full}"
                            )
                            _status_text.info(f"⏳ Öppnar {_tbl_full}...")

                            try:
                                _tbl_path = f"Tables/{_tbl_full}"
                                _uri = (
                                    f"abfss://{_le_ws}@onelake.dfs.fabric.microsoft.com/"
                                    f"{_le_lh}/{_tbl_path}"
                                )
                                _opts = {
                                    "bearer_token":        _le_token,
                                    "use_fabric_endpoint": "true",
                                    "account_name":        "onelake",
                                }
                                _dt      = DeltaTable(_uri, storage_options=_opts)
                                _schema  = _dt.schema()
                                _fields  = [f for f in _schema.fields if not f.name.startswith("__")]
                                _n_cols  = len(_fields)

                                # Läs data — limit=n ger korrekt sampling
                                _status_text.info(f"⏳ Läser data från {_tbl_full}...")
                                _ds = _dt.to_pyarrow_dataset()
                                if _sample_only and _sample_rows:
                                    # Hämta radantal först (från Delta-logg, gratis)
                                    try:
                                        _total_rows = sum(
                                            f.num_rows for f in _ds.get_fragments()
                                            if hasattr(f, 'num_rows') and f.num_rows
                                        ) or None
                                    except Exception:
                                        _total_rows = None
                                    # Läs första N rader via scanner
                                    import pyarrow as pa
                                    _scanner = _ds.scanner(batch_size=_sample_rows)
                                    _batches = []
                                    _rows_read = 0
                                    for _batch in _scanner.to_batches():
                                        _batches.append(_batch)
                                        _rows_read += len(_batch)
                                        if _rows_read >= _sample_rows:
                                            break
                                    _tbl_pa   = pa.Table.from_batches(_batches).slice(0, _sample_rows)
                                    _read_rows = len(_tbl_pa)
                                    _is_sample = True
                                else:
                                    try:
                                        _total_rows = sum(
                                            f.num_rows for f in _ds.get_fragments()
                                            if hasattr(f, 'num_rows') and f.num_rows
                                        ) or None
                                    except Exception:
                                        _total_rows = None
                                    _tbl_pa    = _ds.to_table()
                                    _read_rows = len(_tbl_pa)
                                    _is_sample = False

                                # Analysera kolumner med progressbar
                                _stats = []
                                for _ci, _f in enumerate(_fields):
                                    if st.session_state.get("le_abort"):
                                        break
                                    _col_progress.progress(
                                        _ci / max(_n_cols, 1),
                                        text=f"Kolumn {_ci+1}/{_n_cols}: {_f.name}"
                                    )
                                    try:
                                        _arr      = _tbl_pa.column(_f.name)
                                        _nulls    = _arr.null_count
                                        _null_pct = round(_nulls / max(_read_rows, 1) * 100, 1)
                                        _distinct = len(pc.unique(_arr))
                                        _stats.append({
                                            "Kolumn":           _f.name,
                                            "Datatyp":          str(_f.type),
                                            "Distinkta värden": _distinct,
                                            "Null-värden":      _nulls,
                                            "Null %":           _null_pct,
                                            "Rekommendation":   (
                                                "⚠️ Extrem kardinalitet" if _distinct > 1_000_000 else
                                                "⚠️ Hög kardinalitet"    if _distinct > 100_000  else
                                                "⚠️ Hög null-frekvens"   if _null_pct > 50       else
                                                "✅ OK"
                                            ),
                                        })
                                    except Exception as _ce:
                                        _stats.append({
                                            "Kolumn": _f.name, "Datatyp": str(_f.type),
                                            "Distinkta värden": "?", "Null-värden": "?",
                                            "Null %": "?", "Rekommendation": f"⚠️ {_ce}",
                                        })

                                _all_analyses[_tbl_full] = {
                                    "total_rows": _total_rows,
                                    "read_rows":  _read_rows,
                                    "stats":      _stats,
                                    "sample":     _is_sample,
                                    "sample_rows": _sample_rows if _is_sample else None,
                                }
                            except Exception as _e:
                                _all_analyses[_tbl_full] = {"error": str(_e)}

                        _tbl_progress.progress(1.0, text="✅ Klar!")
                        _status_text.empty()
                        _col_progress.empty()
                        st.session_state["le_analyses"] = _all_analyses

                # ── Visa resultat ──────────────────────────────────────────────
                _analyses = st.session_state.get("le_analyses", {})
                for _tbl_full, _ana in _analyses.items():
                    st.markdown(f"### 📊 {_tbl_full}")
                    if "error" in _ana:
                        st.error(f"❌ {_ana['error']}")
                        continue

                    # Radantal-info
                    _total = _ana.get("total_rows")
                    _read  = _ana.get("read_rows", 0)
                    if _ana.get("sample"):
                        if _total:
                            _row_label = f"~{_read:,} av {_total:,}"
                            _row_help  = f"Samplade {_read:,} av {_total:,} rader ({round(_read/_total*100,1)}%)"
                        else:
                            _row_label = f"~{_read:,} (sample)"
                            _row_help  = f"Samplade {_read:,} rader (totalt okänt)"
                    else:
                        _row_label = f"{_read:,}"
                        _row_help  = "Fullständig analys"
                    st.metric("Analyserade rader", _row_label, help=_row_help)

                    _df_s = pd.DataFrame(_ana["stats"])
                    st.dataframe(
                        _df_s.sort_values("Distinkta värden", ascending=False,
                                          key=lambda x: pd.to_numeric(x, errors="coerce").fillna(0)),
                        use_container_width=True, height=min(400, 50 + len(_df_s) * 35)
                    )

                    _warns = _df_s[_df_s["Rekommendation"] != "✅ OK"]
                    if not _warns.empty:
                        for _, _wr in _warns.iterrows():
                            st.warning(
                                f"**{_wr['Kolumn']}** (`{_wr['Datatyp']}`): "
                                f"{_wr['Rekommendation']} — "
                                f"{_wr['Distinkta värden']} distinkta, {_wr['Null %']}% null"
                            )

                    st.download_button(
                        f"⬇ CSV — {_tbl_full}",
                        _df_s.to_csv(index=False).encode("utf-8"),
                        f"stats_{_tbl_full.replace('/','_')}.csv",
                        "text/csv", key=f"le_dl_{_tbl_full.replace('/','_')}"
                    )
                    st.divider()

# ─── Footer ───────────────────────────────────────────────────────────────────
st.divider()
st.caption("Power BI Semantic Model Load Tester · REST API: executeQueries · Python + Streamlit")