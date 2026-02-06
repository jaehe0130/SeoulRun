from __future__ import annotations

from typing import Any, Dict, List, Optional
import requests


def kakao_keyword_search(
    *,
    query: str,
    category: str | None = None,
    x: float,
    y: float,
    radius: int = 1200,
    size: int = 10,
    api_key: str,
    sort: str = "distance",
) -> List[Dict[str, Any]]:
    """Kakao Local Keyword Search (REST).

    Args:
        query: keyword (e.g., "카페", "맥주", "맛집")
        category: category_group_code (e.g., "CE7" cafe, "FD6" food)
        x, y: center coordinates (x=lon, y=lat)
        radius: meters (max 20000 by Kakao)
        size: results per page (max 15 by Kakao for keyword endpoint)
        api_key: Kakao REST API Key
        sort: "distance" or "accuracy"

    Returns:
        List of Kakao place dicts (docs) as returned by Kakao.
    """
    if not api_key:
        return []

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {api_key}"}

    params: Dict[str, Any] = {
        "query": query,
        "x": x,
        "y": y,
        "radius": int(radius),
        "size": int(min(max(size, 1), 15)),
        "sort": sort,
    }
    if category:
        params["category_group_code"] = category

    r = requests.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data.get("documents", []) or []
