# Ideas

Done: usage totals and history, pause/resume, backups, rename-safe usage.

Next candidates:
- Per-device usage for guests (dynamic nftables counters)
- Scheduled pause windows (for example internet off at night) and bonus-data top-ups
- Quota alerts (Telegram, email or a dashboard banner at 80% / 100%)
- Wi-Fi QR code for visitors, and a separate guest Wi-Fi with its own password and time limit
- Block lists per user (Pi-hole groups) and DNS query log per device in the dashboard
- Upstream speed test button and uplink health graph; auto-tuned CAKE on the uplink for fair sharing when the link is congested
- Real HTTPS (Tailscale certificate or own CA) set up automatically
- Optional 2.4 GHz network for IoT devices (second radio or ESP32 bridge)
- Client isolation, MAC allow-list mode, WPA3 once the radio supports it reliably
- A `.deb` package, an installer script and a read-only status page
