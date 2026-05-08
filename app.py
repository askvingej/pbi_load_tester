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
        # Normalisera unicode och ta bort osynliga tecken
        import unicodedata
        val = unicodedata.normalize("NFC", val).strip()
        # Ta bort alla kontrolltecken utom vanligt whitespace
        val = "".join(c for c in val if unicodedata.category(c) != "Cc")
        if typ == "number":
            return val
        if typ == "date":
            try:
                from datetime import datetime as dt
                d = dt.strptime(val, "%Y-%m-%d")
                return f"DATE({d.year}, {d.month}, {d.day})"
            except Exception:
                return val
        # text — escapa inbyggda citattecken med dubbla citattecken (DAX-standard)
        escaped = val.replace('"', '""')
        return f'"{escaped}"'

    for param in params:
        name   = param.get("name", "").strip()
        mode   = param.get("mode", "Slumpa")
        # Normalisera alla varianter av citattecken till vanliga ASCII-citattecken
        raw_values = param.get("values", [])
        values = []
        for v in raw_values:
            v = v.strip()
            # Ersätt typografiska citattecken med ASCII-citattecken
            v = v.replace("\u201c", '"').replace("\u201d", '"')
            v = v.replace("\u2018", "'").replace("\u2019", "'")
            if v:
                values.append(v)
        typ    = param.get("type", "text")

    for param in params:
        name   = param.get("name", "").strip()
        mode   = param.get("mode", "Slumpa")
        raw_values = param.get("values", [])
        values = []
        for v in raw_values:
            v = v.strip()
            v = v.replace("\u201c", '"').replace("\u201d", '"')
            v = v.replace("\u2018", "'").replace("\u2019", "'")
            if v:
                values.append(v)
        typ = param.get("type", "text")

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

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚡ PBI Load Tester")
    st.divider()

    st.markdown("### 🔐 Autentisering")
    auth_mode = st.selectbox(
        "Metod",
        ["Manuell token", "Service Principal"],
    )

    manual_token = tenant_id = client_id = client_secret = ""

    if auth_mode == "Manuell token":
        manual_token = st.text_area(
            "Bearer Token",
            height=90,
            placeholder="eyJ0eXAiOiJKV1Qi...",
            help="az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv",
        )
        st.caption("Giltigt ~60 min. Hämtas via Azure CLI.")
    else:
        tenant_id     = st.text_input("Tenant ID",     placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        client_id     = st.text_input("Client ID",     placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        client_secret = st.text_input("Client Secret", type="password")

    st.divider()
    st.markdown("### 🎯 Semantisk modell")

    # Bygg ett tillfälligt token för att kunna hämta listor
    _preview_token = None
    if auth_mode == "Manuell token" and manual_token.strip():
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

# ─── Huvudvy ──────────────────────────────────────────────────────────────────
st.markdown("# ⚡ Power BI Semantic Model Load Tester")
st.caption("Stresstesta DAX-queries mot din semantiska modell och mät svarstider i realtid.")
st.divider()

_prefill = st.session_state.pop("profile_load", None)
if _prefill is not None:
    st.session_state["dax_single_input"] = _prefill.get("dax", 'EVALUATE ROW("Ping", 1)')
    st.session_state["dax_multi_input"]  = _prefill.get("dax", 'EVALUATE ROW("Ping", 1)')
    st.session_state["query_mode_radio"] = _prefill.get("mode", "Enkel query")
    st.session_state["dax_params"]       = _prefill.get("params", [])

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
    if auth_mode == "Manuell token" and not manual_token.strip():
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

# ─── Footer ───────────────────────────────────────────────────────────────────
st.divider()
st.caption("Power BI Semantic Model Load Tester · REST API: executeQueries · Python + Streamlit")