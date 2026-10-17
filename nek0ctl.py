#!/usr/bin/env python3
"""nek0ctl - per-user speed limits and monthly quotas for the nek0 Wi-Fi router.

  sudo python3 nek0ctl.py install     one-time setup (copies itself to /opt/nek0, enables systemd units)
  sudo nek0ctl apply                  rebuild leases, speed limits and counters from users.yaml
  sudo nek0ctl status                 show usage per user, guests and total
  sudo nek0ctl devices                list devices connected right now
  sudo nek0ctl add                    pick a connected device and give it a speed/quota (guided)
  sudo nek0ctl set NAME down 10 up 5 quota 50    change a user (quota none = unlimited)
  sudo nek0ctl set NAME name NEWNAME used 12.5   rename (usage is kept) / correct the used GB
  sudo nek0ctl pause NAME | resume NAME          cut / restore a user's internet
  sudo nek0ctl remove NAME            drop a user back to the guest pool (usage is remembered)
  sudo nek0ctl uplink [auto|hotspot|ethernet]    show / force which internet link is used (see users.yaml: uplink)
  sudo nek0ctl backup | restore FILE  save / load users.yaml + usage (install also backs up first)
  sudo nek0ctl reset [name]           zero usage (everyone, or one user) and lift throttling

Usage is polled automatically every 30 s. Add --dry-run to print commands without running them.
Edit /opt/nek0/users.yaml, then run: sudo nek0ctl apply
"""
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

import yaml

__version__ = "0.3.3"

BASE = os.environ.get("NEK0_DIR", "/opt/nek0")
CONF = os.path.join(BASE, "users.yaml")
STATE = os.path.join(BASE, "state.json")
LOCK = os.environ.get("NEK0_LOCK", "/run/nek0ctl.lock")
LEASES = os.environ.get("NEK0_LEASES", "/etc/pihole/dhcp.leases")
PSK_FILE = os.environ.get("NEK0_PSK_FILE", "/etc/hostapd/hostapd.psk")
IFB = "ifb0"
DEFAULT_PROBE = "https://speed.cloudflare.com/__down?bytes=3000000"
GB = 1000 ** 3
DRY = "--dry-run" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]

SAMPLE = """# nek0 users. After editing run: sudo nek0ctl apply
interface: wlan0
total_mbps: 90        # ceiling for all users together (Wi-Fi side)
guest_mbps: 15        # all unknown devices share this
guest_per_mbps: 5     # ceiling for a single guest device
throttle_kbps: 256    # speed a user drops to after passing their quota
watchdog: true        # restart hostapd if the Wi-Fi link drops while hostapd still "runs"
users: []
# users:
#   - name: syed
#     mac: aa:bb:cc:dd:ee:01   # the phone's Wi-Fi MAC for THIS network
#     ip: 10.42.0.20           # 10.42.0.2 - 10.42.0.99 (outside the DHCP pool)
#     down_mbps: 20
#     up_mbps: 10
#     quota_gb: 50             # omit for unlimited
#     psk: syed-secret-wifi    # optional personal Wi-Fi password (auto-attaches rotated MACs)
"""

SVC_APPLY = """[Unit]
Description=nek0 speed limits and quotas (apply)
After=hostapd.service pihole-FTL.service network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 /opt/nek0/nek0ctl.py apply

[Install]
WantedBy=multi-user.target
"""

SVC_POLL = """[Unit]
Description=nek0 usage poll

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/nek0/nek0ctl.py poll
"""

TIMER = """[Unit]
Description=nek0 usage poll timer

[Timer]
OnBootSec=30
OnUnitActiveSec=30

[Install]
WantedBy=timers.target
"""


def run(cmd, check=True, stdin=None):
    if DRY:
        print("+", cmd)
        if stdin:
            print(stdin)
        return 0, ""
    r = subprocess.run(cmd, shell=True, input=stdin, text=True, capture_output=True)
    if check and r.returncode:
        sys.exit(f"FAILED: {cmd}\n{r.stderr.strip()}")
    return r.returncode, r.stdout


# ---------------------------------------------------------------- config / state
def load():
    try:
        with open(CONF) as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        sys.exit(f"cannot read {CONF}: {e}")
    cfg.setdefault("interface", "wlan0")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", str(cfg["interface"])):
        sys.exit("bad interface name in users.yaml")
    cfg.setdefault("total_mbps", 90)
    cfg.setdefault("guest_mbps", 15)
    cfg.setdefault("guest_per_mbps", 5)
    cfg.setdefault("throttle_kbps", 256)
    cfg.setdefault("watchdog", True)
    up = cfg.get("uplink")
    if up:
        up.setdefault("primary_iface", "eth0")
        up.setdefault("min_mbps", 3)
        up.setdefault("backup_cap_mbps", 10)
        up.setdefault("probe_url", DEFAULT_PROBE)
        for k in ("backup_iface", "primary_iface"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", str(up.get(k, ""))):
                sys.exit(f"uplink: bad or missing {k}")
        if not re.fullmatch(r"https?://[A-Za-z0-9._~:/?&=%-]+", str(up["probe_url"])):
            sys.exit("uplink: bad probe_url")
        for k in ("min_mbps", "backup_cap_mbps"):
            if not isinstance(up[k], (int, float)) or up[k] <= 0:
                sys.exit(f"uplink: {k} must be a positive number")
    cfg["users"] = cfg.get("users") or []
    seen = set()
    base = int(ipaddress.ip_address("10.42.0.0"))
    for i, u in enumerate(cfg["users"]):
        for k in ("name", "mac", "ip", "down_mbps", "up_mbps"):
            if k not in u:
                sys.exit(f"user #{i + 1}: missing '{k}'")
        for k in ("down_mbps", "up_mbps"):
            if not isinstance(u[k], (int, float)) or not 0 < u[k] <= 1000:
                sys.exit(f"{u.get('name', i + 1)}: {k} must be between 0.5 and 1000")
        u["name"] = str(u["name"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", u["name"]):
            sys.exit(f"bad name '{u['name']}' (letters, digits, - and _ only)")
        if not re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", str(u["mac"])):
            sys.exit(f"bad MAC for {u['name']}")
        u["mac"] = str(u["mac"]).lower()
        extra = u.get("extra_macs") or []
        if not isinstance(extra, list):
            sys.exit(f"{u['name']}: extra_macs must be a list")
        u["extra_macs"] = []
        for m in extra:
            m = str(m).lower()
            if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m):
                sys.exit(f"bad extra MAC for {u['name']}")
            if m != u["mac"] and m not in u["extra_macs"]:
                u["extra_macs"].append(m)
        if u.get("psk"):
            psk = str(u["psk"])
            if not 8 <= len(psk) <= 63 or any(not 32 <= ord(c) < 127 for c in psk):
                sys.exit(f"{u['name']}: psk must be 8-63 plain characters")
            u["psk"] = psk
        else:
            u.pop("psk", None)
        ip = ipaddress.ip_address(str(u["ip"]))
        if not 2 <= int(ip) - base <= 99:
            sys.exit(f"{u['name']}: ip must be 10.42.0.2 - 10.42.0.99")
        u["ip"] = str(ip)
        u["key"] = u["name"].replace("-", "_")
        u["paused"] = bool(u.get("paused", False))
        u["cid"] = 10 + i
        for field in [u["key"], u["ip"]] + user_macs(u) + ([("psk", u["psk"])] if u.get("psk") else []):
            if field in seen:
                sys.exit(f"duplicate value: {field}")
            seen.add(field)
    return cfg


def user_macs(u):
    """Primary MAC first, then extra (e.g. private/random) MACs of the same person."""
    return [u["mac"]] + [m for m in (u.get("extra_macs") or []) if m != u["mac"]]


def mac_owner_map(cfg):
    """{mac: user} for every known MAC."""
    return {m: u for u in cfg["users"] for m in user_macs(u)}


def is_private_mac(mac):
    """Locally administered bit set = randomised / private Wi-Fi address."""
    return bool(int(mac.split(":")[0], 16) & 2)


def month_now():
    return datetime.date.today().strftime("%Y-%m")


def today():
    return datetime.date.today().isoformat()


def load_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("month", month_now())
    st.setdefault("users", {})
    return st


def save_state(st):
    os.makedirs(BASE, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def S(st, u):
    s = st["users"].setdefault(u["key"], {"used": 0, "last": 0, "throttled": False})
    s.setdefault("rate", 0)
    return s


def TOT(st):
    return st.setdefault("total", {"used": 0, "last": 0, "rate": 0})


def GUEST(st):
    return st.setdefault("guest", {"used": 0, "rate": 0})


def DAY(st, day=None):
    return st.setdefault("days", {}).setdefault(day or today(), {})


def rename_state(st, old_key, new_key):
    """Move a user's usage and history to a new name (so renaming never loses data)."""
    if old_key == new_key:
        return
    if old_key in st.get("users", {}):
        st["users"][new_key] = st["users"].pop(old_key)
    for day in st.get("days", {}).values():
        if old_key in day:
            day[new_key] = day.get(new_key, 0) + day.pop(old_key)


# ---------------------------------------------------------------- usage accounting
def read_counters():
    _, out = run(f"nft -j list counters table ip nek0acct", check=False)
    res = {}
    if out.strip():
        for item in json.loads(out).get("nftables", []):
            c = item.get("counter")
            if c:
                res[c["name"]] = c["bytes"]
    return res


def collect(cfg, st):
    cnt = read_counters()
    now = time.time()
    prev = st.get("ts")
    dt = (now - prev) if prev else 0
    day = DAY(st)
    deltas = 0
    for u in cfg["users"]:
        s = S(st, u)
        c = cnt.get("c_" + u["key"])
        if c is None:
            continue
        d = (c - s["last"]) if c >= s["last"] else c  # counter restarted -> count from 0
        s["used"] += d
        s["last"] = c
        deltas += d
        if d:
            day[u["key"]] = day.get(u["key"], 0) + d
        if dt >= 5:
            s["rate"] = d * 8 / dt
    c = cnt.get("c_total")
    if c is not None:
        t, g = TOT(st), GUEST(st)
        d = (c - t["last"]) if c >= t["last"] else c
        t["used"] += d
        t["last"] = c
        guests = max(0, d - deltas)
        g["used"] += guests
        if guests:
            day["_guest"] = day.get("_guest", 0) + guests
        if dt >= 5:
            t["rate"] = d * 8 / dt
            g["rate"] = guests * 8 / dt
    if cnt:
        st["ts"] = now
    days = st.get("days", {})
    for k in sorted(days)[:-62]:  # keep about two months of daily history
        del days[k]


def monthly_reset(st):
    if st.get("month") != month_now():
        st["month"] = month_now()
        for s in st["users"].values():
            s["used"] = 0
        TOT(st)["used"] = 0
        GUEST(st)["used"] = 0


def set_rate(cfg, u, throttled):
    for dev, key in ((cfg["interface"], "down_mbps"), (IFB, "up_mbps")):
        kb = cfg["throttle_kbps"] if throttled else int(u[key] * 1000)
        run(f"tc class change dev {dev} parent 1:1 classid 1:{u['cid']} "
            f"htb rate {min(1000, kb)}kbit ceil {kb}kbit")


def enforce(cfg, st):
    for u in cfg["users"]:
        s = S(st, u)
        q = u.get("quota_gb")
        over = q is not None and s["used"] >= q * GB
        if over != s["throttled"]:
            s["throttled"] = over
            set_rate(cfg, u, over)
            print(f"{u['name']}: {'quota reached, throttled' if over else 'back to full speed'}")


# ---------------------------------------------------------------- building blocks
def leaf(dev, cid):
    rc, _ = run(f"tc qdisc add dev {dev} parent 1:{cid} handle {cid}: cake", check=False)
    if rc:
        run(f"tc qdisc add dev {dev} parent 1:{cid} handle {cid}: fq_codel")


def setup_dhcp(cfg, st, restart=True):
    """Fixed leases by MAC. Names are NOT included: renaming a user must not restart Pi-hole."""
    hosts = [f"{','.join(user_macs(u))},{u['ip']}" for u in cfg["users"]]
    arr = json.dumps(hosts)
    h = hashlib.sha1(arr.encode()).hexdigest()
    if st.get("dhcp") == h:
        return False
    run(f"pihole-FTL --config dhcp.hosts '{arr}'")
    st["dhcp"] = h
    if restart:
        run("systemctl restart pihole-FTL")
    return True


def setup_psk(cfg):
    """Write personal Wi-Fi passwords (wpa_psk_file) and reload hostapd without dropping connections."""
    lines = [f"keyid={u['key']} 00:00:00:00:00:00 {u['psk']}" for u in cfg["users"] if u.get("psk")]
    text = "\n".join(lines) + ("\n" if lines else "")
    try:
        if os.path.exists(PSK_FILE) and open(PSK_FILE).read() == text:
            return
        os.makedirs(os.path.dirname(PSK_FILE) or ".", exist_ok=True)
        with open(PSK_FILE, "w") as f:
            f.write(text)
        os.chmod(PSK_FILE, 0o600)
    except OSError:
        return
    run(f"hostapd_cli -i {cfg['interface']} reload_wpa_psk", check=False)


def setup_shaping(cfg):
    dev = cfg["interface"]
    total = int(cfg["total_mbps"] * 1000)
    guest = int(cfg["guest_mbps"] * 1000)
    gper = int((cfg.get("guest_per_mbps") or cfg["guest_mbps"]) * 1000)
    run("modprobe ifb numifbs=1", check=False)
    run(f"ip link add {IFB} type ifb", check=False)
    run(f"ip link set {IFB} up")
    for d in (dev, IFB):
        run(f"tc qdisc del dev {d} root", check=False)
    run(f"tc qdisc del dev {dev} ingress", check=False)
    run(f"tc qdisc add dev {dev} handle ffff: ingress")
    run(f"tc filter add dev {dev} parent ffff: protocol ip u32 match u32 0 0 "
        f"action mirred egress redirect dev {IFB}")
    # download = traffic leaving wlan0 (match destination); upload = mirrored ingress (match source)
    for d, side, key in ((dev, "dst", "down_mbps"), (IFB, "src", "up_mbps")):
        dfl = 900 if gper < guest else 999
        run(f"tc qdisc add dev {d} root handle 1: htb default {dfl}")
        run(f"tc class add dev {d} parent 1: classid 1:1 htb rate {total}kbit ceil {total}kbit")
        run(f"tc class add dev {d} parent 1:1 classid 1:999 htb rate {min(1000, guest)}kbit ceil {guest}kbit")
        if gper < guest:
            off = 19 if side == "dst" else 15  # last byte of IPv4 dst (19) or src (15)
            for b in range(16):
                cid = 900 + b
                run(f"tc class add dev {d} parent 1:999 classid 1:{cid} htb rate {min(500, gper)}kbit ceil {gper}kbit")
                leaf(d, cid)
                run(f"tc filter add dev {d} parent 1: protocol ip prio 2 u32 match u8 {b} 0x0f at {off} flowid 1:{cid}")
        else:
            leaf(d, 999)
        for u in cfg["users"]:
            kb = int(u[key] * 1000)
            run(f"tc class add dev {d} parent 1:1 classid 1:{u['cid']} "
                f"htb rate {min(1000, kb)}kbit ceil {kb}kbit")
            leaf(d, u["cid"])
            run(f"tc filter add dev {d} parent 1: protocol ip prio 1 u32 "
                f"match ip {side} {u['ip']}/32 flowid 1:{u['cid']}")


def accounting_text(cfg):
    dev = cfg["interface"]
    lines = ["table ip nek0acct", "delete table ip nek0acct", "table ip nek0acct {"]
    for u in cfg["users"]:
        lines.append(f"  counter c_{u['key']} {{ packets 0 bytes 0 }}")
    lines.append("  counter c_total { packets 0 bytes 0 }")
    lines.append("  chain acct_fwd {")
    lines.append("    type filter hook forward priority 10; policy accept;")
    for u in cfg["users"]:
        if u["paused"]:
            lines.append(f"    ip saddr {u['ip']} drop")
            lines.append(f"    ip daddr {u['ip']} drop")
    for u in cfg["users"]:
        lines.append(f"    ip saddr {u['ip']} counter name c_{u['key']}")
        lines.append(f"    ip daddr {u['ip']} counter name c_{u['key']}")
    lines.append(f'    iifname "{dev}" counter name c_total')
    lines.append(f'    oifname "{dev}" counter name c_total')
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def setup_accounting(cfg):
    run("nft -f -", stdin=accounting_text(cfg))


# ---------------------------------------------------------------- commands
def cmd_apply(cfg, st, restart_ftl=True):
    run("nft -c -f -", stdin=accounting_text(cfg))  # syntax check before touching anything
    monthly_reset(st)
    collect(cfg, st)  # bank usage before the counters are rebuilt
    setup_psk(cfg)
    need_restart = setup_dhcp(cfg, st, restart=restart_ftl)
    setup_shaping(cfg)
    setup_accounting(cfg)
    for u in cfg["users"]:
        s = S(st, u)
        s["last"] = 0
        s["throttled"] = False
    TOT(st)["last"] = 0
    enforce(cfg, st)
    if cfg.get("uplink") and st.get("uplink", {}).get("mode") == "backup":
        if not uplink_switch(cfg, st["uplink"], "backup"):
            uplink_switch(cfg, st["uplink"], "primary")
    print(f"applied: {len(cfg['users'])} user(s). Devices take their fixed IP when they reconnect.")
    return need_restart


def iface_state(dev):
    try:
        with open(f"/sys/class/net/{dev}/operstate") as f:
            return f.read().strip()
    except OSError:
        return None


def ap_watchdog(cfg, st):
    """hostapd can keep 'running' after something takes the Wi-Fi link down (for example a
    NetworkManager reload). If the link stays down for two polls, restart the access point."""
    if not cfg.get("watchdog", True):
        return
    if iface_state(cfg["interface"]) != "down":
        st["wd_down"] = 0
        return
    rc, _ = run("systemctl is-active --quiet hostapd", check=False)
    if rc != 0:  # hostapd was stopped on purpose: leave it alone
        st["wd_down"] = 0
        return
    st["wd_down"] = st.get("wd_down", 0) + 1
    if st["wd_down"] < 2 or time.time() - st.get("wd_last", 0) < 300:
        return
    st["wd_down"] = 0
    st["wd_last"] = time.time()
    print("watchdog: the Wi-Fi link is down while hostapd is running; restarting it")
    run("systemctl restart hostapd", check=False)
    time.sleep(5)
    run("systemctl restart pihole-FTL", check=False)
    cmd_apply(cfg, st)  # rebuild the speed limits on wlan0


def probe_ping(iface):
    if not backup_gw(iface):
        return False
    for ip in ("8.8.8.8", "9.9.9.9"):
        rc, _ = run(f"ping -I {iface} -c 1 -W 2 -q {ip}", check=False)
        if rc == 0:
            return True
    return False


def probe_speed(iface, url):
    """Mbps measured over one interface, or None when the check was inconclusive."""
    _, out = run(f"curl -sk --interface {iface} -o /dev/null -m 10 -w '%{{http_code}} %{{size_download}} %{{speed_download}}' '{url}'",
                 check=False)
    try:
        code, size, speed = (out or "").split()
        if code == "200" and float(size) >= 1e6:
            return float(speed) * 8 / 1e6
    except ValueError:
        pass
    return None


def backup_gw(dev):
    _, out = run(f"ip -4 route show default dev {dev}", check=False)
    m = re.search(r"via (\d+\.\d+\.\d+\.\d+)", out or "")
    return m.group(1) if m else None


def set_total_cap(cfg, mbps):
    kb = int(mbps * 1000)
    for d in (cfg["interface"], IFB):
        run(f"tc class change dev {d} parent 1: classid 1:1 htb rate {kb}kbit ceil {kb}kbit", check=False)


def uplink_switch(cfg, u, to):
    """Use the phone hotspot (backup) or Ethernet. Backup = a lower-metric default route plus a speed cap."""
    up = cfg["uplink"]
    dev = up["backup_iface"]
    if to == "backup":
        gw = backup_gw(dev)
        if not gw:
            run(f"nmcli -w 8 device wifi rescan ifname {dev}", check=False)
            run(f"nmcli -w 10 device connect {dev}", check=False)
            gw = backup_gw(dev)
        if not gw or (u.get("mode") != "backup" and not probe_ping(dev)):
            return False
        run(f"ip route replace default via {gw} dev {dev} metric 50", check=False)
        set_total_cap(cfg, up["backup_cap_mbps"])  # protects the phone's mobile data
    else:
        run(f"ip route del default dev {dev} metric 50", check=False)
        set_total_cap(cfg, cfg["total_mbps"])
    if u.get("mode") != to:
        u["mode"], u["since"] = to, time.time()
        print("uplink: now using", "the phone hotspot" if to == "backup" else "Ethernet")
    return True


def uplink_watchdog(cfg, st):
    """Switch to the phone hotspot when Ethernet is down or slow, and back when it recovers.
    Checks always go out over Ethernet, so they never use mobile data."""
    up = cfg.get("uplink")
    if not up:
        return
    u = st.setdefault("uplink", {"mode": "primary", "bad": 0, "good": 0, "slow": 0, "n": 0})
    if u.get("force"):
        uplink_switch(cfg, u, "backup" if u["force"] == "backup" else "primary")
        return
    u["n"] += 1
    ok = probe_ping(up["primary_iface"])
    if ok and u["n"] % 10 == 1:  # a small download about every 5 minutes (the poll runs every 30 s)
        mbps = probe_speed(up["primary_iface"], up["probe_url"])
        if mbps is not None:
            u["mbps"] = round(mbps, 1)
            u["slow"] = u["slow"] + 1 if mbps < up["min_mbps"] else 0
    u["bad"] = 0 if ok else u["bad"] + 1
    u["good"] = u["good"] + 1 if ok else 0
    if u["mode"] == "primary":
        if u["bad"] >= 2 or u["slow"] >= 2:
            if uplink_switch(cfg, u, "backup"):
                u["slow"] = 0
    else:
        back = (u["good"] >= 3 and u["slow"] == 0 and time.time() - u.get("since", 0) > 300
                and (u.get("mbps") or 99) >= up["min_mbps"] * 1.5)
        if back or not uplink_switch(cfg, u, "backup"):
            uplink_switch(cfg, u, "primary")


def cmd_uplink(cfg, st, mode):
    if not cfg.get("uplink"):
        sys.exit("add an 'uplink:' section to users.yaml first (see the README)")
    u = st.setdefault("uplink", {"mode": "primary", "bad": 0, "good": 0, "slow": 0, "n": 0})
    if mode in ("hotspot", "ethernet"):
        u["force"] = "backup" if mode == "hotspot" else "primary"
        if not uplink_switch(cfg, u, u["force"]):
            u.pop("force")
            sys.exit("the hotspot is not connected (is the phone's hotspot on?)")
    elif mode == "auto":
        u.pop("force", None)
    print(f"uplink: {'phone hotspot' if u['mode'] == 'backup' else 'Ethernet'}"
          f"{' (forced)' if u.get('force') else ' (automatic)'} | last Ethernet speed check: {u.get('mbps', '-')} Mbps")


def psk_adopt(cfg, st):
    """If a device connected using a user's personal Wi-Fi password (keyid=...), attach its MAC automatically
    only when none of that user's existing devices is already online (prevents password sharing)."""
    if not any(u.get("psk") for u in cfg["users"]):
        return
    _, out = run(f"hostapd_cli -i {cfg['interface']} all_sta", check=False)
    by_key = {u["key"]: u for u in cfg["users"] if u.get("psk")}
    owners = mac_owner_map(cfg)
    stations = []
    for line in (out or "").splitlines():
        line = line.strip()
        if re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", line):
            stations.append([line.lower(), None])
        elif stations and line.startswith("keyid="):
            stations[-1][1] = line.split("=", 1)[1]
    online_macs = {m for m, _ in stations}
    changed = False
    for mac, keyid in stations:
        u = by_key.get(keyid)
        if not u or mac in owners:
            continue
        if any(m in online_macs for m in user_macs(u)):
            continue  # user already has a device online -> do not auto-adopt a 2nd concurrent device
        u.setdefault("extra_macs", []).append(mac)
        owners[mac] = u
        online_macs.add(mac)
        changed = True
        print(f"psk: attached new MAC {mac} to {u['name']}")
    if changed:
        save_cfg(cfg)
        setup_dhcp(cfg, st, restart=True)


def cmd_poll(cfg, st):
    monthly_reset(st)
    collect(cfg, st)
    enforce(cfg, st)
    psk_adopt(cfg, st)
    ap_watchdog(cfg, st)
    uplink_watchdog(cfg, st)


def fmt_bytes(b):
    b = float(b)
    if b >= 1e9:
        return f"{b / 1e9:.2f} GB"
    if b >= 1e6:
        return f"{b / 1e6:.0f} MB"
    return f"{b / 1e3:.0f} KB"


def cmd_status(cfg, st):
    monthly_reset(st)
    collect(cfg, st)
    _, iw = run(f"iw dev {cfg['interface']} station dump", check=False)
    assoc = {m.lower() for m in re.findall(r"(?m)^Station ([0-9a-fA-F:]{17})", iw or "")}
    t, g = TOT(st), GUEST(st)
    day = st.get("days", {}).get(today(), {})
    everyone = sum(S(st, u)["used"] for u in cfg["users"]) + g["used"]
    print(f"month {st['month']} | total {fmt_bytes(everyone)} (guests {fmt_bytes(g['used'])}) | "
          f"today {fmt_bytes(sum(day.values()))} | now {t.get('rate', 0) / 1e6:.1f} Mbps")
    print(f"limits: total {cfg['total_mbps']} Mbps | guests {cfg['guest_mbps']} Mbps shared ({cfg.get('guest_per_mbps', cfg['guest_mbps'])} Mbps/guest)")
    w = max([len(u["name"]) for u in cfg["users"]] + [4]) + 2
    print(f"{'user':<{w}}{'ip':<13}{'down/up':<10}{'used':>10}{'quota':>8}{'now':>11}  state")
    for u in cfg["users"]:
        s = S(st, u)
        q = f"{u['quota_gb']} GB" if u.get("quota_gb") is not None else "-"
        on = any(m in assoc for m in user_macs(u))
        state = "PAUSED" if u["paused"] else ("THROTTLED" if s["throttled"] else ("ONLINE" if on else "ok"))
        print(f"{u['name']:<{w}}{u['ip']:<13}{str(u['down_mbps']) + '/' + str(u['up_mbps']):<10}"
              f"{fmt_bytes(s['used']):>10}{q:>8}{s.get('rate', 0) / 1e6:>7.1f} Mbps  {state}")


def cmd_reset(cfg, st, who):
    target = find_user(cfg, who)["name"] if who is not None else None
    for u in cfg["users"]:
        if target in (None, u["name"]):
            S(st, u)["used"] = 0
    enforce(cfg, st)


# ---------------------------------------------------------------- easy management
def num(v):
    f = float(v)
    return int(f) if f == int(f) else f


def num_cli(v, what="value"):
    try:
        val = num(v)
    except (TypeError, ValueError, OverflowError):
        sys.exit(f"{what}: expected a number, got '{v}'")
    if val < 0 or val != val:
        sys.exit(f"{what}: must be a positive number")
    return val


def read_leases():
    out = []
    try:
        with open(LEASES) as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    out.append({"mac": p[1].lower(), "ip": p[2], "host": "" if p[3] == "*" else p[3]})
    except OSError:
        pass
    return sorted(out, key=lambda d: ipaddress.ip_address(d["ip"]))


def save_cfg(cfg):
    data = {k: cfg[k] for k in ("interface", "total_mbps", "guest_mbps", "guest_per_mbps", "throttle_kbps", "watchdog")
            if cfg.get(k) is not None}
    if cfg.get("uplink"):
        data["uplink"] = {k: cfg["uplink"][k] for k in ("backup_iface", "primary_iface", "min_mbps", "backup_cap_mbps", "probe_url")}
    data["users"] = [{k: u[k] for k in ("name", "mac", "extra_macs", "ip", "down_mbps", "up_mbps", "quota_gb", "psk", "paused")
                      if u.get(k) not in (None, False, [], "")} for u in cfg["users"]]
    with open(CONF, "w") as f:
        f.write("# nek0 users - managed by 'nek0ctl add/set/remove', or edit by hand then: sudo nek0ctl apply\n")
        yaml.safe_dump(data, f, sort_keys=False)


def cmd_devices(cfg):
    reg = {m: u["name"] for m, u in mac_owner_map(cfg).items()}
    print(f"{'ip':<14}{'mac':<20}{'device name':<18}status")
    for d in read_leases():
        who = ('user: ' + reg[d['mac']]) if d['mac'] in reg else 'guest'
        priv = ' (private MAC)' if is_private_mac(d['mac']) else ''
        print(f"{d['ip']:<14}{d['mac']:<20}{(d['host'] or '-'):<18}{who}{priv}")


def cmd_add(cfg, st):
    reg = set(mac_owner_map(cfg))
    devs = [d for d in read_leases() if d["mac"] not in reg]
    if not devs:
        sys.exit("no unregistered devices are connected right now")
    for i, d in enumerate(devs, 1):
        print(f"{i}) {d['host'] or '(no name)':<18}{d['mac']}  {d['ip']}")
    d = devs[int(input("Which device number? ")) - 1]
    default = re.sub(r"[^A-Za-z0-9_-]", "-", d["host"] or "user")
    name = re.sub(r"[^A-Za-z0-9_-]", "-", input(f"Name [{default}]: ").strip() or default)
    if name.replace("-", "_") in {u["key"] for u in cfg["users"]}:
        sys.exit("that name is already used")
    down = num_cli(input("Download limit in Mbps [10]: ") or 10, "download")
    up = num_cli(input("Upload limit in Mbps [5]: ") or 5, "upload")
    q = input("Monthly quota in GB (blank = unlimited): ").strip()
    used = {u["ip"] for u in cfg["users"]}
    ip = next(f"10.42.0.{i}" for i in range(20, 100) if f"10.42.0.{i}" not in used)
    user = {"name": name, "mac": d["mac"], "ip": ip, "down_mbps": down, "up_mbps": up}
    if q:
        user["quota_gb"] = num_cli(q, "quota")
    cfg["users"].append(user)
    save_cfg(cfg)
    cmd_apply(load(), st)
    print(f"{name} now has fixed IP {ip}. Turn the device's Wi-Fi off and on to pick it up.")


def find_user(cfg, name):
    norm = str(name).replace("-", "_").lower()
    for u in cfg["users"]:
        if u["name"] == name or u["key"].lower() == norm:
            return u
    names = ", ".join(u["name"] for u in cfg["users"]) or "none"
    sys.exit(f"no user called '{name}' (available: {names})")


def cmd_set(cfg, st, args):
    if len(args) < 3 or len(args) % 2 == 0:
        sys.exit("usage: sudo nek0ctl set NAME down 10 up 5 quota 50 name NEWNAME used 12.5 psk PASS\n"
                 "       (quota/psk none = unset; used = GB used this month)")
    u = find_user(cfg, args[0])
    for k, v in zip(args[1::2], args[2::2]):
        if k == "down":
            u["down_mbps"] = num_cli(v, "down")
        elif k == "up":
            u["up_mbps"] = num_cli(v, "up")
        elif k == "quota":
            u["quota_gb"] = None if v == "none" else num_cli(v, "quota")
        elif k == "psk":
            if v in ("none", ""):
                u.pop("psk", None)
            else:
                if not 8 <= len(v) <= 63 or any(not 32 <= ord(c) < 127 for c in v):
                    sys.exit("psk: 8-63 plain characters")
                if any(x is not u and x.get("psk") == v for x in cfg["users"]):
                    sys.exit("that psk is already used by another user")
                u["psk"] = v
        elif k == "name":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", v):
                sys.exit("name: letters, digits, - and _ only")
            if v != u["name"] and v.replace("-", "_") in {x["key"] for x in cfg["users"]}:
                sys.exit("that name is already used")
            rename_state(st, u["key"], v.replace("-", "_"))
            u["name"] = v
            u["key"] = v.replace("-", "_")
        elif k == "used":
            S(st, u)["used"] = num_cli(v, "used") * GB
        elif k in ("mac", "addmac", "delmac"):
            m = v.lower().replace("-", ":")
            if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m):
                sys.exit("mac: like aa:bb:cc:dd:ee:ff")
            if k == "delmac":
                if m == u["mac"]:
                    sys.exit("that is the primary MAC; set a different one with 'mac' first")
                if m not in u["extra_macs"]:
                    sys.exit("that MAC is not an extra MAC of this user")
                u["extra_macs"].remove(m)
                continue
            if any(x is not u and m in user_macs(x) for x in cfg["users"]):
                sys.exit("that MAC already belongs to another user")
            if k == "mac":
                u["mac"] = m
                u["extra_macs"] = [e for e in u["extra_macs"] if e != m]
            elif m != u["mac"] and m not in u["extra_macs"]:
                u["extra_macs"].append(m)
        else:
            sys.exit(f"unknown field '{k}' (use down, up, quota, name, used, mac, addmac, delmac or psk)")
    save_cfg(cfg)
    cmd_apply(load(), st)


def cmd_pause(cfg, st, name, paused):
    u = find_user(cfg, name)
    u["paused"] = paused
    save_cfg(cfg)
    cmd_apply(load(), st)


def cmd_remove(cfg, st, name):
    u = find_user(cfg, name)
    cfg["users"].remove(u)
    save_cfg(cfg)
    cmd_apply(load(), st)


def cmd_backup():
    files = [p for p in (CONF, STATE) if os.path.exists(p)]
    if not files:
        return None
    os.makedirs(os.path.join(BASE, "backups"), exist_ok=True)
    path = os.path.join(BASE, "backups", time.strftime("nek0-%Y%m%d-%H%M%S.tar.gz"))
    with tarfile.open(path, "w:gz") as t:
        for p in files:
            t.add(p, arcname=os.path.basename(p))
    backups = sorted(os.listdir(os.path.join(BASE, "backups")))
    for old in backups[:-20]:  # keep the newest 20
        os.remove(os.path.join(BASE, "backups", old))
    return path


def cmd_restore(path):
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if m.name in ("users.yaml", "state.json") and m.isfile():
                with open(os.path.join(BASE, m.name), "wb") as f:
                    f.write(t.extractfile(m).read())
    print("restored. Run: sudo nek0ctl apply")


def cmd_install():
    os.makedirs("/opt/nek0", exist_ok=True)
    saved = cmd_backup() if BASE == "/opt/nek0" else None  # an update never loses users or usage
    if saved:
        print("backup of users and usage:", saved)
    dst = "/opt/nek0/nek0ctl.py"
    if os.path.abspath(__file__) != dst:
        shutil.copy(os.path.abspath(__file__), dst)
    os.chmod(dst, 0o755)
    if not os.path.exists("/opt/nek0/users.yaml"):
        with open("/opt/nek0/users.yaml", "w") as f:
            f.write(SAMPLE)
    if not os.path.exists("/usr/local/bin/nek0ctl"):
        os.symlink(dst, "/usr/local/bin/nek0ctl")
    for name, text in (("nek0ctl.service", SVC_APPLY), ("nek0ctl-poll.service", SVC_POLL),
                       ("nek0ctl-poll.timer", TIMER)):
        with open(f"/etc/systemd/system/{name}", "w") as f:
            f.write(text)
    run("systemctl daemon-reload")
    run("systemctl enable nek0ctl.service nek0ctl-poll.timer")
    run("systemctl restart nek0ctl.service")  # re-runs apply so new versions take effect now
    run("systemctl restart nek0ctl-poll.timer")
    print(f"installed nek0ctl {__version__}. Edit /opt/nek0/users.yaml, then run: sudo nek0ctl apply")


def main():
    cmds = ("install", "apply", "poll", "status", "reset", "devices", "add", "set", "remove",
            "pause", "resume", "backup", "restore", "uplink")
    if not ARGS or ARGS[0] not in cmds:
        sys.exit(__doc__)
    if not DRY and os.geteuid() != 0:
        sys.exit("run with sudo")
    cmd = ARGS[0]
    if cmd == "install":
        return cmd_install()
    if cmd == "backup":
        return print(cmd_backup() or "nothing to back up")
    if cmd == "restore":
        if len(ARGS) < 2:
            sys.exit("usage: sudo nek0ctl restore FILE.tar.gz")
        return cmd_restore(ARGS[1])
    lock = open(LOCK, "w") if not DRY else None
    if lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
    cfg, st = load(), load_state()
    if cmd == "apply":
        cmd_apply(cfg, st)
    elif cmd == "poll":
        cmd_poll(cfg, st)
    elif cmd == "status":
        cmd_status(cfg, st)
    elif cmd == "reset":
        cmd_reset(cfg, st, ARGS[1] if len(ARGS) > 1 else None)
    elif cmd == "devices":
        cmd_devices(cfg)
    elif cmd == "add":
        cmd_add(cfg, st)
    elif cmd == "set":
        cmd_set(cfg, st, ARGS[1:])
    elif cmd == "remove":
        if len(ARGS) < 2:
            sys.exit("usage: sudo nek0ctl remove NAME")
        cmd_remove(cfg, st, ARGS[1])
    elif cmd == "uplink":
        cmd_uplink(cfg, st, ARGS[1] if len(ARGS) > 1 else None)
    elif cmd in ("pause", "resume"):
        if len(ARGS) < 2:
            sys.exit(f"usage: sudo nek0ctl {cmd} NAME")
        cmd_pause(cfg, st, ARGS[1], cmd == "pause")
    if not DRY:
        save_state(st)


if __name__ == "__main__":
    main()
