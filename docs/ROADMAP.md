# Roadmap & Ideas

Done (`v0.3.4`):
- Per-user download/upload limits (HTB + CAKE) and monthly quotas with automatic reset
- Usage totals, 14-day history charts, live rates, pause/resume, rename-safe usage, backups
- Automatic phone-hotspot failover (`wlan1`) with mobile-data speed cap and recovery checks
- Multi-MAC support (`extra_macs`), 1-click unknown-device adoption, and instant limit enforcement on active DHCP leases
- Per-user personal Wi-Fi passwords (`wpa_psk_file`) with anti-password-sharing auto-adoption
- Shared + per-device guest speed caps (`guest_mbps` + `guest_per_mbps`)
- Local `.lan` DNS (`nek0.lan`, `router.lan`)
- ESP32-C3 USB status light & physical button daemon (`nek0led.service` + `esp32/main.py`)

Next candidates:
- 1-click "Gaming / Priority Mode" (temporarily cap all other users/guests to 1 Mbps)
- Pi-hole stats & 1-click "Pause ad-blocking for 5 min" button inside the dashboard
- Scheduled pause/throttle windows (for example late-night caps) and bonus-data top-ups
- Quota alerts (Telegram, email or a dashboard banner at 80% / 100%)
- Wi-Fi QR code for visitors
- Block lists per user (Pi-hole groups) in the dashboard
