# nek0-router

Turn a Raspberry Pi into a Wi-Fi router with **per-user speed limits**, **monthly data quotas**, **DNS filtering (Pi-hole + Unbound)** and a **web dashboard** to manage it all from your phone.

> **Status: v0.3.4.** Built and tested on a Raspberry Pi 5 running Raspberry Pi OS (Debian 13 "trixie"), built-in Wi-Fi (`wlan0`) as a 5 GHz access point, Ethernet (`eth0`) primary uplink + USB Wi-Fi (`wlan1`) phone hotspot failover, optional ESP32-C3 Mini USB status light, and Pi-hole v6 + Unbound. Use at your own risk and read the limitations below.

## Features

- 5 GHz access point (`hostapd`) with automatic watchdog recovery
- **Automatic phone-hotspot failover**: when Ethernet is unplugged, dead, or slow, switches to a secondary Wi-Fi adapter (`wlan1`) connected to a phone hotspot (with an automatic speed cap to save mobile data) and switches back when Ethernet recovers
- **ESP32-C3 USB status light & button (`esp32/main.py` + `nek0led.service`)**:
  - **Steady ON** (with heartbeat wink): Ethernet internet is healthy (`OK`)
  - **Double-blink**: Running on phone hotspot failover (`HOTSPOT`)
  - **Fast 5 Hz flash**: No internet on Ethernet or hotspot (`NO_NET`)
  - **Triple-blink**: Wi-Fi access point (`wlan0`) is down (`AP_DOWN`)
  - **BOOT button (`GPIO 9`)**: short press blinks the connected device count; 2.5s hold toggles hotspot failover
- Per-user **download/upload limits** and **monthly quotas** (over quota = throttled, not cut off)
- **Random / private MAC resilience**:
  - Each user can hold **multiple MAC addresses** (`extra_macs`) sharing one fixed IP, speed limit, and quota
  - Optional **per-user Wi-Fi passwords** (`wpa_psk_file`): each user can have their own Wi-Fi password so new random MACs are recognized and attached automatically (only when their existing device isn't already online, preventing password sharing)
  - 1-click **"Belongs to this user"** adoption in the web dashboard (limits apply immediately on their current DHCP IP without reconnecting)
- **Shared + per-device guest caps**: unknown devices share a guest pool (e.g. 15 Mbps total) with an individual per-guest ceiling (e.g. 5 Mbps per guest)
- **Data used** per user, for guests and in total, today and this month, with a 14-day chart and live speed
- **Pause / resume** a user's internet
- Edit, rename or remove users **without losing usage**; correct the used amount by hand
- Wi-Fi name, password, channel, width and security editable from the dashboard, with **automatic revert** if you don't confirm (no lock-outs)
- Pi-hole for DNS filtering and local `.lan` names (`nek0.lan`); Unbound as a local recursive resolver
- Safe for shared uplinks: DNS/DHCP/web are firewalled off the upstream (`eth0` / `wlan1`) side
- Backups, unit tests, CI

## How it works

```
phones / laptops
      |  Wi-Fi 5 GHz (hostapd + per-user wpa_psk_file)
   wlan0 10.42.0.1 ----+--- Pi-hole (DNS :53, DHCP)  ->  Unbound 127.0.0.1:5335 (recursive)
                       |
                       +--- nftables: NAT + usage counters + pause rules
                       +--- tc: HTB class per user + per-guest slots + CAKE queues (upload via IFB)
                       +--- /dev/ttyACM0: ESP32-C3 status LED + button (nek0led.service)
      |
   eth0 (primary DHCP uplink) / wlan1 (phone hotspot backup) ---- internet
```

- `nek0ctl.py` reads `/opt/nek0/users.yaml`, writes fixed DHCP leases to Pi-hole, writes per-user Wi-Fi keys to `/etc/hostapd/hostapd.psk`, builds the traffic-control classes and nftables counters, checks uplink health, drives the ESP32-C3 status LED, and polls usage every 30 seconds (systemd timer).
- `nek0web.py` is a small HTTPS dashboard that edits the same files through the same code.
- Usage lives in `/opt/nek0/state.json`, separate from the settings, so updates never touch it.

## Requirements

Raspberry Pi with Wi-Fi (tested on a Pi 5), a wired uplink (plus optional USB Wi-Fi adapter for hotspot failover and optional ESP32-C3 Mini over USB for status LED), Raspberry Pi OS (Debian 13), Python 3.11+, and these packages: `hostapd nftables unbound python3-yaml python3-serial openssl`, plus Pi-hole v6.

## Install

1. Follow [docs/SETUP.md](docs/SETUP.md) once to prepare the access point, DNS and firewall.
2. Then:

```
git clone https://github.com/SAA2007/pi-router.git
cd pi-router
sudo python3 nek0ctl.py install
sudo python3 nek0web.py setup
```

3. From a device on your Wi-Fi open **https://nek0.lan:8443** or **https://10.42.0.1:8443** (accept the certificate warning, see [docs/HTTPS.md](docs/HTTPS.md)).

To update later: `git pull`, then `sudo python3 nek0ctl.py install` and `sudo python3 nek0web.py update`. Users, usage, password and certificate are kept (and a backup is made first).

## Command line

| Command | What it does |
|---|---|
| `sudo nek0ctl status` | usage per user (with online status), guests, total, and active uplink |
| `sudo nek0ctl devices` | devices connected right now (flags random/private MACs) |
| `sudo nek0ctl add` | guided: pick a device, set speed and quota |
| `sudo nek0ctl set NAME down 10 up 5 quota 50` | change limits (`quota none` = unlimited) |
| `sudo nek0ctl set NAME psk SECRET123` | set a personal Wi-Fi password for auto-adopting new MACs (`psk none` to clear) |
| `sudo nek0ctl set NAME addmac MAC` / `delmac MAC` | attach / detach an extra MAC address for a user |
| `sudo nek0ctl set NAME name NEWNAME used 12.5` | rename (usage kept) / correct used GB |
| `sudo nek0ctl uplink [auto\|hotspot\|ethernet]` | show or switch internet uplink mode |
| `sudo nek0ctl led [status\|auto\|night\|off\|blink]` | check or control the ESP32-C3 USB status light |
| `sudo nek0ctl pause NAME` / `resume NAME` | cut / restore internet |
| `sudo nek0ctl remove NAME` | back to the guest pool (usage remembered) |
| `sudo nek0ctl reset [NAME]` | zero usage |
| `sudo nek0ctl backup` / `restore FILE` | save / load users and usage |
| `sudo nek0ctl apply` | rebuild everything from `users.yaml` |
| `sudo python3 /opt/nek0/nek0web.py restore-wifi` | emergency: restore the last confirmed Wi-Fi settings |

## Limits and honest caveats

- Limits are **per IP address**. Each user gets a fixed IP through their device's MAC(s), or automatically when they connect using their personal Wi-Fi password (`psk`).
- Speed limits are caps, not guarantees. The uplink is the real ceiling.
- Only IPv4 traffic is shaped and counted. Only traffic routed through the Pi is counted.
- The Pi 5's built-in radio is 1x1, so expect roughly 100 Mbps in practice on 1x1 40 MHz.
- Guests share a total pool (`guest_mbps`) and each guest IP is capped at `guest_per_mbps` (mapped into 16 hash buckets).
- The dashboard runs as root and uses a self-signed certificate. Read [SECURITY.md](SECURITY.md).

## Responsible use

Only share or NAT a network you own or have permission to share. Many campuses, hostels and ISPs prohibit routers or shared connections. This project is not affiliated with Raspberry Pi, Pi-hole or Tailscale.

## Development

```
pip install -r requirements-dev.txt
python -m pytest -q
```

See [docs/ROADMAP.md](docs/ROADMAP.md) for ideas and [CHANGELOG.md](CHANGELOG.md) for history. Licensed under the [MIT License](LICENSE).
