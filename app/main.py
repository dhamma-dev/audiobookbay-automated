"""Entrypoint: build the app and serve it.

    python main.py            # serve on 0.0.0.0:5078 (waitress)

Waitress is a production WSGI server; it runs ONE process with a thread pool,
which this app requires — the in-memory caches, the managed Tor process, and
the wanted worker all assume a single process. Don't put gunicorn with
multiple workers in front of this.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # before create_app so FLASK_SECRET_KEY etc. in .env are seen

from abb import create_app  # noqa: E402  (dotenv must load first)

app = create_app()

if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "5078")), threads=16)
