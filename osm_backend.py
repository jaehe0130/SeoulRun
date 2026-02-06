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


def _safe_get(d: Dict[str, Any], k: str, default: Any = None) -> Any:
    v = d.get(k)
    return default if v is None else v


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def polyline_length_km(latlon: List[Tuple[float, float]]) -> float:
    if len(latlon) < 2:
        return 0.0
    s = 0.0
    for i in range(1, len(latlon)):
        s += haversine_m(latlon[i - 1][0], latlon[i - 1][1], latlon[i][0], latlon[i][1])
    return s / 1000.0


def bbox_from_center(
    lat: float, lon: float, radius_km: float
) -> Tuple[float, float, float, float]:
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    south = lat - dlat
    north = lat + dlat
    west = lon - dlon
    east = lon + dlon
    return (south, west, north, east)


def overpass_post(query: str, timeout: int = 60) -> Dict[str, Any]:
    last_err = None
    for url in OVERPASS_URLS:
        try:
            r = requests.post(url, data={"data": query}, headers=UA, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.2)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.5)
            continue
    raise RuntimeError(f"Overpass failed: {last_err}")


def overpass_trails_query(
    bbox: Tuple[float, float, float, float], max_relations: int = 50
) -> str:
    s, w, n, e = bbox
    return f"""
    [out:json][timeout:45];
    (
      relation["route"="hiking"]({s},{w},{n},{e});
    );
    out body {max_relations};
    >;
    out geom;
    """


def fetch_trails_relations(
    bbox: Tuple[float, float, float, float], max_relations: int = 50
) -> List[Dict[str, Any]]:
    q = overpass_trails_query(bbox, max_relations=max_relations)
    data = overpass_post(q, timeout=60)
    els = data.get("elements") or []
    rels = [el for el in els if el.get("type") == "relation"]
    return rels


def difficulty_label(sac_scale: str, dist_km: float) -> str:
    sac = (sac_scale or "").lower()
    if "demanding" in sac or "alpine" in sac:
        return "어려움"
    if dist_km >= 16:
        return "어려움"
    if dist_km >= 8:
        return "보통"
    return "쉬움"


# ===== 공공데이터(GPX) =====
def _bbox_intersects(
    a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]
) -> bool:
    a_s, a_w, a_n, a_e = a
    b_s, b_w, b_n, b_e = b
    return not (a_e < b_w or a_w > b_e or a_n < b_s or a_s > b_n)


def _parse_gpx_bounds_and_endpoints(path: Path) -> Optional[Dict[str, Any]]:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None

    ns = ""
    if "}" in root.tag:
        ns = root.tag.split("}")[0] + "}"

    trkpts = root.findall(f".//{ns}trkpt")
    if len(trkpts) < 2:
        return None

    lats = []
    lons = []
    for pt in trkpts:
        lat = pt.attrib.get("lat")
        lon = pt.attrib.get("lon")
        if lat is None or lon is None:
            continue
        lats.append(float(lat))
        lons.append(float(lon))

    if len(lats) < 2:
        return None

    start_lat, start_lon = lats[0], lons[0]
    end_lat, end_lon = lats[-1], lons[-1]

    name = path.stem
    b = (min(lats), min(lons), max(lats), max(lons))

    return {
        "name": name,
        "start_lat": start_lat,
        "start_lon": start_lon,
        "end_lat": end_lat,
        "end_lon": end_lon,
        "bbox": b,
        "path": str(path),
    }


def load_official_gpx_index(
    data_dir: str,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    max_files: int = 1500,
) -> List[Dict[str, Any]]:
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

        if bbox is not None and not _bbox_intersects(bbox, info["bbox"]):
            continue

        out.append(info)
        picked += 1

    return out


def match_official_by_endpoints(
    course_start: Tuple[float, float],
    course_end: Tuple[float, float],
    official_index: List[Dict[str, Any]],
) -> Dict[str, Any]:
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
        trust = max(0.0, 30.0 * (1.0 - (best_nearest / th)))
        return {
            "matched": True,
            "trust_score": round(trust, 3),
            "nearest_m": int(best_nearest),
            "official_name": best_name,
        }

    return {
        "matched": False,
        "trust_score": 0.0,
        "nearest_m": int(best_nearest),
        "official_name": best_name,
    }


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

    m = match_official_by_endpoints(start, end, official_index or [])
    trust_score = float(m["trust_score"])
    score_final = round(score_osm + trust_score, 3)

    return {
        "course_id": f"{name} ({dist_km}km)",
        "name": name,
        "distance_km": dist_km,
        "difficulty": diff,
        "score": score_final,
        "score_osm": score_osm,
        "trust_score": trust_score,
        "score_breakdown": {
            "members_term": round(math.log1p(len(members)) * 0.8, 3),
            "distance_term": round(math.log1p(dist_km) * 0.6, 3),
            "trust_score": trust_score,
            "score_osm": score_osm,
            "score_final": score_final,
            "members": int(len(members)),
        },
        "official_matched": bool(m["matched"]),
        "official_nearest_m": m["nearest_m"],
        "official_name": m["official_name"],
        "coords": latlon,
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
    if not api_key:
        raise ValueError("ORS_API_KEY is empty")

    latlon = _sample_latlon(latlon, max_points=min(ORS_MAX_VERTICES - 50, 1800))
    coords_lonlat = [[float(lon), float(lat)] for (lat, lon) in latlon]

    payload = {"format_in": "point", "format_out": "point", "geometry": coords_lonlat}
    headers = {"Authorization": api_key, **UA}

    r = requests.post(ORS_ELEVATION_LINE_URL, json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()

    coords = data.get("geometry", {}).get("coordinates") or []
    out: List[Tuple[float, float, float]] = []
    for c in coords:
        if len(c) >= 3:
            lon, lat, elev = c[0], c[1], c[2]
            out.append((float(lat), float(lon), float(elev)))
    return out


def elevation_profile(
    latlon: List[Tuple[float, float]], api_key: str
) -> List[Dict[str, float]]:
    """
    ✅ 기존: dist_km, elev_m
    ✅ 수정: dist_km, elev_m + lat, lon도 같이 제공(지도 고도 색칠용)
    """
    coords3d = ors_elevation_line(latlon, api_key=api_key)
    if len(coords3d) < 2:
        return []

    prof: List[Dict[str, float]] = []
    dist_km = 0.0

    prof.append(
        {
            "dist_km": 0.0,
            "elev_m": float(coords3d[0][2]),
            "lat": float(coords3d[0][0]),
            "lon": float(coords3d[0][1]),
        }
    )

    for i in range(1, len(coords3d)):
        prev = coords3d[i - 1]
        cur = coords3d[i]
        dist_km += haversine_m(prev[0], prev[1], cur[0], cur[1]) / 1000.0
        prof.append(
            {
                "dist_km": round(dist_km, 4),
                "elev_m": float(cur[2]),
                "lat": float(cur[0]),
                "lon": float(cur[1]),
            }
        )

    return prof
