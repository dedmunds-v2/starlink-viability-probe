#!/usr/bin/env python3
"""Path identification for the Starlink viability probe.

Periodically traceroutes each target and derives the metrics needed to split
end-to-end latency into segments:

  satellite segment : probe -> dish -> satellite -> ground station -> POP
                      (estimated from the RTT of the last CGNAT hop
                      100.64.0.0/10, which is Starlink's access network)
  starlink exit/POP : first hop in AS14593 (SpaceX/Starlink). Its RTT is the
                      latency to the Starlink edge. AS14593 originates ~2000
                      v4 prefixes, many named per-POP (bgp.he.net/AS14593),
                      so the exit IP maps to a specific ground location.
  ground segment    : POP -> internet/transit -> anchor
                      (derived in Grafana: probe RTT minus exit-hop RTT)

Also computes a path_hash so latency spikes can be correlated with routing
changes (Starlink POP egress changes with scheduler/handover cycles).

Notes:
- Requires a system 'traceroute' binary. TCP mode (-T -p 443) is preferred so
  probes follow the same ECMP treatment as the measured flows; it needs
  root/CAP_NET_RAW. Falls back to ICMP, then UDP.
- -A makes traceroute embed [ASnnnnn] tags for each hop; if the local
  traceroute lacks -A, ASN-based POP detection degrades to "first public hop".
- Paris-traceroute flow stability: we pin TCP probes to port 443; per-hop
  variance from ECMP hashing is accepted for a skeleton. Dublin/mtrace
  realism is a later refinement if data shows aliasing.
"""

import hashlib
import ipaddress
import re
import shutil
import subprocess
import threading
import time

CGNAT = ipaddress.ip_network("100.64.0.0/10")
STARLINK_ASN = "14593"

HOP_LINE = re.compile(r"^\s*(\d+)\s+(.+)$")
IP_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
ASN_RE = re.compile(r"\[AS(\d+)\]")
RTT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ms")


class Hop:
    __slots__ = ("ttl", "ip", "rtt_s", "asn")

    def __init__(self, ttl, ip, rtt_s, asn):
        self.ttl = ttl
        self.ip = ip
        self.rtt_s = rtt_s
        self.asn = asn


def _is_public(ip_str):
    ip = ipaddress.ip_address(ip_str)
    return ip.is_global and ip not in CGNAT


def _traceroute(host, port):
    """Run traceroute with the best available method. Returns stdout."""
    traceroute = shutil.which("traceroute")
    if not traceroute:
        return None
    # TCP first (matches measured flows, passes most firewalls), then ICMP,
    # then classic UDP.
    attempts = [
        [traceroute, "-n", "-A", "-q", "1", "-w", "2", "-T", "-p", str(port), host],
        [traceroute, "-n", "-A", "-q", "1", "-w", "2", "-I", host],
        [traceroute, "-n", "-A", "-q", "1", "-w", "2", host],
    ]
    for cmd in attempts:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=120).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        if out and IP_RE.search(out):
            return out
    return None


def _parse(output):
    hops = []
    for line in output.splitlines():
        m = HOP_LINE.match(line)
        if not m:
            continue
        ttl = int(m.group(1))
        rest = m.group(2)
        ip_m = IP_RE.search(rest)
        rtt_m = RTT_RE.search(rest)
        asn_m = ASN_RE.search(rest)
        if ip_m and rtt_m:  # skip "* * *" unresolved hops
            hops.append(Hop(
                ttl=ttl,
                ip=ip_m.group(1),
                rtt_s=float(rtt_m.group(1)) / 1000.0,
                asn=asn_m.group(1) if asn_m else "",
            ))
    return hops


def _classify(hops):
    """Derive segment landmarks from the hop list."""
    first_public = next((h for h in hops if _is_public(h.ip)), None)
    last_cgnat = None
    for h in hops:
        try:
            if ipaddress.ip_address(h.ip) in CGNAT:
                last_cgnat = h
            elif last_cgnat is not None and _is_public(h.ip):
                break
        except ValueError:
            continue
    starlink_exit = next((h for h in hops if h.asn == STARLINK_ASN), None)
    if starlink_exit is None:
        # ASN lookup unavailable or exit hop silent: approximate with first
        # public hop (Starlink egress is the first routable address seen).
        starlink_exit = first_public
    return first_public, last_cgnat, starlink_exit


def _path_hash(hops):
    key = "|".join(f"{h.ttl}:{h.ip}" for h in hops)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class PathCollector:
    """Periodically traceroutes all targets; keeps latest path per target."""

    def __init__(self, targets, interval=300):
        self.targets = targets
        self.interval = interval
        self.available = shutil.which("traceroute") is not None
        self.paths = {}            # target.name -> dict(latest path data)
        self.changes = {t.name: 0 for t in targets}
        self._lock = threading.Lock()
        self._last_hash = {}

    def trace(self, target):
        out = _traceroute(target.host, target.port)
        if out is None:
            return
        hops = _parse(out)
        if not hops:
            return
        first_public, last_cgnat, starlink_exit = _classify(hops)
        phash = _path_hash(hops)
        with self._lock:
            prev = self._last_hash.get(target.name)
            if prev is not None and prev != phash:
                self.changes[target.name] += 1
            self._last_hash[target.name] = phash
            self.paths[target.name] = {
                "ts": time.time(),
                "hash": phash,
                "hops": hops,
                "length": hops[-1].ttl,
                "final_rtt_s": hops[-1].rtt_s,
                "exit_ip": starlink_exit.ip if starlink_exit else "",
                "exit_asn": starlink_exit.asn if starlink_exit else "",
                "exit_rtt_s": (starlink_exit.rtt_s if starlink_exit
                               else float("nan")),
                # Satellite segment estimate: prefer last CGNAT hop RTT;
                # fall back to exit-hop RTT (includes terrestrial backhaul).
                "satellite_rtt_s": (last_cgnat.rtt_s if last_cgnat
                                    else (starlink_exit.rtt_s
                                          if starlink_exit else float("nan"))),
                "satellite_from_fallback": last_cgnat is None,
            }

    def run(self):
        if not self.available:
            print("WARNING: 'traceroute' not found; path metrics disabled")
            return
        while True:
            start = time.perf_counter()
            for target in self.targets:
                try:
                    self.trace(target)
                except Exception as exc:  # never kill the path thread
                    print(f"path trace failed for {target.name}: {exc}")
            elapsed = time.perf_counter() - start
            time.sleep(max(0.0, self.interval - elapsed))

    def render_metrics(self):
        lines = []
        with self._lock:
            paths = dict(self.paths)
            changes = dict(self.changes)

        lines.append("# HELP starlink_path_collector_available "
                     "1 if traceroute is available on this host")
        lines.append("# TYPE starlink_path_collector_available gauge")
        lines.append(f"starlink_path_collector_available "
                     f"{1 if self.available else 0}")

        emission = [
            ("starlink_path_length", "Number of responding hops", "length"),
            ("starlink_path_final_rtt_seconds", "RTT of last responding hop",
             "final_rtt_s"),
            ("starlink_path_starlink_exit_rtt_seconds",
             "RTT to Starlink exit/POP hop (first AS14593 or first public hop)",
             "exit_rtt_s"),
            ("starlink_path_satellite_segment_seconds",
             "Estimated satellite segment RTT (last CGNAT hop, else exit hop)",
             "satellite_rtt_s"),
        ]
        for metric, helptext, key in emission:
            lines.append(f"# HELP {metric} {helptext}")
            lines.append(f"# TYPE {metric} gauge")
            for t in self.targets:
                p = paths.get(t.name)
                if p:
                    lines.append(f'{metric}{{target="{t.name}"}} {p[key]}')

        lines.append("# HELP starlink_path_satellite_estimate_source "
                     "How the satellite segment was estimated "
                     "(cgnat=last 100.64/10 hop, exit=fallback to exit hop)")
        lines.append("# TYPE starlink_path_satellite_estimate_source gauge")
        for t in self.targets:
            p = paths.get(t.name)
            if p:
                src = "exit_fallback" if p["satellite_from_fallback"] else "cgnat"
                lines.append(
                    f'starlink_path_satellite_estimate_source'
                    f'{{target="{t.name}",source="{src}"}} 1')

        lines.append("# HELP starlink_path_info Static info about current "
                     "path (value always 1)")
        lines.append("# TYPE starlink_path_info gauge")
        for t in self.targets:
            p = paths.get(t.name)
            if p:
                exit_asn = p["exit_asn"] or "unknown"
                lines.append(
                    f'starlink_path_info{{target="{t.name}",'
                    f'path_hash="{p["hash"]}",exit_ip="{p["exit_ip"]}",'
                    f'exit_asn="{exit_asn}"}} 1')

        lines.append("# HELP starlink_path_hop_rtt_seconds "
                     "RTT per hop from the most recent traceroute")
        lines.append("# TYPE starlink_path_hop_rtt_seconds gauge")
        for t in self.targets:
            p = paths.get(t.name)
            if p:
                for h in p["hops"]:
                    asn = h.asn or "unknown"
                    lines.append(
                        f'starlink_path_hop_rtt_seconds{{target="{t.name}",'
                        f'ttl="{h.ttl}",ip="{h.ip}",asn="{asn}"}} {h.rtt_s}')

        lines.append("# HELP starlink_path_changes_total "
                     "Traceroute path-hash changes seen per target")
        lines.append("# TYPE starlink_path_changes_total counter")
        for t in self.targets:
            lines.append(f'starlink_path_changes_total{{target="{t.name}"}} '
                         f'{changes[t.name]}')
        return "\n".join(lines) + "\n"
