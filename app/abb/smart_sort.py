"""Gemini calls: smart-sort ranking and the wanted-list verdict.

Privacy boundary (deliberate — do not widen): only the fields in RANK_FIELDS
plus the stable per-result id are ever sent. Links, covers, and the mirror
hostname never leave the box, and the user's library is never in a prompt —
ownership is joined locally (see library.py). Calls go straight to Google's
API; Tor shields only the AudiobookBay scrape.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict

log = logging.getLogger("abb.rank")

# Fields we are willing to forward to Gemini. Everything else (link, cover,
# is_m4b, etc.) is dropped before the request is built.
RANK_FIELDS = ("title", "format", "bitrate", "language", "size", "keywords")

RANK_SYSTEM_INSTRUCTION = (
    "You help a user sort noisy audiobook search results. You are given the "
    "user's search query and a list of candidate audiobooks (each with an id "
    "and metadata). The source site's search is unreliable and often returns "
    "irrelevant items, so your job is to rank the candidates by how well they "
    "match what the user is most likely looking for.\n"
    "Rules:\n"
    "- Rank strictly by relevance to the query (title/author/series match).\n"
    "- Break genuine ties by preferring M4B format, then higher bitrate.\n"
    "- Bucket each item: 'strong' (clearly matches), 'possible' (might match), "
    "or 'unlikely' (almost certainly not what they want).\n"
    "- If the query could plausibly refer to several distinct works or topics, "
    "set ambiguous=true and list the interpretations, each with the ids it "
    "covers. Otherwise set ambiguous=false and return an empty interpretations "
    "list.\n"
    "Series grouping:\n"
    "- If several candidates are entries in the same series, add a 'series' "
    "block: the series label, and an ordered list of entries (one per distinct "
    "book, in reading order). Use your own knowledge of the series to name and "
    "order the books and to give each a canonical title -- but every id you emit "
    "MUST be a real candidate id. Never invent a book that has no matching "
    "candidate, and only add a series block when two or more distinct books are "
    "genuinely present.\n"
    "- For each entry choose the single best edition as best_id and list the rest "
    "as alt_ids (best first). Selection rules, in order: prefer M4B; then a "
    "healthy bitrate; demote suspiciously low bitrate (<=64 kbps) and files whose "
    "size looks too small to be a complete book; never silently prefer an "
    "abridged, TTS/AI-narrated, or wrong-language upload over a clean full one.\n"
    "- When you demote an edition keep it in alt_ids and set alt_note to the axis "
    "it differs on (e.g. 'format', 'bitrate', 'abridged', 'language').\n"
    "- An abridged edition or a different narrator is its OWN entry, never an "
    "alternative of the unabridged one.\n"
    "- Number entries by their position in the series (seq). Include an entry for "
    "every book from book 1 up to the highest-numbered book present, in reading "
    "order. If a book in that run has no matching candidate, still include its "
    "entry (seq + title) but omit best_id -- that marks a gap. Never invent books "
    "beyond the highest one present.\n"
    "- If you confidently know the series' full length, set 'total' to it (so the "
    "UI can say 'X of Y'). Omit 'total' if unsure.\n"
    "- An omnibus/box-set/collection upload (one file spanning several books) "
    "goes in the 'collections' array, not in 'entries': give its id, a title, and "
    "'covers' = the list of book numbers (seq) it contains. Do not also use that "
    "id as an entry's best_id.\n"
    "- If nothing forms a series, return an empty 'series' list.\n"
    "Editions (standalone books):\n"
    "- When several candidates are the same standalone work (not part of a "
    "series block) -- different uploads of one book -- group them in 'editions': "
    "pick the best edition as best_id and list the rest as alt_ids, using the "
    "same quality rules as above and an alt_note for what each differs on. "
    "Different works stay separate, and a book with only one upload needs no "
    "edition entry. Never put a book that is already in a series block here.\n"
    "- If nothing needs grouping, return an empty 'editions' list.\n"
    "Return every input id exactly once in 'ordering', best match first."
)

# JSON shape we ask Gemini to return. Kept flat and id-based so a chatty or
# truncated response still maps cleanly back onto the rendered cards.
RANK_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ordering": {"type": "array", "items": {"type": "integer"}},
        "buckets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "bucket": {"type": "string",
                               "enum": ["strong", "possible", "unlikely"]},
                },
                "required": ["id", "bucket"],
            },
        },
        "ambiguous": {"type": "boolean"},
        "interpretations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "result_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["label", "result_ids"],
            },
        },
        "series": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "total": {"type": "integer"},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "seq": {"type": "integer"},
                                "title": {"type": "string"},
                                "best_id": {"type": "integer"},
                                "alt_ids": {"type": "array", "items": {"type": "integer"}},
                                "alt_note": {"type": "string"},
                            },
                            "required": ["seq", "title"],
                        },
                    },
                    "collections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "title": {"type": "string"},
                                "covers": {"type": "array", "items": {"type": "integer"}},
                            },
                            "required": ["id", "title", "covers"],
                        },
                    },
                },
                "required": ["label", "entries"],
            },
        },
        "editions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "best_id": {"type": "integer"},
                    "alt_ids": {"type": "array", "items": {"type": "integer"}},
                    "alt_note": {"type": "string"},
                },
                "required": ["best_id", "alt_ids"],
            },
        },
    },
    "required": ["ordering", "buckets", "ambiguous", "interpretations", "series", "editions"],
}

# Asked for only when ABS matching is on, so the model canonicalizes each
# public result into a clean identity the app can join to the library LOCALLY.
# No library data is ever put in the prompt.
RANK_CANONICALIZE_INSTRUCTION = (
    "\nCanonicalize (for local library matching):\n"
    "- Return a 'canonical' array of clean identities used to match results "
    "against the user's library. Include an entry ONLY for candidates you did "
    "NOT place in a 'series' block above -- i.e. standalone books, and lone "
    "series books with too few results to form a shelf. For books already in a "
    "series block we read the series and number straight from the shelf, so "
    "listing them here too is redundant work. For each included id give the "
    "work's canonical title, its primary author, and -- if it belongs to a "
    "series -- the series name and number (seq), using your knowledge to resolve "
    "variant/bare titles (e.g. a lone 'Dreadgod' is Cradle #11). You are NOT "
    "given the user's library and must not guess what they own."
)

WANTED_VERDICT_INSTRUCTION = (
    "You verify audiobook listings against ONE specific wanted book. You get "
    "the wanted book's full title and author(s), and a list of listings (id, "
    "title, format, bitrate, size, language) from a DELIBERATELY BROAD "
    "torrent-site search -- most listings may be other works that merely share "
    "words with the title, so be strict on identity.\n"
    "Rules:\n"
    "- A listing matches only if it IS that exact work by that author, as a "
    "complete audiobook. Match on the work, not the wording: the wanted title "
    "may carry subtitles or volume designators the listing omits (and vice "
    "versa), and the wanted author list may include co-authors, illustrators "
    "or narrators the listing doesn't name. Different works, other "
    "volumes/entries in the same series, samples, and request posts are NOT "
    "matches.\n"
    "- Rank matching listings best-first: prefer M4B, then a healthy bitrate, "
    "then completeness; demote abridged, dramatized/adaptation, AI/TTS-narrated "
    "and suspiciously small files -- and give such listings a short note "
    "(e.g. 'abridged', 'AI-narrated', 'low bitrate').\n"
    "- If the wanted item cannot exist as an audiobook (e.g. it is a comic or "
    "graphic novel), set match_found=false and say so in reason.\n"
    "- reason is one short human sentence explaining the pick or the rejection."
)

WANTED_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "match_found": {"type": "boolean"},
        "ranked": {"type": "array", "items": {"type": "integer"}},
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "note": {"type": "string"}},
                "required": ["id", "note"],
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["match_found", "ranked", "notes", "reason"],
}


def rank_payload(books):
    """The slim, privacy-conscious payload sent to Gemini for re-ranking: the
    stable id plus RANK_FIELDS, nothing else."""
    payload = []
    for book in books:
        item = {"id": book["id"]}
        for field in RANK_FIELDS:
            value = book.get(field)
            if value:
                item[field] = value
        payload.append(item)
    return payload


def sanitize_results(incoming):
    """Server-side re-sanitize of client-posted results: only ids and the
    allowed fields are ever forwarded to Gemini, regardless of what was sent."""
    results = []
    for item in incoming:
        if not isinstance(item, dict) or "id" not in item:
            continue
        slim = {"id": item["id"]}
        for field in RANK_FIELDS:
            value = item.get(field)
            if value:
                slim[field] = value
        results.append(slim)
    return results


def _is_quota_error(e):
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


def _schema_with_canonical():
    """A deep copy of the base schema with the per-result 'canonical' block
    added and required, used only when ABS matching is on."""
    schema = json.loads(json.dumps(RANK_RESPONSE_SCHEMA))
    schema["properties"]["canonical"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "author": {"type": "string"},
                "series": {"type": "string"},
                "seq": {"type": "number"},
            },
            "required": ["id"],
        },
    }
    schema["required"] = schema["required"] + ["canonical"]
    return schema


class RankService:
    """Owns the Gemini client config, the rank cache, and the shared
    "does this model support a thinking budget" flag."""

    _CACHE_MAX = 32

    def __init__(self, config):
        self.config = config
        self.enabled = config.smart_sort_enabled
        # Flipped off once the model rejects a thinking budget, so a failed
        # call isn't paid again on every rank.
        self._thinking_supported = True
        # Completed rankings cached briefly so re-running the same search (a
        # second tab, a reload with prefetch on) doesn't pay Gemini — or its
        # latency — again. Stores the raw response text so every hit
        # deserializes to a fresh object (resolve_ownership mutates its
        # argument).
        self._cache = OrderedDict()  # key -> (expires_at, response_json_text)
        self._cache_lock = threading.Lock()

    # --- cache ------------------------------------------------------------------
    def _cache_key(self, query, results, want_ownership):
        payload = json.dumps(results, sort_keys=True, ensure_ascii=False)
        return (query, want_ownership, self.config.rank_model,
                self.config.rank_thinking_budget, hash(payload))

    def _cache_get(self, key):
        with self._cache_lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            expires_at, text = hit
            if time.monotonic() > expires_at:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return text

    def _cache_put(self, key, text):
        with self._cache_lock:
            self._cache[key] = (time.monotonic() + self.config.rank_cache_ttl, text)
            self._cache.move_to_end(key)
            while len(self._cache) > self._CACHE_MAX:
                self._cache.popitem(last=False)

    # --- Gemini plumbing ----------------------------------------------------------
    def _generate(self, system_instruction, schema, prompt):
        """One structured generate_content call with the shared temperature /
        thinking-budget handling. A budget the model rejects is retried once
        without and then never sent again."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.config.gemini_api_key)
        config_kwargs = dict(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0,
        )
        if self.config.rank_thinking_budget is not None and self._thinking_supported:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.config.rank_thinking_budget)

        def call():
            return client.models.generate_content(
                model=self.config.rank_model, contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs))

        try:
            return call()
        except Exception as e:
            # The retry exists for models that reject a thinking budget. A
            # quota/rate error (429) would fail the retry identically — and
            # permanently disabling the fast thinking=0 path over a billing
            # blip would silently slow every later call.
            if "thinking_config" not in config_kwargs or _is_quota_error(e):
                raise
            self._thinking_supported = False
            log.warning("thinking_budget=%s failed (%s); retrying without it.",
                        self.config.rank_thinking_budget, e)
            config_kwargs.pop("thinking_config")
            return call()

    # --- smart sort -----------------------------------------------------------------
    def rank(self, query, results, want_ownership=False):
        """Ask Gemini to re-rank already-scraped results. Returns the parsed
        JSON dict. Raises on any transport or parsing failure so the caller
        can fall back to the existing order. When want_ownership is set, also
        asks for a per-result 'canonical' identity (for local library
        matching); the base behaviour is byte-identical otherwise."""
        cache_key = self._cache_key(query, results, want_ownership)
        cached = self._cache_get(cache_key)
        if cached is not None:
            log.info("cache hit for %r (%d results)", query, len(results))
            return json.loads(cached)

        system_instruction = RANK_SYSTEM_INSTRUCTION
        if self.config.preferred_language:
            lang = self.config.preferred_language
            system_instruction += (
                f"\nLanguage: the user listens in {lang}. Rank editions "
                f"in other languages far below {lang} ones, bucket "
                "clearly wrong-language items as 'unlikely', and never pick a "
                "wrong-language upload as a series/edition best_id when a "
                f"{lang} one exists."
            )
        schema = RANK_RESPONSE_SCHEMA
        if want_ownership:
            system_instruction += RANK_CANONICALIZE_INSTRUCTION
            schema = _schema_with_canonical()
        prompt = (
            f"User search query: {query}\n\n"
            f"Candidates (JSON):\n{json.dumps(results, ensure_ascii=False)}"
        )

        started = time.monotonic()
        response = self._generate(system_instruction, schema, prompt)
        log.info("%s thinking=%s %d results in %.1fs", self.config.rank_model,
                 self.config.rank_thinking_budget, len(results),
                 time.monotonic() - started)
        ranking = json.loads(response.text)  # validate before caching
        self._cache_put(cache_key, response.text)
        return ranking

    # --- wanted verdict ---------------------------------------------------------------
    def wanted_verdict(self, title, author, listings):
        """Rate a successful wanted search's listings against the wanted
        identity with one small Gemini call. Returns the parsed verdict dict,
        or None on any failure/disablement so the caller can fall back to the
        deterministic pick."""
        if not (self.enabled and self.config.wanted_llm and listings):
            return None
        try:
            system_instruction = WANTED_VERDICT_INSTRUCTION
            if self.config.preferred_language:
                system_instruction += (
                    f"\n- The user listens in {self.config.preferred_language}; "
                    "rank other languages below it and note them.")
            prompt = json.dumps({"wanted": {"title": title, "author": author},
                                 "listings": listings}, ensure_ascii=False)
            started = time.monotonic()
            response = self._generate(system_instruction, WANTED_VERDICT_SCHEMA, prompt)
            verdict = json.loads(response.text)
            log.info("verdict for %r: match=%s in %.1fs", title,
                     verdict.get("match_found"), time.monotonic() - started)
            return verdict
        except Exception as e:
            log.warning("wanted verdict failed for %r: %s", title, e)
            return None
