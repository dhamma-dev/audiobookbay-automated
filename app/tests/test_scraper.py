from abb.scraper import infohash_from_magnet, parse_search_page

FIXTURE = """
<html><body>
<div class="post">
  <div class="postTitle"><h2><a href="/abss/some-book/">Some Book - Author Name</a></h2></div>
  <div class="postContent">
    <img src="https://images.example-mirror.lu/covers/1.jpg">
    <p>Posted: 12 May 2026 Format: MP3 / 45kbps, Bitrate: 45 Kbps File Size: 545.34 MBs</p>
    <p>Language: English Keywords: fantasy, litrpg.</p>
  </div>
</div>
<div class="post">
  <div class="postTitle"><h2><a href="/abss/other-book/">Other Book - Someone Else</a></h2></div>
  <div class="postContent">
    <p>Bitrate: 128 Kbps, Language: German Keywords: thriller, m4b.</p>
  </div>
</div>
<div class="post">
  <div class="postContent"><p>malformed post with no title link</p></div>
</div>
</body></html>
"""


def test_parse_search_page_fields():
    books = parse_search_page(FIXTURE, "audiobookbay.lu")
    assert len(books) == 2  # the malformed post is skipped, not fatal

    first, second = books
    assert first["title"] == "Some Book - Author Name"
    assert first["link"] == "https://audiobookbay.lu/abss/some-book/"
    assert first["cover"] == "https://images.example-mirror.lu/covers/1.jpg"
    assert first["size"] == "545.34 MB"           # fished out of "File Size:"
    assert first["format"] == "MP3"               # "MP3 / 45kbps" -> "MP3"
    assert first["bitrate"] == "45 Kbps"          # "File Size:" tail stripped
    assert first["language"] == "English"         # "Keywords:" tail stripped
    assert first["keywords"] == ["fantasy", "litrpg"]
    assert first["is_m4b"] is False

    assert second["format"] == "Unknown"
    assert second["language"] == "German"
    assert second["is_m4b"] is True               # flagged via keywords


def test_parse_search_page_defaults():
    html = ('<div class="post"><div class="postTitle"><h2>'
            '<a href="/abss/x/">Bare Post</a></h2></div></div>')
    (book,) = parse_search_page(html, "audiobookbay.lu")
    assert book["size"] == "Unknown"
    assert book["language"] == "English"          # the mirror's usual default
    assert book["cover"] == "/static/images/default-cover.svg"


DETAIL_PAGE = """
<table>
  <tr><td>Info Hash:</td><td>ABCDEF0123456789ABCDEF0123456789ABCDEF01</td></tr>
  <tr><td>udp://tracker.example.org:1337/announce</td></tr>
</table>
"""


def test_magnet_carries_a_display_name():
    """dn= makes the download client show the book, not a 40-char hash,
    until metadata arrives — encoded so '&'/':' in titles can't break it."""
    from abb.scraper import Scraper
    from tests.conftest import make_config

    class Resp:
        status_code, text = 200, DETAIL_PAGE

    class Sess:
        def get(self, url, headers=None, timeout=None):
            return Resp()

    scraper = Scraper(make_config(), outbound=None)
    magnet = scraper.extract_magnet_link("https://audiobookbay.lu/abss/x/", sess=Sess(),
                                         title="Atomic Habits: An Easy & Proven Way")
    assert magnet.startswith("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01"
                             "&dn=Atomic%20Habits%3A%20An%20Easy%20%26%20Proven%20Way&tr=udp")
    assert infohash_from_magnet(magnet) == "abcdef0123456789abcdef0123456789abcdef01"
    # No title (older callers): same magnet as before, just without dn=.
    assert "&dn=" not in scraper.extract_magnet_link("https://audiobookbay.lu/abss/x/", sess=Sess())


def test_infohash_from_magnet():
    assert infohash_from_magnet("magnet:?xt=urn:btih:ABC123&tr=x") == "abc123"
    assert infohash_from_magnet(None) is None
