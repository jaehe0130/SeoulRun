import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

# 네이버 검색 API(블로그) -> 링크 수집 -> 블로그 본문에서 "코스" 문장 추출 -> JSON 저장

QUERY = "용산구 트레킹"
DISPLAY = 50  # 1~100
START = 1     # 1~1000
SAVE_DIR = "naver_blog_trekking"
OUTPUT_JSON = os.path.join(SAVE_DIR, "trekking_courses.json")

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)


def ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.mkdir(path)


def api_request(query: str, display: int, start: int) -> dict:
    enc_query = urllib.parse.quote(query)
    url = (
        "https://openapi.naver.com/v1/search/blog.json?"
        f"query={enc_query}&display={display}&start={start}&sort=sim"
    )
    req = urllib.request.Request(url)
    req.add_header("X-Naver-Client-Id", NAVER_CLIENT_ID)
    req.add_header("X-Naver-Client-Secret", NAVER_CLIENT_SECRET)
    with urllib.request.urlopen(req) as res:
        if res.status != 200:
            raise RuntimeError(f"HTTP {res.status}")
        data = res.read().decode("utf-8")
        return json.loads(data)


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as res:
        return res.read().decode("utf-8", errors="ignore")


def parse_blog_id_logno(url: str) -> tuple[str, str] | tuple[None, None]:
    patterns = [
        r"blogId=([^&]+)&logNo=(\d+)",
        r"blog.naver.com/([^/]+)/(\d+)",
        r"m.blog.naver.com/([^/]+)/(\d+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1), m.group(2)
    return None, None


def build_post_url(blog_id: str, log_no: str) -> str:
    return f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}&redirect=Dlog"


def strip_html(text: str) -> str:
    # script/style 제거
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    # 태그 제거
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    # 엔티티 처리
    text = html.unescape(text)
    # 공백 정리
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def extract_course_snippets(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    keywords = ["코스", "루트", "경로", "산책로"]
    return [s.strip() for s in sentences if any(k in s for k in keywords)]


def count_snippet_mentions(snippets: list[str]) -> dict[str, int]:
    return dict(Counter(snippets))


def fetch_course_snippets_from_blog(url: str) -> tuple[list[str], str]:
    blog_id, log_no = parse_blog_id_logno(url)
    target_url = build_post_url(blog_id, log_no) if blog_id and log_no else url
    html_doc = fetch_html(target_url)
    text = strip_html(html_doc)
    snippets = extract_course_snippets(text)
    return snippets, target_url


def main() -> None:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("에러: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 필요합니다.")
        sys.exit(1)

    ensure_dir(SAVE_DIR)

    all_items = []
    start = START

    while len(all_items) < DISPLAY and start <= 1000:
        remaining = DISPLAY - len(all_items)
        batch = min(100, remaining)
        result = api_request(QUERY, batch, start)
        items = result.get("items", [])
        if not items:
            break
        all_items.extend(items)
        start += batch
        time.sleep(0.2)

    results = []
    for item in all_items:
        link = item.get("link", "")
        description = html.unescape(item.get("description", "") or "")

        snippets = []
        source = "none"
        fetched_url = link
        error = ""

        try:
            snippets, fetched_url = fetch_course_snippets_from_blog(link)
            if snippets:
                source = "content"
            else:
                # 본문에서 못 찾으면 요약(description)에서 추출
                snippets = extract_course_snippets(description)
                if snippets:
                    source = "description"
        except Exception as e:
            error = str(e)
            snippets = extract_course_snippets(description)
            if snippets:
                source = "description"

        snippet_counts = count_snippet_mentions(snippets)
        total_mentions = sum(snippet_counts.values())

        results.append(
            {
                "title": html.unescape(item.get("title", "") or ""),
                "link": link,
                "fetched_url": fetched_url,
                "description": description,
                "course_snippets": snippets,
                "course_snippet_counts": snippet_counts,
                "course_mentions_count": total_mentions,
                "source": source,
                "error": error,
            }
        )

        time.sleep(0.2)

    output = {
        "query": QUERY,
        "count": len(results),
        "results": results,
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("완료")
    print(f"JSON 저장: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
