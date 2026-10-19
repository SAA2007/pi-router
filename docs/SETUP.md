# Setup

These are the steps that were run on the author's Raspberry Pi 5. Run them over SSH (pasting long blocks into a local console can garble them).

## 0. Packages

```
sudo apt update
sudo apt install -y hostapd nftables unbound python3-yaml openssl ethtool
sudo systemctl unmask hostapd
```

## 1. DNS: Unbound (recursive) behind Pi-hole

```
sudo cp examples/unbound-pi-hole.conf /etc/unbound/unbound.conf.d/pi-hole.conf
sudo cp examples/unbound-tuning.conf   /etc/unbound/unbound.conf.d/tuning.conf
sudo unbound-checkconf && sudo systemctl restart unbound
dig @127.0.0.1 -p 5335 example.com                      # should answer, with the "ad" flag
dig @127.0.0.1 -p 5335 sigfail.verteiltesysteme.net     # should say SERVFAIL (DNSSEC works)
```

If the first query fails, the upstream network may filter outbound port 53. Add a `forward-zone` in Unbound pointing at that network's own DNS server.

## 2. Keep NetworkManager away from the Wi-Fi interfaces

```
sudo tee /etc/NetworkManager/conf.d/99-nek0-unmanaged.conf >/dev/null <<'EOT'
[keyfile]
unmanaged-devices=interface-name:wlan0;interface-name:wlan1
EOT
sudo systemctl reload NetworkManager
```

`eth0` stays with NetworkManager (DHCP from the upstream network). Never give `eth0` a static address on a network you don't run.

## 3. Address for wlan0

```
sudo tee /etc/systemd/network/10-wlan0.network >/dev/null <<'EOT'
[Match]
Name=wlan0
[Network]
Address=10.42.0.1/24
ConfigureWithoutCarrier=yes
EOT
sudo systemctl enable --now systemd-networkd
sudo rfkill unblock wlan
```

## 4. Access point

Copy `examples/hostapd.conf` to `/etc/hostapd/hostapd.conf`, set `ssid`, a passphrase of 12+ characters and the right `country_code`. Check legal channels with `iw reg get` (Pakistan: only 149-165 on 5 GHz).

```
sudo install -m 600 examples/hostapd.conf /etc/hostapd/hostapd.conf
sudo nano /etc/hostapd/hostapd.conf
sudo systemctl enable --now hostapd
iw dev wlan0 info        # expect: type AP, width: 40 MHz
```

## 5. Forwarding, NAT and the upstream-side firewall

```
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/90-nek0.conf
sudo sysctl --system
sudo cp /etc/nftables.conf /etc/nftables.conf.bak
sudo cp examples/nek0.nft /etc/nftables.conf
sudo nft -c -f /etc/nftables.conf && sudo nft -f /etc/nftables.conf
sudo systemctl enable nftables
```

## 6. Pi-hole v6

```
sudo mkdir -p /etc/pihole
sudo tee /etc/pihole/setupVars.conf >/dev/null <<'EOT'
PIHOLE_INTERFACE=wlan0
PIHOLE_DNS_1=127.0.0.1#5335
QUERY_LOGGING=true
INSTALL_WEB_SERVER=true
INSTALL_WEB_INTERFACE=true
BLOCKING_ENABLED=true
EOT
curl -sSL https://install.pi-hole.net | sudo bash -s -- --unattended
```

Then lock it to wlan0 and enable DHCP there (set the range **before** switching DHCP on):

```
sudo pihole-FTL --config dns.upstreams '["127.0.0.1#5335"]'
sudo pihole-FTL --config dns.interface wlan0
sudo pihole-FTL --config dns.listeningMode BIND
sudo pihole-FTL --config dhcp.start 10.42.0.100
sudo pihole-FTL --config dhcp.end 10.42.0.250
sudo pihole-FTL --config dhcp.router 10.42.0.1
sudo pihole-FTL --config dhcp.netmask 255.255.255.0
sudo pihole-FTL --config dhcp.leaseTime 24h
sudo pihole-FTL --config dhcp.active true
sudo systemctl restart pihole-FTL
sudo pihole setpassword
```

## 7. nek0-router

```
sudo python3 nek0ctl.py install
sudo python3 nek0web.py setup
```

## 8. Check

```
sudo ss -tulpn | grep -E ':(53|67|5335|8443) '   # nothing on your eth0 address
sudo nek0ctl status
```

Connect a phone to your Wi-Fi, then open https://10.42.0.1:8443 and add it as a user.

## 9. Optional: backup internet through a phone hotspot (second Wi-Fi adapter)

```
sudo sed -i 's/;interface-name:wlan1//' /etc/NetworkManager/conf.d/99-nek0-unmanaged.conf
sudo nmcli device set wlan1 managed yes
sudo nmcli connection add type wifi ifname wlan1 con-name hotspot ssid 'YOUR-SSID' wifi-sec.key-mgmt wpa-psk wifi-sec.psk 'YOUR-PASSWORD' ipv4.route-metric 600 ipv6.method disabled connection.autoconnect yes
```

Use `examples/nek0.nft` (it masquerades and protects `wlan1` too), then add the `uplink:` section from `examples/users.yaml`. A small USB adapter is usually 2.4 GHz only, so set the phone's hotspot to 2.4 GHz. While the hotspot is in use everyone's traffic uses your mobile data, hence the speed cap.

## 10. Optional: ESP32-C3 Mini USB status light & button

Plug an ESP32-C3 SuperMini / Mini into the Pi over USB (`/dev/ttyACM0`), flash MicroPython, and copy `esp32/main.py` onto it:

```
sudo apt install -y esptool python3-serial mpremote
curl -fsSL -o /tmp/mp_c3.bin https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20241129-v1.24.1.bin
sudo esptool --chip esp32c3 --port /dev/ttyACM0 erase_flash
sudo esptool --chip esp32c3 --port /dev/ttyACM0 write_flash -z 0x0 /tmp/mp_c3.bin
sudo mpremote connect /dev/ttyACM0 fs cp esp32/main.py :main.py + reset
```

`nek0led.service` (installed automatically by `sudo python3 nek0ctl.py install`) drives the onboard `GPIO 8` LED (`Steady ON` = Ethernet OK, `Double-blink` = Hotspot failover, `Slow blink` = No internet, `Triple-blink` = Wi-Fi AP down) and reads the onboard `BOOT` button (`GPIO 9`: short press toggles LED night mode; 3s hold toggles hotspot failover).

