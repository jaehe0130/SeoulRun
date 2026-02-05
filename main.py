# main.py
import streamlit as st
import pandas as pd
from math import radians, cos, sin, asin, sqrt

# 지도(leaflet) 라이브러리
from streamlit_folium import st_folium
import folium

st.set_page_config(page_title="서울 러닝코스 추천", layout="wide")


# -----------------------------
# Utils
# -----------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    """두 좌표 사이 거리(km)"""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def get_courses_stub():
    """
    TODO: 실제 데이터로 교체
    최소 컬럼 예시:
    - course_id, name, course_type, km, day_night, district
    - path: [(lat, lon), ...]  # 코스 폴리라인
    - start_lat, start_lng, end_lat, end_lng
    """
    return [
        {
            "course_id": "C001",
            "name": "한강 러닝(여의도-마포)",
            "course_type": "강변",
            "km": 7,
            "day_night": "주간",
            "district": "영등포구",
            "path": [
                (37.5287, 126.9327), (37.5312, 126.9248), (37.5392, 126.9168),
                (37.5455, 126.9087), (37.5513, 126.9021)
            ],
        },
        {
            "course_id": "C002",
            "name": "남산 둘레길 러닝",
            "course_type": "트레일",
            "km": 5,
            "day_night": "야간",
            "district": "중구",
            "path": [
                (37.5512, 126.9882), (37.5522, 126.9853), (37.5535, 126.9828),
                (37.5548, 126.9802), (37.5562, 126.9780)
            ],
        },
        {
            "course_id": "C003",
            "name": "석촌호수 한바퀴",
            "course_type": "호수",
            "km": 3,
            "day_night": "주간",
            "district": "송파구",
            "path": [
                (37.5079, 127.1000), (37.5065, 127.1028), (37.5050, 127.1046),
                (37.5038, 127.1030), (37.5049, 127.1006), (37.5070, 127.0990)
            ],
        },
    ]


def filter_courses(courses, course_type, km_range, day_night):
    km_min, km_max = km_range
    filtered = []
    for c in courses:
        if course_type != "전체" and c["course_type"] != course_type:
            continue
        if day_night != "전체" and c["day_night"] != day_night:
            continue
        if not (km_min <= c["km"] <= km_max):
            continue
        filtered.append(c)
    return filtered


def course_center(path):
    lats = [p[0] for p in path]
    lngs = [p[1] for p in path]
    return sum(lats) / len(lats), sum(lngs) / len(lngs)


def draw_map(selected_course, places_df):
    """OpenStreetMap 기반 folium 지도 + 코스 폴리라인 + POI 마커"""
    path = selected_course["path"]
    center_lat, center_lng = course_center(path)

    m = folium.Map(location=[center_lat, center_lng], zoom_start=14, tiles="OpenStreetMap")

    # 코스 표시
    folium.PolyLine(
        locations=path,
        weight=6,
        opacity=0.9,
        tooltip=f'{selected_course["name"]} ({selected_course["km"]}km)',
    ).add_to(m)

    # 시작/끝 마커
    folium.Marker(path[0], tooltip="START", icon=folium.Icon(color="green")).add_to(m)
    folium.Marker(path[-1], tooltip="END", icon=folium.Icon(color="red")).add_to(m)

    # POI 마커
    if places_df is not None and len(places_df) > 0:
        for _, r in places_df.iterrows():
            tooltip = f'{r["name"]} · {r["category"]}'
            folium.CircleMarker(
                location=[r["lat"], r["lng"]],
                radius=6,
            ).add_to(m)
            folium.Marker([r["lat"], r["lng"]], tooltip=tooltip).add_to(m)

    return m


def search_places_stub(center_lat, center_lng, radius_km=1.2):
    """
    TODO: 여기서 네이버/카카오/구글/공공데이터 등으로 실제 검색 결과를 가져오면 됨
    반환: DataFrame(name, category, lat, lng, dist_km, address, url)
    """
    sample = [
        ("러너스카페", "카페", center_lat + 0.004, center_lng + 0.003, "서울 어딘가 1", ""),
        ("한잔포차", "술집", center_lat - 0.003, center_lng - 0.002, "서울 어딘가 2", ""),
        ("든든한국밥", "맛집", center_lat + 0.002, center_lng - 0.004, "서울 어딘가 3", ""),
        ("브루펍", "술집", center_lat - 0.005, center_lng + 0.001, "서울 어딘가 4", ""),
        ("베이커리카페", "카페", center_lat + 0.006, center_lng - 0.001, "서울 어딘가 5", ""),
    ]
    rows = []
    for name, cat, lat, lng, addr, url in sample:
        dist = haversine_km(center_lat, center_lng, lat, lng)
        if dist <= radius_km:
            rows.append(
                {"name": name, "category": cat, "lat": lat, "lng": lng, "dist_km": dist, "address": addr, "url": url}
            )
    df = pd.DataFrame(rows).sort_values("dist_km")
    return df


# -----------------------------
# UI: Sidebar
# -----------------------------
st.title("🏃‍♀️ 서울 러닝코스 추천 (지도 + 주변 핫플)")

courses = get_courses_stub()

course_types = ["전체"] + sorted(list({c["course_type"] for c in courses}))
day_night_options = ["전체", "주간", "야간"]

with st.sidebar:
    st.header("필터")
    course_type = st.selectbox("코스 유형", course_types, index=0)
    km_range = st.slider("KM 범위", min_value=1, max_value=20, value=(3, 8), step=1)
    day_night = st.radio("주간 / 야간", day_night_options, horizontal=True)

    st.divider()
    radius_km = st.slider("주변 장소 반경(km)", 0.5, 3.0, 1.2, 0.1)

filtered = filter_courses(courses, course_type, km_range, day_night)


# -----------------------------
# Main Layout
# -----------------------------
left, right = st.columns([1.2, 0.8], gap="large")

with left:
    st.subheader("🗺️ 코스 지도 (OpenStreetMap)")

    if len(filtered) == 0:
        st.warning("조건에 맞는 코스가 없어요. 필터를 조금 완화해봐!")
        st.stop()

    # 코스 선택
    course_names = [f'{c["name"]} · {c["km"]}km · {c["course_type"]} · {c["day_night"]}' for c in filtered]
    idx = st.selectbox("추천 코스 선택", list(range(len(filtered))), format_func=lambda i: course_names[i])

    selected_course = filtered[idx]
    center_lat, center_lng = course_center(selected_course["path"])

    # 주변 장소 검색(현재는 스텁)
    places_df = search_places_stub(center_lat, center_lng, radius_km=radius_km)

    # 지도 렌더
    m = draw_map(selected_course, places_df)
    st_folium(m, height=560, width=None)

    st.caption("지도 타일: OpenStreetMap / 코스: 폴리라인 표시 / 주변 장소: 마커 표시")


with right:
    st.subheader("☕🍺🍜 코스 근처 추천")
    st.write(f'**선택 코스:** {selected_course["name"]}  \n'
             f'**거리:** {selected_course["km"]}km  \n'
             f'**유형:** {selected_course["course_type"]} / **시간대:** {selected_course["day_night"]}')

    st.divider()

    if places_df is None or len(places_df) == 0:
        st.info("반경 내에 표시할 장소가 없어요.")
    else:
        tabs = st.tabs(["전체", "카페", "술집", "맛집"])

        def render_list(df):
            # 카드 느낌으로 리스트
            for _, r in df.iterrows():
                st.markdown(
                    f"""
**{r['name']}** · {r['category']}  
📍 {r['address']}  
📏 약 {r['dist_km']:.2f}km
""")
                st.write("---")

        with tabs[0]:
            render_list(places_df)

        with tabs[1]:
            render_list(places_df[places_df["category"] == "카페"])

        with tabs[2]:
            render_list(places_df[places_df["category"] == "술집"])

        with tabs[3]:
            render_list(places_df[places_df["category"] == "맛집"])

    st.divider()
    st.subheader("🔧 다음 단계(연동 포인트)")
    st.markdown(
        """
- `get_courses_stub()` → **네이버 크롤링/공공데이터로 만든 코스 DB**(CSV/DB/Google Sheets)로 교체  
- `search_places_stub()` → **네이버 지역검색 API**(또는 카카오 로컬) 호출로 교체  
- 장소 결과는 `DataFrame(name, category, lat, lng, address, url)` 형태로만 맞추면 지도/리스트는 그대로 동작
"""
    )

