#!/usr/bin/env python3
"""Starlink viability probe.

Measures TCP connect / TLS handshake / TTFB latency from this host to a set of
cloud-region anchors (stand-ins for financial colos) and exposes the results in
Prometheus text format on /metrics.

Stdlib only: no third-party packages required.

Usage:
    python3 probe.py [--config targets.json] [--port 9108] [--interval 10]
"""

import argparse
import http.server
import json
import socket
import ssl
import threading
import time
from dataclasses import dataclass

import path as pathmod

METRICS_PORT = 9108
REQUEST_TIMEOUT = 5.0  # seconds per measurement stage


@dataclass
class Target:
    name: str
    host: str
    port: int = 443
    metro: str = ""
    colo_proxy: str = ""
    tls: bool = True


@dataclass
class Sample:
    """Latest measurement for one target."""
    tcp_connect_s: float = float("nan")
    tls_handshake_s: float = float("nan")  # additional time after TCP connect
    ttfb_s: float = float("nan")           # time to first response byte after request
    total_s: float = float("nan")          # tcp+tls+ttfb end-to-end
    success: int = 0
    error: str = ""
    ts: float = 0.0


class Probe:
    def __init__(self, targets, interval):
        self.targets = targets
        self.interval = interval
        self.latest = {t.name: Sample() for t in targets}
        self.cycle_errors = {t.name: 0 for t in targets}
        self.cycles_total = {t.name: 0 for t in targets}
        self.started = time.time()
        self._lock = threading.Lock()

    def measure(self, target: Target) -> Sample:
        sample = Sample(ts=time.time())
        sock = None
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection(
                (target.host, target.port), timeout=REQUEST_TIMEOUT
            )
            t1 = time.perf_counter()
            sample.tcp_connect_s = t1 - t0

            if target.tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ssock = ctx.wrap_socket(sock, server_hostname=target.host)
                t2 = time.perf_counter()
                sample.tls_handshake_s = t2 - t1
                sock = ssock
                sock.settimeout(REQUEST_TIMEOUT)

                # Application read: TTFB of a HEAD request reveals bufferbloat.
                req = (
                    f"HEAD / HTTP/1.1\r\nHost: {target.host}\r\n"
                    "Connection: close\r\nUser-Agent: starlink-probe/0.1\r\n\r\n"
                ).encode()
                sock.sendall(req)
                sock.recv(1)
                t3 = time.perf_counter()
                sample.ttfb_s = t3 - t2
                sample.total_s = t3 - t0
            else:
                sample.total_s = sample.tcp_connect_s
                sample.tls_handshake_s = float("nan")
                sample.ttfb_s = float("nan")

            sample.success = 1
        except (OSError, ssl.SSLError) as exc:
            sample.error = f"{type(exc).__name__}: {exc}"
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        return sample

    def run(self):
        while True:
            cycle_start = time.perf_counter()
            for target in self.targets:
                sample = self.measure(target)
                with self._lock:
                    self.latest[target.name] = sample
                    self.cycles_total[target.name] += 1
                    if not sample.success:
                        self.cycle_errors[target.name] += 1
                if not sample.success:
                    print(f"[{time.strftime('%H:%M:%S')}] {target.name}: {sample.error}")
            elapsed = time.perf_counter() - cycle_start
            time.sleep(max(0.0, self.interval - elapsed))

    # ----- Prometheus exposition -----

    def render_metrics(self) -> str:
        lines = []
        lines.append("# HELP starlink_probe_up 1 if the probe is running")
        lines.append("# TYPE starlink_probe_up gauge")
        lines.append("starlink_probe_up 1")

        def emit(metric, help_text, value_fn):
            lines.append(f"# HELP {metric} {help_text}")
            lines.append(f"# TYPE {metric} gauge")
            with self._lock:
                for t in self.targets:
                    s = self.latest[t.name]
                    v = value_fn(s)
                    labels = (
                        f'target="{t.name}",host="{t.host}",'
                        f'metro="{t.metro}",colo_proxy="{t.colo_proxy}"'
                    )
                    lines.append(f"{metric}{{{labels}}} {v}")

        emit("starlink_probe_tcp_connect_seconds",
             "TCP connect latency to target", lambda s: s.tcp_connect_s)
        emit("starlink_probe_tls_handshake_seconds",
             "TLS handshake time after TCP connect", lambda s: s.tls_handshake_s)
        emit("starlink_probe_ttfb_seconds",
             "Time to first byte of HEAD / after handshake", lambda s: s.ttfb_s)
        emit("starlink_probe_total_seconds",
             "End-to-end time (connect+TLS+TTFB)", lambda s: s.total_s)
        emit("starlink_probe_success",
             "1 if the last measurement cycle succeeded", lambda s: s.success)

        lines.append("# HELP starlink_probe_cycles_total Measurement cycles attempted")
        lines.append("# TYPE starlink_probe_cycles_total counter")
        lines.append("# HELP starlink_probe_errors_total Measurement cycles failed")
        lines.append("# TYPE starlink_probe_errors_total counter")
        with self._lock:
            for t in self.targets:
                lbl = f'target="{t.name}"'
                lines.append(f"starlink_probe_cycles_total{{{lbl}}} "
                             f"{self.cycles_total[t.name]}")
                lines.append(f"starlink_probe_errors_total{{{lbl}}} "
                             f"{self.cycle_errors[t.name]}")
        return "\n".join(lines) + "\n"


class Handler(http.server.BaseHTTPRequestHandler):
    probe: Probe = None       # set before server start
    paths = None              # optional pathmod.PathCollector

    def do_GET(self):
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
        elif self.path in ("/", "/metrics"):
            text = self.probe.render_metrics()
            if self.paths is not None:
                text += self.paths.render_metrics()
            body = text.encode()
            self.send_response(200)
        else:
            body = b"not found\n"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


def load_targets(path):
    with open(path) as f:
        raw = json.load(f)["targets"]
    return [
        Target(
            name=t["name"],
            host=t["host"],
            port=int(t.get("port", 443)),
            metro=t.get("metro", ""),
            colo_proxy=t.get("colo_proxy", ""),
            tls=bool(t.get("tls", True)),
        )
        for t in raw
    ]


def main():
    ap = argparse.ArgumentParser(description="Starlink viability probe")
    ap.add_argument("--config", default="targets.json")
    ap.add_argument("--port", type=int, default=METRICS_PORT)
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between measurement cycles")
    ap.add_argument("--path-interval", type=float, default=300.0,
                    help="seconds between per-target traceroutes (0 disables "
                         "path/hop identification)")
    args = ap.parse_args()

    targets = load_targets(args.config)
    probe = Probe(targets, args.interval)
    Handler.probe = probe

    if args.path_interval > 0:
        paths = pathmod.PathCollector(targets, args.path_interval)
        Handler.paths = paths
        pt = threading.Thread(target=paths.run, daemon=True)
        pt.start()

    t = threading.Thread(target=probe.run, daemon=True)
    t.start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Measuring {len(targets)} targets every {args.interval}s; "
          f"metrics on http://0.0.0.0:{args.port}/metrics")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
