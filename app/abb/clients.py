"""Download-client registry: one add/list pair per client.

/send and the status page dispatch through the registry instead of branching
inline, so adding a client is a single table entry. Connection objects are
built per call (cheap, and avoids holding stale sessions); any transport error
propagates to the caller for reporting. Exactly one client per deploy, chosen
by DOWNLOAD_CLIENT (validated in Config.validate_client)."""

from __future__ import annotations

import re

import requests
from flask import has_request_context, session as flask_session
from qbittorrentapi import Client as QbtClient
from transmission_rpc import Client as TransmissionClient
from deluge_web_client import DelugeWebClient

from .config import CLIENT_LABELS


class PutioNotConnected(Exception):
    """Raised when put.io is the client but no usable token is available."""

    def __init__(self):
        super().__init__("Put.io is not connected. Log in with Put.io or set PUTIO_ACCESS_TOKEN.")


def sanitize_title(title):
    return re.sub(r'[<>:"/\\|?*]', "", title).strip()


class ClientRegistry:
    def __init__(self, config):
        self.config = config
        self.ok, self.config_error = config.validate_client()
        self._backends = {
            "qbittorrent": (self._qbittorrent_add, self._qbittorrent_list),
            "transmission": (self._transmission_add, self._transmission_list),
            "delugeweb": (self._deluge_add, self._deluge_list),
            "putio": (self._putio_add, self._putio_list),
        }

    @property
    def label(self):
        return CLIENT_LABELS.get(self.config.download_client or "", "Download Client")

    def add(self, magnet_link, title):
        self._backends[self.config.download_client][0](magnet_link, title)

    def list_torrents(self):
        if not self.ok:
            raise ValueError(self.config_error)
        return self._backends[self.config.download_client][1]()

    def _save_path(self, title):
        """Per-book save path for the torrent clients. put.io ignores this and
        uses PUTIO_SAVE_PARENT_ID instead. None when SAVE_PATH_BASE is unset so
        the client falls back to its own default download location."""
        base = self.config.save_path_base
        return f"{base}/{sanitize_title(title)}" if base else None

    # --- qBittorrent ------------------------------------------------------------
    def _qbt(self):
        c = self.config
        qb = QbtClient(host=c.dl_host, port=c.dl_port, username=c.dl_username,
                       password=c.dl_password)
        qb.auth_log_in()
        return qb

    def _qbittorrent_add(self, magnet_link, title):
        self._qbt().torrents_add(urls=magnet_link, save_path=self._save_path(title),
                                 category=self.config.dl_category)

    def _qbittorrent_list(self):
        return [
            {
                "name": t.name,
                "progress": round(t.progress * 100, 2),
                "state": t.state,
                "size": f"{t.total_size / (1024 * 1024):.2f} MB",
            }
            for t in self._qbt().torrents_info(category=self.config.dl_category)
        ]

    # --- Transmission ------------------------------------------------------------
    def _transmission_add(self, magnet_link, title):
        c = self.config
        client = TransmissionClient(host=c.dl_host, port=c.dl_port, protocol=c.dl_scheme,
                                    username=c.dl_username, password=c.dl_password)
        client.add_torrent(magnet_link, download_dir=self._save_path(title))

    def _transmission_list(self):
        c = self.config
        client = TransmissionClient(host=c.dl_host, port=c.dl_port,
                                    username=c.dl_username, password=c.dl_password)
        return [
            {
                "name": t.name,
                "progress": round(t.progress, 2),
                "state": t.status,
                "size": f"{t.total_size / (1024 * 1024):.2f} MB",
            }
            for t in client.get_torrents()
        ]

    # --- Deluge (web UI) -----------------------------------------------------------
    def _deluge(self):
        deluge = DelugeWebClient(url=self.config.dl_url, password=self.config.dl_password)
        deluge.login()
        return deluge

    def _deluge_add(self, magnet_link, title):
        self._deluge().add_torrent_magnet(magnet_link, save_directory=self._save_path(title),
                                          label=self.config.dl_category)

    def _deluge_list(self):
        torrents = self._deluge().get_torrents_status(
            filter_dict={"label": self.config.dl_category},
            keys=["name", "state", "progress", "total_size"],
        )
        return [
            {
                "name": t["name"],
                "progress": round(t["progress"], 2),
                "state": t["state"],
                "size": f"{t['total_size'] / (1024 * 1024):.2f} MB",
            }
            for _, t in torrents.result.items()
        ]

    # --- put.io -----------------------------------------------------------------------
    def putio_token(self):
        """The active put.io token: the OAuth token from the user's session if
        they logged in, otherwise the static PUTIO_ACCESS_TOKEN. Background
        work (wanted auto-download) has no request, so only the static token
        applies there."""
        session_token = flask_session.get("putio_access_token") if has_request_context() else None
        return session_token or self.config.putio_access_token

    def _putio_add(self, magnet_link, title):
        token = self.putio_token()
        if not token:
            raise PutioNotConnected()
        data = {"url": magnet_link}
        if self.config.putio_save_parent_id:
            data["save_parent_id"] = self.config.putio_save_parent_id
        response = requests.post(
            "https://api.put.io/v2/transfers/add", data=data,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=self.config.request_timeout)
        if response.status_code != 200:
            raise Exception(f"Put.io API error: {response.text}")

    def _putio_list(self):
        token = self.putio_token()
        if not token:
            raise PutioNotConnected()
        response = requests.get(
            "https://api.put.io/v2/transfers/list",
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=self.config.request_timeout)
        if response.status_code != 200:
            raise Exception(f"Put.io API error: {response.text}")
        return [
            {
                "name": tr.get("name", "Unknown"),
                "progress": tr.get("percent_done", 0),
                "state": tr.get("status", "Unknown"),
                "size": f"{tr.get('size', 0) / (1024 * 1024):.2f} MB",
            }
            for tr in response.json().get("transfers", [])
        ]
