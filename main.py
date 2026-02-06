from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import altair as alt
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

import osm_backend as ob
from kakaomap import kakao_keyword_search

st.set_page_config(page_title="트레킹 코스 추천", page_icon="🥾", layout="wide")
st.title("🥾 트레킹 코스 추천")

# ====== Weather(OpenWeather) ======
OPENWEATHER_API_KEY = st.secrets.get("OPENWEATHER_API_KEY", "")


@st.cache_data(ttl=600)  # 10분 캐시
def get_weather_openweather(lat: float, lon: float, api_key: str):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "metric",
        "lang": "kr",
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def judge_outdoor(w):
    """야외(런닝/트레킹) 적합도 판정"""
    main = w.get("main", {})
    wind = w.get("wind", {})
    weather = (w.get("weather") or [{}])[0]
    rain = w.get("rain") or {}
    snow = w.get("snow") or {}

    temp = float(main.get("temp", 0))
    feels = float(main.get("feels_like", temp))
    humidity = float(main.get("humidity", 0))
    wind_speed = float(wind.get("speed", 0))  # m/s
    desc = weather.get("description", "")

    # 강수량(시간당 mm 추정)
    precip = 0.0
    if "1h" in rain:
        precip = max(precip, float(rain.get("1h", 0)))
    if "3h" in rain:
        precip = max(precip, float(rain.get("3h", 0)) / 3.0)
    if "1h" in snow:
        precip = max(precip, float(snow.get("1h", 0)))
    if "3h" in snow:
        precip = max(precip, float(snow.get("3h", 0)) / 3.0)

    score = 100
    reasons = []

    # 강수
    if precip >= 2.0:
        score -= 55
        reasons.append(f"비/눈 많음({precip:.1f}mm/h)")
    elif precip >= 0.5:
        score -= 25
        reasons.append(f"약한 비/눈({precip:.1f}mm/h)")

    # 체감온도
    if feels <= -5:
        score -= 35
        reasons.append(f"너무 추움(체감 {feels:.0f}°C)")
    elif feels <= 0:
        score -= 18
        reasons.append(f"추움(체감 {feels:.0f}°C)")
    elif feels >= 30:
        score -= 30
        reasons.append(f"너무 더움(체감 {feels:.0f}°C)")

    # 바람
    if wind_speed >= 10:
        score -= 25
        reasons.append(f"강풍({wind_speed:.1f}m/s)")
    elif wind_speed >= 7:
        score -= 12
        reasons.append(f"바람 강함({wind_speed:.1f}m/s)")

    # 습도
    if humidity >= 85 and feels >= 25:
        score -= 12
        reasons.append(f"습도 높음({humidity:.0f}%)")

    score = max(0, min(100, score))

    if score >= 75:
        level, label = "good", "오늘은 야외(트레킹)하기 좋아요 ✅"
    elif score >= 50:
        level, label = "warn", "가능은 하지만 주의가 필요해요 ⚠️"
    else:
        level, label = "bad", "오늘은 야외 활동 비추천 ⛔"

    return {
        "level": level,
        "label": label,
        "score": score,
        "temp": temp,
        "feels": feels,
        "humidity": humidity,
        "wind_speed": wind_speed,
        "precip_per_h": precip,
        "desc": desc,
        "reasons": reasons or ["특이사항 없음"],
    }


# ====== 공공데이터(GPX) 인덱스 로드 ======
DATA_DIR = Path(__file__).parent / "data"


@st.cache_data(ttl=60 * 60 * 12)  # 12시간 캐시
def cached_official_index(
    data_dir_str: str, bbox: Tuple[float, float, float, float], max_files: int
) -> List[Dict[str, Any]]:
    return ob.load_official_gpx_index(data_dir_str, bbox=bbox, max_files=max_files)


# ====== Cached backend ======
@st.cache_data(ttl=60 * 60)
def cached_courses(
    bbox: Tuple[float, float, float, float],
    max_relations: int,
    official_key: str,
    official_index: List[Dict[str, Any]],
) -> pd.DataFrame:
    _ = official_key  # 캐시 키
    courses = ob.build_courses(
        bbox, max_relations=max_relations, official_index=official_index
    )
    if not courses:
        return pd.DataFrame()
    df = pd.DataFrame(courses)
    df = df.sort_values(["score", "distance_km"], ascending=False).reset_index(
        drop=True
    )
    return df


@st.cache_data(ttl=60 * 20)
def cached_places(lat: float, lon: float, radius_m: int) -> List[Dict[str, Any]]:
    return ob.places_near(lat, lon, radius_m)


@st.cache_data(ttl=60 * 60)
def cached_elevation_profile(coords_latlon, ors_api_key: str):
    return ob.elevation_profile(coords_latlon, api_key=ors_api_key)


@st.cache_data(ttl=60 * 10)
def cached_kakao_places(
    query: str,
    category: str,
    x: float,
    y: float,
    radius_m: int,
    size: int,
    api_key: str,
) -> List[Dict[str, Any]]:
    return kakao_keyword_search(
        query=query,
        category=category,
        x=x,
        y=y,
        radius=radius_m,
        size=size,
        api_key=api_key,
    )


# ====== Sidebar ======
with st.sidebar:
    st.header("1) 지역 선택")
    preset = st.selectbox(
        "프리셋 지역",
        [
            "서울 전체",
            "용산구",
            "은평,강북,도봉구",
            "동작/영등포구",
            "강남구",
            "사용자 지정",
        ],
    )

    if preset == "사용자 지정":
        lat = st.number_input("중심 위도(lat)", value=37.5665, format="%.6f")
        lon = st.number_input("중심 경도(lon)", value=126.9780, format="%.6f")
        radius_km = st.slider("반경(km)", 2.0, 30.0, 12.0, 0.5)
    else:
        presets = {
            "서울 전체": (37.5665, 126.9780, 18.0),
            "용산구": (37.5512, 126.9882, 8.0),
            "은평,강북,도봉구": (37.6584, 126.9800, 12.0),
            "동작/영등포구": (37.5250, 126.9250, 10.0),
            "강남구": (37.4840, 127.0350, 10.0),
        }
        lat, lon, radius_km = presets[preset]

    st.header("2) 난이도/추천 수")
    diff_filter = st.radio("난이도", ["전체", "쉬움", "보통", "어려움"], index=0)
    topk = st.slider("추천 코스 개수", 3, 10, 4)
    max_relations = st.slider("후보 탐색량(Overpass 부담)", 20, 80, 50, 5)

    st.header("공공데이터(GPX) 반영")
    official_on = st.toggle("공공데이터 매칭 점수 가산", value=True)
    max_gpx_files = st.slider(
        "GPX 인덱싱 최대 파일 수(속도/정확도)", 200, 4000, 1500, 100
    )
    match_threshold_m = st.slider("매칭 허용 거리(m)", 100, 800, 250, 50)

    st.header("3) 트레킹 후 추천")
    near_radius_m = st.slider("주변 추천 반경(m)", 100, 2000, 700, 50)
    sip_choice = st.radio(
        "추천 종류", ["전체", "카페(☕)", "맥주(🍺)"], horizontal=True
    )

    st.header("4) 고도 그래프")
    show_elevation = st.checkbox("선택 코스 고도 그래프 보기", value=False)

    st.header("5) 카카오 카페/맥주 마커")
    show_kakao = st.checkbox("카카오 마커 표시", value=True)
    kakao_radius_m = st.slider("카카오 검색 반경(m)", 200, 5000, 1200, 100)
    kakao_size = st.slider("카카오 결과 수(각 카테고리)", 5, 15, 10, 1)

    st.divider()

    if st.button("🔄 캐시 초기화", use_container_width=True):
        st.cache_data.clear()
        st.success("캐시 초기화 완료! 새로고침하면 다시 수집합니다.")


# ====== Load courses ======
bbox = ob.bbox_from_center(lat, lon, radius_km)

# 공공데이터 인덱스 로드 (옵션)
official_index: List[Dict[str, Any]] = []
official_key = "OFFICIAL_OFF"
if official_on:
    try:
        official_index = cached_official_index(
            str(DATA_DIR), bbox=bbox, max_files=max_gpx_files
        )
        # 매칭 임계값은 backend에서 참조하도록 global setter로 전달
        ob.set_official_match_threshold(int(match_threshold_m))
        official_key = f"OFFICIAL_ON_{DATA_DIR.stat().st_mtime}_{len(official_index)}_{match_threshold_m}"
    except Exception as e:
        st.warning("공공데이터(GPX) 인덱스 로드 실패(폴더/형식/권한 확인).")
        st.exception(e)
        official_index = []
        official_key = "OFFICIAL_OFF"

with st.status("코스 불러오는 중...", expanded=False) as status:
    try:
        df = cached_courses(
            bbox,
            max_relations=max_relations,
            official_key=official_key,
            official_index=official_index,
        )
        status.update(label=f"코스 로딩 완료 ({len(df)})", state="complete")
    except Exception as e:
        status.update(label="코스 로딩 실패", state="error")
        st.error("서버 제한(429) 또는 일시적 오류입니다. 잠시 후 다시 시도하세요.")
        st.exception(e)
        st.stop()

if df.empty:
    st.error(
        "이 지역에서 코스를 찾지 못했습니다. 반경을 늘리거나 다른 지역을 선택하세요."
    )
    st.stop()

# ====== Select a course first (weather shows earlier) ======
selected = st.selectbox("상세로 볼 코스 선택", df["name"].tolist(), index=0)
row = df[df["name"] == selected].iloc[0].to_dict()

# difficulty filter
if diff_filter != "전체":
    df_use = df[df["difficulty"] == diff_filter].copy()
else:
    df_use = df.copy()

if df_use.empty:
    st.info("선택한 난이도의 코스가 없습니다. 다른 난이도를 선택하세요.")
    st.stop()

# top-k
df_use = df_use.sort_values("score", ascending=False).head(topk).reset_index(drop=True)
df_chart = df_use[
    ["name", "difficulty", "distance_km", "members", "trust_score", "score"]
].copy()

# ====== Kakao places (near selected course end) ======
kakao_cafe: List[Dict[str, Any]] = []
kakao_beer: List[Dict[str, Any]] = []
kakao_center: Tuple[float, float] | None = None

if show_kakao:
    try:
        kakao_key = st.secrets.get("KAKAO_REST_API_KEY", "") or st.secrets.get(
            "KAKAO_REST_KEY", ""
        )
        if not kakao_key:
            st.info("KAKAO_REST_API_KEY가 Secrets에 없어 카카오 마커를 숨깁니다.")
        else:
            end_lon = float(row["end_lon"])
            end_lat = float(row["end_lat"])
            kakao_center = (end_lat, end_lon)

            kakao_cafe = cached_kakao_places(
                query="카페",
                category="CE7",
                x=end_lon,
                y=end_lat,
                radius_m=int(kakao_radius_m),
                size=int(kakao_size),
                api_key=kakao_key,
            )
            kakao_beer = cached_kakao_places(
                query="맥주",
                category="FD6",  # 음식점/주점도 키워드로 많이 잡힘
                x=end_lon,
                y=end_lat,
                radius_m=int(kakao_radius_m),
                size=int(kakao_size),
                api_key=kakao_key,
            )
    except Exception as e:
        st.warning("Kakao Local 호출 실패. API 키/권한(IP 제한) 확인하세요.")
        st.exception(e)

# ====== Weather / Outdoor score ======
st.caption("날씨/야외 적합도 (선택 코스 시작점 기준)")

if not OPENWEATHER_API_KEY:
    st.info("OPENWEATHER_API_KEY가 없어 날씨 패널을 숨깁니다.")
else:
    wlat, wlon = float(row["start_lat"]), float(row["start_lon"])

    try:
        w = get_weather_openweather(wlat, wlon, OPENWEATHER_API_KEY)
        judge = judge_outdoor(w)

        msg = f"{judge['label']}  (점수 {judge['score']}/100) · {judge['desc']}"
        if judge["level"] == "good":
            st.success(msg)
        elif judge["level"] == "warn":
            st.warning(msg)
        else:
            st.error(msg)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("기온(°C)", f"{judge['temp']:.1f}")
        c2.metric("체감(°C)", f"{judge['feels']:.1f}")
        c3.metric("바람(m/s)", f"{judge['wind_speed']:.1f}")
        c4.metric("강수(mm/h)", f"{judge['precip_per_h']:.1f}")

        st.progress(int(judge["score"]))

    except Exception as e:
        st.warning("날씨 API 호출 실패. 나중에 다시 시도하세요.")
        st.exception(e)

# ====== Map + Panel ======
col_map, col_panel = st.columns([1.35, 1])

with col_map:
    st.subheader("🗺️ 추천 코스 지도 + 카카오 카페/맥주")
    m = folium.Map(location=[lat, lon], zoom_start=12, tiles="OpenStreetMap")

    s, w_, n, e = bbox
    folium.Rectangle(
        bounds=[[s, w_], [n, e]], color="#0984e3", weight=2, fill=False
    ).add_to(m)

    colors = [
        "#6c5ce7",
        "#00b894",
        "#e17055",
        "#0984e3",
        "#d63031",
        "#e84393",
        "#2d3436",
        "#fdcb6e",
    ]

    selected_name = row["name"]

    for i, r in df_use.iterrows():
        latlon = r["coords"]
        color = colors[i % len(colors)]

        weight = 8 if r["name"] == selected_name else 6
        opacity = 0.95 if r["name"] == selected_name else 0.85

        folium.PolyLine(
            latlon,
            color=color,
            weight=weight,
            opacity=opacity,
            tooltip=f"{i+1}위 {r['name']}",
        ).add_to(m)

        folium.Marker(
            location=[r["end_lat"], r["end_lon"]],
            tooltip=f"{i+1}위 종료점 · {r['difficulty']} · {r['distance_km']}km",
            icon=folium.Icon(color="green", icon="flag"),
        ).add_to(m)

    # Kakao markers
    if kakao_center:
        folium.CircleMarker(
            location=[kakao_center[0], kakao_center[1]],
            radius=6,
            color="#2d3436",
            fill=True,
            fill_color="#2d3436",
            tooltip="카카오 검색 기준점(코스 종료)",
        ).add_to(m)

    for p in kakao_cafe:
        try:
            lat_p = float(p.get("y", 0))
            lon_p = float(p.get("x", 0))
        except Exception:
            continue
        name = p.get("place_name", "")
        addr = p.get("address_name", "")
        url = p.get("place_url", "")
        popup = f"<b>{name}</b><br>{addr}<br><a href='{url}' target='_blank'>상세</a>"
        folium.Marker(
            location=[lat_p, lon_p],
            popup=popup,
            icon=folium.Icon(color="blue", icon="coffee"),
        ).add_to(m)

    for p in kakao_beer:
        try:
            lat_p = float(p.get("y", 0))
            lon_p = float(p.get("x", 0))
        except Exception:
            continue
        name = p.get("place_name", "")
        addr = p.get("address_name", "")
        url = p.get("place_url", "")
        popup = f"<b>{name}</b><br>{addr}<br><a href='{url}' target='_blank'>상세</a>"
        folium.Marker(
            location=[lat_p, lon_p],
            popup=popup,
            icon=folium.Icon(color="red", icon="glass"),
        ).add_to(m)

    st_folium(m, height=620, width=None)

with col_panel:
    st.subheader(f"🏅 추천 Top {len(df_use)}")
    show_cols = ["name", "difficulty", "distance_km", "members", "trust_score", "score"]
    st.dataframe(df_use[show_cols], use_container_width=True, hide_index=True)

    chart = (
        alt.Chart(df_chart)
        .mark_bar()
        .encode(
            x=alt.X("name:N", title="코스"),
            y=alt.Y("score:Q", title="최종 점수(신뢰도 포함)"),
            tooltip=[
                "name",
                "difficulty",
                "distance_km",
                "members",
                "trust_score",
                "score",
            ],
        )
    )
    st.altair_chart(chart, use_container_width=True)

st.divider()

# ====== ORS Elevation ======
st.subheader("⛰️ 고도 그래프")

if show_elevation:
    ors_key = st.secrets.get("ORS_API_KEY", "")
    if not ors_key:
        st.warning("ORS_API_KEY가 Secrets에 없습니다. (Settings → Secrets)")
    else:
        try:
            prof = cached_elevation_profile(row["coords"], ors_key)
        except Exception as e:
            st.error("ORS 고도 요청 실패. 키/쿼터/네트워크를 확인하세요.")
            st.exception(e)
            prof = []

        if prof:
            df_ele = pd.DataFrame(prof)

            ele_chart = (
                alt.Chart(df_ele)
                .mark_line()
                .encode(
                    x=alt.X("dist_km:Q", title="거리(km)"),
                    y=alt.Y("elev_m:Q", title="고도(m)"),
                    tooltip=["dist_km", "elev_m"],
                )
            )
            st.altair_chart(ele_chart, use_container_width=True)

            elev = df_ele["elev_m"].tolist()
            ascent = 0.0
            descent = 0.0
            for i in range(1, len(elev)):
                delta = elev[i] - elev[i - 1]
                if delta > 0:
                    ascent += delta
                else:
                    descent += -delta

            st.write(
                {
                    "min_m": round(float(df_ele["elev_m"].min()), 1),
                    "max_m": round(float(df_ele["elev_m"].max()), 1),
                    "total_ascent_m": round(ascent, 1),
                    "total_descent_m": round(descent, 1),
                    "points": int(len(df_ele)),
                }
            )
        else:
            st.info("이 코스의 고도 데이터를 가져오지 못했습니다.")
else:
    st.caption("사이드바에서 '선택 코스 고도 그래프 보기'를 체크하면 표시됩니다.")

# ====== After trekking ======
st.subheader("🍻 트레킹 후 주변 추천 Top 10")
try:
    places = cached_places(
        float(row["end_lat"]), float(row["end_lon"]), int(near_radius_m)
    )
except Exception as e:
    st.error("주변 장소 조회 실패(Overpass 제한 또는 오류). 나중에 다시 시도하세요.")
    st.exception(e)
    st.stop()

if sip_choice != "전체":
    want = "coffee" if sip_choice.startswith("카페") else "beer"
    places = [p for p in places if p.get("category") == want]

if not places:
    st.info("주변 추천 장소를 찾지 못했습니다. 반경을 늘려보세요.")
else:
    dfp = pd.DataFrame(places[:10])
    keep = [
        "name",
        "category",
        "distance_m",
        "quality_score",
        "combined_score",
        "opening_hours",
        "website",
    ]
    st.dataframe(dfp[keep], use_container_width=True, hide_index=True)

    top_place = places[0]
    emoji = "☕" if top_place["category"] == "coffee" else "🍺"
    st.info(
        f"추천: {emoji} **{top_place['name']}** (약 {top_place['distance_m']}m) — 점수 {top_place['combined_score']}"
    )
