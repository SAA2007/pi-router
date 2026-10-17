# Security

## Reporting a problem
Please use GitHub's private vulnerability reporting ("Security" tab, "Report a vulnerability") rather than a public issue.

## Design notes
- The dashboard listens only on the router's Wi-Fi address (10.42.0.1:8443) over HTTPS, and a firewall rule drops that port on every interface except `wlan0` and `lo`.
- The admin password is stored as a salted scrypt hash. Sessions use HttpOnly, Secure, SameSite=Strict cookies and every change needs a CSRF token. Five wrong passwords lock a client out for ten minutes.
- All input is validated before it reaches a shell command or a config file (names, MACs, IPs and numbers are checked against strict patterns).
- Wi-Fi setting changes revert automatically unless confirmed, so a bad change cannot lock you out.

## Known limitations
- The service runs as root (it needs to change firewall, traffic-control and hostapd settings).
- The certificate is self-signed; see docs/HTTPS.md.
- Everyone who knows the Wi-Fi password can reach the login page. Use a strong admin password.
- No client isolation: devices on the same Wi-Fi can talk to each other.
