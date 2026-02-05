# osm_backend.py
# OSM(Overpass) 기반 트레킹 코스/주변 장소 추천 로직 + ORS 고도(Altitude) 프로파일
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121 Safari/537.36"
    )
}

# Overpass는 공용 서버라 429(요청 제한) 발생 가능 → 엔드포인트 로테이션
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]

# ORS(OpenRouteService) Elevation(고도) API
ORS_ELEVATION_LINE_URL = "https://api.openrouteservice.org/elevation/line"
ORS_MAX_VERTICES = 2000  # vertex 제한이 있을 수 있어 안전하게 샘플링


def bbox_from_center(
    lat: float, lon: float, radius_km: float
) -> Tuple[float, float, float, float]:
    """Overpass bbox: (south, west, north, east)"""
    d = radius_km / 111.0
    return (lat - d, lon - d, lat + d, lon + d)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p = math.pi / 180
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
    v = d.get(k) or ""
    return str(v).strip() if v is not None else default


def _difficulty_from_sac(sac: str) -> str:
    """
    OSM sac_scale 예:
      hiking
      mountain_hiking
      demanding_mountain_hiking
      alpine_hiking
      demanding_alpine_hiking
      difficult_alpine_hiking
    """
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
    from_sac = _difficulty_from_sac(sac_hint)
    if from_sac:
        return from_sac
    if distance_km < 5:
        return "쉬움"
    if distance_km < 10:
        return "보통"
    return "어려움"


def overpass_post(
    query: str, timeout: int = 60, max_retries: int = 3
) -> Dict[str, Any]:
    """
    429(Too Many Requests) 대응:
    - 429면 지수 백오프 + Retry-After 존중
    - 여러 Overpass 서버 로테이션
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
    """
    relation["route"="hiking"|"foot"] 기반 코스 후보.
    out body geom; 로 relation members geometry까지 받으려 시도.
    """
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

    # 이름 있는 relation 우선
    rels_named = [r for r in rels if (r.get("tags") or {}).get("name")]
    rels = rels_named if rels_named else rels
    return rels[:max_relations]


def relation_to_course(rel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tags = rel.get("tags") or {}
    name = _safe_get(tags, "name", "")
    if not name:
        return None

    sac = _safe_get(tags, "sac_scale", "")

    # members geometry 이어붙이기
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
            if latlon and pts:
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

    # 간단 점수: members 수 + 거리
    score = round(math.log1p(len(members)) * 0.8 + math.log1p(dist_km) * 0.6, 3)

    return {
        "course_id": f"{name} ({dist_km}km)",
        "name": name,
        "distance_km": dist_km,
        "difficulty": diff,
        "score": score,
        "coords": latlon,  # [(lat, lon), ...]
        "start_lat": start[0],
        "start_lon": start[1],
        "end_lat": end[0],
        "end_lon": end[1],
        "members": len(members),
    }


def build_courses(
    bbox: Tuple[float, float, float, float], max_relations: int = 50
) -> List[Dict[str, Any]]:
    """bbox에서 코스 후보 리스트 생성"""
    rels = fetch_trails_relations(bbox, max_relations=max_relations)
    courses: List[Dict[str, Any]] = []
    for r in rels:
        c = relation_to_course(r)
        if c:
            courses.append(c)

    # 중복 이름 제거(점수 높은 것 우선)
    courses.sort(key=lambda x: (x["score"], x["distance_km"]), reverse=True)
    dedup: Dict[str, Dict[str, Any]] = {}
    for c in courses:
        if c["name"] not in dedup:
            dedup[c["name"]] = c
    return list(dedup.values())


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

    # 간단 품질 점수(0~5)
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


# ===== ORS Elevation (고도 프로파일) =====


def _sample_latlon(
    latlon: List[Tuple[float, float]], max_points: int = 1800
) -> List[Tuple[float, float]]:
    """좌표가 너무 많으면 균등 샘플링해서 ORS vertex 제한을 피합니다."""
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
    입력: [(lat, lon), ...] 2D
    출력: [(lat, lon, elev_m), ...] 3D
    """
    if not api_key:
        raise ValueError("ORS API key is empty")

    latlon = _sample_latlon(latlon, max_points=min(ORS_MAX_VERTICES - 50, 1800))
    coords_lonlat = [[float(lon), float(lat)] for (lat, lon) in latlon]

    payload = {
        "format_in": "geojson",
        "format_out": "geojson",
        "geometry": {"type": "LineString", "coordinates": coords_lonlat},
        "dataset": dataset,
    }

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        **UA,
    }

    r = requests.post(ORS_ELEVATION_LINE_URL, json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()

    geom = data.get("geometry") or {}
    coords = geom.get("coordinates") or []

    out: List[Tuple[float, float, float]] = []
    for c in coords:
        if not isinstance(c, list) or len(c) < 3:
            continue
        lon, lat, ele = c[0], c[1], c[2]
        out.append((float(lat), float(lon), float(ele)))
    return out


def elevation_profile(
    latlon: List[Tuple[float, float]], api_key: str
) -> List[Dict[str, float]]:
    """
    고도 그래프용 프로파일:
      [{'dist_km':..., 'elev_m':...}, ...]
    """
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
