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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
import streamlit as st
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                 Spacer, Table, TableStyle)

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


def dfs_trends_explore(login: str, password: str, keywords: list[str], location_code: int,
                        time_range: str = "past_5_years", max_retries: int = 2,
                        progress_callback=None) -> tuple[dict[str, pd.DataFrame], list[str], dict[str, str]]:
    """Zwraca (słownik fraza -> DataFrame(date, interest), lista fraz które się nie udały, powody).

    WAŻNE: endpoint `google_trends/explore/live` w DataForSEO przyjmuje TYLKO JEDNO zadanie
    na request ("You can set only one task at a time.") — więc każda fraza to osobne
    zapytanie HTTP, wysyłane pojedynczo, a nie w paczkach.

    Każda fraza jest też pytana OSOBNO od pozostałych (bez porównywania kilku fraz naraz),
    bo Google Trends przy porównaniu normalizuje wynik względem najsilniejszej frazy w grupie
    i potrafi zwrócić mnóstwo braków danych dla słabszych fraz — psuje to wykres. Pojedyncze
    zapytania dają dokładnie taki wykres, jak ręczne sprawdzenie frazy na trends.google.com.

    Domyślnie pobiera 5 lat historii (jak w trends.google.com). Dłuższy timeout i ponowne
    próby na wypadek wolnej odpowiedzi; jedno niepowodzenie nie przerywa całego biegu."""
    headers = dfs_auth_header(login, password)
    result: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    reasons: dict[str, str] = {}  # fraza -> prawdziwy powód niepowodzenia (diagnostyka)

    for i, kw in enumerate(keywords):
        if progress_callback:
            progress_callback(i, len(keywords), kw)

        payload = [{
            "keywords": [kw],
            "location_code": location_code,
            "time_range": time_range,
            "type": "web",
        }]

        resp = None
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(f"{DFS_BASE}/keywords_data/google_trends/explore/live",
                                      headers=headers, json=payload, timeout=60)
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                last_exc = e
                resp = None
                if attempt < max_retries:
                    time.sleep(2)
                continue

        if resp is None:
            detail = str(last_exc)
            if isinstance(last_exc, requests.exceptions.HTTPError) and last_exc.response is not None:
                detail = f"HTTP {last_exc.response.status_code}: {last_exc.response.text[:200]}"
            reasons[kw] = detail
            failed.append(kw)
            continue

        data = resp.json()
        tasks = data.get("tasks", [])
        found = False
        task_status = "brak wyniku w odpowiedzi API"
        for task in tasks:
            task_status = f"{task.get('status_code')}: {task.get('status_message')}"
            for item in (task.get("result") or []):
                if item is None:
                    continue
                # rzeczywista struktura DataForSEO:
                # result[] -> items[] -> {"type": "google_trends_graph", "keywords": [...], "data": [{"date_from":..,"values":[..]}]}
                for sub in (item.get("items") or []):
                    if sub.get("type") != "google_trends_graph":
                        continue
                    kws = sub.get("keywords") or [kw]
                    series_data = sub.get("data") or []
                    dates, values = [], []
                    for point in series_data:
                        try:
                            v = point.get("values", [None] * len(kws))[0]
                        except (IndexError, TypeError):
                            v = None
                        dates.append(point.get("date_from"))
                        values.append(v)
                    if dates:
                        tdf = pd.DataFrame({"data": pd.to_datetime(dates, errors="coerce"), "trend": values})
                        tdf = tdf.dropna(subset=["data"]).sort_values("data").reset_index(drop=True)
                        result[kw] = tdf
                        found = True

        if not found:
            failed.append(kw)
            reasons[kw] = task_status
        time.sleep(0.15)  # uprzejmie dla rate limitu

    return result, failed, reasons


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
# Sygnał z Google Trends (osobno od wolumenu Google Ads)
# ---------------------------------------------------------------------------

def compute_trend_signal(trends: dict[str, pd.DataFrame], threshold_pct: float = 10.0) -> pd.DataFrame:
    """Dla każdej frazy porównuje średnią popularność w pierwszej i drugiej połowie
    pobranego okresu Google Trends — daje niezależne od Google Ads potwierdzenie,
    czy zainteresowanie frazą rośnie czy spada."""
    rows = []
    for phrase, tdf in trends.items():
        if tdf is None or tdf.empty or len(tdf) < 4:
            continue
        d = tdf.dropna(subset=["trend"])
        if len(d) < 4:
            continue
        mid = len(d) // 2
        early = d["trend"].iloc[:mid].mean()
        late = d["trend"].iloc[mid:].mean()
        change = round((late - early) / early * 100, 1) if early else None
        if change is None:
            kierunek = "brak danych"
        elif change > threshold_pct:
            kierunek = "rosnący"
        elif change < -threshold_pct:
            kierunek = "malejący"
        else:
            kierunek = "stabilny"
        rows.append({
            "fraza": phrase,
            "trend_google_wczesniej": round(early, 1),
            "trend_google_ostatnio": round(late, 1),
            "trend_google_zmiana_%": change,
            "trend_google_kierunek": kierunek,
        })
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

    out = {
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

    # niezależne potwierdzenie z Google Trends (jeśli dane są dostępne w df)
    if "trend_google_kierunek" in df.columns:
        tg = df.dropna(subset=["trend_google_kierunek"])
        if not tg.empty:
            out["trend_google_rosnacych"] = int((tg["trend_google_kierunek"] == "rosnący").sum())
            out["trend_google_malejacych"] = int((tg["trend_google_kierunek"] == "malejący").sum())
            out["trend_google_stabilnych"] = int((tg["trend_google_kierunek"] == "stabilny").sum())

    return out


# ---------------------------------------------------------------------------
# Eksport: Excel (podsumowanie + tabela + wykres) i PDF (raport dla klienta)
# ---------------------------------------------------------------------------

def _total_volume_chart_png(summary: dict) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(5, 3))
    labels = ["Rok temu", "Teraz"]
    values = [summary["total_before"], summary["total_now"]]
    colors_ = ["#0B1F3A", "#D4A537"]
    ax.bar(labels, values, color=colors_)
    ax.set_ylabel("Łączny wolumen wyszukiwań / mies.")
    ax.set_title("Łączny wolumen — porównanie rok do roku")
    for i, v in enumerate(values):
        ax.text(i, v, f"{v:,}".replace(",", " "), ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _movers_chart_png(summary: dict) -> io.BytesIO:
    top_up = summary["top_rosnace"]
    top_down = summary["top_spadajace"]
    combo = pd.concat([top_up, top_down]).drop_duplicates(subset="fraza")
    combo = combo.sort_values("zmiana_%")
    fig, ax = plt.subplots(figsize=(6, max(3, 0.4 * len(combo))))
    bar_colors = ["#dc2626" if v < 0 else "#16a34a" for v in combo["zmiana_%"]]
    ax.barh(combo["fraza"], combo["zmiana_%"], color=bar_colors)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Zmiana r/r (%)")
    ax.set_title("Najmocniej rosnące i spadające frazy")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _trend_line_chart_png(phrase: str, tdf: pd.DataFrame) -> io.BytesIO | None:
    if tdf is None or tdf.empty:
        return None
    d = tdf.copy()
    d["data"] = pd.to_datetime(d["data"], errors="coerce")
    d = d.dropna(subset=["data"]).sort_values("data")
    if d.empty:
        return None
    fig, ax = plt.subplots(figsize=(6, 2.5))
    ax.plot(d["data"], d["trend"], color="#D4A537", linewidth=1.5)
    ax.set_title(f"Google Trends: {phrase}", fontsize=10)
    ax.set_ylabel("Popularność (0–100)")
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_excel_report(df: pd.DataFrame, summary: dict | None) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Dane szczegółowe")

        if summary is not None:
            ws = writer.book.create_sheet("Podsumowanie", 0)
            bold = Font(bold=True, color="0B1F3A")
            title = Font(bold=True, size=14, color="0B1F3A")

            ws["A1"] = "Aura Herbals — podsumowanie fraz"
            ws["A1"].font = title
            ws["A2"] = f"Wygenerowano: {dt.date.today().isoformat()}"

            ws["A4"] = "Sprawdzonych fraz"
            ws["B4"] = summary["liczba_fraz"]
            ws["A5"] = "Wolumen rok temu (łącznie/mies.)"
            ws["B5"] = summary["total_before"]
            ws["A6"] = "Wolumen teraz (łącznie/mies.)"
            ws["B6"] = summary["total_now"]
            ws["A7"] = "Zmiana r/r"
            ws["B7"] = f"{summary['overall_change']:+.1f}%"
            ws["A8"] = "Kierunek"
            ws["B8"] = summary["kierunek"]
            for r in range(4, 9):
                ws[f"A{r}"].font = bold

            ws["A10"] = "Frazy rosnące"
            ws["B10"] = summary["liczba_rosnacych"]
            ws["A11"] = "Frazy spadające"
            ws["B11"] = summary["liczba_spadajacych"]
            ws["A12"] = "Frazy stabilne"
            ws["B12"] = summary["liczba_stabilnych"]
            for r in range(10, 13):
                ws[f"A{r}"].font = bold

            ws["D4"] = "Łączny wolumen"
            ws["D4"].font = bold
            ws["D5"] = "Rok temu"
            ws["E5"] = summary["total_before"]
            ws["D6"] = "Teraz"
            ws["E6"] = summary["total_now"]

            chart = BarChart()
            chart.title = "Łączny wolumen — rok do roku"
            chart.y_axis.title = "Wyszukiwania / mies."
            data_ref = Reference(ws, min_col=5, min_row=5, max_row=6)
            cats_ref = Reference(ws, min_col=4, min_row=5, max_row=6)
            chart.add_data(data_ref, titles_from_data=False)
            chart.set_categories(cats_ref)
            if chart.series:
                chart.series[0].graphicalProperties.solidFill = "D4A537"
            chart.width = 12
            chart.height = 7
            ws.add_chart(chart, "D9")

            for col, width in zip("ABCDE", (32, 16, 4, 12, 12)):
                ws.column_dimensions[col].width = width

    return buf.getvalue()


def generate_pdf_report(df: pd.DataFrame, summary: dict | None, trends: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    NAVY = colors.HexColor("#0B1F3A")
    GOLD = colors.HexColor("#D4A537")
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceAfter=6, textColor=NAVY)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=6, textColor=NAVY)
    body = styles["BodyText"]

    story = [
        Paragraph("Aura Herbals — raport trendów wyszukiwań", h1),
        Paragraph(f"Wygenerowano: {dt.date.today().strftime('%d.%m.%Y')}", body),
        Spacer(1, 0.4 * cm),
    ]

    if summary is None:
        story.append(Paragraph("Za mało danych, żeby policzyć podsumowanie.", body))
    else:
        zdanie = (
            f"Dla sprawdzonych {summary['liczba_fraz']} fraz łączny wolumen wyszukiwań jest "
            f"<b>{summary['kierunek']}</b> niż rok temu "
            f"({summary['overall_change']:+.1f}%, "
            f"{summary['total_before']:,} → {summary['total_now']:,} wyszukiwań miesięcznie łącznie)."
        ).replace(",", " ")
        story.append(Paragraph(zdanie, body))
        story.append(Spacer(1, 0.3 * cm))

        metrics_table = Table(
            [["Frazy rosnące", "Frazy spadające", "Frazy stabilne"],
             [str(summary["liczba_rosnacych"]), str(summary["liczba_spadajacych"]), str(summary["liczba_stabilnych"])]],
            colWidths=[5.5 * cm] * 3,
        )
        metrics_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
        ]))
        story.append(metrics_table)
        story.append(Spacer(1, 0.4 * cm))

        chart_buf = _total_volume_chart_png(summary)
        story.append(Image(chart_buf, width=13 * cm, height=7.8 * cm))
        story.append(Spacer(1, 0.3 * cm))

        movers_buf = _movers_chart_png(summary)
        story.append(Image(movers_buf, width=14 * cm, height=9 * cm))
        story.append(PageBreak())

        story.append(Paragraph("Najmocniej rosnące frazy", h2))
        up_rows = [["Fraza", "Zmiana r/r"]] + [
            [r["fraza"], f"{r['zmiana_%']:+.1f}%"] for _, r in summary["top_rosnace"].iterrows()
        ]
        t_up = Table(up_rows, colWidths=[11 * cm, 4 * cm])
        t_up.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dcfce7")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
        ]))
        story.append(t_up)
        story.append(Spacer(1, 0.4 * cm))

        story.append(Paragraph("Najmocniej spadające frazy", h2))
        down_rows = [["Fraza", "Zmiana r/r"]] + [
            [r["fraza"], f"{r['zmiana_%']:+.1f}%"] for _, r in summary["top_spadajace"].iterrows()
        ]
        t_down = Table(down_rows, colWidths=[11 * cm, 4 * cm])
        t_down.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#fee2e2")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
        ]))
        story.append(t_down)

        # wykresy Google Trends dla max 3 najmocniej rosnących fraz, jeśli dostępne
        top_phrases_for_trend = list(summary["top_rosnace"]["fraza"].head(3))
        trend_images = [(p, trends.get(p)) for p in top_phrases_for_trend if trends.get(p) is not None]
        if trend_images:
            story.append(PageBreak())
            story.append(Paragraph("Google Trends — najmocniej rosnące frazy", h2))
            for phrase, tdf in trend_images:
                png = _trend_line_chart_png(phrase, tdf)
                if png is not None:
                    story.append(Image(png, width=14 * cm, height=5.8 * cm))
                    story.append(Spacer(1, 0.3 * cm))

    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        "Źródło danych: DataForSEO (Google Ads — wolumen wyszukiwań, Google Trends — popularność 0–100). "
        "Wolumen „rok temu” pochodzi z historii miesięcznej ostatnich 12 miesięcy.",
        ParagraphStyle("footer", parent=body, fontSize=8, textColor=colors.HexColor("#64748b")),
    ))

    doc.build(story)
    return buf.getvalue()


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
    trends_range = st.selectbox(
        "Okres Google Trends",
        options=["past_5_years", "past_12_months", "past_90_days"],
        format_func=lambda v: {"past_5_years": "5 lat (jak w trends.google.com)",
                                "past_12_months": "12 miesięcy",
                                "past_90_days": "90 dni"}[v],
        index=0,
    )

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
            except Exception as e:
                st.error(f"Błąd DataForSEO (search_volume): {e}")
                vol_df = pd.DataFrame()

        st.caption("Pobieram Google Trends (DataForSEO) — każda fraza to osobne zapytanie, może to potrwać kilka minut.")
        trends_progress = st.progress(0.0)
        trends_status = st.empty()

        def _trends_progress(i, total, kw):
            trends_progress.progress((i + 1) / total)
            trends_status.text(f"{i + 1}/{total}: {kw}")

        try:
            trends, failed_trends, trend_reasons = dfs_trends_explore(
                dfs_login, dfs_password, phrases, int(location_code), time_range=trends_range,
                progress_callback=_trends_progress)
            trends_progress.empty()
            trends_status.empty()
            if failed_trends:
                unique_reasons = sorted(set(trend_reasons.get(kw, "nieznany powód") for kw in failed_trends))
                st.warning(
                    f"Nie udało się pobrać Google Trends dla {len(failed_trends)} fraz: "
                    f"{', '.join(failed_trends)}.\n\n"
                    f"Powód(y) zwrócone przez API: {' | '.join(unique_reasons[:5])}"
                    + (" (i inne)" if len(unique_reasons) > 5 else "") + ". "
                    f"Reszta danych jest kompletna — spróbuj ponownie, jeśli te frazy są kluczowe."
                )
        except Exception as e:
            trends_progress.empty()
            trends_status.empty()
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
        trend_signal_df = compute_trend_signal(trends)
        if not trend_signal_df.empty:
            merged = merged.merge(trend_signal_df, on="fraza", how="left")

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
        m1.metric("Frazy rosnące (wolumen)", summary["liczba_rosnacych"])
        m2.metric("Frazy spadające (wolumen)", summary["liczba_spadajacych"])
        m3.metric("Frazy stabilne (wolumen)", summary["liczba_stabilnych"])

        if "trend_google_rosnacych" in summary:
            st.caption("Niezależne potwierdzenie z Google Trends (popularność wyszukiwań w czasie):")
            g1, g2, g3 = st.columns(3)
            g1.metric("Rosnący trend Google", summary["trend_google_rosnacych"])
            g2.metric("Malejący trend Google", summary["trend_google_malejacych"])
            g3.metric("Stabilny trend Google", summary["trend_google_stabilnych"])

        c1, c2 = st.columns(2)
        with c1:
            st.caption("Najmocniej rosnące frazy")
            st.dataframe(summary["top_rosnace"], hide_index=True, use_container_width=True)
        with c2:
            st.caption("Najmocniej spadające frazy")
            st.dataframe(summary["top_spadajace"], hide_index=True, use_container_width=True)

    st.subheader("4. Szczegółowa tabela")
    st.dataframe(df, use_container_width=True, hide_index=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("Pobierz CSV", data=csv_bytes, file_name="aura_herbals_raport_fraz.csv", mime="text/csv")
    with col2:
        excel_bytes = generate_excel_report(df, summary)
        st.download_button("Pobierz Excel (z podsumowaniem i wykresem)", data=excel_bytes,
                            file_name="aura_herbals_raport_fraz.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with col3:
        pdf_bytes = generate_pdf_report(df, summary, st.session_state.trends_data)
        st.download_button("Pobierz PDF (raport dla klienta)", data=pdf_bytes,
                            file_name="aura_herbals_raport_klienta.pdf",
                            mime="application/pdf")

    st.subheader("5. Google Trends — wykres dla wybranej frazy")
    trends = st.session_state.trends_data
    if trends:
        chosen = st.selectbox("Wybierz frazę", options=sorted(trends.keys()))
        tdf = trends.get(chosen)
        if tdf is not None and not tdf.empty:
            tdf_plot = tdf.copy()
            tdf_plot["data"] = pd.to_datetime(tdf_plot["data"], errors="coerce")
            tdf_plot = tdf_plot.dropna(subset=["data"]).sort_values("data").set_index("data")
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
