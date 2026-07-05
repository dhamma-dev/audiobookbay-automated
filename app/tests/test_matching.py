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
