# Starlink Viability Probe — Project Plan

## 1. Objective

Build and deploy a monitoring system that measures the **performance and adequacy of
Starlink connectivity** for reaching major financial venues/colocations, to determine
whether Starlink is a viable complement (or alternative) to low-latency fiber paths in a
global trading WAN.

A **residential Starlink terminal** is the test source. Since it cannot connect to real
exchange infrastructure, financial venues are represented by **public cloud anchors** in
the same metros as the major colos (e.g., `us-east-1` ≈ Northern Virginia/NY metro,
`eu-west-2` ≈ London, `ap-northeast-1` ≈ Tokyo).

This project does **not** trade or route exchange traffic over Starlink. It answers the
question: *"What latency, jitter, loss and availability can Starlink deliver on
metro-to-metro paths, and how does that compare to our fiber baseline?"*

## 2. Success criteria (Starlink viability thresholds)

Draft thresholds — refine after baseline data is in:

| Metric | Viable | Marginal | Not viable |
|---|---|---|---|
| RTT to same-continent metro (e.g., UK→London anchor) | < 40 ms | 40–60 ms | > 60 ms |
| RTT intercontinental (e.g., UK→NY) | < 100 ms | 100–130 ms | > 130 ms |
| Jitter (stdev of RTT, 5-min windows) | < 5 ms | 5–15 ms | > 15 ms |
| Packet loss (per 5-min window) | < 0.1% | 0.1–1% | > 1% |
| Micro-outage frequency (gap > 2 s) | < 1/day | 1–10/day | > 10/day |
| Availability (monthly) | > 99.9% | 99–99.9% | < 99% |

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Host on the Starlink LAN (laptop/RPi/VM)                        │
│                                                                 │
│  ┌──────────────┐   exposes    ┌────────────┐  ┌────────────┐ │
│  │ Probe (Python)│  /metrics   │ Prometheus │─▶│  Grafana   │ │
│  │ stdlib only   │◀───────────│  (scrape)  │  │ (dashboards)│ │
│  └──────┬───────┘  scrape     └────────────┘  └────────────┘ │
└─────────┼──────────────────────────────────────────────────────┘
          │ TCP/ICMP + TLS-timing measurements, every N sec
          ▼
   Cloud anchors: ec2.<region>.amazonaws.com (etc.)
   in metros near major colos
```

Design principles:

- **Zero third-party dependencies for the probe** (Python 3.10+ stdlib only) so it runs
  anywhere: RPi on the Starlink LAN, a container, a laptop.
- **Prometheus/Grafana** for collection/visualization — consistent with WAN-ops tooling.
- Every measurement labeled with `target` (anchor) and `site` (this probe's location),
  so additional probes (fiber-side probes, other metros) can be added later and compared
  side by side in one Grafana instance.

## 4. Probe — what we measure

Per target, every cycle (default 5 s):

- **TCP connect latency** to `host:443` — the primary "wire latency" proxy, works even
  where ICMP is blocked.
- **TLS handshake RTT** (optional per-target) — closer to a real ordered-reliable
  session setup, reveals middlebox/path weirdness.
- **Application read latency** (time from request to first/last byte of a `HEAD /`) —
  catches bufferbloat that TCP handshake alone hides.
- **Loss**: measurement failures count as lost cycles; plus (phase 2) dedicated
  high-rate ICMP ping for micro-outage detection.

### Hop/path identification (satellite vs ground segmentation)

End-to-end latency is decomposed using three independent signals:

1. **Periodic traceroutes** per target (default every 5 min, TCP 443 preferred so
   probes follow the same ECMP treatment as measured flows; ICMP/UDP fallback).
   From each path we derive:
   - **Satellite segment estimate** — RTT of the last hop in Starlink's CGNAT
     range `100.64.0.0/10` (dish → satellite → ground station reachability).
   - **Starlink exit/POP** — first hop in **AS14593** (SpaceX; ~2,000 v4
     prefixes, many named per-POP at bgp.he.net/AS14593, so the exit IP maps to
     a ground location). Its RTT ≈ latency to the Starlink edge.
   - **Ground segment** — probe RTT minus exit-hop RTT (POP → transit → anchor).
     This is exactly the segment we will later validate against real terrestrial
     last-mile measurements when we can measure ground-station→colo directly.
   - **path_hash** — sha256 of the hop list; churn of this hash flags POP
     egress/routing changes (satellite scheduling causes periodic path shifts).
2. **Dish self-measurement** (M5) — the dish's gRPC interface emits
   `pop_ping_latency_ms`/`pop_ping_drop_rate` at 1 Hz: Starlink's own RTT and
   loss measurement to the POP. This is the ground truth for the satellite
   segment and needs no traceroute inference. (`starlink-grpc-tools`
   `dish_grpc_prometheus.py status ping_drop` exports exactly this.)
3. **Both-ended probing** (M4+) — once a fiber-side probe exists near an anchor
   metro, probing back toward the Starlink POP/exit IP bounds the
   ground-station↔colo segment without any inference.

Fallback behavior: if hops are silent or no CGNAT hop responds, the exit-hop
RTT is used as the satellite estimate and
`starlink_path_satellite_estimate_source{source="exit_fallback"}` flags the
reduced confidence.

Starlink's own dish telemetry (`192.168.100.1` gRPC — obstruction %, SNR, alerts,
scheduled downtime) is **phase 2**, to correlate RF events with latency/loss events.
Note: several fields are firmware-dependent — SNR bulk history, GPS location, and
some obstruction fields are obsolete/unavailable on recent firmware (see
starlink-grpc-tools docs, checked 2026-09); `pop_ping_*`, state, and alerts remain.

## 5. Initial target set (cloud anchors ≈ financial metros)

| Metro / venue stand-in | Region anchor | Colo represented |
|---|---|---|
| New York metro | `us-east-1` (N. Virginia), `us-east-2` + `ny` anchors | NY4/NY5, Mahwah, Carteret |
| Chicago | `us-east-2` (Ohio proxy) / `us-central1` | CME Aurora |
| London | `eu-west-2` | LD4–LD6, Equinix Slough |
| Frankfurt | `eu-central-1` | Equinix FR2, Deutsche Börse |
| Tokyo | `ap-northeast-1` | TY2/TY3, JPX/CC2 |
| Singapore | `ap-southeast-1` | SGX |
| Hong Kong | `ap-east-1`/`ap-southeast-2proxy` | HKEX |

Targets live in `probe/targets.json` — easy to extend.

## 6. Milestones

- [x] **M0 — Plan agreed** (this document)
- [x] **M1 — Probe skeleton**: Python exporter measuring TCP/TLS/read latency to cloud
      anchors, Prometheus `/metrics` endpoint. **Done:** metrics live.
- [x] **M2 — Local stack**: docker-compose with Prometheus + Grafana provisioned,
      basic latency/loss dashboard. **Done:** full stack verified, dashboards live.
- [x] **M2.5 — Hop/path identification**: periodic per-target traceroutes with
      AS14593/CGNAT segmentation, path-hash churn detection, per-hop RTT metrics.
      **Done:** `starlink_path_*` metrics flowing (validated against a terrestrial
      uplink; CGNAT segmentation to be validated on the real dish).
- [ ] **M3 — Baseline capture**: run ≥ 1 week from the residential Starlink site;
      capture by time-of-day, weather, day-of-week.
- [ ] **M4 — Reference baseline**: run the same probe from a fiber-connected site
      (or pull RIPE Atlas data) and build side-by-side comparison dashboards.
- [ ] **M5 — Dish telemetry integration**: STARLINK gRPC metrics (obstruction, SNR,
      alerts) correlated with latency/loss events.
- [ ] **M6 — Viability report**: evaluate against Section 2 thresholds; recommend
      which metro paths (if any) merit a real Starlink enterprise/roaming trial.

## 7. Repo layout

```
starlink/
├── PROJECT_PLAN.md          ← this document
├── probe/
│   ├── probe.py             ← measurement loop + Prometheus exporter (stdlib only)
│   ├── path.py              ← per-target traceroute / hop-segmentation collector
│   ├── Dockerfile           ← probe image (adds traceroute binary)
│   └── targets.json         ← cloud-anchor targets
├── prometheus/
│   └── prometheus.yml       ← scrape config
├── grafana/
│   └── provisioning/        ← datasource + dashboards
└── docker-compose.yml
```

## 8. Risks & notes

- **Residential Starlink ≠ enterprise**: residential plans are deprioritized and CGNAT'd;
  treat results as a *floor/indicator*. An enterprise/roaming service test is the likely
  follow-up if results look promising.
- **Cloud anchors are proxies**: cloud-region latency ≈ metro latency, not exact colo
  latency. The gap is documented and constant-ish for comparison purposes.
- **Laser-interlink dependence**: Starlink intercontinental latency depends on the
  constellation's inter-satellite links and gateway routing; expect more path variance
  over time than fiber. Long capture windows are essential (M3).
- **Weather/ obstruction events** will show in the data — M5 lets us attribute them.
