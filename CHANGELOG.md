# Changelog

## 0.3.3
- Personal Wi-Fi passwords (`psk` per user via `hostapd` `wpa_psk_file`): each user can optionally have their own Wi-Fi password (`nek0ctl set NAME psk PASS` or in the dashboard). When their phone connects with a new random MAC, `nek0ctl` automatically attaches the new MAC to that user (only when none of that user's existing devices is already online, preventing password sharing).
- Per-guest speed limit (`guest_per_mbps`, default 5 Mbps inside the 15 Mbps shared `guest_mbps` pool): each unknown device is capped individually while sharing the guest pool.
- Failover hardening: `probe_ping` checks for a valid default gateway first and probes `8.8.8.8` / `9.9.9.9` (avoiding campus routers that spoof `1.1.1.1`), and `uplink_switch` triggers a NetworkManager Wi-Fi rescan/connect on `wlan1` when switching to the backup hotspot.

## 0.3.2
- A user can own several MACs (`extra_macs`). Private or random Wi-Fi addresses no longer need phone settings: all of a person's MACs share one fixed IP, limits and usage (dnsmasq reservation `mac1,mac2,ip`).
- Dashboard: new "Unknown devices" card. Each device that is not a user gets a "Belongs to this user" button, with a hint when its DHCP hostname matches one of a user's devices. Users have an "Other MACs" field to detach a MAC.
- CLI: `nek0ctl set NAME addmac MAC` / `delmac MAC`; `nek0ctl devices` flags private MACs and finds owners across all of a user's MACs.

## 0.3.1
- A user's MAC can be changed (dashboard field or `nek0ctl set NAME mac aa:bb:cc:dd:ee:ff`) so a phone that got a new private Wi-Fi address keeps its fixed IP, limits and usage.
- Docs: tips for a stable AP (power save off, 20 MHz, stable per-network MAC on phones).

## 0.3.0
- Backup internet: a second Wi-Fi adapter can join a phone hotspot. When Ethernet is down or slow (two failed checks, or two slow speed checks) the router switches to the hotspot and caps total speed to protect mobile data; it switches back after Ethernet has recovered. Checks always run over Ethernet, so they never use mobile data. `nek0ctl uplink [auto|hotspot|ethernet]` to force a link; the dashboard shows which link is in use.

## 0.2.3
- The device list shows who is really connected right now (online / offline) with signal strength and link speed, instead of listing everyone who still holds a DHCP lease.
- A warning appears when a user's phone is not on its fixed IP yet (its limits and usage count only apply after the phone reconnects to the Wi-Fi).
- The add-user list shows online devices first.
- The Pi-hole link uses HTTPS.

## 0.2.2
- Fix: the dashboard could freeze for minutes. The TLS handshake now happens in each connection's own thread with a timeout, so one idle browser connection can no longer block everyone.
- Renaming a user no longer restarts Pi-hole (DHCP reservations are by MAC and IP only), and when a restart is needed it runs in the background instead of inside the web request.
- Logins survive restarts of the service (stored as hashes, 30 days) and the cookie is SameSite=Lax.
- The usage numbers and the connected-devices list update by themselves every 15 seconds without disturbing the forms.
- "Everyone" is now users plus guests, so it can never show less than the users alone.

## 0.2.1
- Wi-Fi watchdog: if the Wi-Fi link goes down while hostapd is still running (for example after a NetworkManager reload), the access point is restarted automatically. Switch off with `watchdog: false`.
- Status table adapts to long user names.

## 0.2.0
- Dashboard shows data used this month: everyone, users, guests, today, and live speed, with a 14-day chart.
- Per-user usage, today's usage, live speed and a 14-day chart.
- Edit a user without losing usage; renaming keeps usage and history; the used amount can be corrected by hand.
- Removing a user keeps their usage, so re-adding the same name continues where it left off.
- Pause / resume a user's internet.
- `backup` / `restore` commands and a "Download backup" link; `install` backs up automatically before updating.
- `nek0web.py update` upgrades the dashboard without touching the password, certificate, users or usage.
- Unit tests and GitHub Actions CI.

## 0.1.0
- `nek0ctl`: per-user speed limits (tc HTB + CAKE), monthly quotas (nftables counters), fixed DHCP leases via Pi-hole.
- `nek0web`: HTTPS dashboard, Wi-Fi settings with automatic revert, admin login.
