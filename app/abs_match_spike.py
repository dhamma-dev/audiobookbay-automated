"""
Audiobookshelf match spike -- EXPERIMENTAL, safe to delete.

Pulls your Audiobookshelf library, scrapes a real AudioBook Bay search, and
prints how well each ABB result matches something you already own -- so we can
judge precision/recall on YOUR data before committing to any UI.

Run it inside the running container so it reuses the app's Tor and .env:

    docker compose exec audiobookbay-automated \
        python abs_match_spike.py "cradle" "land fit for heroes"

Offline logic check (no ABS or ABB needed):

    docker compose exec audiobookbay-automated python abs_match_spike.py --selftest

Environment (add ABS_* to your .env):
    ABS_URL          e.g. https://audiobooks.example.com   (required for live runs)
    ABS_TOKEN        Audiobookshelf API token (Settings -> Users -> your user)
    ABS_LIBRARY_ID   optional; defaults to the first "book" library
    ABB_HOSTNAME     optional; defaults to audiobookbay.lu (shared with the app)
    TOR_SOCKS_PORT   optional; defaults to 9050 (the app's Tor)
"""

import os
import re
import sys
import requests
from bs4 import BeautifulSoup

import abs_match

# .strip() guards against trailing spaces/newlines in .env values, which would
# otherwise produce an invalid URL like "http://host:8080 /api/libraries".
ABS_URL = (os.getenv("ABS_URL") or "").strip().rstrip("/")
ABS_TOKEN = (os.getenv("ABS_TOKEN") or "").strip()
ABS_LIBRARY_ID = (os.getenv("ABS_LIBRARY_ID") or "").strip()
ABB_HOSTNAME = os.getenv("ABB_HOSTNAME", "audiobookbay.lu")
TOR_SOCKS_PORT = os.getenv("TOR_SOCKS_PORT", "9050")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"}

TIER_MARK = {abs_match.STRONG: "✓ STRONG", abs_match.MAYBE: "~ maybe ", abs_match.NONE: "· none  "}


# --- Audiobookshelf ----------------------------------------------------------
def _abs_get(path, **params):
    r = requests.get(f"{ABS_URL}{path}", headers={"Authorization": f"Bearer {ABS_TOKEN}"},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def pick_library():
    if ABS_LIBRARY_ID:
        return ABS_LIBRARY_ID, ABS_LIBRARY_ID
    libs = _abs_get("/api/libraries").get("libraries", [])
    for lib in libs:
        if lib.get("mediaType") == "book":
            return lib["id"], lib.get("name", lib["id"])
    if libs:
        return libs[0]["id"], libs[0].get("name", libs[0]["id"])
    raise SystemExit("No Audiobookshelf libraries found.")


def load_library(library_id):
    """Return a list of {title, author, series:[(name,seq)], asin, isbn}."""
    items, page, limit = [], 0, 500
    while True:
        data = _abs_get(f"/api/libraries/{library_id}/items", limit=limit, page=page)
        results = data.get("results", [])
        for it in results:
            md = (it.get("media") or {}).get("metadata") or {}
            authors = md.get("authors") or []
            author = md.get("authorName") or ", ".join(a.get("name", "") for a in authors)
            series = [(s.get("name"), s.get("sequence")) for s in (md.get("series") or [])]
            items.append({
                "title": md.get("title") or "",
                "author": author,
                "series": series,
                "asin": md.get("asin") or "",
                "isbn": md.get("isbn") or "",
                "language": md.get("language") or "",
            })
        if len(results) < limit or (data.get("total") and len(items) >= data["total"]):
            break
        page += 1
    return items


# --- AudioBook Bay (minimal scrape, routed through the app's Tor) ------------
def _session():
    """Reuse the container's Tor SOCKS proxy if it's up; fall back to direct."""
    s = requests.Session()
    proxy = f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}"
    s.proxies = {"http": proxy, "https": proxy}
    try:
        s.get("https://check.torproject.org/api/ip", timeout=15)
        print(f"  (routing ABB through Tor on 127.0.0.1:{TOR_SOCKS_PORT})")
        return s
    except Exception:
        print("  (Tor SOCKS not reachable -- scraping ABB DIRECTLY, your real IP is exposed)")
        s.proxies = {}
        return s


def _split_title_author(raw):
    return abs_match.split_title_author(raw)


def search_abb(session, query, max_pages=2):
    results = []
    for page in range(1, max_pages + 1):
        url = f"https://{ABB_HOSTNAME}/page/{page}/?s={query.replace(' ', '+')}"
        resp = session.get(url, headers=_UA, timeout=60)
        if resp.status_code != 200:
            break
        posts = BeautifulSoup(resp.text, "html.parser").select(".post")
        if not posts:
            break
        for post in posts:
            a = post.select_one(".postTitle > h2 > a")
            if not a:
                continue
            raw = a.text.strip()
            title, author = _split_title_author(raw)
            # Grab the post's Language field (like the app does) so the foreign
            # -edition guard can use it alongside any marker in the title.
            lm = re.search(r"Language:\s*([A-Za-z]+)", post.get_text(" ", strip=True))
            language = lm.group(1) if lm else ""
            results.append({"raw": raw, "title": title, "author": author, "language": language})
    return results


# --- Reporting ---------------------------------------------------------------
def run_query(session, library, query):
    print(f"\n=== '{query}' ===")
    hits = search_abb(session, query)
    if not hits:
        print("  no ABB results")
        return 0, 0, 0
    counts = {abs_match.STRONG: 0, abs_match.MAYBE: 0, abs_match.NONE: 0}
    for abb in hits:
        tier, score, item, reason = abs_match.best_match(abb, library)
        counts[tier] += 1
        label = TIER_MARK[tier]
        left = abb["raw"][:58].ljust(58)
        if item:
            right = f"-> {item['title']} / {item['author']}  [{score:.2f}; {reason}]"
        else:
            right = f"[{reason}]"
        print(f"  {label}  {left}  {right}")
    print(f"  -- {counts[abs_match.STRONG]} strong, {counts[abs_match.MAYBE]} maybe, "
          f"{counts[abs_match.NONE]} none of {len(hits)}")
    return counts[abs_match.STRONG], counts[abs_match.MAYBE], counts[abs_match.NONE]


# --- Offline self-test (demonstrates the matcher without ABS/ABB) ------------
def selftest():
    def item(title, author, series=None, language="English"):
        return {"title": title, "author": author, "series": series or [],
                "asin": "", "isbn": "", "language": language}

    library = [
        item("The Steel Remains", "Richard K. Morgan", [("A Land Fit for Heroes", "1")]),
        item("Unsouled", "Will Wight", [("Cradle", "1")]),
        item("The Gathering Storm", "Robert Jordan", [("The Wheel of Time", "12")]),
        item("The Sandman", "Neil Gaiman, Dirk Maggs"),
        item("He Who Fights with Monsters 10", "Travis Deverell Shirtaloon",
             [("He Who Fights with Monsters", "10")]),
        item("No Man's Land", "Richard K. Morgan"),
    ]
    cases = [
        ("The Steel Remains (A Land Fit for Heroes #1) - Richard K. Morgan", "STRONG (title+author+series)"),
        ("Unsouled - Cradle Book 1 - Will Wight [Unabridged M4B]", "STRONG (noise stripped)"),
        ("The Gathering Storm - Kim Fielding", "NONE (same title, WRONG author -> rejected)"),
        ("The Steel Remains", "MAYBE (title only, no author to confirm)"),
        ("Some Book We Do Not Own - Nobody", "NONE (no match)"),
        # Guards added after the first real run:
        ("The Sandman [Spanish Edition] (Libros 1-3) - Neil Gaiman", "MAYBE (foreign edition -> not owned copy)"),
        ("The Sandman - Neil Gaiman, Dirk Maggs", "STRONG (English original, still matches)"),
        ("He Who Fights with Monsters, Books 01-10 - Shirtaloon", "MAYBE (bundle vs single owned volume)"),
        ("Sandman Slim - Richard Kadrey", "NONE (shared first name only -> author rejected)"),
    ]
    print("Self-test (thresholds: STRONG_TITLE=%.2f MAYBE_TITLE=%.2f AUTHOR_MIN=%.2f)\n"
          % (abs_match.STRONG_TITLE, abs_match.MAYBE_TITLE, abs_match.AUTHOR_MIN))
    for raw, expect in cases:
        title, author = _split_title_author(raw)
        tier, score, item, reason = abs_match.best_match(
            {"raw": raw, "title": title, "author": author}, library)
        matched = item["title"] if item else "-"
        print(f"  {TIER_MARK[tier]}  {raw[:55].ljust(55)}  [{score:.2f}] {matched}")
        print(f"          expected: {expect}\n          reason:   {reason}\n")


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        return
    if not ABS_URL or not ABS_TOKEN:
        raise SystemExit("Set ABS_URL and ABS_TOKEN (in .env) for a live run, "
                         "or use --selftest for an offline logic check.")
    queries = args or ["cradle"]
    lib_id, lib_name = pick_library()
    print(f"Loading Audiobookshelf library '{lib_name}'...")
    library = load_library(lib_id)
    print(f"Loaded {len(library)} items.")
    session = _session()
    totals = [0, 0, 0]
    for q in queries:
        s, m, n = run_query(session, library, q)
        totals[0] += s; totals[1] += m; totals[2] += n
    print(f"\nTOTAL across {len(queries)} query(ies): "
          f"{totals[0]} strong, {totals[1]} maybe, {totals[2]} none")
    print("Only STRONG would show as an 'In your library' badge. Tune thresholds "
          "at the top of abs_match.py and re-run.")


if __name__ == "__main__":
    main()
