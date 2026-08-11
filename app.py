"""
Aura Herbals — raport fraz: wolumen wyszukiwań (aktualny i sprzed roku) + Google Trends.

Źródła danych:
- DataForSEO: keywords_data/google_ads/search_volume/live -> wolumen aktualny + historia miesięczna
              (z historii wyciągamy wartość sprzed ~12 miesięcy jako "rok temu")
- DataForSEO: keywords_data/google_trends/explore/live -> szereg czasowy trendu (0-100) dla frazy
- Senuto: integracja "best effort" — Senuto nie ma w pełni publicznej, ujednoliconej
          dokumentacji parametrów dla konta czytelnika. Uzupełnij funkcję `senuto_get_volumes`
          zgodnie z endpointem dostępnym na Twoim planie (docs-api.senuto.com), jeśli chcesz
          traktować Senuto jako źródło danych PL zamiast/obok DataForSEO.

Uruchomienie lokalnie:
    pip install -r requirements.txt
    streamlit run app.py
"""

import base64
import datetime as dt
import io
import time

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Aura Herbals — raport fraz", layout="wide")

DFS_BASE = "https://api.dataforseo.com/v3"
SENUTO_BASE = "https://api.senuto.com/api"

# ---------------------------------------------------------------------------
# Pomocnicze
# ---------------------------------------------------------------------------

def parse_phrases(raw_text: str) -> list[str]:
    phrases = [p.strip() for p in raw_text.splitlines() if p.strip()]
    # usuń duplikaty, zachowaj kolejność
    seen = set()
    result = []
    for p in phrases:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# DataForSEO
# ---------------------------------------------------------------------------

def dfs_auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def dfs_search_volume(login: str, password: str, keywords: list[str], location_code: int, language_code: str) -> pd.DataFrame:
    """Aktualny wolumen + odczyt sprzed 12 miesięcy z monthly_searches."""
    headers = dfs_auth_header(login, password)
    rows = []
    for batch in chunked(keywords, 700):  # limit 1000/req, zapas na bezpieczeństwo
        payload = [{
            "keywords": batch,
            "location_code": location_code,
            "language_code": language_code,
            "search_partners": False,
        }]
        resp = requests.post(f"{DFS_BASE}/keywords_data/google_ads/search_volume/live",
                              headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        tasks = data.get("tasks", [])
        for task in tasks:
            for item in (task.get("result") or []):
                if item is None:
                    continue
                kw = item.get("keyword")
                current_vol = item.get("search_volume")
                monthly = item.get("monthly_searches") or []
                # monthly_searches: lista {"year":..,"month":..,"search_volume":..}, ostatnie 12 mies.
                vol_year_ago = None
                if monthly:
                    monthly_sorted = sorted(monthly, key=lambda m: (m["year"], m["month"]))
                    if len(monthly_sorted) >= 12:
                        vol_year_ago = monthly_sorted[-12]["search_volume"]
                    else:
                        vol_year_ago = monthly_sorted[0]["search_volume"]
                rows.append({
                    "fraza": kw,
                    "wolumen_aktualny": current_vol,
                    "wolumen_rok_temu": vol_year_ago,
                    "cpc": item.get("cpc"),
                    "konkurencja": item.get("competition"),
                })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["zmiana_%"] = df.apply(
            lambda r: round(((r["wolumen_aktualny"] - r["wolumen_rok_temu"]) / r["wolumen_rok_temu"] * 100), 1)
            if r["wolumen_rok_temu"] not in (None, 0) and r["wolumen_aktualny"] is not None else None,
            axis=1,
        )
    return df


def dfs_trends_explore(login: str, password: str, keywords: list[str], location_code: int) -> dict[str, pd.DataFrame]:
    """Zwraca słownik fraza -> DataFrame(date, interest) z Google Trends (max 5 fraz/zapytanie)."""
    headers = dfs_auth_header(login, password)
    result: dict[str, pd.DataFrame] = {}
    for batch in chunked(keywords, 5):
        payload = [{
            "keywords": batch,
            "location_code": location_code,
            "time_range": "past_12_months",
            "type": "web",
        }]
        resp = requests.post(f"{DFS_BASE}/keywords_data/google_trends/explore/live",
                              headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        tasks = data.get("tasks", [])
        for task in tasks:
            for item in (task.get("result") or []):
                if item is None:
                    continue
                for sub in (item.get("data") or []):
                    if sub.get("type") != "google_trends":
                        continue
                for series in (item.get("items") or []):
                    pass
                # struktura DataForSEO trends bywa zagnieżdżona: item -> data -> [{"date_from":..,"values":[..]}]
                kws = item.get("keywords") or batch
                series_data = item.get("data") or []
                for idx, kw in enumerate(kws):
                    dates, values = [], []
                    for point in series_data:
                        try:
                            v = point.get("values", [None] * len(kws))[idx]
                        except (IndexError, TypeError):
                            v = None
                        dates.append(point.get("date_from"))
                        values.append(v)
                    if dates:
                        result[kw] = pd.DataFrame({"data": dates, "trend": values})
        time.sleep(0.2)  # uprzejmie dla rate limitu
    return result


# ---------------------------------------------------------------------------
# Senuto (best-effort — dostosuj do swojego planu API)
# ---------------------------------------------------------------------------

def senuto_get_token(email: str, password: str) -> str:
    resp = requests.post(f"{SENUTO_BASE}/users/token", json={"email": email, "password": password}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # nazwa pola tokena może się różnić w zależności od wersji API — sprawdź docs-api.senuto.com
    return data.get("token") or data.get("access_token") or data.get("bearer_token")


def senuto_get_volumes(bearer_token: str, keywords: list[str]) -> pd.DataFrame:
    """SZKIELET integracji. Endpoint/parametry do potwierdzenia w Twojej dokumentacji Senuto API
    (Postman collection na docs-api.senuto.com, sekcja Keyword Database)."""
    headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
    rows = []
    for kw in keywords:
        try:
            resp = requests.get(f"{SENUTO_BASE}/keyword_database/keywords",
                                 headers=headers, params={"keyword": kw}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            item = (data.get("data") or data.get("items") or [None])[0]
            rows.append({
                "fraza": kw,
                "senuto_wolumen": item.get("volume") if item else None,
            })
        except Exception:
            rows.append({"fraza": kw, "senuto_wolumen": None})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Podsumowanie dla klienta
# ---------------------------------------------------------------------------

def generate_summary(df: pd.DataFrame, threshold_pct: float = 5.0) -> dict | None:
    """Zestawia wynik na poziomie całej listy fraz: czy trend wyszukiwań w tym roku
    jest wyższy, niższy czy stabilny względem roku poprzedniego."""
    d = df.dropna(subset=["wolumen_aktualny", "wolumen_rok_temu", "zmiana_%"]).copy()
    if d.empty:
        return None

    total_now = d["wolumen_aktualny"].sum()
    total_before = d["wolumen_rok_temu"].sum()
    overall_change = round((total_now - total_before) / total_before * 100, 1) if total_before else None

    up = d[d["zmiana_%"] > threshold_pct]
    down = d[d["zmiana_%"] < -threshold_pct]
    stable = d[(d["zmiana_%"] >= -threshold_pct) & (d["zmiana_%"] <= threshold_pct)]

    if overall_change is None:
        kierunek = "brak danych"
    elif overall_change > threshold_pct:
        kierunek = "wyższy"
    elif overall_change < -threshold_pct:
        kierunek = "niższy"
    else:
        kierunek = "na podobnym poziomie"

    return {
        "liczba_fraz": len(d),
        "total_now": int(total_now),
        "total_before": int(total_before),
        "overall_change": overall_change,
        "kierunek": kierunek,
        "liczba_rosnacych": len(up),
        "liczba_spadajacych": len(down),
        "liczba_stabilnych": len(stable),
        "top_rosnace": d.sort_values("zmiana_%", ascending=False).head(5)[["fraza", "zmiana_%"]],
        "top_spadajace": d.sort_values("zmiana_%", ascending=True).head(5)[["fraza", "zmiana_%"]],
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Aura Herbals — raport fraz (suplementy w kroplach)")
st.caption("Wolumen aktualny, wolumen sprzed roku i trend Google dla wklejonej listy fraz.")

with st.sidebar:
    st.header("Klucze API")

    st.subheader("DataForSEO")
    dfs_login = st.text_input("DataForSEO login (e-mail)", type="default")
    dfs_password = st.text_input("DataForSEO password", type="password")

    st.subheader("Senuto (opcjonalnie)")
    use_senuto = st.checkbox("Dołącz dane z Senuto", value=False)
    senuto_email = st.text_input("Senuto e-mail", disabled=not use_senuto)
    senuto_password = st.text_input("Senuto hasło", type="password", disabled=not use_senuto)

    st.subheader("Ustawienia")
    location_code = st.number_input("Kod lokalizacji DataForSEO (Polska = 2616)", value=2616, step=1)
    language_code = st.text_input("Kod języka DataForSEO", value="pl")

st.subheader("1. Wklej listę fraz (jedna fraza w linii, max 50)")
raw_phrases = st.text_area("Frazy", height=220, placeholder="krople na sen\nkrople na odporność\n...")

phrases = parse_phrases(raw_phrases)
if phrases:
    st.caption(f"Wykryto {len(phrases)} unikalnych fraz.")
    if len(phrases) > 50:
        st.warning("Wykryto więcej niż 50 fraz — zostaną przetworzone wszystkie, ale sprawdź listę.")

run = st.button("2. Pobierz dane", type="primary", disabled=not phrases)

if "report_df" not in st.session_state:
    st.session_state.report_df = None
if "trends_data" not in st.session_state:
    st.session_state.trends_data = {}

if run:
    if not dfs_login or not dfs_password:
        st.error("Podaj login i hasło do DataForSEO w panelu bocznym.")
    else:
        with st.spinner("Pobieram wolumen wyszukiwań (DataForSEO)..."):
            try:
                vol_df = dfs_search_volume(dfs_login, dfs_password, phrases, int(location_code), language_code)
            except requests.HTTPError as e:
                st.error(f"Błąd DataForSEO (search_volume): {e}")
                vol_df = pd.DataFrame()

        with st.spinner("Pobieram Google Trends (DataForSEO)..."):
            try:
                trends = dfs_trends_explore(dfs_login, dfs_password, phrases, int(location_code))
            except requests.HTTPError as e:
                st.error(f"Błąd DataForSEO (google_trends/explore): {e}")
                trends = {}

        senuto_df = pd.DataFrame()
        if use_senuto:
            if not senuto_email or not senuto_password:
                st.warning("Zaznaczono Senuto, ale brak danych logowania — pomijam ten krok.")
            else:
                with st.spinner("Pobieram dane z Senuto..."):
                    try:
                        token = senuto_get_token(senuto_email, senuto_password)
                        senuto_df = senuto_get_volumes(token, phrases)
                    except Exception as e:
                        st.warning(f"Nie udało się pobrać danych z Senuto: {e}. "
                                   f"Sprawdź endpoint w swojej dokumentacji API (docs-api.senuto.com).")

        merged = vol_df
        if not senuto_df.empty:
            merged = merged.merge(senuto_df, on="fraza", how="left")

        st.session_state.report_df = merged
        st.session_state.trends_data = trends

# ---------------------------------------------------------------------------
# Wyniki
# ---------------------------------------------------------------------------

if st.session_state.report_df is not None and not st.session_state.report_df.empty:
    df = st.session_state.report_df

    st.subheader("3. Podsumowanie dla klienta")
    summary = generate_summary(df)
    if summary is None:
        st.info("Za mało danych (wolumen aktualny / sprzed roku), żeby policzyć podsumowanie.")
    else:
        box = st.success if summary["kierunek"] == "wyższy" else (
            st.error if summary["kierunek"] == "niższy" else st.info)
        zdanie = (
            f"Dla sprawdzonych {summary['liczba_fraz']} fraz łączny wolumen wyszukiwań jest "
            f"**{summary['kierunek']}** niż rok temu "
            f"({summary['overall_change']:+.1f}%, {summary['total_before']:,} → {summary['total_now']:,} "
            f"wyszukiwań miesięcznie łącznie)."
        ).replace(",", " ")
        box(zdanie)

        m1, m2, m3 = st.columns(3)
        m1.metric("Frazy rosnące", summary["liczba_rosnacych"])
        m2.metric("Frazy spadające", summary["liczba_spadajacych"])
        m3.metric("Frazy stabilne", summary["liczba_stabilnych"])

        c1, c2 = st.columns(2)
        with c1:
            st.caption("Najmocniej rosnące frazy")
            st.dataframe(summary["top_rosnace"], hide_index=True, use_container_width=True)
        with c2:
            st.caption("Najmocniej spadające frazy")
            st.dataframe(summary["top_spadajace"], hide_index=True, use_container_width=True)

    st.subheader("4. Szczegółowa tabela")
    st.dataframe(df, use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("Pobierz CSV", data=csv_bytes, file_name="aura_herbals_raport_fraz.csv", mime="text/csv")
    with col2:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Raport")
        st.download_button("Pobierz Excel", data=buf.getvalue(),
                            file_name="aura_herbals_raport_fraz.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.subheader("5. Google Trends — wykres dla wybranej frazy")
    trends = st.session_state.trends_data
    if trends:
        chosen = st.selectbox("Wybierz frazę", options=sorted(trends.keys()))
        tdf = trends.get(chosen)
        if tdf is not None and not tdf.empty:
            tdf_plot = tdf.copy()
            tdf_plot["data"] = pd.to_datetime(tdf_plot["data"], errors="coerce")
            tdf_plot = tdf_plot.set_index("data")
            st.line_chart(tdf_plot["trend"])
        else:
            st.info("Brak danych trendu dla tej frazy.")
    else:
        st.info("Brak danych Google Trends — sprawdź klucze API lub spróbuj ponownie.")
elif st.session_state.report_df is not None:
    st.info("Nie udało się pobrać danych. Sprawdź klucze API i spróbuj ponownie.")

st.divider()
st.caption(
    "Uwaga: dane DataForSEO oparte są o Google Ads (wolumen) i Google Trends (popularność w skali 0-100). "
    "Wolumen 'rok temu' pochodzi z historii miesięcznej (monthly_searches). "
    "Integracja Senuto jest szkieletowa — endpoint keyword_database wymaga potwierdzenia zgodnie z Twoim planem API."
)
