"""The matcher's regression cases — the spike's offline selftest, promoted to
real assertions. Run after ANY change to abb/matching.py."""

import pytest

from abb import matching


def item(title, author, series=None, language="English"):
    return {"title": title, "author": author, "series": series or [],
            "asin": "", "isbn": "", "language": language}


LIBRARY = [
    item("The Steel Remains", "Richard K. Morgan", [("A Land Fit for Heroes", "1")]),
    item("Unsouled", "Will Wight", [("Cradle", "1")]),
    item("The Gathering Storm", "Robert Jordan", [("The Wheel of Time", "12")]),
    item("The Sandman", "Neil Gaiman, Dirk Maggs"),
    item("He Who Fights with Monsters 10", "Travis Deverell Shirtaloon",
         [("He Who Fights with Monsters", "10")]),
    item("No Man's Land", "Richard K. Morgan"),
]

CASES = [
    # (raw ABB title, expected tier, why)
    ("The Steel Remains (A Land Fit for Heroes #1) - Richard K. Morgan",
     matching.STRONG, "title+author+series"),
    ("Unsouled - Cradle Book 1 - Will Wight [Unabridged M4B]",
     matching.STRONG, "edition noise stripped"),
    ("The Gathering Storm - Kim Fielding",
     matching.NONE, "same title, WRONG author -> rejected"),
    ("The Steel Remains",
     matching.MAYBE, "title only, no author to confirm"),
    ("Some Book We Do Not Own - Nobody",
     matching.NONE, "no match"),
    ("The Sandman [Spanish Edition] (Libros 1-3) - Neil Gaiman",
     matching.MAYBE, "foreign edition -> not the owned copy"),
    ("The Sandman - Neil Gaiman, Dirk Maggs",
     matching.STRONG, "English original still matches"),
    ("He Who Fights with Monsters, Books 01-10 - Shirtaloon",
     matching.MAYBE, "bundle vs single owned volume"),
    ("Sandman Slim - Richard Kadrey",
     matching.NONE, "shared first name only -> author rejected"),
]


@pytest.mark.parametrize("raw,expected,why", CASES, ids=[c[2] for c in CASES])
def test_matcher_regressions(raw, expected, why):
    title, author = matching.split_title_author(raw)
    tier, _score, _item, reason = matching.best_match(
        {"raw": raw, "title": title, "author": author}, LIBRARY)
    assert tier == expected, f"{why}: got {tier} ({reason})"


# Owning one book of a series must not badge its siblings. The old token-set
# matcher scored "Dune" vs "Dune Messiah" 1.0 (one title's words are a subset
# of the other's) and, with the author agreeing, called every sequel owned.
SERIES_LIBRARY = [
    item("Dune", "Frank Herbert", [("Dune", "1")]),
    item("Foundation", "Isaac Asimov", [("Foundation", "1")]),
    item("The Fall of Hyperion", "Dan Simmons", [("Hyperion Cantos", "2")]),  # owns #2 only
    item("Dungeon Crawler Carl", "Matt Dinniman", [("Dungeon Crawler Carl", "1")]),
    item("He Who Fights with Monsters", "Travis Deverell Shirtaloon",
         [("He Who Fights with Monsters", "1")]),
    item("He Who Fights with Monsters 4", "Travis Deverell Shirtaloon",
         [("He Who Fights with Monsters", "4")]),
    item("The Final Empire", "Brandon Sanderson", [("Mistborn", "1")]),
    item("Words of Radiance", "Brandon Sanderson", [("The Stormlight Archive", "2")]),
    dict(item("Atomic Habits", "James Clear"),
         subtitle="An Easy & Proven Way to Build Good Habits & Break Bad Ones"),
    item("Catch-22", "Joseph Heller"),
    item("The Last Wish", "Andrzej Sapkowski", [("The Witcher", "1")]),
    item("Sapiens: A Brief History of Humankind", "Yuval Noah Harari"),
    item("Artemis Fowl", "Eoin Colfer", [("Artemis Fowl", "1")]),
]

IDENTITY_CASES = [
    # (raw ABB title, badge expected?, why)
    ("Dune Messiah - Frank Herbert", False, "sequel title contains book 1's"),
    ("Children of Dune - Frank Herbert", False, "sequel, extra words"),
    ("Foundation and Empire - Isaac Asimov", False, "sequel, extra words"),
    ("Hyperion - Dan Simmons", False, "own only the sequel: book 1 isn't owned"),
    ("Dungeon Crawler Carl Book 2: Carl's Doomsday Scenario - Matt Dinniman", False,
     "series prefix + another volume number"),
    ("He Who Fights with Monsters 5 - Shirtaloon", False, "numbered volume vs 1 and 4"),
    ("Dune - Frank Herbert [Unabridged] M4B", True, "edition cruft still matches"),
    ("Dune (Dune Chronicles #1) - Frank Herbert", True, "declared number agrees"),
    ("He Who Fights with Monsters 4 - Shirtaloon", True, "same numbered volume"),
    ("Dungeon Crawler Carl: A LitRPG/Gamelit Adventure - Matt Dinniman", True,
     "genre subtitle is filler"),
    ("Mistborn: The Final Empire (Mistborn #1) - Brandon Sanderson [M4B]", True,
     "series-name prefix is explained by the owned series"),
    ("Words of Radiance - Brandon Sanderson", True, "unnumbered listing of an owned #2"),
    ("Atomic Habits: An Easy & Proven Way to Build Good Habits & Break Bad Ones - James Clear",
     True, "subtitle matches the owned copy's"),
    ("Catch-22 - Joseph Heller", True, "a number that belongs to the title"),
    ("The Last Wish: Introducing the Witcher - Andrzej Sapkowski", True,
     "after-the-colon subtitle"),
    ("Sapiens - Yuval Noah Harari", True, "owned title has its subtitle baked in"),
    ("Artemis Fowl: The Arctic Incident - Eoin Colfer", False,
     "colon follows the series name: the rest is book 2's title"),
]


@pytest.mark.parametrize("raw,owned,why", IDENTITY_CASES, ids=[c[2] for c in IDENTITY_CASES])
def test_identity_guards(raw, owned, why):
    title, author = matching.split_title_author(raw)
    tier, _score, _item, reason = matching.best_match(
        {"raw": raw, "title": title, "author": author}, SERIES_LIBRARY)
    assert (tier == matching.STRONG) is owned, f"{why}: got {tier} ({reason})"


def test_identity_guards_on_clean_identities():
    # The wanted pipeline and smart sort's canonical join pass clean
    # title/author (+ an explicit number) rather than raw ABB titles.
    def strong(abb):
        return matching.best_match(abb, SERIES_LIBRARY)[0] == matching.STRONG
    assert not strong({"title": "Dune Messiah", "author": "Frank Herbert"})
    assert not strong({"title": "He Who Fights with Monsters 5", "author": "Shirtaloon"})
    assert strong({"title": "Dune", "author": "Frank Herbert"})
    # Hardcover titles carry subtitles; the owned copy's metadata may not.
    assert strong({"title": "The Last Wish: Introducing the Witcher",
                   "author": "Andrzej Sapkowski"})
    # A canonical number decides even when the titles agree completely.
    assert not strong({"title": "Dune", "author": "Frank Herbert", "series": "Dune", "seq": 2})
    assert strong({"title": "The Final Empire", "author": "Brandon Sanderson",
                   "series": "Mistborn", "seq": 1})


def test_series_numbers_compare_numerically():
    # Stripping zeros as text used to read book 10 as book 1.
    assert not matching._series_match("Cradle Book 1", [("Cradle", "10")])
    assert matching._series_match("Cradle Book 10", [("Cradle", "10.0")])
    assert matching._series_match("Edgedancer (Stormlight #2.5)", [("Stormlight", "2.50")])
    assert matching._norm_num("02") == "2" and matching._norm_num("Book 3") == "3"


def test_split_title_author():
    assert matching.split_title_author("Unsouled - Will Wight") == ("Unsouled", "Will Wight")
    # A long tail is title text, not an author credit.
    raw = "Something - with a very long hyphenated tail of many many words here"
    assert matching.split_title_author(raw)[1] == ""
    assert matching.split_title_author("Dune by Frank Herbert") == ("Dune", "Frank Herbert")


def test_foreign_edition_from_field_and_title():
    assert matching.foreign_edition("Whatever", "Spanish") == "Spanish"
    assert matching.foreign_edition("The Alchemist [Hindi Edition]") == "Hindi"
    assert matching.foreign_edition("The Alchemist", "English") is None


def test_is_multi_volume():
    assert matching.is_multi_volume("Cradle, Books 1-10")
    assert matching.is_multi_volume("Wheel of Time #1-14")
    assert matching.is_multi_volume("The Complete Collection Box Set")
    assert not matching.is_multi_volume("He Who Fights with Monsters 10")


def test_identifier_short_circuit():
    abb = {"title": "totally different", "author": "someone", "asin": "B00X"}
    lib = [dict(item("Real Title", "Real Author"), asin="B00X")]
    tier, score, _i, reason = matching.best_match(abb, lib)
    assert tier == matching.STRONG and score == 1.0 and "ASIN" in reason
