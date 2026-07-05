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


def test_infohash_from_magnet():
    assert infohash_from_magnet("magnet:?xt=urn:btih:ABC123&tr=x") == "abc123"
    assert infohash_from_magnet(None) is None
