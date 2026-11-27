"""Labs: opt-in experiments that don't belong in the main flow yet.

An experiment is one entry in EXPERIMENTS. Each is OFF until an admin
switches it on at /labs, and each states plainly what it sends out of the
box — Labs is where a deliberate exception to the app's privacy defaults can
live, behind a switch, instead of leaking into the core. The flags are stored
in SQLite, so Labs needs the data volume.
"""

from __future__ import annotations

EXPERIMENTS = [
    {
        "key": "series_sweep",
        "name": "Library series sweep",
        "icon": "library-big",
        "endpoint": "labs.series_page",
        "summary": "Finds books in your Audiobookshelf library that have no series "
                   "or no series number, proposes the label, and writes the ones "
                   "you approve. It only fills blanks, and every write can be undone.",
        "leaves": "For each unlabelled book, its Audible ID — or, without one, its "
                  "title and author — is sent directly to Audible's public catalogue "
                  "(the service Audiobookshelf's own Quick Match uses). The "
                  "\"subtitles only\" mode sends nothing at all.",
        "needs": "Audiobookshelf (ABS_URL + ABS_TOKEN), with a token allowed to update books.",
    },
]

BY_KEY = {e["key"]: e for e in EXPERIMENTS}


class Labs:
    def __init__(self, store):
        self.store = store

    @property
    def available(self):
        """Flags persist in SQLite; without the data volume nothing can be on."""
        return self.store.enabled

    def enabled(self, key):
        return key in BY_KEY and key in self.store.labs_enabled()

    def set(self, key, on, user):
        if key not in BY_KEY:
            raise KeyError(key)
        self.store.labs_set(key, on, user)

    def cards(self):
        on = self.store.labs_enabled()
        return [dict(e, enabled=e["key"] in on) for e in EXPERIMENTS]
