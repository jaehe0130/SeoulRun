from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121 Safari/537.36"
    )
}

# Overpass 공용 서버(429 대비 로테이션)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]

# ORS Elevation(고도)
ORS_ELEVATION_LINE_URL = "https://api.openrouteservice.org/elevation/line"
ORS_MAX_VERTICES = 2000

# ===== 공공데이터(GPX) 매칭 설정 =====
_OFFICIAL_MATCH_THRESHOLD_M = 250


def set_official_match_threshold(threshold_m: int) -> None:
    global _OFFICIAL_MATCH_THRESHOLD_M
    _OFFICIAL_MATCH_THRESHOLD_M = max(10, int(threshold_m))


# ===== 기본 유틸 =====
def bbox_from_center(
    lat: float, lon: float, radius_km: float
) -> Tuple[float, float, float, float]:
    """bbox: (south, west, north, east)"""
    d = radius_km / 111.0
    return (lat - d, lon - d, lat + d, lon + d)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2) + math.cos(lat1 * p) * math.cos(lat2 * p) * (
        math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def polyline_length_km(latlon: List[Tuple[float, float]]) -> float:
    if len(latlon) < 2:
        return 0.0
    dist = 0.0
    for i in range(1, len(latlon)):
        dist += haversine_m(
            latlon[i - 1][0], latlon[i - 1][1], latlon[i][0], latlon[i][1]
        )
    return dist / 1000.0


def _safe_get(d: Dict[str, Any], k: str, default: str = "") -> str:
    v = d.get(k) if isinstance(d, dict) else None
    return str(v).strip() if v is not None else default


def _difficulty_from_sac(sac: str) -> str:
    sac = (sac or "").strip()
    if sac == "hiking":
        return "쉬움"
    if sac == "mountain_hiking":
        return "보통"
    if sac in {
        "demanding_mountain_hiking",
        "alpine_hiking",
        "demanding_alpine_hiking",
        "difficult_alpine_hiking",
    }:
        return "어려움"
    return ""


def difficulty_label(sac_hint: str, distance_km: float) -> str:
    d = _difficulty_from_sac(sac_hint)
    if d:
        return d
    if distance_km < 5:
        return "쉬움"
    if distance_km < 10:
        return "보통"
    return "어려움"


# ===== Overpass =====
def overpass_post(
    query: str, timeout: int = 60, max_retries: int = 3
) -> Dict[str, Any]:
    """
    429 대응:
    - 429면 백오프 + Retry-After(있으면 반영)
    - 서버 로테이션
    """
    last_err: Exception | None = None

    for base in OVERPASS_URLS:
        wait_s = 2.0
        for _ in range(max_retries):
            try:
                r = requests.post(
                    base, data=query.encode("utf-8"), headers=UA, timeout=timeout
                )

                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        try:
                            wait_s = max(wait_s, float(ra))
                        except Exception:
                            pass
                    time.sleep(wait_s)
                    wait_s = min(wait_s * 2, 20.0)
                    continue

                r.raise_for_status()
                return r.json()

            except Exception as e:
                last_err = e
                time.sleep(min(wait_s, 10.0))
                wait_s = min(wait_s * 1.6, 15.0)

    if last_err:
        raise last_err
    raise RuntimeError("Overpass request failed")


def fetch_trails_relations(
    bbox: Tuple[float, float, float, float], max_relations: int = 50
) -> List[Dict[str, Any]]:
    s, w, n, e = bbox
    q = f"""
    [out:json][timeout:60];
    (
      relation["route"="hiking"]({s},{w},{n},{e});
      relation["route"="foot"]({s},{w},{n},{e});
    );
    out body geom;
    """
    data = overpass_post(q, timeout=75)
    elements = data.get("elements", [])
    rels = [el for el in elements if el.get("type") == "relation"]

    rels_named = [r for r in rels if (r.get("tags") or {}).get("name")]
    rels = rels_named if rels_named else rels
    return rels[:max_relations]


# ===== 공공데이터(GPX) 인덱스 =====
_GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}


def _bbox_intersects(
    b1: Tuple[float, float, float, float],
    b2: Tuple[float, float, float, float],
) -> bool:
    # (south, west, north, east)
    s1, w1, n1, e1 = b1
    s2, w2, n2, e2 = b2
    return not (e1 < w2 or e2 < w1 or n1 < s2 or n2 < s1)


def _parse_gpx_bounds_and_endpoints(gpx_path: Path) -> Optional[Dict[str, Any]]:
    """
    GPX에서 metadata/bounds + 트랙 시작/끝점만 빠르게 파싱
    반환 dict:
      name, start_lat, start_lon, end_lat, end_lon, bbox(s,w,n,e)
    """
    try:
        root = ET.parse(gpx_path).getroot()
    except Exception:
        return None

    meta = root.find("g:metadata", _GPX_NS)
    b = meta.find("g:bounds", _GPX_NS) if meta is not None else None
    if b is None or not b.attrib:
        return None

    try:
        minlat = float(b.attrib.get("minlat"))
        minlon = float(b.attrib.get("minlon"))
        maxlat = float(b.attrib.get("maxlat"))
        maxlon = float(b.attrib.get("maxlon"))
    except Exception:
        return None

    trk = root.find("g:trk", _GPX_NS)
    if trk is None:
        return None
    seg = trk.find("g:trkseg", _GPX_NS)
    if seg is None:
        return None
    pts = seg.findall("g:trkpt", _GPX_NS)
    if len(pts) < 2:
        return None

    try:
        s_lat = float(pts[0].attrib["lat"])
        s_lon = float(pts[0].attrib["lon"])
        e_lat = float(pts[-1].attrib["lat"])
        e_lon = float(pts[-1].attrib["lon"])
    except Exception:
        return None

    # 이름 우선순위: wpt name 1->last, 없으면 파일명
    wpts = root.findall("g:wpt", _GPX_NS)
    if wpts:
        w1 = (wpts[0].findtext("g:name", default="", namespaces=_GPX_NS) or "").strip()
        w2 = (wpts[-1].findtext("g:name", default="", namespaces=_GPX_NS) or "").strip()
        name = f"{w1} → {w2}".strip(" →")
    else:
        name = gpx_path.stem

    return {
        "name": name,
        "start_lat": s_lat,
        "start_lon": s_lon,
        "end_lat": e_lat,
        "end_lon": e_lon,
        "bbox": (minlat, minlon, maxlat, maxlon),  # (s,w,n,e)
        "path": str(gpx_path),
    }


def load_official_gpx_index(
    data_dir: str,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    max_files: int = 1500,
) -> List[Dict[str, Any]]:
    """
    data_dir 아래의 .gpx를 스캔해서 '공식 구간 인덱스' 생성
    - bbox가 주어지면 GPX bounds 교차 여부로 1차 필터링(빠름)
    - max_files까지 로드(속도/메모리 제어)
    """
    base = Path(data_dir)
    if not base.exists():
        return []

    gpx_files = list(base.rglob("*.gpx"))
    out: List[Dict[str, Any]] = []

    picked = 0
    for p in gpx_files:
        if picked >= max_files:
            break

        info = _parse_gpx_bounds_and_endpoints(p)
        if not info:
            continue

        if bbox is not None:
            if not _bbox_intersects(bbox, info["bbox"]):
                continue

        out.append(info)
        picked += 1

    return out


def match_official_by_endpoints(
    course_start: Tuple[float, float],
    course_end: Tuple[float, float],
    official_index: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    OSM 코스 start/end가 공식 GPX 구간 start/end와 가까우면 매칭
    - 방향 반대 가능성 고려(정방향/역방향)
    - trust_score: 0~30점 (가까울수록 높음)
    """
    if not official_index:
        return {
            "matched": False,
            "trust_score": 0.0,
            "nearest_m": None,
            "official_name": None,
        }

    s_lat, s_lon = course_start
    e_lat, e_lon = course_end

    best_nearest = 1e18
    best_name = None

    for r in official_index:
        os_lat, os_lon = float(r["start_lat"]), float(r["start_lon"])
        oe_lat, oe_lon = float(r["end_lat"]), float(r["end_lon"])

        d1 = (
            haversine_m(s_lat, s_lon, os_lat, os_lon)
            + haversine_m(e_lat, e_lon, oe_lat, oe_lon)
        ) / 2.0
        d2 = (
            haversine_m(s_lat, s_lon, oe_lat, oe_lon)
            + haversine_m(e_lat, e_lon, os_lat, os_lon)
        ) / 2.0
        nearest = min(d1, d2)

        if nearest < best_nearest:
            best_nearest = nearest
            best_name = r.get("name")

    th = float(_OFFICIAL_MATCH_THRESHOLD_M)
    if best_nearest <= th:
        trust = 5.0 + (th - best_nearest) / th * 25.0  # 5~30
        return {
            "matched": True,
            "trust_score": round(float(trust), 2),
            "nearest_m": round(float(best_nearest), 1),
            "official_name": best_name,
        }

    return {
        "matched": False,
        "trust_score": 0.0,
        "nearest_m": round(float(best_nearest), 1),
        "official_name": best_name,
    }


# ===== 코스 생성(공식 매칭 포함) =====
def relation_to_course(
    rel: Dict[str, Any],
    official_index: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    tags = rel.get("tags") or {}
    name = _safe_get(tags, "name", "")
    if not name:
        return None

    sac = _safe_get(tags, "sac_scale", "")

    latlon: List[Tuple[float, float]] = []
    members = rel.get("members") or []

    for m in members:
        geom = m.get("geometry") or []
        pts = [
            (float(p["lat"]), float(p["lon"]))
            for p in geom
            if "lat" in p and "lon" in p
        ]
        if len(pts) >= 2:
            if latlon:
                if haversine_m(latlon[-1][0], latlon[-1][1], pts[0][0], pts[0][1]) < 5:
                    latlon.extend(pts[1:])
                else:
                    latlon.extend(pts)
            else:
                latlon.extend(pts)

    if len(latlon) < 2:
        return None

    dist_km = round(polyline_length_km(latlon), 2)
    if dist_km < 1.0 or dist_km > 35.0:
        return None

    diff = difficulty_label(sac, dist_km)
    start = latlon[0]
    end = latlon[-1]

    score_osm = round(math.log1p(len(members)) * 0.8 + math.log1p(dist_km) * 0.6, 3)

    # ✅ 공공데이터 매칭 가산점
    m = match_official_by_endpoints(start, end, official_index or [])
    trust_score = float(m["trust_score"])
    score_final = round(score_osm + trust_score, 3)

    return {
        "course_id": f"{name} ({dist_km}km)",
        "name": name,
        "distance_km": dist_km,
        "difficulty": diff,
        "score": score_final,  # 최종 점수(신뢰도 포함)
        "score_osm": score_osm,  # 참고용
        "trust_score": trust_score,
        "official_matched": bool(m["matched"]),
        "official_nearest_m": m["nearest_m"],
        "official_name": m["official_name"],
        "coords": latlon,  # [(lat, lon), ...]
        "start_lat": start[0],
        "start_lon": start[1],
        "end_lat": end[0],
        "end_lon": end[1],
        "members": len(members),
    }


def build_courses(
    bbox: Tuple[float, float, float, float],
    max_relations: int = 50,
    official_index: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    rels = fetch_trails_relations(bbox, max_relations=max_relations)
    courses: List[Dict[str, Any]] = []
    for r in rels:
        c = relation_to_course(r, official_index=official_index)
        if c:
            courses.append(c)

    courses.sort(key=lambda x: (x["score"], x["distance_km"]), reverse=True)

    dedup: Dict[str, Dict[str, Any]] = {}
    for c in courses:
        if c["name"] not in dedup:
            dedup[c["name"]] = c
    return list(dedup.values())


# ===== 주변 장소(카페/펍) =====
def overpass_places_query(lat: float, lon: float, radius_m: int) -> str:
    return f"""
    [out:json][timeout:45];
    (
      node(around:{radius_m},{lat},{lon})[amenity=cafe];
      node(around:{radius_m},{lat},{lon})[amenity=bar];
      node(around:{radius_m},{lat},{lon})[amenity=pub];
    );
    out body;
    """


def extract_place(
    el: Dict[str, Any], origin_lat: float, origin_lon: float
) -> Optional[Dict[str, Any]]:
    if el.get("type") != "node":
        return None
    tags = el.get("tags") or {}
    name = tags.get("name")
    if not name:
        return None

    lat = el.get("lat")
    lon = el.get("lon")
    if lat is None or lon is None:
        return None

    amenity = tags.get("amenity", "")
    category = "coffee" if amenity == "cafe" else "beer"
    dist = int(haversine_m(origin_lat, origin_lon, float(lat), float(lon)))

    quality = 0
    if tags.get("opening_hours"):
        quality += 2
    if tags.get("website") or tags.get("contact:website"):
        quality += 2
    if tags.get("addr:street") or tags.get("addr:full"):
        quality += 1
    quality = min(5, quality)

    return {
        "name": str(name),
        "category": category,
        "lat": float(lat),
        "lon": float(lon),
        "distance_m": dist,
        "quality_score": quality,
        "opening_hours": tags.get("opening_hours", ""),
        "website": tags.get("website") or tags.get("contact:website") or "",
    }


def places_near(lat: float, lon: float, radius_m: int) -> List[Dict[str, Any]]:
    q = overpass_places_query(lat, lon, radius_m)
    data = overpass_post(q, timeout=60)
    elements = data.get("elements", [])

    places = [p for p in (extract_place(el, lat, lon) for el in elements) if p]
    for p in places:
        dist_score = 1 - (p["distance_m"] / max(1, radius_m))
        p["combined_score"] = round(
            dist_score * 0.6 + (p["quality_score"] / 5) * 0.4, 3
        )

    places.sort(key=lambda x: x["combined_score"], reverse=True)
    return places


# ===== ORS 고도 프로파일 =====
def _sample_latlon(
    latlon: List[Tuple[float, float]], max_points: int = 1800
) -> List[Tuple[float, float]]:
    n = len(latlon)
    if n <= max_points:
        return latlon
    step = max(1, n // max_points)
    sampled = latlon[::step]
    if sampled and sampled[-1] != latlon[-1]:
        sampled.append(latlon[-1])
    return sampled


def ors_elevation_line(
    latlon: List[Tuple[float, float]],
    api_key: str,
    dataset: str = "srtm",
) -> List[Tuple[float, float, float]]:
    """
    입력: [(lat, lon), ...]
    출력: [(lat, lon, elev_m), ...]
    """
    if not api_key:
        raise ValueError("ORS_API_KEY is empty")

    latlon = _sample_latlon(latlon, max_points=min(ORS_MAX_VERTICES - 50, 1800))
    coords_lonlat = [[float(lon), float(lat)] for (lat, lon) in latlon]

    payload = {
        "format_in": "geojson",
        "format_out": "geojson",
        "geometry": {"type": "LineString", "coordinates": coords_lonlat},
        "dataset": dataset,
    }
    headers = {"Authorization": api_key, "Content-Type": "application/json", **UA}

    r = requests.post(ORS_ELEVATION_LINE_URL, json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()

    geom = data.get("geometry") or {}
    coords = geom.get("coordinates") or []

    out: List[Tuple[float, float, float]] = []
    for c in coords:
        if isinstance(c, list) and len(c) >= 3:
            lon, lat, ele = c[0], c[1], c[2]
            out.append((float(lat), float(lon), float(ele)))
    return out


def elevation_profile(
    latlon: List[Tuple[float, float]], api_key: str
) -> List[Dict[str, float]]:
    coords3d = ors_elevation_line(latlon, api_key=api_key)
    if len(coords3d) < 2:
        return []

    prof: List[Dict[str, float]] = []
    dist_km = 0.0
    prof.append({"dist_km": 0.0, "elev_m": float(coords3d[0][2])})

    for i in range(1, len(coords3d)):
        prev = coords3d[i - 1]
        cur = coords3d[i]
        dist_km += haversine_m(prev[0], prev[1], cur[0], cur[1]) / 1000.0
        prof.append({"dist_km": round(dist_km, 4), "elev_m": float(cur[2])})

    return prof
