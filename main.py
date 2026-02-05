# app_trekking_streamlit.py
# Streamlit-only "서울 트레킹 코스 추천" (공공데이터 + 네이버 검색 크롤링)
#
# 사용 라이브러리: streamlit, folium, streamlit-folium, pandas, altair, requests, beautifulsoup4
# 추가 파일/JSON 저장 없음 (전부 런타임 수집 + Streamlit cache)
#
# 실행:
#   pip install streamlit folium streamlit-folium pandas altair requests beautifulsoup4
#   streamlit run app_trekking_streamlit.py

from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import altair as alt
import folium
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit_folium import st_folium


# ----------------------------
# 네이버 검색(블로그/뉴스) HTML 크롤링
# ----------------------------
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121 Safari/537.36"
    )
}


def _parse_total_count(text: str) -> int:
    m = re.search(r"약\s*([\d,]+)\s*건", text)
    if not m:
        m = re.search(r"([\d,]+)\s*건", text)
    if not m:
        return 0
    return int(m.group(1).replace(",", ""))


@st.cache_data(ttl=60 * 30)
def naver_search(where: str, query: str, topk: int = 5) -> Dict[str, Any]:
    """
    where: 'blog' or 'news'
    return: {total:int, items:[{title, link, snippet}]}
    """
    url = "https://search.naver.com/search.naver"
    params = {"where": where, "query": query, "sm": "tab_opt"}

    try:
        resp = requests.get(url, params=params, headers=UA, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        page_text = soup.get_text(" ", strip=True)
        total = _parse_total_count(page_text)

        items: List[Dict[str, str]] = []

        if where == "blog":
            links = soup.select(".api_txt_lines.total_tit") or soup.select(
                "a.api_txt_lines"
            )
            descs = soup.select(".api_txt_lines.dsc_txt")
        else:
            links = (
                soup.select("a.news_tit")
                or soup.select(".api_txt_lines.total_tit")
                or soup.select("a.api_txt_lines")
            )
            descs = soup.select(".news_dsc .dsc_txt_wrap") or soup.select(
                ".api_txt_lines.dsc_txt"
            )

        for a in links[:topk]:
            title = a.get_text(strip=True)
            link = a.get("href", "")
            if title:
                items.append({"title": title, "link": link, "snippet": ""})

        for i, d in enumerate(descs[:topk]):
            if i < len(items):
                items[i]["snippet"] = d.get_text(strip=True)[:180]

        return {"total": total, "items": items}
    except Exception as e:
        return {"total": 0, "items": [], "error": str(e)}


def popularity_score(blog_total: int, news_total: int) -> float:
    # 블로그가 코스 체감/후기와 더 직접적이라 가중치 ↑
    return float(round(math.log1p(blog_total) * 1.0 + math.log1p(news_total) * 0.6, 3))


# ----------------------------
# 공공데이터: VWorld(국토부/산림청) "등산로" API
# - 2D 데이터 API 2.0 / 데이터: LT_L_FRSTCLIMB
# - 요청 URL: https://api.vworld.kr/req/data
# - 주요 필드: mntn_nm(산명), cat_nam(난이도 상/중/하), sec_len(거리m), up_min/down_min(분)
# ----------------------------
VWORLD_URL = "https://api.vworld.kr/req/data"


def bbox_from_center(
    lat: float, lon: float, radius_km: float
) -> Tuple[float, float, float, float]:
    """
    대충 1도 ~ 111km 가정한 간단 bbox (서울 규모에서는 충분히 MVP용)
    returns: (minx, miny, maxx, maxy) = (lon_min, lat_min, lon_max, lat_max)
    """
    d = radius_km / 111.0
    return (lon - d, lat - d, lon + d, lat + d)


@st.cache_data(ttl=60 * 60)
def vworld_get_trails(
    api_key: str, bbox: Tuple[float, float, float, float], size: int = 1000
) -> List[Dict[str, Any]]:
    """
    bbox: (minx, miny, maxx, maxy) in EPSG:4326
    return: list of raw features (geojson-like)
    """
    minx, miny, maxx, maxy = bbox
    params = {
        "service": "data",
        "version": "2.0",
        "request": "GetFeature",
        "format": "json",
        "data": "LT_L_FRSTCLIMB",
        "key": api_key,
        "geomFilter": f"BOX({minx},{miny},{maxx},{maxy})",
        "size": str(size),
        "page": "1",
        "geometry": "true",
        "attribute": "true",
        "crs": "EPSG:4326",
    }

    resp = requests.get(VWORLD_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if data.get("response", {}).get("status") != "OK":
        # NOT_FOUND도 여기로 옴
        return []

    # response.result.featureCollection.features
    fc = data["response"]["result"]["featureCollection"]
    return fc.get("features", [])


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def normalize_feature(f: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    feature -> record
    geometry: LineString coords [[lon,lat],...]
    """
    props = f.get("properties") or {}
    geom = f.get("geometry") or {}
    if geom.get("type") != "LineString":
        return None

    coords = geom.get("coordinates") or []
    if len(coords) < 2:
        return None

    mntn_nm = (props.get("mntn_nm") or "").strip()
    cat_nam = (props.get("cat_nam") or "").strip()  # 상/중/하
    sec_len = _safe_float(props.get("sec_len"), 0.0)
    up_min = _safe_int(props.get("up_min"), 0)
    down_min = _safe_int(props.get("down_min"), 0)

    if not mntn_nm:
        mntn_nm = "이름없음"

    # 대표 포인트(끝점) = 마지막 좌표
    end_lon, end_lat = coords[-1][0], coords[-1][1]

    return {
        "mntn_nm": mntn_nm,
        "difficulty_raw": cat_nam or "미상",
        "sec_len_m": sec_len,
        "up_min": up_min,
        "down_min": down_min,
        "coords": coords,
        "end_lat": end_lat,
        "end_lon": end_lon,
    }


def difficulty_label(raw: str) -> str:
    # VWorld: 상/중/하
    raw = (raw or "").strip()
    if raw == "상":
        return "어려움(상)"
    if raw == "중":
        return "보통(중)"
    if raw == "하":
        return "쉬움(하)"
    return "미상"


def aggregate_courses(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    산명 + 난이도 기준으로 코스 후보 생성
    - 길이는 구간거리 합
    - 대표 라인은 가장 긴 구간의 라인(시각화용)
    """
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["difficulty"] = df["difficulty_raw"].apply(difficulty_label)

    # 코스 ID: 산명 + 난이도
    df["course_id"] = df["mntn_nm"] + " | " + df["difficulty"]

    # 대표 라인 = sec_len_m 최대인 feature
    idx = df.groupby("course_id")["sec_len_m"].idxmax()
    rep = df.loc[idx, ["course_id", "coords", "end_lat", "end_lon"]].set_index(
        "course_id"
    )

    agg = df.groupby("course_id", as_index=False).agg(
        mntn_nm=("mntn_nm", "first"),
        difficulty=("difficulty", "first"),
        total_len_m=("sec_len_m", "sum"),
        total_up_min=("up_min", "sum"),
        total_down_min=("down_min", "sum"),
        segments=("sec_len_m", "count"),
    )
    agg["total_len_km"] = (agg["total_len_m"] / 1000.0).round(2)
    agg["est_time_min"] = (agg["total_up_min"] + agg["total_down_min"]).astype(int)

    # rep merge
    agg = agg.set_index("course_id").join(rep).reset_index()

    return agg


@st.cache_data(ttl=60 * 30)
def score_course_row(course_id: str, mntn_nm: str, area_hint: str) -> Dict[str, Any]:
    """
    네이버 검색 크롤링 근거 만들기
    """
    q = f"{area_hint} {mntn_nm} 등산로 트레킹"
    blog = naver_search("blog", q, topk=5)
    news = naver_search("news", q, topk=5)
    score = popularity_score(
        int(blog.get("total", 0) or 0), int(news.get("total", 0) or 0)
    )
    return {"query": q, "blog": blog, "news": news, "score": score}


# ----------------------------
# 트레킹 후 카페/맥주: OSM Overpass (옵션)
# ----------------------------
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def overpass_query(lat: float, lon: float, radius_m: int) -> List[Dict[str, Any]]:
    query = f"""
    [out:json][timeout:25];
    (
      node(around:{radius_m},{lat},{lon})[amenity=cafe];
      node(around:{radius_m},{lat},{lon})[amenity=bar];
      node(around:{radius_m},{lat},{lon})[amenity=pub];
    );
    out body;
    """
    r = requests.post(OVERPASS_URL, data=query.encode("utf-8"), headers=UA, timeout=30)
    r.raise_for_status()
    return (r.json() or {}).get("elements", [])


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2) + math.cos(lat1 * p) * math.cos(lat2 * p) * (
        math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def extract_place(
    el: Dict[str, Any], origin_lat: float, origin_lon: float
) -> Optional[Dict[str, Any]]:
    if el.get("type") != "node":
        return None
    tags = el.get("tags") or {}
    name = tags.get("name")
    if not name:
        return None

    amenity = tags.get("amenity", "")
    category = "coffee" if amenity == "cafe" else "beer"

    lat = el.get("lat")
    lon = el.get("lon")
    if lat is None or lon is None:
        return None

    dist = int(haversine_m(origin_lat, origin_lon, float(lat), float(lon)))

    # 품질점수: 이름/웹사이트/영업시간 등 단순 휴리스틱(0~5)
    quality = 0
    if tags.get("opening_hours"):
        quality += 2
    if tags.get("website") or tags.get("contact:website"):
        quality += 2
    if tags.get("addr:street") or tags.get("addr:full"):
        quality += 1
    quality = min(5, quality)

    return {
        "name": name,
        "category": category,
        "lat": float(lat),
        "lon": float(lon),
        "distance_m": dist,
        "opening_hours": tags.get("opening_hours", ""),
        "website": tags.get("website") or tags.get("contact:website") or "",
        "quality_score": quality,
    }


@st.cache_data(ttl=60 * 30)
def places_near(lat: float, lon: float, radius_m: int) -> List[Dict[str, Any]]:
    try:
        elements = overpass_query(lat, lon, radius_m)
    except Exception:
        return []

    places = [p for p in (extract_place(el, lat, lon) for el in elements) if p]
    for p in places:
        dist_score = 1 - (p["distance_m"] / max(1, radius_m))
        p["combined_score"] = round(
            dist_score * 0.6 + (p["quality_score"] / 5) * 0.4, 3
        )
    places.sort(key=lambda x: x["combined_score"], reverse=True)
    return places


def render_links(title: str, items: List[Dict[str, Any]]) -> None:
    st.markdown(f"**{title}**")
    if not items:
        st.caption("결과 없음")
        return
    for it in items[:5]:
        t = it.get("title", "(제목없음)")
        link = it.get("link", "")
        snip = it.get("snippet", "")
        if link:
            st.markdown(f"- [{t}]({link})")
        else:
            st.markdown(f"- {t}")
        if snip:
            st.caption(snip)


# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(
    page_title="서울 트레킹 추천 (공공데이터+크롤링)", page_icon="🥾", layout="wide"
)

st.title("🥾 서울 트레킹 코스 추천")
st.caption(
    "공공데이터(등산로) + 네이버 블로그/뉴스 크롤링 근거로 난이도/지역별 Top 코스를 추천합니다."
)

with st.sidebar:
    st.header("1) 공공데이터 설정")
    st.caption("VWorld(브이월드) 등산로 API 키가 필요합니다.")
    api_key = st.text_input(
        "VWorld API Key", type="password", placeholder="발급받은 키를 입력"
    )

    st.header("2) 지역 선택")
    # MVP: 중심+반경 방식 (구 경계 없이도 '지역 선택' 느낌 구현)
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
        radius_km = st.slider("반경(km)", 2.0, 20.0, 8.0, 0.5)
        area_hint = "서울"
    else:
        # 간단 프리셋 중심점들
        presets = {
            "서울 전체(대략)": (37.5665, 126.9780, 18.0, "서울"),
            "남산/용산권": (37.5512, 126.9882, 7.0, "서울 남산 용산"),
            "북한산권(은평/강북/도봉)": (37.6584, 126.9800, 10.0, "서울 북한산"),
            "한강/여의도권": (37.5250, 126.9250, 9.0, "서울 한강 여의도"),
            "강남/양재권": (37.4840, 127.0350, 8.0, "서울 강남 양재"),
        }
        lat, lon, radius_km, area_hint = presets[preset]

    st.header("3) 난이도/정렬")
    diff_filter = st.radio(
        "난이도", ["전체", "쉬움(하)", "보통(중)", "어려움(상)"], horizontal=False
    )
    topk = st.slider("추천 코스 개수", 3, 10, 3)

    st.header("4) 트레킹 후 추천")
    near_radius_m = st.slider("종료점 주변 추천 반경(m)", 100, 1500, 600, 50)
    sip_choice = st.radio(
        "추천 종류", ["전체", "카페(☕)", "맥주(🍺)"], horizontal=True
    )

    st.divider()
    st.caption("⚠️ 네이버/Overpass/공공 API는 가끔 느리거나 제한될 수 있어요.")
    if st.button("🔄 캐시 초기화", use_container_width=True):
        st.cache_data.clear()
        st.success("캐시 초기화 완료! 새로고침하면 다시 수집합니다.")


if not api_key:
    st.warning("왼쪽에서 VWorld API Key를 입력하면 코스 수집을 시작할 수 있어요.")
    st.stop()

bbox = bbox_from_center(lat, lon, radius_km)

# with st.status(
#     "공공데이터에서 등산로(트레킹 코스 후보) 가져오는 중…", expanded=False
# ) as status:
#     try:
#         feats = vworld_get_trails(api_key, bbox)
#     except Exception as e:
#         st.error(f"VWorld 호출 실패: {e}")
#         st.stop()

#     records = []
#     for f in feats:
#         r = normalize_feature(f)
#         if r:
#             records.append(r)

#     courses = aggregate_courses(records)
#     status.update(label=f"코스 후보 생성 완료 ({len(courses)}개)", state="complete")

# if courses.empty:
#     st.info(
#         "선택한 지역에서 코스 후보를 찾지 못했습니다. 반경을 늘리거나 다른 지역을 선택해 보세요."
#     )
#     st.stop()

# 난이도 필터
if diff_filter != "전체":
    courses = courses[courses["difficulty"] == diff_filter].copy()

if courses.empty:
    st.info("해당 난이도에서 코스 후보가 없습니다. 다른 난이도를 선택해 보세요.")
    st.stop()

# 코스별 네이버 근거 점수 (상위만)
# - 전부 크롤링하면 시간이 오래 걸려서, 일단 길이 기준으로 상위 30개만 점수 계산
courses = (
    courses.sort_values("total_len_m", ascending=False).head(30).reset_index(drop=True)
)

with st.status("네이버 블로그/뉴스에서 인기 근거 수집 중…", expanded=False) as status:
    scores = []
    for _, row in courses.iterrows():
        ev = score_course_row(row["course_id"], row["mntn_nm"], area_hint)
        scores.append(ev)
        time.sleep(0.15)  # 너무 빠르면 차단/불안정해질 수 있어 살짝 쉬어줌
    status.update(label="근거 수집 완료", state="complete")

# evidence merge
courses["query"] = [x["query"] for x in scores]
courses["blog_total"] = [x["blog"].get("total", 0) for x in scores]
courses["news_total"] = [x["news"].get("total", 0) for x in scores]
courses["popularity"] = [x["score"] for x in scores]
courses["evidence_blog"] = [x["blog"] for x in scores]
courses["evidence_news"] = [x["news"] for x in scores]

# 최종 점수(가중치 조정 가능)
# - 길이 조금 반영(너무 짧은 코스가 항상 이기는 걸 방지)
courses["final_score"] = (
    courses["popularity"] * 1.0
    + courses["total_len_km"].apply(lambda x: math.log1p(float(x))) * 0.4
).round(3)

courses = courses.sort_values("final_score", ascending=False).reset_index(drop=True)
top = courses.head(topk).copy()

# ----------------------------
# 시각화: 지도 + 표 + 근거 패널
# ----------------------------
col_map, col_panel = st.columns([1.35, 1])

with col_map:
    st.subheader("🗺️ 추천 코스 지도")
    center = [lat, lon]
    m = folium.Map(location=center, zoom_start=12, tiles="OpenStreetMap")

    # bbox 표시(대략)
    minx, miny, maxx, maxy = bbox
    folium.Rectangle(
        bounds=[[miny, minx], [maxy, maxx]], color="#0984e3", weight=2, fill=False
    ).add_to(m)

    line_colors = [
        "#6c5ce7",
        "#00b894",
        "#e17055",
        "#0984e3",
        "#d63031",
        "#e84393",
        "#2d3436",
        "#fdcb6e",
    ]

    for i, row in top.iterrows():
        coords = row["coords"]  # [[lon,lat],...]
        latlon = [[c[1], c[0]] for c in coords]
        color = line_colors[i % len(line_colors)]
        folium.PolyLine(
            latlon,
            color=color,
            weight=6,
            opacity=0.85,
            tooltip=f"{i+1}위 {row['course_id']}",
        ).add_to(m)

        # 끝점 마커
        folium.Marker(
            location=[row["end_lat"], row["end_lon"]],
            tooltip=f"{i+1}위 종료점 · {row['mntn_nm']} · {row['difficulty']}",
            icon=folium.Icon(color="green", icon="flag"),
        ).add_to(m)

    st_folium(m, height=620, width=None)

with col_panel:
    st.subheader(f"🏅 추천 Top {topk}")
    show_cols = [
        "course_id",
        "difficulty",
        "total_len_km",
        "est_time_min",
        "blog_total",
        "news_total",
        "final_score",
    ]
    st.dataframe(top[show_cols], use_container_width=True, hide_index=True)

    # 차트(검색량 비교)
    df_long = top[["course_id", "final_score", "blog_total", "news_total"]].melt(
        id_vars=["course_id", "final_score"],
        value_vars=["blog_total", "news_total"],
        var_name="source",
        value_name="count",
    )

    chart = (
        alt.Chart(df_long)
        .mark_bar()
        .encode(
            x=alt.X("course_id:N", title="코스"),
            y=alt.Y("count:Q", title="검색량(추정)"),
            column=alt.Column("source:N", title=None),
            tooltip=["course_id", "source", "count", "final_score"],
        )
    )
    st.altair_chart(chart, use_container_width=True)

st.divider()

# 상세 보기
selected = st.selectbox("상세로 볼 코스 선택", top["course_id"].tolist(), index=0)
row = top[top["course_id"] == selected].iloc[0].to_dict()

st.subheader("🧾 추천 근거(크롤링 결과)")
st.write(
    {
        "course": row["course_id"],
        "difficulty": row["difficulty"],
        "distance_km": row["total_len_km"],
        "estimated_time_min": row["est_time_min"],
        "query": row["query"],
        "blog_total": int(row["blog_total"]),
        "news_total": int(row["news_total"]),
        "final_score": float(row["final_score"]),
    }
)

render_links("🧱 블로그 상위 결과", (row.get("evidence_blog") or {}).get("items", []))
render_links("📰 뉴스 상위 결과", (row.get("evidence_news") or {}).get("items", []))

st.divider()

st.subheader("☕/🍺 트레킹 후 추천 TOP 10 (종료점 기준)")
places = places_near(float(row["end_lat"]), float(row["end_lon"]), int(near_radius_m))

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
    keep = [c for c in keep if c in dfp.columns]
    st.dataframe(dfp[keep], use_container_width=True, hide_index=True)
