"""
╔══════════════════════════════════════════════════════════════════════════════╗
║       İY/MS ANALİZ BOTU — Streamlit Community Cloud Uyumlu Versiyon       ║
║       Scraper: requests + BeautifulSoup (Playwright gerektirmez)           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import time
import random
import re
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────────────────────────
# Loglama
# ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("IYMS-Bot")

# ──────────────────────────────────────────────────────────────────
# Sabitler
# ──────────────────────────────────────────────────────────────────
TARGET_COMBINATIONS = {"1/2", "2/2", "X/1", "X/2"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ──────────────────────────────────────────────────────────────────
# Veri Modeli
# ──────────────────────────────────────────────────────────────────
@dataclass
class Match:
    saat: str
    lig: str
    mac: str
    iy_ms: str
    oran: str
    kaynak: str = ""

    def unique_key(self) -> str:
        return f"{self.mac.strip().lower()}_{self.iy_ms.strip().lower()}"


# ──────────────────────────────────────────────────────────────────
# Yardımcı Fonksiyonlar
# ──────────────────────────────────────────────────────────────────
def get_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def random_delay(a=1.0, b=3.0):
    time.sleep(random.uniform(a, b))


def normalize_iy_ms(raw: str) -> Optional[str]:
    if not raw:
        return None
    text = raw.strip().upper()
    text = re.sub(r"\s*[-–]\s*", "/", text)
    text = (text.replace("EV", "1").replace("DEPLASMAN", "2")
            .replace("BERABERLİK", "X").replace("BERABERLIK", "X"))
    m = re.search(r"([12X])/([12X])", text)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def deduplicate(matches: list) -> list:
    seen, unique = set(), []
    for m in matches:
        k = m.unique_key()
        if k not in seen:
            seen.add(k)
            unique.append(m)
    logger.info(f"Deduplikasyon: {len(matches)} → {len(unique)} maç")
    return unique


def filter_targets(matches: list) -> list:
    return [m for m in matches if m.iy_ms in TARGET_COMBINATIONS]


# ──────────────────────────────────────────────────────────────────
# SCRAPER — Sahadan.com (statik HTML, BS4 ile parse edilebilir)
# ──────────────────────────────────────────────────────────────────
def scrape_sahadan() -> list:
    """
    sahadan.com/program/maclar/ adresinden günün maçlarını çeker.
    Sayfa statik HTML içerdiğinden requests+BS4 ile parse edilebilir.
    """
    url = "https://www.sahadan.com/program/maclar/"
    matches = []
    try:
        session = requests.Session()
        resp = session.get(url, headers=get_headers(), timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Sahadan tablo yapısı: her lig için bir tablo bölümü
        current_lig = "Bilinmiyor"
        for row in soup.select("tr"):
            # Lig başlık satırı
            lig_cell = row.select_one("td.lig-adi, td[class*='lig']")
            if lig_cell:
                current_lig = lig_cell.get_text(strip=True)
                continue

            cols = row.select("td")
            if len(cols) < 6:
                continue

            try:
                saat  = cols[0].get_text(strip=True)
                ev    = cols[2].get_text(strip=True)
                dep   = cols[4].get_text(strip=True)
                skor  = cols[3].get_text(strip=True)   # MS skoru
                iy    = cols[5].get_text(strip=True) if len(cols) > 5 else ""

                if not ev or not dep or not skor:
                    continue

                # İY/MS kombinasyonu: skoru gol sayısına göre yorumla
                iy_norm = _score_to_iyms(iy, skor)
                if not iy_norm:
                    continue

                matches.append(Match(
                    saat=saat, lig=current_lig,
                    mac=f"{ev} - {dep}",
                    iy_ms=iy_norm, oran="-", kaynak="Sahadan"
                ))
            except Exception:
                continue

        random_delay()
    except Exception as e:
        logger.error(f"Sahadan hatası: {e}")

    logger.info(f"Sahadan: {len(matches)} maç")
    return matches


# ──────────────────────────────────────────────────────────────────
# SCRAPER — Mackolik API (JSON endpoint, oldukça stabil)
# ──────────────────────────────────────────────────────────────────
def scrape_mackolik() -> list:
    """
    Mackolik'in günlük sonuç JSON endpoint'ini kullanır.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"https://www.mackolik.com/mac-sonuclari/{today}"
    matches = []
    try:
        session = requests.Session()
        # Önce ana sayfayı ziyaret ederek cookie al
        session.get("https://www.mackolik.com", headers=get_headers(), timeout=15)
        random_delay(1, 2)

        resp = session.get(url, headers=get_headers(), timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        current_lig = "Bilinmiyor"
        for row in soup.select(".match-row, [class*='matchRow'], [class*='event']"):
            try:
                lig_el  = row.select_one("[class*='league'], [class*='tournament']")
                time_el = row.select_one("[class*='time'], [class*='hour']")
                home_el = row.select_one("[class*='home'], [class*='team1']")
                away_el = row.select_one("[class*='away'], [class*='team2']")
                ht_el   = row.select_one("[class*='ht'], [class*='half']")
                ft_el   = row.select_one("[class*='score'], [class*='result'], [class*='ft']")

                if lig_el:
                    current_lig = lig_el.get_text(strip=True)
                if not (home_el and away_el and ft_el):
                    continue

                ht   = ht_el.get_text(strip=True) if ht_el else ""
                ft   = ft_el.get_text(strip=True)
                norm = _score_to_iyms(ht, ft)
                if not norm:
                    continue

                matches.append(Match(
                    saat=time_el.get_text(strip=True) if time_el else "--",
                    lig=current_lig,
                    mac=f"{home_el.get_text(strip=True)} - {away_el.get_text(strip=True)}",
                    iy_ms=norm, oran="-", kaynak="Mackolik"
                ))
            except Exception:
                continue

        random_delay()
    except Exception as e:
        logger.error(f"Mackolik hatası: {e}")

    logger.info(f"Mackolik: {len(matches)} maç")
    return matches


# ──────────────────────────────────────────────────────────────────
# SCRAPER — Nesine (JSON API endpoint)
# ──────────────────────────────────────────────────────────────────
def scrape_nesine() -> list:
    """
    Nesine'nin biten maç JSON API'sini kullanır.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    api_url = (
        "https://nesine-api-cdn.nesine.com/eventsapi/v2/finishedevents"
        f"?sportId=1&date={today}&lang=tr"
    )
    matches = []
    try:
        headers = {**get_headers(), "Referer": "https://www.nesine.com/"}
        resp = requests.get(api_url, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        events = data.get("data", data.get("events", data.get("result", [])))
        if isinstance(events, dict):
            events = events.get("events", [])

        for ev in events:
            try:
                lig  = ev.get("leagueName", ev.get("league", {}).get("name", "Bilinmiyor"))
                home = ev.get("homeTeamName", ev.get("homeTeam", {}).get("name", ""))
                away = ev.get("awayTeamName", ev.get("awayTeam", {}).get("name", ""))
                ht_h = str(ev.get("htScore", ev.get("halfTimeHomeScore", "")))
                ht_a = str(ev.get("htScoreAway", ev.get("halfTimeAwayScore", "")))
                ft_h = str(ev.get("homeScore", ev.get("ftHomeScore", "")))
                ft_a = str(ev.get("awayScore", ev.get("ftAwayScore", "")))
                saat = ev.get("matchTime", ev.get("startTime", "--"))[:5]

                ht = f"{ht_h}-{ht_a}" if ht_h and ht_a else ""
                ft = f"{ft_h}-{ft_a}" if ft_h and ft_a else ""
                norm = _score_to_iyms(ht, ft)
                if not norm:
                    continue

                # İY/MS oran varsa çek
                oran = "-"
                for odd in ev.get("odds", []):
                    if odd.get("type") == "HTFT":
                        oran = str(odd.get("value", "-"))
                        break

                matches.append(Match(
                    saat=saat, lig=lig,
                    mac=f"{home} - {away}",
                    iy_ms=norm, oran=oran, kaynak="Nesine"
                ))
            except Exception:
                continue

        random_delay()
    except Exception as e:
        logger.error(f"Nesine hatası: {e}")

    logger.info(f"Nesine: {len(matches)} maç")
    return matches


# ──────────────────────────────────────────────────────────────────
# Skor → İY/MS kombinasyonu çevirici
# ──────────────────────────────────────────────────────────────────
def _score_to_iyms(ht_raw: str, ft_raw: str) -> Optional[str]:
    """
    'İY skoru' ve 'MS skoru' metinlerinden İY/MS kombinasyonunu üretir.
    Örnek: ht='1-0', ft='1-2' → '1/2'
    """
    def parse_score(s: str):
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", s)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def winner(h, a):
        if h > a: return "1"
        if h < a: return "2"
        return "X"

    ht = parse_score(ht_raw) if ht_raw else None
    ft = parse_score(ft_raw) if ft_raw else None

    if ht and ft:
        return f"{winner(*ht)}/{winner(*ft)}"
    if ft:
        # Sadece MS varsa sadece MS kısmını normalize et
        return normalize_iy_ms(ft_raw)
    return None


# ──────────────────────────────────────────────────────────────────
# MOCK VERİ (siteler erişilemezse gösterilir)
# ──────────────────────────────────────────────────────────────────
def _mock_data() -> list:
    fixtures = [
        ("20:00", "Süper Lig",     "Galatasaray - Fenerbahçe",   "1/2",  "3.45"),
        ("19:00", "La Liga",       "Real Madrid - Barcelona",     "2/2",  "2.10"),
        ("18:30", "Premier League","Manchester City - Arsenal",   "X/1",  "4.20"),
        ("21:45", "Bundesliga",    "Bayern Münih - Dortmund",     "X/2",  "5.60"),
        ("22:00", "Serie A",       "Juventus - Milan",            "1/2",  "3.80"),
        ("17:00", "Süper Lig",     "Beşiktaş - Trabzonspor",      "2/2",  "2.95"),
        ("16:00", "Ligue 1",       "PSG - Marseille",             "X/2",  "6.10"),
        ("15:00", "Premier League","Liverpool - Chelsea",         "1/2",  "4.00"),
        ("20:45", "Champions Lg",  "Atletico Madrid - Inter",     "X/1",  "3.75"),
        ("18:00", "Süper Lig",     "Başakşehir - Kasımpaşa",      "2/2",  "2.50"),
    ]
    return [Match(s, l, m, c, o, "Demo") for s, l, m, c, o in fixtures]


# ──────────────────────────────────────────────────────────────────
# STREAMLİT ARAYÜZÜ
# ──────────────────────────────────────────────────────────────────
def configure_page():
    st.set_page_config(
        page_title="İY/MS Analiz Botu",
        page_icon="⚽",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;700&family=Barlow:wght@400;500&display=swap');

    html, body, [class*="css"] {
        font-family: 'Barlow', sans-serif;
        background-color: #0d1117;
        color: #e6edf3;
    }
    h1,h2,h3 { font-family: 'Barlow Condensed', sans-serif; letter-spacing:.03em; }

    [data-testid="stSidebar"] {
        background: linear-gradient(180deg,#161b22 0%,#0d1117 100%);
        border-right: 1px solid #30363d;
    }
    [data-testid="metric-container"] {
        background:#161b22; border:1px solid #30363d;
        border-radius:8px; padding:12px 16px;
    }
    .header-bar {
        background: linear-gradient(90deg,#1f6feb,#388bfd);
        padding:16px 24px; border-radius:10px; margin-bottom:20px;
    }
    .combo-1-2 { color:#ff6b6b; font-weight:700; }
    .combo-2-2 { color:#ffd93d; font-weight:700; }
    .combo-x-1 { color:#6bcb77; font-weight:700; }
    .combo-x-2 { color:#4d96ff; font-weight:700; }
    .stDataFrame { border-radius:8px; overflow:hidden; }
    .last-update { font-size:12px; color:#8b949e; }
    </style>
    """, unsafe_allow_html=True)


def render_header():
    st.markdown("""
    <div class="header-bar">
        <h1 style="margin:0;color:white;">⚽ İY/MS Analiz Botu</h1>
        <p style="margin:4px 0 0;color:#cae3ff;font-size:14px;">
            Hedef Kombinasyonlar: &nbsp;
            <b style="color:#ff6b6b;">1/2</b> &nbsp;·&nbsp;
            <b style="color:#ffd93d;">2/2</b> &nbsp;·&nbsp;
            <b style="color:#6bcb77;">X/1</b> &nbsp;·&nbsp;
            <b style="color:#4d96ff;">X/2</b>
        </p>
    </div>
    """, unsafe_allow_html=True)


def render_sidebar():
    st.sidebar.header("⚙️ Ayarlar")
    st.sidebar.subheader("Veri Kaynakları")
    use_sahadan  = st.sidebar.checkbox("Sahadan",  value=True)
    use_mackolik = st.sidebar.checkbox("Mackolik", value=True)
    use_nesine   = st.sidebar.checkbox("Nesine",   value=True)

    st.sidebar.subheader("Kombinasyon Filtresi")
    selected = st.sidebar.multiselect(
        "Gösterilecek kombinasyonlar",
        sorted(TARGET_COMBINATIONS),
        default=sorted(TARGET_COMBINATIONS)
    )
    show_all = st.sidebar.checkbox("Tüm maçları göster (filtre kapalı)", value=False)

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Veriler ücretsiz scraping ile toplanır.\n"
        "Siteler erişilemezse demo veri gösterilir."
    )
    return {
        "use_sahadan": use_sahadan,
        "use_mackolik": use_mackolik,
        "use_nesine": use_nesine,
        "selected": selected,
        "show_all": show_all,
    }


@st.cache_data(ttl=300, show_spinner=False)
def fetch_all_data(use_sahadan, use_mackolik, use_nesine):
    """5 dakika cache'li veri çekme fonksiyonu."""
    all_matches = []
    if use_sahadan:  all_matches += scrape_sahadan()
    if use_mackolik: all_matches += scrape_mackolik()
    if use_nesine:   all_matches += scrape_nesine()

    if not all_matches:
        logger.warning("Hiçbir kaynaktan veri gelmedi, demo veri kullanılıyor.")
        all_matches = _mock_data()

    unique = deduplicate(all_matches)
    return unique


def matches_to_df(matches: list) -> pd.DataFrame:
    if not matches:
        return pd.DataFrame(columns=["Saat","Lig","Maç","İY/MS","Oran","Kaynak"])
    return pd.DataFrame([{
        "Saat":   m.saat,
        "Lig":    m.lig,
        "Maç":    m.mac,
        "İY/MS":  m.iy_ms,
        "Oran":   m.oran,
        "Kaynak": m.kaynak,
    } for m in sorted(matches, key=lambda x: x.saat)])


def color_iy_ms(val):
    c = {"1/2":"#3d1e1e;color:#ff6b6b","2/2":"#3d3400;color:#ffd93d",
         "X/1":"#1a3d21;color:#6bcb77","X/2":"#1a2a3d;color:#4d96ff"}
    style = c.get(val, "")
    return f"background-color:{style};font-weight:bold;" if style else ""


def main():
    configure_page()
    render_header()
    cfg = render_sidebar()

    # Metrikler için placeholder'lar
    metric_cols = st.columns(5)

    # Yenile butonu + son güncelleme
    c1, c2 = st.columns([1, 3])
    with c1:
        refresh = st.button("🔄 Verileri Çek / Yenile", type="primary", use_container_width=True)
    with c2:
        ts_placeholder = st.empty()

    if refresh:
        fetch_all_data.clear()

    with st.spinner("Veriler çekiliyor..."):
        raw = fetch_all_data(cfg["use_sahadan"], cfg["use_mackolik"], cfg["use_nesine"])

    ts_placeholder.markdown(
        f'<div class="last-update">Son güncelleme: {datetime.now().strftime("%d.%m.%Y %H:%M:%S")}</div>',
        unsafe_allow_html=True
    )

    # Filtre uygula
    if cfg["show_all"]:
        shown = raw
    else:
        shown = [m for m in raw if m.iy_ms in cfg["selected"]]

    df = matches_to_df(shown)

    # Metrikler
    labels = ["Toplam", "1/2", "2/2", "X/1", "X/2"]
    values = [
        len(df),
        len(df[df["İY/MS"] == "1/2"]) if not df.empty else 0,
        len(df[df["İY/MS"] == "2/2"]) if not df.empty else 0,
        len(df[df["İY/MS"] == "X/1"]) if not df.empty else 0,
        len(df[df["İY/MS"] == "X/2"]) if not df.empty else 0,
    ]
    for col, lbl, val in zip(metric_cols, labels, values):
        col.metric(lbl, val)

    st.markdown("---")

    # Ana tablo
    if df.empty:
        st.info("🔍 Seçili kombinasyonlara uyan maç bulunamadı.")
    else:
        styled = df.style.map(color_iy_ms, subset=["İY/MS"])
        st.dataframe(styled, use_container_width=True, height=460)

    # Grafikler
    if not df.empty:
        st.markdown("---")
        g1, g2 = st.columns(2)
        with g1:
            st.subheader("📊 Kombinasyon Dağılımı")
            dist = df["İY/MS"].value_counts().reset_index()
            dist.columns = ["Kombinasyon", "Sayı"]
            st.bar_chart(dist.set_index("Kombinasyon"))
        with g2:
            st.subheader("🏆 En Çok Maçı Olan Ligler")
            lig_dist = df["Lig"].value_counts().head(8).reset_index()
            lig_dist.columns = ["Lig", "Maç"]
            st.bar_chart(lig_dist.set_index("Lig"))

    # CSV İndir
    if not df.empty:
        st.markdown("---")
        csv = df.to_csv(index=False, encoding="utf-8-sig")
        st.download_button(
            "📥 CSV İndir",
            data=csv,
            file_name=f"iyms_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv"
        )

    with st.expander("ℹ️ Nasıl Çalışır?"):
        st.markdown("""
        1. **Veri Çekme**: Sahadan, Mackolik ve Nesine sitelerinden `requests + BeautifulSoup` ile HTML/JSON verisi çekilir.
        2. **Bot Koruması**: Rastgele User-Agent rotasyonu ve gecikmeler uygulanır.
        3. **Deduplikasyon**: Aynı maç birden fazla kaynakta varsa tekrar silinir.
        4. **Filtreleme**: Yalnızca `1/2, 2/2, X/1, X/2` kombinasyonları gösterilir.
        5. **Cache**: Veriler 5 dakika boyunca önbellekte tutulur; gereksiz istek önlenir.
        > Siteler erişilemezse otomatik olarak **demo veri** gösterilir.
        """)


if __name__ == "__main__":
    main()
