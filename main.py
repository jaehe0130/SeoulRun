from __future__ import annotations

from typing import Any, Dict, List

import altair as alt
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

import osm_backend as ob
from kakaomap import kakao_keyword_search


# ===============================
# Page config
# ===============================
st.set_page_config(
    page_title="트레킹 코스 추천",
    page_icon="🥾",
    layout="wide",
)
st.title("🥾 트레킹 코스 추천")


# ===============================
# Weather
# ===============================
OPENWEATHER_API_KEY = st.secrets.get("OPENWEATHER_API_KEY", "")


@st.cache_data(ttl=600)
def get_weather(lat: float, lon: float):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "kr",
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def judge_outdoor(w: Dict[str, Any]) -> Dict[str, Any]:
    main = w.get("main", {})
    wind = w.get("wind", {})
    weather = (w.get("weather") or [{}])[0]
    rain = w.get("rain") or {}

    temp = float(main.get("temp", 0))
    feels = float(main.get("feels_like", temp))
    wind_speed = float(wind.get("speed", 0))
    desc = weather.get("description", "")
    precip = float(rain.get("1h", 0))

    score = 100
    if precip >= 1:
        score -= 40
    if feels <= 0 or feels >= 30:
        score -= 20
    if wind_speed >= 8:
        score -= 15

    score = max(0, min(100, score))

    return {
        "score": score,
        "desc": desc,
        "temp": temp,
        "feels": feels,
        "wind": wind_speed,
        "rain": precip,
    }


# ===============================
# Elevation helpers
# ===============================
def elev_color(elev: float) -> str:
    if elev < 120:
        return "#2ecc71"  # green
    elif elev < 300:
        return "#f1c40f"  # yellow
    else:
        return "#e67e22"  # orange


@st.cache_data(ttl=3600)
def cached_elevation(coords, api_key: str):
    return ob.elevation_profile(coords, api_key=api_key)


# ===============================
# Sidebar
# ===============================
with st.sidebar:
    # ✅ 세션 리셋 버튼 (추가된 부분)
    if st.button("🔄 세션 / 캐시 초기화"):
        st.session_state.clear()
        st.cache_data.clear()
        st.rerun()

    st.header("지역 선택")
    lat = st.number_input("위도", value=37.5665, format="%.6f")
    lon = st.number_input("경도", value=126.9780, format="%.6f")
    radius_km = st.slider("반경 (km)", 3.0, 25.0, 10.0)
    topk = st.slider("추천 코스 수", 3, 10, 5)

    st.divider()
    show_kakao = st.checkbox("카페 / 맥주 마커 표시", value=True)
    kakao_radius = st.slider("카카오 검색 반경(m)", 300, 3000, 1000)


# ===============================
# Load courses
# ===============================
bbox = ob.bbox_from_center(lat, lon, radius_km)
df = pd.DataFrame(ob.build_courses(bbox, max_relations=40))

if df.empty:
    st.error("추천 코스를 찾지 못했습니다.")
    st.stop()

df = df.sort_values("score", ascending=False).head(topk).reset_index(drop=True)

selected_name = st.selectbox("상세로 볼 코스 선택", df["name"])
row = df[df["name"] == selected_name].iloc[0]


# ===============================
# Kakao places
# ===============================
kakao_food, kakao_cafe = [], []
kakao_key = st.secrets.get("KAKAO_REST_API_KEY", "")

if show_kakao and kakao_key:
    kakao_food = kakao_keyword_search(
        query="맥주",
        category="FD6",
        x=row["end_lon"],
        y=row["end_lat"],
        radius=kakao_radius,
        size=10,
        api_key=kakao_key,
    )
    kakao_cafe = kakao_keyword_search(
        query="카페",
        category="CE7",
        x=row["end_lon"],
        y=row["end_lat"],
        radius=kakao_radius,
        size=10,
        api_key=kakao_key,
    )


# ===============================
# Layout
# ===============================
col_map, col_info = st.columns([1.4, 1])


# ===============================
# MAP
# ===============================
with col_map:
    m = folium.Map(location=[lat, lon], zoom_start=12)

    ors_key = st.secrets.get("ORS_API_KEY", "")
    elev_profile: List[Dict[str, Any]] = []

    if ors_key:
        try:
            elev_profile = cached_elevation(row["coords"], ors_key)
        except Exception:
            elev_profile = []

    for _, r in df.iterrows():
        latlon = r["coords"]
        is_selected = r["name"] == selected_name

        # ---- route ----
        if is_selected and elev_profile:
            elevs = [p["elev_m"] for p in elev_profile]
            n = min(len(latlon), len(elevs))
            for i in range(n - 1):
                folium.PolyLine(
                    [latlon[i], latlon[i + 1]],
                    color=elev_color(elevs[i]),
                    weight=8,
                    opacity=0.95,
                ).add_to(m)
        else:
            folium.PolyLine(
                latlon,
                color="#2ecc71",
                weight=8 if is_selected else 5,
                opacity=0.9,
                tooltip=f"{r['name']} · {r['distance_km']}km · {r['difficulty']}",
            ).add_to(m)

        # ---- start / end markers ----
        folium.Marker(
            [r["start_lat"], r["start_lon"]],
            icon=folium.Icon(color="blue", icon="play"),
            tooltip=f"[출발] {r['name']}",
        ).add_to(m)

        folium.Marker(
            [r["end_lat"], r["end_lon"]],
            icon=folium.Icon(color="red", icon="flag"),
            tooltip=f"[도착] {r['name']}",
        ).add_to(m)

    # Kakao markers
    for p in kakao_food:
        folium.Marker(
            [float(p["y"]), float(p["x"])],
            icon=folium.Icon(color="purple", icon="glass"),
            popup=f"<b>{p['place_name']}</b> · <a href='{p['place_url']}' target='_blank'>상세보기</a>",
        ).add_to(m)

    for p in kakao_cafe:
        folium.Marker(
            [float(p["y"]), float(p["x"])],
            icon=folium.Icon(color="pink", icon="coffee"),
            popup=f"<b>{p['place_name']}</b> · <a href='{p['place_url']}' target='_blank'>상세보기</a>",
        ).add_to(m)

    st_folium(m, height=650, use_container_width=True)


# ===============================
# RIGHT PANEL – Weather & Elevation
# ===============================
with col_info:
    st.subheader("날씨 / 야외 적합도")

    if OPENWEATHER_API_KEY:
        w = get_weather(row["start_lat"], row["start_lon"])
        j = judge_outdoor(w)

        st.metric("야외 적합도 점수", f"{j['score']} / 100")
        st.caption(j["desc"])

        st.markdown(
            f"""
- **기온** : {j['temp']:.1f}℃
- **체감 온도** : {j['feels']:.1f}℃
- **바람** : {j['wind']:.1f} m/s
- **강수량** : {j['rain']:.1f} mm
"""
        )
    else:
        st.info("날씨 API 키가 없습니다.")

    st.divider()
    st.subheader("고도 그래프")

    if elev_profile:
        df_ele = pd.DataFrame(elev_profile)
        chart = (
            alt.Chart(df_ele)
            .mark_line()
            .encode(
                x="dist_km",
                y="elev_m",
                tooltip=["dist_km", "elev_m"],
            )
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("고도 정보가 없습니다.")


# ===============================
# Bottom – course list
# ===============================
st.divider()
st.subheader("추천 코스 목록")

st.dataframe(
    df[["name", "difficulty", "distance_km", "members", "score"]],
    use_container_width=True,
    hide_index=True,
)
