"""AudiobookBay Automated — application package.

Importing this package (or any module in it) has no side effects: no Tor
process, no disk writes, no network. Everything is wired together by
``create_app()``, so tests and scripts can import freely.
"""

from .factory import create_app

__all__ = ["create_app"]
