# HTTPS and certificates

Both Pi-hole and the dashboard use **self-signed certificates**, so browsers show a warning the first time. The connection is still encrypted; the warning only means the browser cannot verify who issued the certificate.

A publicly trusted certificate needs a real name, and no public authority issues one for a private address like 10.42.0.1. The options:

## A. Accept the warning (default)
Fine for a page only reachable from your own Wi-Fi. Check the fingerprint once if you want to be strict.

## B. Tailscale certificate with a local DNS name
Tailscale can issue a real Let's Encrypt certificate for your machine's `*.ts.net` name (`tailscale cert`). If your Pi-hole answers that name with `10.42.0.1`, Wi-Fi clients can open `https://<name>.<tailnet>.ts.net:8443` with no warning and without installing Tailscale themselves. Needs MagicDNS and HTTPS enabled in the Tailscale admin console, renewal every ~90 days, and the host name becomes visible in public certificate logs. Run Tailscale on the Pi with `--accept-dns=false` so it does not take over the Pi's DNS. This route is documented but not yet automated here.

## C. Your own domain
Get a certificate for a name you own with a DNS-based challenge (for example with `acme.sh`), and point the name at 10.42.0.1 in Pi-hole's local DNS.

## D. Your own certificate authority
Create a small CA and install its certificate on your devices. Works offline; you must install the CA on every device.

To use another certificate with the dashboard, replace `/opt/nek0/web.crt` and `/opt/nek0/web.key` and run `sudo systemctl restart nek0web`.
