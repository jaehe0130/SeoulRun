from __future__ import annotations

from typing import Any, Dict, List, Tuple

import altair as alt
import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

import osm_backend as ob


st.set_page_config(
    page_title="서울 트레킹 코스 추천 (OSM only)",
    page_icon="🥾",
    layout="wide",
)
st.title("🥾 서울 트레킹 코스 추천 (OSM만 사용)")
st.caption("OSM(Overpass)만으로 트레킹 코스 후보 + 난이도 + 종료점 주변 카페/맥주 추천")


@st.cache_data(ttl=60 * 60)
def cached_courses(
    bbox: Tuple[float, float, float, float],
    max_relations: int,
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


with st.sidebar:
    st.header("1) 지역 선택")
    preset = st.selectbox(
        "프리셋 지역(중심점)",
        [
            "서울 전체(대략)",
            "남산/용산권",
            "북한산권(은평/강북/도봉)",
            "한강/여의도권",
            "강남/양재권",
            "사용자 지정(위경도)",
        ],
    )

    if preset == "사용자 지정(위경도)":
        lat = st.number_input("중심 위도(lat)", value=37.5665, format="%.6f")
        lon = st.number_input("중심 경도(lon)", value=126.9780, format="%.6f")
        radius_km = st.slider("반경(km)", 2.0, 30.0, 12.0, 0.5)
    else:
        presets = {
            "서울 전체(대략)": (37.5665, 126.9780, 18.0),
            "남산/용산권": (37.5512, 126.9882, 8.0),
            "북한산권(은평/강북/도봉)": (37.6584, 126.9800, 12.0),
            "한강/여의도권": (37.5250, 126.9250, 10.0),
            "강남/양재권": (37.4840, 127.0350, 10.0),
        }
        lat, lon, radius_km = presets[preset]

    st.header("2) 난이도/추천 수")
    diff_filter = st.radio("난이도", ["전체", "쉬움", "보통", "어려움"], index=0)
    topk = st.slider("추천 코스 개수", 3, 10, 4)
    max_relations = st.slider("후보 탐색량(Overpass 부담)", 20, 80, 50, 5)

    st.header("3) 트레킹 후 추천")
    near_radius_m = st.slider("종료점 주변 추천 반경(m)", 100, 2000, 700, 50)
    sip_choice = st.radio(
        "추천 종류",
        ["전체", "카페(☕)", "맥주(🍺)"],
        horizontal=True,
    )

    st.divider()
    st.caption(
        "⚠️ Overpass는 공용 서버라 429(요청 제한)이 날 수 있어요. 잠시 후 재시도하면 대부분 해결됩니다."
    )
    if st.button("🔄 캐시 초기화", use_container_width=True):
        st.cache_data.clear()
        st.success("캐시 초기화 완료! 새로고침하면 다시 수집합니다.")


# ✅ OSM backend로 bbox 생성
bbox = ob.bbox_from_center(lat, lon, radius_km)

# ✅ 코스 후보 수집
with st.status("OSM(Overpass)에서 트레킹 코스 후보 수집 중…", expanded=False) as status:
    try:
        df = cached_courses(bbox, max_relations=max_relations)
        status.update(label=f"코스 후보 생성 완료 ({len(df)}개)", state="complete")
    except Exception as e:
        status.update(label="코스 후보 수집 실패", state="error")
        st.error(
            "Overpass 서버가 요청 제한(429) 또는 일시 오류로 응답했습니다. 잠시 후 다시 시도해 주세요."
        )
        st.exception(e)
        st.stop()

if df.empty:
    st.error(
        "선택한 지역에서 코스 후보를 찾지 못했습니다. 반경을 늘리거나 다른 지역을 선택해 보세요."
    )
    with st.expander("해결 팁", expanded=True):
        st.write(
            {
                "1": "반경(km)을 18~30으로 늘려보세요.",
                "2": "프리셋에서 '북한산권'을 먼저 테스트하면 성공 확률이 높아요.",
                "3": "Overpass가 일시적으로 제한일 수 있어요(잠깐 뒤 재시도).",
            }
        )
    st.stop()

# 난이도 필터
df_use = df.copy()
if diff_filter != "전체":
    df_use = df_use[df_use["difficulty"] == diff_filter].copy()

if df_use.empty:
    st.info("선택한 난이도에서 후보가 없습니다. 다른 난이도를 선택해 보세요.")
    st.stop()

df_use = df_use.sort_values("score", ascending=False).head(topk).reset_index(drop=True)
df_chart = df_use[["name", "difficulty", "distance_km", "members", "score"]].copy()

col_map, col_panel = st.columns([1.35, 1])

with col_map:
    st.subheader("🗺️ 추천 코스 지도")
    m = folium.Map(location=[lat, lon], zoom_start=12, tiles="OpenStreetMap")

    # bbox 표시
    s, w, n, e = bbox
    folium.Rectangle(
        bounds=[[s, w], [n, e]], color="#0984e3", weight=2, fill=False
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

    for i, r in df_use.iterrows():
        latlon = r["coords"]
        color = colors[i % len(colors)]

        folium.PolyLine(
            latlon,
            color=color,
            weight=6,
            opacity=0.85,
            tooltip=f"{i+1}위 {r['name']}",
        ).add_to(m)

        folium.Marker(
            location=[r["end_lat"], r["end_lon"]],
            tooltip=f"{i+1}위 종료점 · {r['difficulty']} · {r['distance_km']}km",
            icon=folium.Icon(color="green", icon="flag"),
        ).add_to(m)

    st_folium(m, height=620, width=None)

with col_panel:
    st.subheader(f"🏅 추천 Top {len(df_use)}")
    show_cols = ["name", "difficulty", "distance_km", "members", "score"]
    st.dataframe(df_use[show_cols], use_container_width=True, hide_index=True)

    chart = (
        alt.Chart(df_chart)
        .mark_bar()
        .encode(
            x=alt.X("name:N", title="코스"),
            y=alt.Y("distance_km:Q", title="거리(km)"),
            tooltip=["name", "difficulty", "distance_km", "members", "score"],
        )
    )
    st.altair_chart(chart, use_container_width=True)

st.divider()

selected = st.selectbox("상세로 볼 코스 선택", df_use["name"].tolist(), index=0)
row = df_use[df_use["name"] == selected].iloc[0].to_dict()

st.subheader("🧭 선택 코스 정보")
st.write(
    {
        "name": row["name"],
        "difficulty": row["difficulty"],
        "distance_km": row["distance_km"],
        "route_members": int(row["members"]),
        "start": (row["start_lat"], row["start_lon"]),
        "end": (row["end_lat"], row["end_lon"]),
    }
)

st.subheader("☕/🍺 트레킹 후 추천 TOP 10 (종료점 기준)")
try:
    places = cached_places(
        float(row["end_lat"]), float(row["end_lon"]), int(near_radius_m)
    )
except Exception as e:
    st.error(
        "주변 장소 조회 중 Overpass 제한/오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    )
    st.exception(e)
    st.stop()

if sip_choice != "전체":
    want = "coffee" if "카페" in sip_choice else "beer"
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
