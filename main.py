from __future__ import annotations

from typing import Any, Dict, List, Tuple

import altair as alt
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

import osm_backend as ob
from kakaomap import kakao_keyword_search


st.set_page_config(page_title="SeoulTREK", page_icon="🥾", layout="wide")
st.title("SeoulTREK🥾")
st.markdown(":green[서울의 트래킹 코스를 한눈에]")
st.divider()


# ====== Weather(OpenWeather) ======
OPENWEATHER_API_KEY = st.secrets.get("OPENWEATHER_API_KEY", "")


@st.cache_data(ttl=600)
def get_weather_openweather(lat: float, lon: float, api_key: str) -> Dict[str, Any]:
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


def judge_outdoor(w: Dict[str, Any]) -> Dict[str, Any]:
    main = w.get("main", {})
    wind = w.get("wind", {})
    weather = (w.get("weather") or [{}])[0]
    rain = w.get("rain") or {}
    snow = w.get("snow") or {}

    temp = float(main.get("temp", 0))
    feels = float(main.get("feels_like", temp))
    humidity = float(main.get("humidity", 0))
    wind_speed = float(wind.get("speed", 0))
    desc = weather.get("description", "")

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
    reasons: List[str] = []

    if precip >= 2.0:
        score -= 55
        reasons.append(f"강한 비/눈 ({precip:.1f}mm/h)")
    elif precip >= 0.5:
        score -= 25
        reasons.append(f"약한 비/눈 ({precip:.1f}mm/h)")

    if feels <= -5:
        score -= 35
        reasons.append(f"매우 추움 ({feels:.0f}°C)")
    elif feels <= 0:
        score -= 18
        reasons.append(f"추움 ({feels:.0f}°C)")
    elif feels >= 30:
        score -= 30
        reasons.append(f"더움 ({feels:.0f}°C)")

    if wind_speed >= 10:
        score -= 25
        reasons.append(f"강한 바람 ({wind_speed:.1f}m/s)")
    elif wind_speed >= 7:
        score -= 12
        reasons.append(f"바람이 강함 ({wind_speed:.1f}m/s)")

    if humidity >= 85 and feels >= 25:
        score -= 12
        reasons.append(f"습함 ({humidity:.0f}%)")

    score = max(0, min(100, score))
    if score >= 75:
        level, label = "good", "야외 활동하기 좋아요"
    elif score >= 50:
        level, label = "warn", "괜찮지만 주의가 필요합니다"
    else:
        level, label = "bad", "오늘은 권장하지 않아요"

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
        "reasons": reasons or ["특이 사항 없음"],
    }


def elev_color(elev: float) -> str:
    # 간단 3단계 색상
    if elev < 120:
        return "#2ecc71"  # green
    elif elev < 300:
        return "#f1c40f"  # yellow
    else:
        return "#e67e22"  # orange


def _bounds_from_latlon_list(latlon_list):
    lats = [float(p[0]) for p in latlon_list]
    lons = [float(p[1]) for p in latlon_list]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


# ====== Cached backend ======
@st.cache_data(ttl=60 * 60)
def cached_official_index(
    bbox: Tuple[float, float, float, float],
) -> List[Dict[str, Any]]:
    return ob.load_official_gpx_index("data", bbox=bbox, max_files=1500)


@st.cache_data(ttl=60 * 60)
def cached_courses(
    bbox: Tuple[float, float, float, float], max_relations: int, use_public: bool
) -> pd.DataFrame:
    official_index = cached_official_index(bbox) if use_public else None
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
) -> List[Dict[str, str]]:
    return kakao_keyword_search(
        query=query,
        category=category,
        x=x,
        y=y,
        radius=radius_m,
        size=size,
        api_key=api_key,
    )


def _tooltip_one_line(name: str, distance_km: float, difficulty: str) -> folium.Tooltip:
    html = (
        "<div style='white-space:nowrap; font-size:12px;'>"
        f"<b>{name}</b>&nbsp;&nbsp;·&nbsp;&nbsp;{distance_km:.2f}km&nbsp;&nbsp;·&nbsp;&nbsp;{difficulty}"
        "</div>"
    )
    return folium.Tooltip(html, sticky=True)


def _kakao_popup_compact(name: str, url: str) -> str:
    safe_url = url or "#"
    return (
        "<div style='display:flex; align-items:center; gap:10px; "
        "max-width:260px; white-space:nowrap;'>"
        f"<div style='font-weight:700; overflow:hidden; text-overflow:ellipsis;'>{name}</div>"
        f"<a href='{safe_url}' target='_blank' style='text-decoration:none;'>상세보기</a>"
        "</div>"
    )


# ====== Sidebar ======
with st.sidebar:
    st.header("1) 지역 선택")
    preset = st.selectbox(
        "프리셋",
        ["서울 전체", "용산구", "도봉/노원", "동작/영등포", "강남구", "사용자 지정"],
    )

    if preset == "사용자 지정":
        lat = st.number_input("중심 위도", value=37.5665, format="%.6f")
        lon = st.number_input("중심 경도", value=126.9780, format="%.6f")
        radius_km = st.slider("반경 (km)", 2.0, 30.0, 12.0, 0.5)
    else:
        presets = {
            "서울 전체": (37.5665, 126.9780, 18.0),
            "용산구": (37.5512, 126.9882, 8.0),
            "도봉/노원": (37.6584, 126.9800, 12.0),
            "동작/영등포": (37.5250, 126.9250, 10.0),
            "강남구": (37.4840, 127.0350, 10.0),
        }
        lat, lon, radius_km = presets[preset]

    st.header("2) 난이도(중복 선택)")
    diff_filter = st.multiselect(
        "난이도(중복 선택 가능)",
        ["쉬움", "보통", "어려움"],
        default=["쉬움", "보통", "어려움"],
    )

    topk = st.slider("추천 코스 개수", 3, 10, 4)
    max_relations = st.slider("Overpass 최대 관계 수", 20, 80, 50, 5)

    st.header("3) 공공데이터 반영")
    use_public = st.checkbox("공공데이터 매칭 반영", value=True)

    st.header("4) 카카오 카페/맥주 마커")
    show_kakao = st.checkbox("카카오 마커 표시", value=True)
    kakao_radius_m = st.slider("카카오 검색 반경 (m)", 200, 5000, 1200, 100)
    kakao_size = st.slider("카카오 결과 수", 5, 20, 10, 1)

    st.header("5) 고도 그래프/지도 색칠")
    show_elevation = st.checkbox("고도 그래프 표시", value=True)

    st.divider()
    if st.button("캐시 초기화", use_container_width=True):
        st.cache_data.clear()
        st.success("캐시가 초기화되었습니다. 다시 실행해보세요.")


# ====== Load courses ======
bbox = ob.bbox_from_center(lat, lon, radius_km)

with st.status("코스 불러오는 중...", expanded=False) as status:
    try:
        df = cached_courses(bbox, max_relations=max_relations, use_public=use_public)
        status.update(label=f"코스 로딩 완료 ({len(df)})", state="complete")
    except Exception as e:
        status.update(label="코스 로딩 실패", state="error")
        st.error("서버 제한(429) 또는 일시적 오류입니다. 다시 시도해주세요.")
        st.exception(e)
        st.stop()

if df.empty:
    st.error(
        "이 지역에서 코스를 찾지 못했습니다. 반경을 늘리거나 다른 지역을 선택하세요."
    )
    st.stop()

# difficulty filter (중복 선택 가능)
df_use = df[df["difficulty"].isin(diff_filter)].copy() if diff_filter else df.copy()

if df_use.empty:
    st.info(
        "선택한 난이도의 코스가 없습니다. 난이도 선택을 바꾸거나 반경을 늘려보세요."
    )
    st.stop()

df_use = df_use.sort_values("score", ascending=False).head(topk).reset_index(drop=True)

selected = st.selectbox("상세로 볼 코스 선택", df_use["name"].tolist(), index=0)
row = df_use[df_use["name"] == selected].iloc[0].to_dict()

# ====== Kakao places (near selected course end) ======
kakao_beer: List[Dict[str, str]] = []
kakao_cafe: List[Dict[str, str]] = []
kakao_center: Tuple[float, float] | None = None

if show_kakao:
    try:
        kakao_key = st.secrets.get("KAKAO_REST_API_KEY", "") or st.secrets.get(
            "KAKAO_REST_KEY", ""
        )
        if kakao_key:
            end_lon = float(row["end_lon"])
            end_lat = float(row["end_lat"])
            kakao_center = (end_lat, end_lon)

            kakao_beer = cached_kakao_places(
                query="맥주",
                category="FD6",
                x=end_lon,
                y=end_lat,
                radius_m=int(kakao_radius_m),
                size=int(kakao_size),
                api_key=kakao_key,
            )
            kakao_cafe = cached_kakao_places(
                query="카페",
                category="CE7",
                x=end_lon,
                y=end_lat,
                radius_m=int(kakao_radius_m),
                size=int(kakao_size),
                api_key=kakao_key,
            )
        else:
            st.sidebar.info("KAKAO_REST_API_KEY가 없어 카카오 마커를 숨깁니다.")
    except Exception as e:
        st.sidebar.warning(
            "Kakao Local 호출 실패. API 키와 네트워크/IP 제한을 확인하세요."
        )
        st.sidebar.exception(e)

# ====== Elevation (for panel + selected route coloring) ======
ors_key = st.secrets.get("ORS_API_KEY", "")
prof: List[Dict[str, Any]] = []
elev_available = False

if show_elevation and ors_key:
    try:
        prof = cached_elevation_profile(row["coords"], ors_key) or []
        # lat/lon/elev_m이 있어야 지도 색칠 가능
        elev_available = (
            len(prof) >= 2
            and isinstance(prof[0], dict)
            and ("lat" in prof[0] and "lon" in prof[0] and "elev_m" in prof[0])
        )
    except Exception:
        prof = []
        elev_available = False

# ====== Layout ======
col_map, col_side = st.columns([1.35, 1], gap="large")

with col_map:
    st.subheader("추천 코스")

    # ✅ 초기 location은 선택 코스 시작점으로
    map_center = [float(row["start_lat"]), float(row["start_lon"])]
    m = folium.Map(location=map_center, zoom_start=13, tiles="OpenStreetMap")

    # bbox outline
    s, w_, n, e = bbox
    folium.Rectangle(
        bounds=[[s, w_], [n, e]], color="#0984e3", weight=2, fill=False
    ).add_to(m)

    # draw routes
    selected_name = row["name"]
    for i, r in df_use.iterrows():
        is_selected = r["name"] == selected_name

        # ✅ 선택 코스는 고도(ORS) 프로파일이 있으면 구간별 색칠
        if is_selected and elev_available and isinstance(prof, list) and len(prof) >= 2:
            pts = []
            for p in prof:
                try:
                    pts.append((float(p["lat"]), float(p["lon"]), float(p["elev_m"])))
                except Exception:
                    pts = []
                    break

            if len(pts) >= 2:
                for j in range(len(pts) - 1):
                    lat1, lon1, e1 = pts[j]
                    lat2, lon2, _ = pts[j + 1]
                    folium.PolyLine(
                        [(lat1, lon1), (lat2, lon2)],
                        color=elev_color(e1),
                        weight=8,
                        opacity=0.95,
                        tooltip=_tooltip_one_line(
                            str(r["name"]),
                            float(r["distance_km"]),
                            str(r["difficulty"]),
                        ),
                    ).add_to(m)
                continue  # 선택 코스는 이미 그렸으니 다음 코스로

        # 나머지(또는 고도 데이터 없을 때)는 단색
        latlon = r["coords"]
        color = "#2ecc71" if is_selected else "#6c5ce7"
        weight = 8 if is_selected else 5
        opacity = 0.95 if is_selected else 0.75

        folium.PolyLine(
            latlon,
            color=color,
            weight=weight,
            opacity=opacity,
            tooltip=_tooltip_one_line(
                str(r["name"]), float(r["distance_km"]), str(r["difficulty"])
            ),
        ).add_to(m)

    # 선택 코스 출발/도착(코스명 포함)
    folium.Marker(
        location=[float(row["start_lat"]), float(row["start_lon"])],
        tooltip=f"출발: {selected_name}",
        icon=folium.Icon(color="blue", icon="play"),
    ).add_to(m)
    folium.Marker(
        location=[float(row["end_lat"]), float(row["end_lon"])],
        tooltip=f"도착: {selected_name}",
        icon=folium.Icon(color="red", icon="flag"),
    ).add_to(m)

    # Kakao 기준점
    if kakao_center:
        folium.CircleMarker(
            location=[kakao_center[0], kakao_center[1]],
            radius=5,
            color="#2d3436",
            fill=True,
            fill_color="#2d3436",
            tooltip="카카오 검색 기준점",
        ).add_to(m)

    # 맥주: 보라 / 카페: 분홍
    for p in kakao_beer:
        try:
            lat_p = float(p.get("y", 0))
            lon_p = float(p.get("x", 0))
        except Exception:
            continue
        name = p.get("place_name", "") or "맥주"
        url = p.get("place_url", "")
        folium.Marker(
            location=[lat_p, lon_p],
            popup=_kakao_popup_compact(name, url),
            icon=folium.Icon(color="purple", icon="glass"),
        ).add_to(m)

    for p in kakao_cafe:
        try:
            lat_p = float(p.get("y", 0))
            lon_p = float(p.get("x", 0))
        except Exception:
            continue
        name = p.get("place_name", "") or "카페"
        url = p.get("place_url", "")
        folium.Marker(
            location=[lat_p, lon_p],
            popup=_kakao_popup_compact(name, url),
            icon=folium.Icon(color="pink", icon="coffee"),
        ).add_to(m)

    # ✅ 선택 코스 화면에 맞춰 자동 이동/줌 (선택 코스 기준)
    try:
        if elev_available and isinstance(prof, list) and len(prof) >= 2:
            sel_latlon = [(float(p["lat"]), float(p["lon"])) for p in prof]
        else:
            sel_latlon = [(float(a), float(b)) for (a, b) in row["coords"]]

        m.fit_bounds(_bounds_from_latlon_list(sel_latlon), padding=(20, 20))
    except Exception:
        pass

    st_folium(m, height=620, width=None)

with col_side:
    st.subheader("날씨 / 야외 적합도")
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
            st.warning("날씨 API 호출 실패. 네트워크/키를 확인하세요.")
            st.exception(e)

    st.subheader("고도 그래프")
    if not show_elevation:
        st.caption("사이드바에서 '고도 그래프 표시'를 켜면 표시됩니다.")
    elif not ors_key:
        st.info("ORS_API_KEY가 없어 고도 그래프를 표시할 수 없습니다.")
    elif not elev_available:
        st.info("이 루트는 고도 정보가 없습니다.")
    else:
        df_ele = pd.DataFrame(prof)
        st.markdown(
            """
        <div style="display:flex; justify-content:space-between; width:100%; font-size:0.85rem; color:rgba(49,51,63,0.6);">
        <span>⬅️ 시작점</span>
        <span>도착점 ➡️</span>
        </div>
        """,
            unsafe_allow_html=True,
        )

        ele_chart = (
            alt.Chart(df_ele)
            .mark_line()
            .encode(
                x=alt.X("dist_km:Q", title="거리(km)"),
                y=alt.Y("elev_m:Q", title="고도(m)"),
                tooltip=["dist_km", "elev_m"],
            )
            .properties(height=260)
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

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("최저(m)", f"{float(df_ele['elev_m'].min()):.0f}")
        m2.metric("최고(m)", f"{float(df_ele['elev_m'].max()):.0f}")
        m3.metric("올라간 거리(m)", f"{ascent:.0f}")
        m4.metric("내려간 거리(m)", f"{descent:.0f}")


st.divider()

# ====== 아래(전체 폭): 추천코스 정보 / 점수(가중치) ======
st.subheader("추천코스 정보 / 점수(가중치)")

show_cols = [
    "name",
    "difficulty",
    "distance_km",
    "score",
    "score_osm",
    "trust_score",
    "official_matched",
]

exist_cols = [c for c in show_cols if c in df_use.columns]
st.dataframe(df_use[exist_cols], use_container_width=True, hide_index=True)
