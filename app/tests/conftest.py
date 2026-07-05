import os
import sys

import pytest

# Tests import the package from the source tree: app/tests -> app on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from abb import create_app          # noqa: E402
from abb.config import Config       # noqa: E402


def make_config(**overrides) -> Config:
    """A quiet, side-effect-free config: no log DB, no Tor, no external keys.
    Tests opt back in per feature via overrides."""
    base = dict(
        log_db_path="",              # download log off -> nothing touches disk
        secret_key="test-secret",    # no persisted-key file writes
        tor_autostart=False,
        use_tor=False,
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def app():
    """An app with services wired but NOT started: no Tor process, no DB init,
    no worker thread. Exactly what create_app(start=False) is for."""
    application = create_app(make_config(), start=False)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()
