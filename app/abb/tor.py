"""Tor lifecycle: launch, bootstrap, circuit renewal, status.

AudiobookBay requests can be routed through Tor so the mirror only ever sees a
Tor exit node, never the server's real IP. The app starts and manages its own
tor process (with a localhost control port so a circuit can be renewed on
demand). If something is already listening on the SOCKS port it is reused
instead (renewal is then unavailable — we don't control that Tor).

start() never raises and never blocks: bootstrapping is awaited in a
background thread, so the web server serves immediately (Direct works at
once; Tor flips to 'ready' when bootstrapped).
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading

log = logging.getLogger("abb.tor")


def _socks_port_open(port):
    """True if something is already accepting connections on the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class TorManager:
    def __init__(self, config):
        self.config = config
        self._process = None
        self._ready_event = threading.Event()
        self._data_dir = None        # where Tor writes its control auth cookie
        self._renew_lock = threading.Lock()
        self.available = False       # a SOCKS proxy we can route through
        self.managed = False         # we launched it, so we can renew circuits
        self.starting = False        # launched but not bootstrapped yet
        self.on_ready = None         # callback (Outbound builds its session)

    # --- lifecycle ------------------------------------------------------------
    def start(self):
        """Bring Tor up if possible and record whether it is usable/renewable."""
        if _socks_port_open(self.config.tor_socks_port):
            log.info("reusing Tor already listening on 127.0.0.1:%s", self.config.tor_socks_port)
            self.available = True
            self.managed = False
            if self.on_ready:
                self.on_ready()
            return

        if not self.config.tor_autostart:
            log.info("no Tor on the SOCKS port and TOR_AUTOSTART is off; running Direct-only.")
            return

        tor_bin = shutil.which("tor")
        if not tor_bin:
            log.info("'tor' binary not found; running Direct-only. Install Tor to enable it.")
            return

        data_dir = tempfile.mkdtemp(prefix="abb-tor-")
        log.info("starting Tor (SOCKS 127.0.0.1:%s, control %s)...",
                 self.config.tor_socks_port, self.config.tor_control_port)
        self._process = subprocess.Popen(
            [
                tor_bin,
                "--SocksPort", str(self.config.tor_socks_port),
                "--ControlPort", f"127.0.0.1:{self.config.tor_control_port}",
                "--CookieAuthentication", "1",
                "--DataDirectory", data_dir,
                "--ClientOnly", "1",
                "--AvoidDiskWrites", "1",
                "--Log", "notice stdout",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        atexit.register(self.stop)
        self.starting = True
        threading.Thread(target=self._consume_output, daemon=True, name="tor-output").start()
        threading.Thread(target=self._await_bootstrap, args=(data_dir,), daemon=True,
                         name="tor-bootstrap").start()

    def _consume_output(self):
        """Drain Tor's stdout so its pipe never blocks, surfacing bootstrap
        progress and warnings, and flagging when it reaches 100%."""
        for line in self._process.stdout:
            line = line.strip()
            if "Bootstrapped" in line or "[err]" in line or "[warn]" in line:
                log.info("%s", line)
            if "Bootstrapped 100%" in line:
                self._ready_event.set()
        self._ready_event.set()  # process ended — unblock any waiter

    def _await_bootstrap(self, data_dir):
        """Wait (off the request path) for Tor to bootstrap, then flip it to
        available. On timeout/exit we stay Direct-only."""
        ok = self._ready_event.wait(timeout=self.config.tor_bootstrap_timeout)
        if ok and self._process is not None and self._process.poll() is None:
            self._data_dir = data_dir
            self.managed = True
            self.available = True
            log.info("Tor is ready; circuit renewal is available.")
            if self.on_ready:
                self.on_ready()
        else:
            log.warning("Tor did not bootstrap within %ss; running Direct-only.",
                        self.config.tor_bootstrap_timeout)
        self.starting = False

    def stop(self):
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._data_dir:
            shutil.rmtree(self._data_dir, ignore_errors=True)
            self._data_dir = None

    # --- state ------------------------------------------------------------------
    def status(self):
        """'ready' (route via Tor now), 'starting' (still bootstrapping), or
        'unavailable' (no Tor at all -> Direct-only)."""
        if self.available:
            return "ready"
        if self.starting:
            return "starting"
        return "unavailable"

    @property
    def renewable(self):
        return self.available and self.managed

    def renew_circuit(self):
        """Ask Tor for a fresh circuit (new exit) via the control port.
        Returns (ok, message). The caller must also drop pooled connections
        (Outbound.reset_tor_session) so the old circuit dies."""
        if not self.renewable:
            return False, "Tor isn't running under this app's control, so its circuit can't be renewed."
        with self._renew_lock:
            try:
                with open(os.path.join(self._data_dir, "control_auth_cookie"), "rb") as f:
                    cookie_hex = f.read().hex()
                with socket.create_connection(("127.0.0.1", self.config.tor_control_port),
                                              timeout=10) as ctrl:
                    ctrl.settimeout(10)
                    ctrl.sendall(f"AUTHENTICATE {cookie_hex}\r\n".encode())
                    if not ctrl.recv(1024).decode(errors="replace").startswith("250"):
                        return False, "Tor control authentication failed."
                    ctrl.sendall(b"SIGNAL NEWNYM\r\n")
                    if not ctrl.recv(1024).decode(errors="replace").startswith("250"):
                        return False, "Tor did not accept the new-circuit request."
                return True, "Requested a new Tor circuit."
            except Exception as e:
                return False, f"Could not renew Tor circuit: {e}"
