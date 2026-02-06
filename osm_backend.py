def fetch_trails_relations(
    bbox: Tuple[float, float, float, float], max_relations: int = 50
) -> List[Dict[str, Any]]:
    s, w, n, e = bbox

    # ✅ 핵심: relation만 받고 끝내지 말고, 재귀( >; )로 멤버 way도 가져온다
    q = f"""
    [out:json][timeout:60];
    (
      relation["route"="hiking"]({s},{w},{n},{e});
      relation["route"="foot"]({s},{w},{n},{e});
    );
    out body;
    >;
    out geom;
    """

    data = overpass_post(q, timeout=75)
    elements = data.get("elements", [])

    # 1) way id -> geometry(lat/lon list) 매핑
    way_geom: Dict[int, List[Dict[str, float]]] = {}
    for el in elements:
        if el.get("type") == "way" and el.get("geometry"):
            wid = int(el.get("id"))
            way_geom[wid] = el["geometry"]

    # 2) relation만 추출
    rels = [el for el in elements if el.get("type") == "relation"]

    # 3) relation members에 geometry가 없으면 way_geom에서 채워넣기
    for rel in rels:
        members = rel.get("members") or []
        for m in members:
            if m.get("type") == "way" and not m.get("geometry"):
                ref = m.get("ref")
                if isinstance(ref, int) and ref in way_geom:
                    m["geometry"] = way_geom[ref]

    # 이름 있는 relation 우선
    rels_named = [r for r in rels if (r.get("tags") or {}).get("name")]
    rels = rels_named if rels_named else rels

    return rels[:max_relations]
