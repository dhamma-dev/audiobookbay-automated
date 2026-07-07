"""AudiobookBay scraping: search-result pages and magnet extraction.

`parse_search_page` is pure (HTML in, book dicts out) so the tests can feed it
fixtures. The Scraper knows about routing only through the Outbound object it
is given.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("abb.scraper")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36")

DEFAULT_TRACKERS = [
    "udp://tracker.openbittorrent.com:80",
    "udp://opentor.org:2710",
    "udp://tracker.ccc.de:80",
    "udp://tracker.blackunicorn.xyz:6969",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://tracker.leechers-paradise.org:6969",
]


def parse_search_page(html, hostname):
    """Parse one ABB search-results page into a list of book dicts. The
    mirror's markup is loose free text, so each field is regex-fished out of
    the post body and defaults to "Unknown" (language: "English") when absent."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for post in soup.select(".post"):
        try:
            title_element = post.select_one(".postTitle > h2 > a")
            if not title_element:
                continue

            title = title_element.text.strip()
            link = f"https://{hostname}{title_element['href']}"
            cover = post.select_one("img")["src"] if post.select_one("img") else \
                "/static/images/default-cover.svg"

            # Newlines flattened so the field regexes below can span lines.
            post_text = post.text.strip().replace("\n", " ")

            size = "Unknown"
            m = re.search(r"Size: ([\d.]+\s*[KMGT]B)", post_text)
            if m:
                size = m.group(1).strip()

            format_info = "Unknown"
            m = re.search(r"Format: ([^,]+)", post_text)
            if m:
                format_info = m.group(1).strip()
                if " / " in format_info:  # "MP3 / 64kbps" style
                    format_info = format_info.split(" / ")[0].strip()

            bitrate = "Unknown"
            m = re.search(r"Bitrate: ([^,]+)", post_text)
            if m:
                bitrate = m.group(1).strip()
                if "File Size:" in bitrate:
                    bitrate = bitrate.split("File Size:")[0].strip()

            language = "English"
            m = re.search(r"Language: ([^,]+)", post_text)
            if m:
                language = m.group(1).strip()
                if "Keywords:" in language:
                    language = language.split("Keywords:")[0].strip()

            keywords = []
            m = re.search(r"Keywords: ([^\.]+)", post_text)
            if m:
                keywords = [kw.strip() for kw in m.group(1).strip().split(",")]

            # Flag M4B (the preferred single-file audiobook format). Posts
            # aren't consistent about where they mention it, so check format,
            # title and keywords.
            haystack = f"{title} {format_info} {' '.join(keywords)}".lower()

            results.append({
                "title": title,
                "link": link,
                "cover": cover,
                "size": size,
                "format": format_info,
                "bitrate": bitrate,
                "language": language,
                "keywords": keywords,
                "is_m4b": "m4b" in haystack,
            })
        except Exception as e:
            log.error("error parsing post: %s", e)
            continue
    return results


class Scraper:
    def __init__(self, config, outbound):
        self.config = config
        self.outbound = outbound

    def _fetch_page(self, sess, query, page):
        """Fetch one ABB results page. Returns parsed books, [] when the page
        loads but has no posts, or None when the fetch itself failed — callers
        that need to tell "nothing there" from "couldn't reach the mirror"
        rely on that distinction."""
        url = f"https://{self.config.abb_hostname}/page/{page}/?s={quote_plus(query)}"
        try:
            response = sess.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=self.config.request_timeout)
            if response.status_code != 200:
                log.error("failed to fetch page %s: status %s", page, response.status_code)
                return None
            return parse_search_page(response.text, self.config.abb_hostname)
        except Exception as e:
            log.error("error fetching page %s: %s", page, e)
            return None

    def search(self, query, max_pages=5, sess=None):
        """Scrape ABB search results. Page 1 is fetched first (it answers "are
        there any results at all?" and most queries fit on it); the remaining
        pages are fetched CONCURRENTLY so total latency is ~2 round-trips
        instead of max_pages, and one stalled Tor stream can't serialize the
        rest. Results keep page order; pagination still stops at the first
        empty page.

        Returns None when the mirror couldn't be reached at all (page 1
        failed) — distinct from [] meaning "reached it, nothing found". `sess`
        overrides the per-request route session (used by the wanted worker)."""
        sess = sess or self.outbound.scrape_session()
        results = self._fetch_page(sess, query, 1)
        if results is None:
            return None
        if not results or max_pages < 2:
            return results
        with ThreadPoolExecutor(max_workers=max_pages - 1) as pool:
            pages = pool.map(lambda p: self._fetch_page(sess, query, p),
                             range(2, max_pages + 1))
        for page_results in pages:
            if not page_results:  # a failed or empty later page just ends the run
                break
            results.extend(page_results)
        return results

    def extract_magnet_link(self, details_url):
        """Read the Info Hash + trackers off a detail page and build a magnet
        link. Returns None on any failure."""
        try:
            response = self.outbound.scrape_session().get(
                details_url, headers={"User-Agent": USER_AGENT},
                timeout=self.config.request_timeout)
            if response.status_code != 200:
                log.error("failed to fetch details page: status %s", response.status_code)
                return None

            soup = BeautifulSoup(response.text, "html.parser")

            info_hash_row = soup.find("td", string=re.compile(r"Info Hash", re.IGNORECASE))
            if not info_hash_row:
                log.error("info hash not found on the page")
                return None
            info_hash = info_hash_row.find_next_sibling("td").text.strip()

            tracker_rows = soup.find_all("td", string=re.compile(r"udp://|http://", re.IGNORECASE))
            trackers = [row.text.strip() for row in tracker_rows] or list(DEFAULT_TRACKERS)

            trackers_query = "&".join(f"tr={requests.utils.quote(t)}" for t in trackers)
            magnet = f"magnet:?xt=urn:btih:{info_hash}&{trackers_query}"
            log.debug("generated magnet link: %s", magnet)
            return magnet
        except Exception as e:
            log.error("failed to extract magnet link: %s", e)
            return None


def infohash_from_magnet(magnet_link):
    m = re.search(r"btih:([0-9a-zA-Z]+)", magnet_link or "")
    return m.group(1).lower() if m else None
