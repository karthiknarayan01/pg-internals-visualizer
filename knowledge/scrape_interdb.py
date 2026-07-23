"""One-time crawler for the interdb.jp PostgreSQL internals book.

Starts at the Part 1 (Process and Memory Architecture) index page and
follows same-site links under /pg/pgsql0*/ so the knowledge base covers
process/memory architecture, query processing, and concurrency control
chapters, not just the single page the user pointed at.

Usage: python knowledge/scrape_interdb.py
Writes one cleaned .txt file per page into knowledge/raw_pages/.
"""
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

START_URL = "https://www.interdb.jp/pg/pgsql01/index.html"
ALLOWED_PATH_RE = re.compile(r"^/pg/pgsql\d+/")
OUT_DIR = Path(__file__).parent / "raw_pages"
USER_AGENT = "pg-explain-visualizer-kb-builder/1.0 (local research tool)"


def is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == "www.interdb.jp" and bool(ALLOWED_PATH_RE.match(parsed.path))


def clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [line.strip() for line in main.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def crawl(start_url: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    seen = set()
    queue = [start_url]
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    while queue:
        url = queue.pop(0)
        if url in seen or not is_allowed(url):
            continue
        seen.add(url)

        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"skip {url}: {exc}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        text = clean_text(soup)

        slug = urlparse(url).path.strip("/").replace("/", "__") or "index"
        out_path = OUT_DIR / f"{slug}.txt"
        out_path.write_text(f"SOURCE: {url}\n\n{text}", encoding="utf-8")
        print(f"saved {out_path} ({len(text)} chars)")

        for link in soup.find_all("a", href=True):
            next_url = urljoin(url, link["href"]).split("#")[0]
            if is_allowed(next_url) and next_url not in seen:
                queue.append(next_url)

        time.sleep(0.5)  # be polite to the site


if __name__ == "__main__":
    crawl(START_URL)
    print(f"Done. Pages saved under {OUT_DIR}")
