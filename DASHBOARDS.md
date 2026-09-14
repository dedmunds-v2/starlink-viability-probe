# Starlink Viability Dashboards — what we measure and why

One-pager for the two Grafana boards feeding the Starlink-as-trading-path study.
Probe host: residential Starlink terminal; targets: cloud-region anchors standing
in for major financial colos (NY metro, London, Frankfurt, Tokyo, Singapore, HK).

## Dashboard 1 — Starlink Viability: Overview
*Purpose: the candidate-path quality view — what Starlink delivers, per metro.*

| Panel | Why we collect it |
|---|---|
| **TCP connect RTT by target** | The core latency number per metro — the floor any symmetric session would experience. Closest defensible proxy for a trading-session path. |
| **End-to-end (TCP+TLS+TTFB)** | Realistic session-setup cost, not just wire RTT. Captures middlebox/proxy overhead that pure RTT hides. |
| **TTFB (bufferbloat indicator)** | Time-to-first-byte after handshake. Separates *pipe latency* from *queue depth* — satellite links under load bloat queues, and trading bursts suffer exactly that way. |
| **Loss — failed cycles %** | Measurement failures as loss proxy (5-min windows). Trading is loss-intolerant; even fractional loss breaks UDP market-data semantics. |
| **Segment split: satellite vs ground** | Decomposes each path: probe→Starlink-POP (satellite leg) vs POP→anchor (ground leg). This is *the* experiment's question: how much of end-to-end latency is space, how much is terrestrial, and which side is the variable one. It also predetermines what a future fiber/POP-side trial would need to beat. |
| **Path changes & exit POP** | Path-hash churn = POP re-pins (handovers are sub-IP and invisible here). Churn frequency tells us how stable the ground-side topology is — stable paths are easier to equalize. |

## Dashboard 2 — Starlink Viability: Dish & Ground Truth
*Purpose: prove the telemetry above is trustworthy, and attribute anomalies.*

| Panel | Why we collect it |
|---|---|
| **Dish state** | `CONNECTED` vs `SEARCHING`/`THERMAL_SHUTDOWN` etc. — instant exclusion of "the dish itself was down" when reading latency gaps. |
| **Active alerts** | Thermal throttle, snow-melt, obstruction-map resets. Any alert during a latency spike reclassifies it as hardware/environment, not network path. |
| **Dish uptime** | Reboots reset history buffers and inject gaps; uptime trends flag firmware auto-updates during captures. |
| **GPS sats / azimuth / elevation** | Constellation visibility + pointing sanity; low sat counts explain degraded windows. |
| **Dish→POP latency (pop_ping_latency_ms)** | **Ground truth for the satellite segment**, measured by Starlink's own firmware at 1 Hz — independent of our instrumentation. Validates (or indicts) everything else on these boards. |
| **POP ping drop rate** | Finest-grained availability signal: per-second loss fraction, catches micro-outages between our probe cycles (the thing a 10 s TCP loop cannot see). |
| **Cross-validation: dish ping vs traceroute segment** | The methodological heart of the study. If Starlink's own dish→POP number tracks our CGNAT-landmark estimate, the satellite/ground split in Dashboard 1 is confirmed trustworthy — which is what makes the whole viability report defensible. Persistent divergence = path change or detection failure, and we investigate before reading any baseline. |
| **Throughput & obstruction fraction** | Load context (residential deprioritization couples throughput to latency in evening peaks) and RF-health context (tree growth, weather, alignment drift). |

## How the two boards are used together
1. **First hour on-site (done):** cross-validation panel confirms dish truth ≈ traceroute segment → segmentation method certified for the path.
2. **Baseline week (M3):** Overview answers "what does Starlink deliver per metro, by time of day"; Dish board answers "why" whenever anomalies appear (alerts, drops, throughput).
3. **M6 viability report:** compares per-metro latency/loss distributions against the plan's thresholds, with **persistent-signal vs Starlink-side-events attributable via Dish board annotations**.

Legend: all latency in ms; drop/loss as fractions (0–1); satellite segment = probe→Starlink POP (≈ dish → satellite → ground station → POP edge).
