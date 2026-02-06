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


# =========================
# Weather (OpenWeather)
# =========================
OPENWEATHER_API_KEY = st.secrets.get("OPENWEATHER_API_KEY", "")


@st.cache_data(ttl=600)  # 10 min cache
def get_weather_openweather(lat: float, lon: float, api_key: str):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lon, "appid": api_key, "units": "metric", "lang": "kr"}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def judge_outdoor(w: Dict[str, Any]) -> Dict[str, Any]:
    """Outdoor suitability score (0~100)."""
    main = w.get("main", {}) or {}
    wind = w.get("wind", {}) or {}
    weather = (w.get("weather") or [{}])[0] or {}
    rain = w.get("rain") or {}
    snow = w.get("snow") or {}

    temp = float(main.get("temp", 0))
    feels = float(main.get("feels_like", temp))
    humidity = float(main.get("humidity", 0))
    wind_speed = float(wind.get("speed", 0))  # m/s
    desc = str(weather.get("description", ""))

    # Precipitation per hour (mm)
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

    # Rain
    if precip >= 2.0:
        score -= 55
        reasons.append(f"강한 비 ({precip:.1f}mm/h)")
    elif precip >= 0.5:
        score -= 25
        reasons.append(f"약한 비 ({precip:.1f}mm/h)")

    # Feels-like temperature
    if feels <= -5:
        score -= 35
        reasons.append(f"매우 추움 ({feels:.0f}°C)")
    elif feels <= 0:
        score -= 18
        reasons.append(f"추움 ({feels:.0f}°C)")
    elif feels >= 30:
        score -= 30
        reasons.append(f"더움 ({feels:.0f}°C)")

    # Wind
    if wind_speed >= 10:
        score -= 25
        reasons.append(f"강한 바람 ({wind_speed:.1f}m/s)")
    elif wind_speed >= 7:
        score -= 12
        reasons.append(f"바람이 강함 ({wind_speed:.1f}m/s)")

    # Humidity
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


# =========================
# Cached backend calls
# =========================
@st.cache_data(ttl=60 * 60)
def cached_courses(
    bbox: Tuple[float, float, float, float], max_relations: int
) -> pd.DataFrame:
    courses = ob.build_courses(bbox, max_relations=max_relations)
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
def cached_elevation_profile(
    coords_latlon: List[Tuple[float, float]], ors_api_key: str
):
    return ob.elevation_profile(coords_latlon, api_key=ors_api_key)


@st.cache_data(ttl=60 * 60)
def cached_elevation_line(coords_latlon: List[Tuple[float, float]], ors_api_key: str):
    # (lat, lon, elev_m) list
    return ob.ors_elevation_line(coords_latlon, api_key=ors_api_key)


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


# =========================
# UI
# =========================
st.set_page_config(page_title="트레킹 코스 추천", page_icon="🥾", layout="wide")
st.title("🥾 트레킹 코스 추천")
st.caption("추천 루트 + (선택 시) 카카오 카페/맥주 마커 + 날씨/고도/점수 설명")


# =========================
# Sidebar
# =========================
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

    st.header("2) 추천/난이도")
    diff_filter = st.radio("난이도", ["전체", "쉬움", "보통", "어려움"], index=0)
    topk = st.slider("추천 코스 개수", 3, 10, 4)
    max_relations = st.slider("Overpass 최대 관계 수", 20, 80, 50, 5)

    st.header("3) 주변 추천(Overpass)")
    near_radius_m = st.slider("주변 반경 (m)", 100, 2000, 700, 50)
    sip_choice = st.radio("종류", ["전체", "카페", "맥주"], horizontal=True)

    st.header("4) 고도(ORS)")
    use_elevation = st.checkbox("고도 데이터 사용", value=True)

    st.header("5) 카카오 카페/맥주")
    show_kakao = st.checkbox("카카오 마커 표시", value=True)
    kakao_radius_m = st.slider("카카오 검색 반경 (m)", 200, 5000, 1200, 100)
    kakao_size = st.slider("카카오 결과 수", 5, 20, 10, 1)

    st.divider()

    if st.button("캐시 초기화", use_container_width=True):
        st.cache_data.clear()
        st.success("캐시가 초기화되었습니다. 필요하면 다시 실행하세요.")


# =========================
# Load courses
# =========================
bbox = ob.bbox_from_center(lat, lon, radius_km)

with st.status("코스 불러오는 중...", expanded=False) as status:
    try:
        df = cached_courses(bbox, max_relations=max_relations)
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

# difficulty filter (applies to list + map)
if diff_filter != "전체":
    df_use = df[df["difficulty"] == diff_filter].copy()
else:
    df_use = df.copy()

if df_use.empty:
    st.info("선택한 난이도의 코스가 없습니다. 다른 난이도를 선택하세요.")
    st.stop()

df_use = df_use.sort_values("score", ascending=False).head(topk).reset_index(drop=True)

# (important) select a course before map/panels
selected = st.selectbox("상세로 볼 코스 선택", df_use["name"].tolist(), index=0)
row = df_use[df_use["name"] == selected].iloc[0].to_dict()


# =========================
# Kakao places (near selected course end)
# =========================
kakao_food: List[Dict[str, str]] = []
kakao_cafe: List[Dict[str, str]] = []
kakao_center: Tuple[float, float] | None = None

if show_kakao:
    try:
        kakao_key = st.secrets.get("KAKAO_REST_API_KEY", "") or st.secrets.get(
            "KAKAO_REST_KEY", ""
        )
        if not kakao_key:
            st.info("KAKAO_REST_API_KEY가 없어 카카오 마커를 숨깁니다.")
        else:
            end_lon = float(row["end_lon"])
            end_lat = float(row["end_lat"])
            kakao_center = (end_lat, end_lon)

            # 맥주(술집/호프 포함되게 '맥주' 키워드 + 음식점(FD6))
            kakao_food = cached_kakao_places(
                query="맥주",
                category="FD6",
                x=end_lon,
                y=end_lat,
                radius_m=int(kakao_radius_m),
                size=int(kakao_size),
                api_key=kakao_key,
            )
            # 카페(CE7)
            kakao_cafe = cached_kakao_places(
                query="카페",
                category="CE7",
                x=end_lon,
                y=end_lat,
                radius_m=int(kakao_radius_m),
                size=int(kakao_size),
                api_key=kakao_key,
            )
    except Exception as e:
        st.warning("Kakao Local 호출 실패. API 키와 IP 제한을 확인하세요.")
        st.exception(e)


# =========================
# Elevation data for selected route
# =========================
ors_key = st.secrets.get("ORS_API_KEY", "")
has_elev = False
coords3d: List[Tuple[float, float, float]] = []
prof: List[Dict[str, float]] = []

if use_elevation and ors_key:
    try:
        coords3d = cached_elevation_line(row["coords"], ors_key)
        prof = cached_elevation_profile(row["coords"], ors_key)
        has_elev = bool(coords3d) and bool(prof)
    except Exception:
        has_elev = False
else:
    has_elev = False


def elev_color(norm01: float) -> str:
    # green -> yellow -> orange
    if norm01 <= 0.33:
        return "#2ecc71"
    if norm01 <= 0.66:
        return "#f1c40f"
    return "#e67e22"


# =========================
# Layout: Map (left) + Info Panel (right)
# =========================
col_map, col_panel = st.columns([1.45, 1])

with col_map:
    st.subheader("🗺️ 추천 코스 지도 (OpenStreetMap)")
    m = folium.Map(location=[lat, lon], zoom_start=12, tiles="OpenStreetMap")

    # bbox rectangle
    s, w_, n, e = bbox
    folium.Rectangle(
        bounds=[[s, w_], [n, e]], color="#0984e3", weight=2, fill=False
    ).add_to(m)

    # draw routes
    if not has_elev:
        # 고도 데이터 없으면: 루트 전부 초록색
        for _, r in df_use.iterrows():
            folium.PolyLine(
                r["coords"],
                color="#2ecc71",
                weight=7 if r["name"] == selected else 5,
                opacity=0.95 if r["name"] == selected else 0.8,
                tooltip=f"코스: {r['name']} (점수 {r['score']})",
            ).add_to(m)
    else:
        # 다른 루트는 중립색, 선택 루트는 고도 기반 세그먼트 컬러
        for _, r in df_use.iterrows():
            if r["name"] == selected:
                continue
            folium.PolyLine(
                r["coords"],
                color="#636e72",
                weight=4,
                opacity=0.55,
                tooltip=f"코스: {r['name']} (점수 {r['score']})",
            ).add_to(m)

        # selected route segments
        elevs = [p[2] for p in coords3d] if coords3d else []
        if elevs:
            mn, mx = min(elevs), max(elevs)
            rng = (mx - mn) if (mx - mn) > 1e-6 else 1.0

            for i in range(1, len(coords3d)):
                a = coords3d[i - 1]
                b = coords3d[i]
                seg_e = (a[2] + b[2]) / 2.0
                norm = (seg_e - mn) / rng
                folium.PolyLine(
                    [(a[0], a[1]), (b[0], b[1])],
                    color=elev_color(norm),
                    weight=8,
                    opacity=0.95,
                    tooltip=f"{selected} | elev {seg_e:.0f}m",
                ).add_to(m)
        else:
            folium.PolyLine(
                row["coords"], color="#2ecc71", weight=8, opacity=0.95
            ).add_to(m)

    # start/end markers for selected (blue/red)
    folium.Marker(
        location=[float(row["start_lat"]), float(row["start_lon"])],
        tooltip=f"출발: {selected}",
        icon=folium.Icon(color="blue", icon="play"),
    ).add_to(m)

    folium.Marker(
        location=[float(row["end_lat"]), float(row["end_lon"])],
        tooltip=f"도착: {selected}",
        icon=folium.Icon(color="red", icon="flag"),
    ).add_to(m)

    # Kakao markers (cafe/beer)
    if kakao_center:
        folium.CircleMarker(
            location=[kakao_center[0], kakao_center[1]],
            radius=6,
            color="#2d3436",
            fill=True,
            fill_color="#2d3436",
            tooltip="카카오 검색 기준점(코스 종료)",
        ).add_to(m)

    # beer
    for p in kakao_food:
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

    # cafe
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

    st_folium(m, height=640, width=None)

with col_panel:
    st.subheader("📌 선택 코스 요약")

    c1, c2, c3 = st.columns(3)
    c1.metric("거리(km)", f"{float(row['distance_km']):.2f}")
    c2.metric("난이도", str(row["difficulty"]))
    c3.metric("추천점수", f"{float(row['score']):.3f}")

    c4, c5, c6 = st.columns(3)
    c4.metric("OSM 점수", f"{float(row.get('score_osm', 0)):.3f}")
    c5.metric("신뢰도(trust)", f"{float(row.get('trust_score', 0)):.3f}")
    c6.metric("멤버수", f"{int(row.get('members', 0))}")

    # 1) Weather panel
    st.markdown("### 🌤️ 날씨 / 야외 적합도 (코스 시작점 기준)")
    if not OPENWEATHER_API_KEY:
        st.info("OPENWEATHER_API_KEY가 없어 날씨 패널을 숨깁니다.")
    else:
        wlat, wlon = float(row["start_lat"]), float(row["start_lon"])
        try:
            w = get_weather_openweather(wlat, wlon, OPENWEATHER_API_KEY)
            judge = judge_outdoor(w)

            if judge["level"] == "good":
                st.success(
                    f"{judge['label']}  (점수 {judge['score']}/100) · {judge['desc']}"
                )
            elif judge["level"] == "warn":
                st.warning(
                    f"{judge['label']}  (점수 {judge['score']}/100) · {judge['desc']}"
                )
            else:
                st.error(
                    f"{judge['label']}  (점수 {judge['score']}/100) · {judge['desc']}"
                )

            wc1, wc2, wc3, wc4 = st.columns(4)
            wc1.metric("기온(°C)", f"{judge['temp']:.1f}")
            wc2.metric("체감(°C)", f"{judge['feels']:.1f}")
            wc3.metric("바람(m/s)", f"{judge['wind_speed']:.1f}")
            wc4.metric("강수(mm/h)", f"{judge['precip_per_h']:.1f}")

            st.progress(int(judge["score"]))
            with st.expander("판정 근거 보기", expanded=False):
                st.write(judge["reasons"])
        except Exception as e:
            st.warning("날씨 API 호출 실패. 나중에 다시 시도하세요.")
            st.exception(e)

    # 2) Elevation panel (always shown here)
    st.markdown("### 🏔️ 고도 그래프")
    if not use_elevation:
        st.info("사이드바에서 '고도 데이터 사용'을 켜면 고도 그래프/색상이 표시됩니다.")
    elif not ors_key:
        st.info(
            "ORS_API_KEY가 없어 고도 그래프를 표시할 수 없습니다. (Secrets에 ORS_API_KEY 추가)"
        )
    elif not has_elev:
        st.info("이 루트는 고도 정보가 없습니다.")
    else:
        df_ele = pd.DataFrame(prof)
        ele_chart = (
            alt.Chart(df_ele)
            .mark_line()
            .encode(
                x=alt.X("dist_km:Q", title="거리 (km)"),
                y=alt.Y("elev_m:Q", title="고도 (m)"),
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

    # 3) Score breakdown (weights)
    st.markdown("### 🧮 점수(가중치) 설명")
    bd = row.get("score_breakdown") or {}
    if bd:
        df_bd = pd.DataFrame(
            [
                {
                    "항목": "members_term",
                    "값": bd.get("members_term", 0),
                    "설명": "log1p(멤버수) * 0.8",
                },
                {
                    "항목": "distance_term",
                    "값": bd.get("distance_term", 0),
                    "설명": "log1p(거리km) * 0.6",
                },
                {
                    "항목": "osm_score",
                    "값": bd.get("osm_score", 0),
                    "설명": "OSM 점수 합",
                },
                {
                    "항목": "trust_score",
                    "값": bd.get("trust_score", 0),
                    "설명": "공공데이터 매칭 가산점",
                },
                {
                    "항목": "final_score",
                    "값": bd.get("final_score", row.get("score", 0)),
                    "설명": "최종 점수",
                },
            ]
        )
        st.dataframe(df_bd, use_container_width=True, hide_index=True)
        st.caption(f"수식: {bd.get('formula', '')}")
    else:
        st.info(
            "점수 분해 데이터를 찾지 못했습니다. (백엔드 업데이트가 필요할 수 있어요)"
        )

    # 4) Recommend list + chart (kept in right panel)
    st.markdown("### 📋 추천 Top 목록")
    show_cols = ["name", "difficulty", "distance_km", "members", "score"]
    st.dataframe(df_use[show_cols], use_container_width=True, hide_index=True)

    df_chart = df_use[["name", "distance_km", "score"]].copy()
    chart = (
        alt.Chart(df_chart)
        .mark_bar()
        .encode(
            x=alt.X("name:N", title="코스"),
            y=alt.Y("distance_km:Q", title="거리 (km)"),
            tooltip=["name", "distance_km", "score"],
        )
    )
    st.altair_chart(chart, use_container_width=True)

st.divider()

# =========================
# After trekking (Overpass places near end)
# =========================
st.subheader("트레킹 후 주변 추천 Top 10 (Overpass)")
try:
    places = cached_places(
        float(row["end_lat"]), float(row["end_lon"]), int(near_radius_m)
    )
except Exception as e:
    st.error("주변 장소 조회 실패(Overpass 제한 또는 오류). 나중에 다시 시도하세요.")
    st.exception(e)
    st.stop()

if sip_choice != "전체":
    want = "coffee" if sip_choice == "카페" else "beer"
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
    st.info(
        f"추천: {top_place['name']} ({top_place['distance_m']}m) · 점수 {top_place['combined_score']}"
    )
