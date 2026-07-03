"""
Audiobookshelf match scoring -- EXPERIMENTAL SPIKE.

Pure, dependency-free (stdlib only) matching logic shared by the spike CLI and,
if the feature graduates, the app. Nothing here has import side effects.

Design principle (see the README discussion): we only ever assert a *positive*
("you already own this") and only when confident. Everything else is UNKNOWN,
never a confident "you don't have it". So:

    STRONG  -> safe to badge "In your library"
    MAYBE   -> a plausible match; would stay hidden in the real UI, shown here
               only so we can eyeball recall and tune thresholds
    NONE    -> no usable match (this is NOT a claim that you don't own it)

Precision is protected by *gating title similarity on the author*: a strong
title match with a mismatched author is rejected (the "same title, different
book" case). A strong title match with no author to check is capped at MAYBE.

Thresholds are deliberately module-level constants -- the point of the spike is
to look at real scores and move these.
"""

import re
import unicodedata
from difflib import SequenceMatcher

# --- Tunables ----------------------------------------------------------------
STRONG_TITLE = 0.82   # title similarity needed for a STRONG (author-confirmed) match
MAYBE_TITLE = 0.62    # title similarity needed to surface as a MAYBE
AUTHOR_MIN = 0.55     # author similarity needed to confirm (or, if below, to reject)
SERIES_BONUS = 0.08   # score nudge when series name + number both line up

STRONG, MAYBE, NONE = "STRONG", "MAYBE", "NONE"

# Words that carry no identifying signal for an audiobook title.
_STOPWORDS = {"the", "a", "an", "and", "of", "to", "in"}
# Edition / format noise stripped before comparison.
_NOISE_RE = re.compile(
    r"\b(unabridged|abridged|audiobook|audio\s?book|m4b|mp3|complete|collection|"
    r"bundle|omnibus|box\s?set|boxset|read\s?by|narrated\s?by)\b",
    re.IGNORECASE,
)


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(s):
    """Lowercase, drop accents, parenthetical/bracketed tags, edition noise, and
    punctuation. Parentheses usually hold series tags ("(... #3)"), which we
    compare separately, so they're removed here."""
    s = _strip_accents(s or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s):
    return {t for t in normalize(s).split() if t and t not in _STOPWORDS}


def _seq(a, b):
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def token_set_ratio(a, b):
    """rapidfuzz-style token_set_ratio using only stdlib difflib. Order- and
    extra-word-tolerant, which matters because ABB titles carry series/edition
    cruft the ABS title doesn't."""
    A, B = tokens(a), tokens(b)
    if not A or not B:
        return _seq(normalize(a), normalize(b))
    inter = " ".join(sorted(A & B))
    a_rest = (inter + " " + " ".join(sorted(A - B))).strip()
    b_rest = (inter + " " + " ".join(sorted(B - A))).strip()
    return max(_seq(inter, a_rest), _seq(inter, b_rest), _seq(a_rest, b_rest))


def _split_authors(s):
    """Split a possibly multi-author credit into individual normalized names."""
    return [normalize(p) for p in re.split(r"[,;/&]| and ", s or "") if normalize(p)]


def _author_key(name):
    """(surname, first-initial) for a single normalized author name. A one-word
    credit is treated as a surname with no initial, so "Shirtaloon" stays
    compatible with "Travis Deverell Shirtaloon" instead of reading "S" vs "T"."""
    parts = name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], parts[0][0]


def author_similarity(a, b):
    """0..1, or None when either side has no author.

    Compares author *sets* (a book may credit several) and keys on the SURNAME:
    two names only count as the same author when their surnames agree, with a
    compatible first initial. A shared first name alone -- "Richard Kadrey" vs
    "Richard K. Morgan" -- scores below the gate, so it can't confirm a match."""
    if not a or not b:
        return None
    A, B = _split_authors(a), _split_authors(b)
    if not A or not B:
        return None
    best = 0.0
    for x in A:
        for y in B:
            (sx, ix), (sy, iy) = _author_key(x), _author_key(y)
            if sx and sx == sy:
                # Same surname; a missing initial (surname-only credit) is fine,
                # but a conflicting one is likely a different person.
                if not ix or not iy or ix == iy:
                    best = max(best, max(0.9, _seq(x, y)))
                else:
                    best = max(best, 0.5)
            else:
                # Different surname -> not the same author. Allow a fuzzy
                # full-name score only as a weak signal, capped below the gate.
                best = max(best, min(_seq(x, y), 0.45))
    return best


def parse_series(raw):
    """Best-effort (series_name, number) from a raw title. Handles "(Name #3)",
    "Name, Book 3", "Book 3", "Volume 3". Either element may be None."""
    if not raw:
        return None, None
    name, number = None, None
    m = re.search(r"\(([^)]*?)#\s*([\d]+(?:\.\d+)?)\s*\)", raw)
    if m:
        name = m.group(1).strip(" ,") or None
        number = m.group(2)
    if number is None:
        m = re.search(r"\b(?:book|vol(?:ume)?|part)\s*([\d]+(?:\.\d+)?)\b", raw, re.IGNORECASE)
        if m:
            number = m.group(1)
    return name, number


def _series_match(abb_raw, abs_series):
    """abs_series is a list of (name, sequence) tuples from Audiobookshelf."""
    _, abb_num = parse_series(abb_raw)
    if abb_num is None or not abs_series:
        return False
    for name, seq in abs_series:
        if seq and str(seq).rstrip("0").rstrip(".") == str(abb_num).rstrip("0").rstrip("."):
            return True
    return False


# --- Ownership guards --------------------------------------------------------
# These catch the "you own the work, but not THIS item" cases. They only ever
# downgrade a STRONG match (never upgrade), so they cannot create a false
# positive -- at worst they hide a badge we'd otherwise have shown.
_LANGS = (r"(spanish|espanol|español|french|francais|français|german|deutsch|"
          r"italian|italiano|portuguese|portugues|brazilian|russian|japanese|chinese|"
          r"mandarin|cantonese|korean|hindi|dutch|nederlands|polish|swedish|norwegian|"
          r"danish|finnish|czech|turkish|arabic|hebrew|greek|latin|vietnamese|thai|"
          r"indonesian|tagalog|ukrainian|romanian|hungarian|catalan)")
# A language named inside brackets/parens ("[Spanish Edition]", "(Hindi Edition)")
# or as an explicit "<language> edition/version/translation" marker.
_LANG_BRACKET_RE = re.compile(r"[\[(][^\])]*\b" + _LANGS + r"\b[^\])]*[\])]", re.IGNORECASE)
_LANG_EDITION_RE = re.compile(r"\b" + _LANGS + r"\s+(?:edition|version|translation|dub|language)\b", re.IGNORECASE)
# Multi-book markers: "Books 1-10", "Vol 1 - 3", "#1-12", "Bundle", "Box Set",
# "Omnibus", "Complete Collection", "Full Series".
_RANGE_RE = re.compile(r"\b(?:books?|vol(?:ume)?s?|parts?)\s*\.?\s*0*\d+\s*[-–—]\s*0*\d+", re.IGNORECASE)
_HASH_RANGE_RE = re.compile(r"#\s*0*\d+\s*[-–—]\s*0*\d+")
_BUNDLE_WORDS_RE = re.compile(
    r"\b(bundle|box\s?set|omnibus|anthology|complete\s+(?:series|collection|set)|full\s+series)\b",
    re.IGNORECASE,
)


def foreign_edition(raw, language=None):
    """Return the language name when a result is a non-English edition -- from a
    scraped Language field or an explicit title marker -- else None. Owning the
    English audiobook doesn't mean you own the Spanish or Hindi one."""
    if language:
        norm = language.strip().lower()
        if norm and norm not in ("english", "en", "eng", "unknown"):
            return language.strip().title()
    for rx in (_LANG_BRACKET_RE, _LANG_EDITION_RE):
        m = rx.search(raw or "")
        if m:
            return m.group(1).title()
    return None


def is_multi_volume(raw):
    """True when a title denotes a bundle / box set / range of books rather than
    a single volume, so a match against one owned volume shouldn't read as
    'you own this'."""
    raw = raw or ""
    return bool(_RANGE_RE.search(raw) or _HASH_RANGE_RE.search(raw) or _BUNDLE_WORDS_RE.search(raw))


def score_pair(abb, item):
    """Score one ABB result against one ABS item. `abb` and `item` are dicts.

    abb:  {title, author, asin?, isbn?, raw?, language?}
    item: {title, author, series:[(name,seq)], asin?, isbn?, language?}

    Returns (tier, score, reason).
    """
    # 1) Hard identifier match -- certain when we have it (rare on ABB).
    for key in ("asin", "isbn"):
        av, iv = (abb.get(key) or "").strip().lower(), (item.get(key) or "").strip().lower()
        if av and iv and av == iv:
            return STRONG, 1.0, f"{key.upper()} exact match"

    title_sim = token_set_ratio(abb.get("title", ""), item.get("title", ""))
    author_sim = author_similarity(abb.get("author"), item.get("author"))

    score = title_sim
    reason_bits = [f"title {title_sim:.2f}"]

    # Author gate. Present-and-mismatched actively rejects (different book);
    # present-and-agreeing confirms; absent leaves it unconfirmable.
    if author_sim is None:
        author_ok = None
        reason_bits.append("author n/a")
    elif author_sim < AUTHOR_MIN:
        reason_bits.append(f"author {author_sim:.2f} (mismatch)")
        return NONE, score, "; ".join(reason_bits) + " -> rejected"
    else:
        author_ok = True
        reason_bits.append(f"author {author_sim:.2f}")

    if _series_match(abb.get("raw") or abb.get("title", ""), item.get("series")):
        score = min(1.0, score + SERIES_BONUS)
        reason_bits.append("series+# match")

    if title_sim >= STRONG_TITLE and author_ok is True:
        tier = STRONG
    elif title_sim >= STRONG_TITLE and author_ok is None:
        tier = MAYBE  # strong title but no author to confirm -> stay cautious
    elif title_sim >= MAYBE_TITLE:
        tier = MAYBE
    else:
        tier = NONE

    # Ownership guards -- only ever demote a STRONG that would mislead. A foreign
    # edition (unless the owned item is that same language) or a bundle/range
    # matched against a single owned volume is not "this item you own".
    if tier == STRONG:
        abb_raw = abb.get("raw") or abb.get("title", "")
        lang = foreign_edition(abb_raw, abb.get("language"))
        if lang and foreign_edition(item.get("title", ""), item.get("language")) != lang:
            tier = MAYBE
            reason_bits.append(f"{lang} edition -> not the owned copy")
        elif is_multi_volume(abb_raw) and not is_multi_volume(item.get("title", "")):
            tier = MAYBE
            reason_bits.append("bundle/range vs single volume -> not this item")

    return tier, score, "; ".join(reason_bits)


_TIER_RANK = {NONE: 0, MAYBE: 1, STRONG: 2}


def best_match(abb, items):
    """Return (tier, score, item, reason) for the best-scoring ABS item, or
    (NONE, 0, None, "") when the library is empty. Ranks by tier first, then
    score, so a confirmed MAYBE beats a higher-scoring rejected candidate."""
    best = (NONE, 0.0, None, "no candidates")
    for item in items:
        tier, score, reason = score_pair(abb, item)
        cand = (tier, score, item, reason)
        if (_TIER_RANK[tier], score) > (_TIER_RANK[best[0]], best[1]):
            best = cand
    return best
