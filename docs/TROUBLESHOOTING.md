# Troubleshooting

| Problem | Cause / fix |
|---|---|
| `RTNETLINK answers: Operation not permitted` | run `tc`, `nft` and `ip` commands with `sudo` |
| Windows asks for a "PIN" when joining | the Wi-Fi password is too short (8 characters looks like a WPS PIN). Use 12+ characters and plain WPA2 |
| hostapd will not start on channel 36 | that channel is not legal in some countries. Check `iw reg get`; in Pakistan use 149-165 |
| `DHCP start address is not valid` in Pi-hole | set `dhcp.start/end/router/netmask` first, then `dhcp.active true` |
| Wi-Fi works but there is no DHCP | check `sudo ss -ulpn \| grep ':67 '` and that Pi-hole is bound to wlan0 only |
| Slow internet | test the Pi's own link first: `sudo ethtool eth0` (a 100 Mb/s port tops out near 94 Mbps) and run a speed test on the Pi and on a laptop plugged into the same network |
| Locked out after a Wi-Fi change | wait 150 s for the automatic revert, or over SSH: `sudo python3 /opt/nek0/nek0web.py restore-wifi` |
| A phone keeps showing as a guest | its Wi-Fi MAC changed. Use the MAC shown for this network, or turn off MAC rotation for it |
| `Operation not permitted` from `nft list` | needs `sudo` |
| `chain fwd` syntax error (old versions) | `fwd` is a reserved word in nftables; fixed in this project by naming the chain `acct_fwd` |
| Wi-Fi vanished but `hostapd` says "running" | something (often a NetworkManager reload) took `wlan0` down. Check `ip a show wlan0`: it must say `UP,LOWER_UP`. The built-in watchdog restarts hostapd after about a minute; or run `sudo systemctl restart hostapd` |
| Dashboard hangs for minutes (versions before 0.2.2) | the server did the TLS handshake while accepting connections, so one idle browser connection blocked everything. Update to 0.2.2 |
| Dashboard asks for the password again | sessions are kept for 30 days and survive restarts since 0.2.2. If it still happens, check the browser accepts cookies for a page with an untrusted certificate (see docs/HTTPS.md) |

## Wi-Fi drops, slow or needs "forget and reconnect"
- **Phones with a private (random) MAC.** The fixed IP, limits and usage follow the MAC. If a phone forgets the network it may get a new MAC and fall into the guest pool. Easiest fix (no phone changes): open the dashboard, find the phone in "Unknown devices" and press "Belongs to this user"; the new MAC is added to the same person. Or set the network's MAC to "Use device MAC" (Android) or "Fixed" (iPhone), or paste the new MAC in the user's "Device MAC" field (or run `sudo nek0ctl set NAME mac aa:bb:cc:dd:ee:ff`). Usage is kept.
- **Access point power save.** Check `iw dev wlan0 get power_save`; it should be `off`. A systemd drop-in `/etc/systemd/system/hostapd.service.d/ps.conf` with `ExecStartPost=/usr/sbin/iw dev wlan0 set power_save off` keeps it off.
- **Channel width.** A 1x1 radio shared by many devices is often steadier at 20 MHz (remove `[HT40+]` from `ht_capab`) on a quiet channel.
- **hostapd options that help:** `disassoc_low_ack=0`, `ieee80211w=0`, `ap_max_inactivity=600`, `skip_inactivity_poll=1`.
- **Windows laptops.** Turn off "Allow the computer to turn off this device to save power" for the Wi-Fi adapter.
- Check `vcgencmd get_throttled` (want `0x0`) to rule out a weak power supply.
