# Deployment Guide — Starlink-LAN monitoring laptop

Target host: any Linux laptop (Ubuntu/Debian assumed; adjust package commands for
others). Goal: the probe + Prometheus + Grafana stack running unattended on the
Starlink LAN, collecting the M3 baseline with M5 dish telemetry validating it.

Estimated time: 30–45 min including OS prep.

---

## 1. Physical & network setup

- [ ] Connect the laptop to the Starlink router by **Ethernet** (USB-C dongle is
      fine). Do NOT use Wi-Fi for the monitoring NIC — it pollutes jitter and
      micro-outage data you're trying to attribute to the satellite link.
- [ ] Confirm you're on the StarlinkLAN: `curl -s ifconfig.me` should show your
      public egress IP as SpaceX/Starlink (look it up: the prefix will announce
      from AS14593 — e.g. `whois -h whois.cymru.com " -v $(curl -s ifconfig.me)"`).
- [ ] Confirm dish telemetry reachability: `ping -c3 192.168.100.1` and
      `curl -s http://192.168.100.1 | head -c 200`.
      The Starlink router is at **192.168.1.1** on this LAN. If the two checks
      above fail (e.g. bypass mode or a third-party router in-path), add:
      `sudo ip route add 192.168.100.1/32 via 192.168.1.1`
      (make it persistent via netplan/NetworkManager if it works).
- [ ] Expected traceroute shape on this LAN: hop 1 `192.168.1.1`, then
      `100.64.x.x` (CGNAT / satellite segment), then the AS14593 exit. Seeing
      `192.168.1.1` as hop 1 in `starlink_path_hop_rtt_seconds` is correct.

## 2. OS preparation

```bash
# Disable all sleep paths (lid-close too)
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind

# Time sync (timestamps must be clean for later correlation)
sudo timedatectl set-ntp true && timedatectl status | grep -i sync

# Docker Engine + Compose plugin (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out/in afterwards
docker compose version            # verify v2.x
```

Keep the laptop **plugged in**; set any vendor battery-charge cap if available.

## 3. Get the repo onto the laptop

The repo is public — clone directly, no credentials needed:

```bash
git clone https://github.com/dedmunds-v2/starlink-viability-probe.git ~/starlink
```

(Only if you later want to `git push` changes *from the laptop* would you need
auth; pulling updates with `git pull` stays credential-free over HTTPS.)

## 4. Configure

```bash
cd ~/starlink
printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$(openssl rand -base64 18)" > .env
cat .env   # note the password somewhere; .env is gitignored
```

## 5. Launch

```bash
docker compose up -d --build
docker compose ps        # all three should be Up, probe (healthy)
docker compose logs -f probe   # first measurement lines within ~10s
```

Access (localhost-only by design):
- Grafana: http://localhost:3000 (admin / password from `.env`)
- From other machines: `ssh -L3000:localhost:3000 <user>@<laptop-ip>`

## 6. On-site validation checklist (do this first — it verifies the segmentation)

The council flagged these as must-validate empirically on the dish LAN:

- [ ] **CGNAT hops visible**: `curl -s localhost:9108/metrics | grep estimate_source`
      should show `source="cgnat"` for most targets. (If everything says
      `exit_fallback`, intermediate hops are silent — the dashboard degrades
      gracefully; dish telemetry becomes the sole satellite-segment truth.)
- [ ] **Starlink exit found**: `curl -s localhost:9108/metrics | grep -e '_info'`
      — expect `exit_asn="14593"` and a `starlink_path_exit_confident 1`.
  - [ ] Watch for `source="cgnat"` estimate vs `exit_rtt`: RTT to `100.64.0.1`
      (satellite+cross-segment) should be a large fraction of total RTT to
      nearby anchors.
- [ ] **Alert rules loaded**: http://localhost:9090/rules shows the 5
      `starlink-*` rules.
- [ ] Leave it running; check Grafana "Starlink Viability — Overview" after
      an hour, and again tomorrow. Satellite segment and path-hash panels should
      populate within ~10 minutes of the first traceroutes.

## 7. (M5) Dish telemetry exporter — optional but recommended capture addition

Adds Starlink's own dish→POP latency/loss as ground truth, in Prometheus format
(port 8080). Append to `docker-compose.yml` under `services:`:

```yaml
  dish-exporter:
    image: ghcr.io/sparky8512/starlink-grpc-tools:latest
    command: dish_grpc_prometheus.py status ping_drop
    ports:
      - "127.0.0.1:9854:8080"
    restart: unless-stopped
```

Append to `prometheus/prometheus.yml` under `scrape_configs:`:

```yaml
  - job_name: dish
    static_configs:
      - targets: ["dish-exporter:8080"]
        labels:
          site: starlink-residential
```

Then `docker compose up -d` and confirm new series: `curl -s
localhost:9854/metrics | grep pop_ping`.

**Validation:** dish `pop_ping_latency_ms` should track (and bound from above —
it's low-priority ping) the traceroute-derived `starlink_path_satellite_segment_seconds`.
If they disagree wildly, the segmentation method needs an honest look before M3 data
is declared valid.

## 8. Operations

- **Auto-start**: docker `unless-stopped` restart policies + `sudo systemctl
  enable docker` cover reboots, provided the laptop doesn't sleep (§2).
- **Logs**: `docker compose logs probe` (JSON-file driver, 10 MB × 3 rotation).
- **Updating**: `git pull && docker compose up -d --build`.
- **Updates push from here**: ping the maintainer (you) — the repo tracks this.
- **Do not** update Starlink/router firmware manually mid-capture without noting
  the timestamp; dish reboots/firmware updates will appear in telemetry and
  should be treated as data annotations, not outages.

## 9. What NOT to worry about (for the test rig)

- IPv6 is deliberately unused (probe pins AF_INET); the dish still delegates a
  /56, but metrics describe the v4 path only.
- Occasional `exit_confident 0` alerts on distant targets: expected when the
  Starlink core stays silent for those paths.
- The high-rate (TWAMP-style) lane and p99 statistics are deliberately
  deferred to the production-trial phase — see PROJECT_PLAN.md.
