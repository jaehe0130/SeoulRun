from __future__ import annotations
from typing import Any, Dict, List, Optional
import re

import altair as alt
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

import osm_backend as ob
from kakaomap import kakao_keyword_search


# ======================================================
# 고정 설정값 (UI에서 제거된 값들)
# ======================================================
TOPK = 4  # 추천 코스 개수 (고정)
MAX_RELATIONS = 50  # 후보 탐색량 (고정)

PUBLIC_DATA_FILES = 1500  # 공공데이터 파일 수 (고정)

KAKAO_RADIUS = 2000  # 카카오 검색 반경 (고정)
KAKAO_SIZE = 10  # 카카오 결과 수 (고정)


# ======================================================
# Page
# ======================================================
st.set_page_config(
    page_title="트레킹 코스 추천",
    page_icon="🥾",
    layout="wide",
)
st.title("🥾 트레킹 코스 추천")


# ======================================================
# Weather
# ======================================================
OPENWEATHER_API_KEY = st.secrets.get("OPENWEATHER_API_KEY", "")


@st.cache_data(ttl=600)
def get_weather(lat: float, lon: float) -> Dict[str, Any]:
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

    return {
        "score": max(0, min(100, score)),
        "desc": desc,
        "temp": temp,
        "feels": feels,
        "wind": wind_speed,
        "rain": precip,
    }


# ======================================================
# Elevation
# ======================================================
def elev_color(elev: float) -> str:
    # 초록 / 노랑 / 주황
    if elev < 120:
        return "#2ecc71"
    elif elev < 300:
        return "#f1c40f"
    else:
        return "#e67e22"


@st.cache_data(ttl=3600)
def cached_elevation(coords, api_key: str):
    return ob.elevation_profile(coords, api_key=api_key)


# ======================================================
# 공공데이터(GPX) 인덱스 로드 (공공데이터 토글 실제 반영)
# ======================================================
@st.cache_data(ttl=60 * 60)
def cached_official_index(data_dir: str, bbox, max_files: int = 1500):
    return ob.load_official_gpx_index(
        data_dir=data_dir,
        bbox=bbox,
        max_files=max_files,
    )


# ======================================================
# Sidebar
# ======================================================
with st.sidebar:
    st.header("지역 선택")
    lat = st.number_input("위도", value=37.5665, format="%.6f")
    lon = st.number_input("경도", value=126.9780, format="%.6f")
    radius_km = st.slider("반경 (km)", 3.0, 25.0, 10.0)

    st.divider()
    st.header("공공데이터 반영")
    use_public = st.toggle("공공데이터 매칭 사용", value=True)

    st.divider()
    st.header("난이도")
    diff_filter = st.multiselect(
        "난이도 선택",
        ["쉬움", "보통", "어려움"],
        default=["쉬움", "보통", "어려움"],
    )

    st.divider()
    st.header("추천 종류")
    sip_choice = st.selectbox("추천 종류", ["전체", "카페", "맥주"])

    st.divider()
    show_kakao = st.toggle("카카오 카페/맥주 마커 표시", value=True)


# ======================================================
# Load courses
# ======================================================
bbox = ob.bbox_from_center(lat, lon, radius_km)

official_index = None
if use_public:
    official_index = cached_official_index(
        data_dir="data",
        bbox=bbox,
        max_files=PUBLIC_DATA_FILES,
    )

df = pd.DataFrame(
    ob.build_courses(
        bbox,
        max_relations=MAX_RELATIONS,
        official_index=official_index,  # ✅ 공공데이터 반영 핵심
    )
)

if df.empty:
    st.error("추천 코스를 찾지 못했습니다.")
    st.stop()

# 난이도 필터 (중복 선택)
df = df[df["difficulty"].isin(diff_filter)].copy()
df = df.sort_values("score", ascending=False).head(TOPK).reset_index(drop=True)

course_options = df["name"].tolist()
if not course_options:
    st.error(
        "선택한 난이도 조건에서 코스를 찾지 못했습니다. 난이도를 다시 선택해 주세요."
    )
    st.stop()


# ======================================================
# 선택 코스 상태 (필터 변경에도 안전)
# ======================================================
if (
    "selected_course" not in st.session_state
    or st.session_state["selected_course"] not in course_options
):
    st.session_state["selected_course"] = course_options[0]

selected_name = st.selectbox(
    "상세로 볼 코스 선택",
    course_options,
    index=course_options.index(st.session_state["selected_course"]),
)

st.session_state["selected_course"] = selected_name
row = df[df["name"] == selected_name].iloc[0]


# ======================================================
# Kakao
# ======================================================
kakao_food, kakao_cafe = [], []
kakao_key = st.secrets.get("KAKAO_REST_API_KEY", "")

if show_kakao and kakao_key:
    if sip_choice in ("전체", "맥주"):
        kakao_food = kakao_keyword_search(
            query="맥주",
            category="FD6",
            x=row["end_lon"],
            y=row["end_lat"],
            radius=KAKAO_RADIUS,
            size=KAKAO_SIZE,
            api_key=kakao_key,
        )
    if sip_choice in ("전체", "카페"):
        kakao_cafe = kakao_keyword_search(
            query="카페",
            category="CE7",
            x=row["end_lon"],
            y=row["end_lat"],
            radius=KAKAO_RADIUS,
            size=KAKAO_SIZE,
            api_key=kakao_key,
        )


# ======================================================
# Layout
# ======================================================
col_map, col_info = st.columns([1.4, 1])


# ======================================================
# MAP
# ======================================================
with col_map:
    m = folium.Map(location=[lat, lon], zoom_start=12)

    # 선택 코스 고도만 가져와서 지도에 색칠
    ors_key = st.secrets.get("ORS_API_KEY", "")
    elev_profile: List[Dict[str, Any]] = []
    if ors_key:
        try:
            elev_profile = cached_elevation(row["coords"], ors_key)
        except Exception:
            elev_profile = []

    # 코스 그리기
    for _, r in df.iterrows():
        latlon = r["coords"]
        is_selected = r["name"] == selected_name

        # 코스 오버 툴팁(한 줄)
        line_tooltip = f"{r['name']} · {r['distance_km']}km · {r['difficulty']}"

        if is_selected and elev_profile:
            # 선택 코스만 고도 기반 세그먼트 컬러
            elevs = [float(p.get("elev_m", 0.0)) for p in elev_profile]
            n = min(len(latlon), len(elevs))
            if n >= 2:
                for i in range(n - 1):
                    folium.PolyLine(
                        [latlon[i], latlon[i + 1]],
                        color=elev_color(elevs[i]),
                        weight=8,
                        opacity=0.95,
                        tooltip=line_tooltip,
                    ).add_to(m)
            else:
                folium.PolyLine(
                    latlon,
                    color="#2ecc71",
                    weight=8,
                    opacity=0.95,
                    tooltip=line_tooltip,
                ).add_to(m)
        else:
            # 나머지 코스는 초록 단색
            folium.PolyLine(
                latlon,
                color="#2ecc71",
                weight=8 if is_selected else 5,
                opacity=0.9,
                tooltip=line_tooltip,
            ).add_to(m)

        # 마커 클릭으로 코스 선택 가능하게: popup에 숨은 토큰 심기
        # (st_folium이 tooltip을 항상 반환하진 않아서 popup 기반이 더 안정적)
        course_token = f"__COURSE__:{r['name']}"
        start_popup = (
            f"<div style='white-space:nowrap;'>"
            f"<b>[출발]</b> {r['name']}"
            f"<span style='display:none'>{course_token}</span>"
            f"</div>"
        )
        end_popup = (
            f"<div style='white-space:nowrap;'>"
            f"<b>[도착]</b> {r['name']}"
            f"<span style='display:none'>{course_token}</span>"
            f"</div>"
        )

        folium.Marker(
            [r["start_lat"], r["start_lon"]],
            icon=folium.Icon(color="blue", icon="play"),
            tooltip=f"[출발] {r['name']}",
            popup=folium.Popup(start_popup, max_width=300),
        ).add_to(m)

        folium.Marker(
            [r["end_lat"], r["end_lon"]],
            icon=folium.Icon(color="red", icon="flag"),
            tooltip=f"[도착] {r['name']}",
            popup=folium.Popup(end_popup, max_width=300),
        ).add_to(m)

    # Kakao markers
    for p in kakao_food:
        try:
            lat_p = float(p.get("y", 0))
            lon_p = float(p.get("x", 0))
        except Exception:
            continue
        folium.Marker(
            [lat_p, lon_p],
            icon=folium.Icon(color="purple", icon="glass"),
            popup=folium.Popup(
                f"<div style='white-space:nowrap;'><b>{p.get('place_name','')}</b> · "
                f"<a href='{p.get('place_url','')}' target='_blank'>상세보기</a></div>",
                max_width=350,
            ),
        ).add_to(m)

    for p in kakao_cafe:
        try:
            lat_p = float(p.get("y", 0))
            lon_p = float(p.get("x", 0))
        except Exception:
            continue
        folium.Marker(
            [lat_p, lon_p],
            icon=folium.Icon(color="pink", icon="coffee"),
            popup=folium.Popup(
                f"<div style='white-space:nowrap;'><b>{p.get('place_name','')}</b> · "
                f"<a href='{p.get('place_url','')}' target='_blank'>상세보기</a></div>",
                max_width=350,
            ),
        ).add_to(m)

    map_out = st_folium(
        m,
        height=650,
        use_container_width=True,
        returned_objects=["last_object_clicked_popup", "last_object_clicked"],
    )

    # 마커 클릭으로 코스 선택
    popup_text = (map_out or {}).get("last_object_clicked_popup")
    if popup_text:
        mobj = re.search(r"__COURSE__:(.+)", str(popup_text))
        if mobj:
            clicked_name = mobj.group(1).strip()
            if (
                clicked_name in course_options
                and clicked_name != st.session_state["selected_course"]
            ):
                st.session_state["selected_course"] = clicked_name
                st.experimental_rerun()


# ======================================================
# RIGHT PANEL – Weather & Elevation
# ======================================================
with col_info:
    st.subheader("날씨 / 야외 적합도")

    if OPENWEATHER_API_KEY:
        try:
            w = get_weather(float(row["start_lat"]), float(row["start_lon"]))
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
        except Exception:
            st.warning("날씨 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
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
                x=alt.X("dist_km:Q", title="거리(km)"),
                y=alt.Y("elev_m:Q", title="고도(m)"),
                tooltip=["dist_km", "elev_m"],
            )
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("고도 정보가 없습니다.")


# ======================================================
# Bottom – course list
# ======================================================
st.divider()
st.subheader("추천 코스")

st.dataframe(
    df[["name", "difficulty", "distance_km", "members", "score"]],
    use_container_width=True,
    hide_index=True,
)
