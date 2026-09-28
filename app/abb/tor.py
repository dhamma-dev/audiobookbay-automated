"""Tor lifecycle: launch, bootstrap, circuit renewal, status.

AudiobookBay requests can be routed through Tor so the mirror only ever sees a
Tor exit node, never the server's real IP. The app starts and manages its own
tor process (with a localhost control port so a circuit can be renewed on
demand). If something is already listening on the SOCKS port it is reused
instead (renewal is then unavailable — we don't control that Tor).

start() never raises and never blocks: bootstrapping is awaited in a
background thread, so the web server serves immediately (Direct works at
once; Tor flips to 'ready' when bootstrapped).

The managed process is watched for its whole life, because Tor-routed
traffic fails closed (see outbound.py) and so depends on an honest status: a
bootstrap slower than TOR_BOOTSTRAP_TIMEOUT still flips to 'ready' when it
lands, and a Tor that exits flips to 'unavailable' and is relaunched with a
capped backoff instead of quietly staying 'ready' with nothing behind it.
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

RESTART_BACKOFF_MAX = 300  # seconds; relaunch delays double from 10s up to this


def _socks_port_open(port):
    """True if something is already accepting connections on the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class TorManager:
    def __init__(self, config):
        self.config = config
        self._process = None
        self._tor_bin = None
        self._data_dir = None        # this launch's DataDirectory (holds the control auth cookie)
        self._renew_lock = threading.Lock()
        self._stopping = False       # set by stop(): an exit after that is expected, not a crash
        self._restarts = 0           # relaunches since the last good bootstrap (backoff)
        self._atexit_registered = False
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

        # No Tor at all: anything routed via Tor fails closed (paused, never
        # sent direct). Warn loudly when that is the DEFAULT route.
        level = logging.WARNING if self.config.use_tor else logging.INFO
        if not self.config.tor_autostart:
            log.log(level, "no Tor on 127.0.0.1:%s and TOR_AUTOSTART is off; Tor-routed "
                    "traffic is paused (set USE_TOR=false to default to Direct).",
                    self.config.tor_socks_port)
            return

        tor_bin = shutil.which("tor")
        if not tor_bin:
            log.log(level, "'tor' binary not found; Tor-routed traffic is paused (set "
                    "USE_TOR=false to default to Direct, or install Tor).")
            return
        self._tor_bin = tor_bin
        self._launch()

    def _launch(self):
        """Start one tor process and watch it — bootstrap and exit — off the
        request path."""
        self._data_dir = tempfile.mkdtemp(prefix="abb-tor-")
        log.info("starting Tor (SOCKS 127.0.0.1:%s, control %s)...",
                 self.config.tor_socks_port, self.config.tor_control_port)
        try:
            process = subprocess.Popen(
                [
                    self._tor_bin,
                    "--SocksPort", str(self.config.tor_socks_port),
                    "--ControlPort", f"127.0.0.1:{self.config.tor_control_port}",
                    "--CookieAuthentication", "1",
                    "--DataDirectory", self._data_dir,
                    "--ClientOnly", "1",
                    "--AvoidDiskWrites", "1",
                    "--Log", "notice stdout",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as e:  # start() must never raise; a relaunch must keep trying
            log.error("could not launch Tor: %s", e)
            self.starting = False
            self._schedule_restart()
            return
        self._process = process
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        self.starting = True
        ready = threading.Event()
        threading.Thread(target=self._consume_output, args=(process, ready), daemon=True,
                         name="tor-output").start()
        threading.Thread(target=self._await_bootstrap, args=(process, ready), daemon=True,
                         name="tor-bootstrap").start()

    def _consume_output(self, process, ready):
        """Drain Tor's stdout so its pipe never blocks, surfacing bootstrap
        progress and warnings, and flagging when it reaches 100%. The output
        ending means the process is gone: flip to unavailable — Tor-routed
        requests then fail closed rather than hit a dead proxy — and
        schedule a relaunch."""
        for line in process.stdout:
            line = line.strip()
            if "Bootstrapped" in line or "[err]" in line or "[warn]" in line:
                log.info("%s", line)
            if "Bootstrapped 100%" in line:
                ready.set()
        code = process.wait()
        ready.set()  # unblock the bootstrap waiter either way
        if self._stopping or process is not self._process:
            return
        self.available = False
        self.starting = False
        log.error("Tor exited (code %s). Tor-routed AudioBook Bay traffic is paused — "
                  "never sent direct — until it's back.", code)
        self._schedule_restart()

    def _await_bootstrap(self, process, ready):
        """Wait (off the request path) for Tor to bootstrap, then flip it to
        available. A slow bootstrap isn't a failure: past the timeout Tor
        reports 'unavailable' (so the UI can say so) but this keeps waiting,
        and a late 100% still makes it ready — it used to be ignored,
        leaving the instance without Tor until a restart."""
        if not ready.wait(timeout=self.config.tor_bootstrap_timeout):
            if process.poll() is None:
                log.warning("Tor hasn't bootstrapped within %ss; Tor-routed traffic stays "
                            "paused (never sent direct) while it keeps trying.",
                            self.config.tor_bootstrap_timeout)
                self.starting = False
            ready.wait()
        if process.poll() is None and not self._stopping and process is self._process:
            self.managed = True
            self.available = True
            self.starting = False
            self._restarts = 0
            log.info("Tor is ready; circuit renewal is available.")
            if self.on_ready:
                self.on_ready()

    def _schedule_restart(self):
        if self._stopping or not self._tor_bin:
            return
        delay = min(RESTART_BACKOFF_MAX, 10 * 2 ** self._restarts)
        self._restarts += 1
        log.warning("relaunching Tor in %ss (attempt %d)", delay, self._restarts)
        timer = threading.Timer(delay, self._relaunch)
        timer.daemon = True
        timer.start()

    def _relaunch(self):
        if self._stopping:
            return
        if self._data_dir:
            shutil.rmtree(self._data_dir, ignore_errors=True)
            self._data_dir = None
        self._launch()

    def stop(self):
        self._stopping = True
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
        'unavailable' (no usable Tor: none, bootstrap overdue, or exited).
        Tor-routed traffic waits on anything but 'ready' — never Direct."""
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
        (Outbound.renew_tor_circuit rebuilds its session) so the old circuit dies."""
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
