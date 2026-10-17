#!/usr/bin/env python3
"""nek0web - web dashboard for the nek0 router (users, speeds, quotas, Wi-Fi settings).

  sudo python3 nek0web.py setup         one-time: admin password, HTTPS certificate, systemd service
  sudo python3 nek0web.py update        install a newer version (keeps password, certificate, users, usage)
  sudo python3 /opt/nek0/nek0web.py setpassword     change the admin password from the terminal
  sudo python3 /opt/nek0/nek0web.py restore-wifi    emergency: put the last confirmed Wi-Fi settings back

Then open  https://10.42.0.1:8443  from a device connected to the F-06 Wi-Fi.
(The browser warns about the self-signed certificate once; that is expected.)
Needs nek0ctl.py to be installed first (sudo python3 nek0ctl.py install).
"""
import fcntl
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import ssl
import sys
import threading
import datetime
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.3.3"
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("NEK0_DIR", "/opt/nek0")
AUTH = os.path.join(BASE, "web_auth.json")
CERT = os.path.join(BASE, "web.crt")
KEY = os.path.join(BASE, "web.key")
GOOD = os.path.join(BASE, "hostapd.conf.last-good")
PENDING = os.path.join(BASE, "wifi_pending.json")
HOSTAPD = os.environ.get("NEK0_HOSTAPD", "/etc/hostapd/hostapd.conf")
BIND = os.environ.get("NEK0_BIND", "10.42.0.1")
PORT = int(os.environ.get("NEK0_PORT", "8443"))
CONFIRM_SECONDS = 150
CHANNELS = (149, 153, 157, 161, 165)   # 5 GHz channels allowed in Pakistan (5725-5875 MHz)
HT40 = {149: "[HT40+]", 153: "[HT40-]", 157: "[HT40+]", 161: "[HT40-]"}

UNIT = """[Unit]
Description=nek0 web dashboard
After=network.target hostapd.service nek0ctl.service

[Service]
ExecStart=/usr/bin/python3 /opt/nek0/nek0web.py serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

n = None  # nek0ctl module, imported lazily (serve only)
SESSIONS = {}
FAILS = {}
SESS_FILE = os.path.join(BASE, "sessions.json")
SESSION_SECONDS = 30 * 24 * 3600
CTX = None
WIFI_LOCK = threading.Lock()


# ---------------------------------------------------------------- admin password
def hash_pw(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def write_auth(pw):
    salt = secrets.token_bytes(16)
    os.makedirs(BASE, exist_ok=True)
    with open(AUTH, "w") as f:
        json.dump({"salt": salt.hex(), "hash": hash_pw(pw, salt)}, f)
    os.chmod(AUTH, 0o600)


def check_pw(pw):
    try:
        with open(AUTH) as f:
            a = json.load(f)
        return hmac.compare_digest(hash_pw(pw, bytes.fromhex(a["salt"])), a["hash"])
    except (OSError, ValueError, KeyError):
        return False


def ask_new_password():
    while True:
        p1 = getpass.getpass("New admin password (8+ characters): ")
        if len(p1) < 8:
            print("too short")
            continue
        if p1 == getpass.getpass("Repeat it: "):
            return p1
        print("they did not match")


# ---------------------------------------------------------------- sessions (survive restarts)
def sid_key(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def sessions_load():
    try:
        with open(SESS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    now = time.time()
    for k, v in data.items():
        if v.get("exp", 0) > now:
            SESSIONS[k] = {"csrf": v["csrf"], "exp": v["exp"], "flash": []}


def sessions_save():
    now = time.time()
    data = {k: {"csrf": v["csrf"], "exp": v["exp"]} for k, v in SESSIONS.items() if v["exp"] > now}
    try:
        tmp = SESS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESS_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------- helpers
class Lock:
    def __enter__(self):
        self.f = open(n.LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)

    def __exit__(self, *a):
        self.f.close()


def numf(s, lo, hi, what):
    try:
        v = n.num(str(s).strip())
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{what} must be a number")
    if not lo <= v <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return v


def commit(cfg):
    """Save users.yaml, validate it, apply everything. Restores the old file if it does not validate."""
    with open(n.CONF) as f:
        old = f.read()
    n.save_cfg(cfg)
    try:
        cfg2 = n.load()
    except SystemExit:
        with open(n.CONF, "w") as f:
            f.write(old)
        raise
    st = n.load_state()
    restart = n.cmd_apply(cfg2, st, restart_ftl=False)
    n.save_state(st)
    if restart:  # restarting Pi-hole is slow: do it in the background, not inside the web request
        threading.Thread(target=lambda: n.run("systemctl restart pihole-FTL", check=False), daemon=True).start()


def find(cfg, name):
    for u in cfg["users"]:
        if u["name"] == name:
            return u
    raise ValueError(f"no user called '{name}'")


# ---------------------------------------------------------------- Wi-Fi (hostapd.conf)
def conf_read():
    with open(HOSTAPD) as f:
        return f.read().splitlines()


def conf_get(lines, key, default=""):
    for line in lines:
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return default


def conf_set(lines, key, val):
    out, done = [], False
    for line in lines:
        if line.startswith(key + "="):
            if val is not None and not done:
                out.append(f"{key}={val}")
            done = True
        else:
            out.append(line)
    if val is not None and not done:
        out.append(f"{key}={val}")
    return out


def conf_write(lines):
    tmp = HOSTAPD + ".new"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, HOSTAPD)


def pending():
    try:
        with open(PENDING) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def restart_wifi():
    n.run("systemctl restart hostapd", check=False)
    time.sleep(4)
    n.run("systemctl restart pihole-FTL", check=False)
    n.run("systemctl restart nek0ctl.service", check=False)  # rebuilds speed limits on wlan0


def rollback(reason="reverted"):
    with WIFI_LOCK:
        if os.path.exists(GOOD):
            shutil.copy(GOOD, HOSTAPD)
            os.chmod(HOSTAPD, 0o600)
        if os.path.exists(PENDING):
            os.remove(PENDING)
    print("wifi rollback:", reason, flush=True)
    restart_wifi()


def apply_wifi_async():
    def job():
        time.sleep(2)  # let the web response go out first
        n.run("systemctl restart hostapd", check=False)
        time.sleep(4)
        rc, _ = n.run("systemctl is-active --quiet hostapd", check=False)
        if rc != 0:
            rollback("hostapd did not start with the new settings")
        else:
            n.run("systemctl restart pihole-FTL", check=False)
            n.run("systemctl restart nek0ctl.service", check=False)
    threading.Thread(target=job, daemon=True).start()


def watcher():
    while True:
        time.sleep(3)
        p = pending()
        if p and time.time() > p["deadline"]:
            rollback("new Wi-Fi settings were not confirmed in time")


# ---------------------------------------------------------------- actions (each returns (kind, message))
def a_add(f):
    cfg = n.load()
    mac = (f.get("mac_manual") or f.get("mac_pick") or "").strip().lower()
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise ValueError("pick a device or type a MAC like aa:bb:cc:dd:ee:ff")
    name = f.get("name", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise ValueError("name: letters, digits, - and _ only (max 32)")
    if mac in n.mac_owner_map(cfg):
        raise ValueError("that device is already a user")
    if any(u["key"] == name.replace("-", "_") for u in cfg["users"]):
        raise ValueError("that name is already used")
    down = numf(f.get("down"), 0.5, 1000, "download")
    up = numf(f.get("up"), 0.5, 1000, "upload")
    user = {"name": name, "mac": mac, "down_mbps": down, "up_mbps": up}
    used = {u["ip"] for u in cfg["users"]}
    user["ip"] = next(f"10.42.0.{i}" for i in range(20, 100) if f"10.42.0.{i}" not in used)
    if f.get("quota", "").strip():
        user["quota_gb"] = numf(f["quota"], 0.1, 100000, "quota")
    psk = f.get("psk", "").strip()
    if psk:
        if not 8 <= len(psk) <= 63 or any(not 32 <= ord(c) < 127 for c in psk):
            raise ValueError("personal Wi-Fi password must be 8-63 plain characters")
        if any(x.get("psk") == psk for x in cfg["users"]):
            raise ValueError("that personal Wi-Fi password is already used by another user")
        user["psk"] = psk
    cfg["users"].append(user)
    commit(cfg)
    return "ok", f"{name} added with fixed IP {user['ip']}. Turn the device's Wi-Fi off and on to pick it up."


def a_set(f):
    cfg = n.load()
    u = find(cfg, f.get("name_old", ""))
    newname = f.get("name", u["name"]).strip()
    if newname != u["name"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", newname):
            raise ValueError("name: letters, digits, - and _ only (max 32)")
        if newname.replace("-", "_") in {x["key"] for x in cfg["users"]}:
            raise ValueError("that name is already used")
    down = numf(f.get("down"), 0.5, 1000, "download")
    up = numf(f.get("up"), 0.5, 1000, "upload")
    quota = numf(f["quota"], 0.1, 100000, "quota") if f.get("quota", "").strip() else None
    new_used = None
    if f.get("used", "").strip():
        v = numf(f["used"], 0, 1000000, "used GB")
        try:
            orig = float(f.get("used_orig") or 0)
        except ValueError:
            orig = 0
        if abs(v - orig) > 0.005:  # only touch usage when the field was really edited
            new_used = v
    if newname != u["name"] or new_used is not None:
        st = n.load_state()
        key = newname.replace("-", "_")
        n.rename_state(st, u["key"], key)
        if new_used is not None:
            n.S(st, {"key": key})["used"] = new_used * n.GB
        n.save_state(st)
    u["name"], u["down_mbps"], u["up_mbps"], u["quota_gb"] = newname, down, up, quota
    mac = f.get("mac", "").strip().lower().replace("-", ":")
    owners = n.mac_owner_map(cfg)
    if mac and mac != u["mac"]:
        if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            raise ValueError("MAC like aa:bb:cc:dd:ee:ff")
        if owners.get(mac, u) is not u:
            raise ValueError("that MAC already belongs to another user")
        u["mac"] = mac
    if "extra_macs" in f:
        extra = []
        for m in re.split(r"[\s,;]+", f.get("extra_macs", "").strip().lower().replace("-", ":")):
            if not m or m == u["mac"] or m in extra:
                continue
            if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m):
                raise ValueError(f"other MACs: '{m}' is not a MAC like aa:bb:cc:dd:ee:ff")
            if owners.get(m, u) is not u:
                raise ValueError(f"{m} already belongs to another user")
            extra.append(m)
        u["extra_macs"] = extra
    if "psk" in f:
        psk = f.get("psk", "").strip()
        if psk:
            if not 8 <= len(psk) <= 63 or any(not 32 <= ord(c) < 127 for c in psk):
                raise ValueError("personal Wi-Fi password must be 8-63 plain characters")
            if any(x is not u and x.get("psk") == psk for x in cfg["users"]):
                raise ValueError("that personal Wi-Fi password is already used by another user")
            u["psk"] = psk
        else:
            u.pop("psk", None)
    commit(cfg)
    return "ok", f"{newname} updated (usage kept)."


def a_adopt(f):
    """An unknown device (usually a private/random MAC) is attached to an existing user."""
    cfg = n.load()
    mac = f.get("mac", "").strip().lower()
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise ValueError("bad MAC")
    if mac in n.mac_owner_map(cfg):
        raise ValueError("that device already belongs to a user")
    u = find(cfg, f.get("owner", ""))
    u.setdefault("extra_macs", []).append(mac)
    commit(cfg)
    return "ok", f"Device {mac} added to {u['name']}. Turn its Wi-Fi off and on once to pick up the fixed IP."


def a_pause(f):
    cfg = n.load()
    u = find(cfg, f.get("name", ""))
    u["paused"] = f.get("act") == "pause"
    commit(cfg)
    return "ok", f"{u['name']} {'paused: no internet until you resume' if u['paused'] else 'resumed'}."


def a_remove(f):
    cfg = n.load()
    cfg["users"].remove(find(cfg, f.get("name", "")))
    commit(cfg)
    return "ok", f"{f['name']} removed (back to the guest pool)."


def a_reset(f):
    cfg, st = n.load(), n.load_state()
    who = f.get("name", "")
    n.cmd_reset(cfg, st, None if who == "*" else find(cfg, who)["name"])
    n.save_state(st)
    return "ok", "Usage reset."


def a_limits(f):
    cfg = n.load()
    cfg["total_mbps"] = numf(f.get("total"), 1, 1000, "total")
    cfg["guest_mbps"] = numf(f.get("guest"), 1, 1000, "guest")
    if f.get("guest_per", "").strip():
        cfg["guest_per_mbps"] = numf(f.get("guest_per"), 0.5, 1000, "per-guest")
    cfg["throttle_kbps"] = numf(f.get("throttle"), 16, 100000, "over-quota speed")
    commit(cfg)
    return "ok", "Speed settings applied."


def a_wifi(f):
    ssid = f.get("ssid", "")
    if not 1 <= len(ssid.encode()) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("Wi-Fi name must be 1-32 characters")
    lines = conf_read()
    msg = "Wi-Fi settings saved."
    pw = f.get("password", "")
    if f.get("randpw"):
        pw = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(16))
        msg = f"New Wi-Fi password: {pw}"
    if pw:
        if not 12 <= len(pw) <= 63 or any(not 32 <= ord(c) < 127 for c in pw):
            raise ValueError("password must be 12-63 plain characters (12+ also avoids the Windows PIN prompt)")
        lines = conf_set(lines, "wpa_passphrase", pw)
        if not f.get("randpw"):
            msg = "Wi-Fi password changed."
    try:
        ch = int(f.get("channel", ""))
    except ValueError:
        ch = 0
    if ch not in CHANNELS:
        raise ValueError("pick one of the listed channels")
    width = 40 if f.get("width") == "40" and ch in HT40 else 20
    lines = conf_set(lines, "ssid", ssid)
    lines = conf_set(lines, "channel", str(ch))
    lines = conf_set(lines, "ht_capab", (HT40[ch] + "[SHORT-GI-40]") if width == 40 else "[SHORT-GI-20]")
    lines = conf_set(lines, "ignore_broadcast_ssid", "1" if f.get("hidden") else None)
    if f.get("security") == "wpa3t":
        lines = conf_set(lines, "wpa_key_mgmt", "WPA-PSK SAE")
        lines = conf_set(lines, "ieee80211w", "1")
        lines = conf_set(lines, "sae_pwe", "2")
    else:
        lines = conf_set(lines, "wpa_key_mgmt", "WPA-PSK")
        lines = conf_set(lines, "ieee80211w", None)
        lines = conf_set(lines, "sae_pwe", None)
        lines = conf_set(lines, "ctrl_interface", "/var/run/hostapd")
        lines = conf_set(lines, "wpa_psk_file", "/etc/hostapd/hostapd.psk")
    with WIFI_LOCK:
        if not os.path.exists(PENDING):  # keep the last CONFIRMED config as the fallback
            shutil.copy(HOSTAPD, GOOD)
            os.chmod(GOOD, 0o600)
        conf_write(lines)
        with open(PENDING, "w") as pf:
            json.dump({"deadline": time.time() + CONFIRM_SECONDS}, pf)
    apply_wifi_async()
    return "ok", msg + " The Wi-Fi is restarting: reconnect, open this page again and press Keep. If you cannot, it reverts by itself."


def a_wifi_keep(f):
    with WIFI_LOCK:
        shutil.copy(HOSTAPD, GOOD)
        os.chmod(GOOD, 0o600)
        if os.path.exists(PENDING):
            os.remove(PENDING)
    return "ok", "Wi-Fi settings kept."


def a_wifi_revert(f):
    threading.Thread(target=rollback, args=("reverted by admin",), daemon=True).start()
    return "ok", "Reverting the Wi-Fi settings. Reconnect to the old network."


def a_uplink(f):
    cfg, st = n.load(), n.load_state()
    if not cfg.get("uplink"):
        raise ValueError("uplink failover is not configured in users.yaml")
    mode = f.get("mode", "auto")
    if mode not in ("auto", "hotspot", "ethernet"):
        raise ValueError("mode must be auto, hotspot or ethernet")
    u = st.setdefault("uplink", {"mode": "primary", "bad": 0, "good": 0, "slow": 0, "n": 0})
    if mode == "auto":
        u.pop("force", None)
        n.save_state(st)
        return "ok", "Internet link set to automatic."
    u["force"] = "backup" if mode == "hotspot" else "primary"
    if not n.uplink_switch(cfg, u, u["force"]):
        u.pop("force", None)
        raise ValueError("the phone hotspot is not reachable (is the hotspot turned on?)")
    n.save_state(st)
    return "ok", f"Internet link forced to {'phone hotspot' if mode == 'hotspot' else 'Ethernet'}."


def a_password(f):
    if not check_pw(f.get("current", "")):
        raise ValueError("current password is wrong")
    if len(f.get("new", "")) < 8 or f.get("new") != f.get("new2"):
        raise ValueError("new password: 8+ characters, typed twice the same")
    write_auth(f["new"])
    return "ok", "Admin password changed."


ACTIONS = {"add": a_add, "adopt": a_adopt, "set": a_set, "pause": a_pause, "resume": a_pause, "remove": a_remove, "reset": a_reset, "limits": a_limits,
           "uplink": a_uplink, "wifi": a_wifi, "wifi_keep": a_wifi_keep, "wifi_revert": a_wifi_revert, "password": a_password}


def perform(act, form):
    fn = ACTIONS.get(act)
    if not fn:
        return "err", "unknown action"
    try:
        form["act"] = act
        with Lock():
            return fn(form)
    except ValueError as e:
        return "err", str(e)
    except SystemExit as e:
        return "err", str(e.code)
    except Exception as e:  # keep the dashboard alive whatever happens
        return "err", f"unexpected error: {e}"


# ---------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f4f5f7;--card:#fff;--fg:#1c1f24;--mut:#667085;--line:#e3e6eb;--acc:#2563eb;--bad:#c0392b;--ok:#1e8e4e}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--fg:#e8eaee;--mut:#98a2b3;--line:#2c313a;--acc:#5b8def;--bad:#ff7b6b;--ok:#4cc38a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:12px}h1{font-size:20px;margin:10px 0}h2{font-size:16px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:end}label{display:block;font-size:12px;color:var(--mut)}
input,select{padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);max-width:100%}
input[type=number]{width:90px}input[type=text],input[type=password]{width:170px}
button{padding:8px 12px;border:0;border-radius:6px;background:var(--acc);color:#fff;cursor:pointer}
button.g{background:var(--line);color:var(--fg)}button.d{background:var(--bad)}
.msg{padding:10px;border-radius:8px;margin:10px 0;border:1px solid var(--line)}.ok{border-color:var(--ok)}.err{border-color:var(--bad)}
.mut{color:var(--mut);font-size:13px}.bar{height:8px;background:var(--line);border-radius:5px;overflow:hidden;margin:4px 0}
.bar i{display:block;height:100%;background:var(--acc)}.bar.hot i{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}
.spark{display:flex;align-items:flex-end;gap:2px;height:36px;margin:6px 0}.spark i{flex:1;background:var(--acc);border-radius:2px;opacity:.85}.big{font-size:22px;font-weight:600}.stats{display:flex;flex-wrap:wrap;gap:18px}
.wrap{overflow-x:auto}.tag{font-size:11px;padding:2px 6px;border-radius:5px;background:var(--line)}.tag.bad{background:var(--bad);color:#fff}.tag.ok{background:var(--ok);color:#fff}
details{margin-top:6px}summary{cursor:pointer;font-size:13px;color:var(--acc);user-select:none}
"""


def esc(x):
    return html.escape(str(x), quote=True)


SCRIPT = ("setInterval(function(){fetch('/stats',{credentials:'same-origin'}).then(function(r){return r.json()})"
          ".then(function(d){for(var k in d){var e=document.getElementById(k);if(e){e.innerHTML=d[k]}}})"
          ".catch(function(){})},15000);")


def page(title, body, nonce=None):
    script = f"<script nonce='{nonce}'>{SCRIPT}</script>" if nonce else ""
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main>{script}</body></html>")


def login_page(error=""):
    err = f"<div class='msg err'>{esc(error)}</div>" if error else ""
    return page("nek0 login", f"<h1>nek0 router</h1>{err}<div class=card><form method=post action=/login>"
                "<label>Admin password</label><div class=row><input type=password name=password autofocus>"
                "<button>Log in</button></div></form></div>")


def form(csrf, act, inner, button="Save", cls="", confirm=""):
    onclick = f" onclick=\"return confirm('{esc(confirm)}')\"" if confirm else ""
    return (f"<form method=post action=/act><input type=hidden name=csrf value='{esc(csrf)}'>"
            f"<input type=hidden name=act value='{act}'>{inner}<button class='{cls}'{onclick}>{esc(button)}</button></form>")


def fmt(b):
    b = float(b)
    if b >= 1e9:
        return f"{b / 1e9:.2f} GB"
    if b >= 1e6:
        return f"{b / 1e6:.0f} MB"
    return f"{b / 1e3:.0f} KB"


def spark(st, key=None, days=14):
    """Tiny bar chart of the last days of usage (key=None means everything)."""
    today = datetime.date.today()
    items = []
    for i in range(days - 1, -1, -1):
        d = today - datetime.timedelta(days=i)
        rec = st.get("days", {}).get(d.isoformat(), {})
        items.append((d.strftime("%b %d"), sum(rec.values()) if key is None else rec.get(key, 0)))
    top = max(v for _, v in items) or 1
    bars = "".join(f"<i title='{esc(lbl)}: {fmt(v)}' style='height:{max(3, v / top * 100):.0f}%'></i>" for lbl, v in items)
    return f"<div class=spark>{bars}</div>"


def read_stations(dev="wlan0"):
    """Devices really associated with the access point right now (not just holding a DHCP lease)."""
    _, out = n.run(f"iw dev {dev} station dump", check=False)
    res = {}
    for block in re.split(r"(?m)^Station ", out or "")[1:]:
        mac = block.split()[0].lower()
        sig = re.search(r"signal:\s*(-?\d+)", block)
        rate = re.search(r"tx bitrate:\s*([\d.]+) MBit/s", block)
        res[mac] = {"signal": int(sig.group(1)) if sig else None, "rate": float(rate.group(1)) if rate else None}
    return res


def uplink_inner(cfg, st):
    if not cfg.get("uplink"):
        return ""
    u = st.get("uplink", {})
    via = "phone hotspot (Ethernet is down or slow)" if u.get("mode") == "backup" else "hostel Ethernet"
    mode = " (forced)" if u.get("force") else " (automatic)"
    sp = f" &middot; last Ethernet speed check {u['mbps']} Mbps" if u.get("mbps") is not None else ""
    return f"<b>Internet via:</b> {via}{mode}{sp}"


def usage_inner(cfg, st):
    tot, gst = n.TOT(st), n.GUEST(st)
    day = st.get("days", {}).get(datetime.date.today().isoformat(), {})
    users_used = sum(n.S(st, u)["used"] for u in cfg["users"])
    return ("<div class=stats>"
            f"<div><div class=big>{fmt(users_used + gst['used'])}</div><div class=mut>everyone</div></div>"
            f"<div><div class=big>{fmt(users_used)}</div><div class=mut>users</div></div>"
            f"<div><div class=big>{fmt(gst['used'])}</div><div class=mut>guests</div></div>"
            f"<div><div class=big>{fmt(sum(day.values()))}</div><div class=mut>today</div></div>"
            f"<div><div class=big>{tot.get('rate', 0) / 1e6:.1f}</div><div class=mut>Mbps now</div></div></div>"
            + spark(st) + "<div class=mut>Last 14 days, all devices. Updates by itself every 15 seconds.</div>")


def user_stats(u, st, leases=(), stations=None):
    s = n.S(st, u)
    day = st.get("days", {}).get(datetime.date.today().isoformat(), {})
    used = s["used"] / n.GB
    q = u.get("quota_gb")
    pct = min(100, used / q * 100) if q else 0
    tag = ""
    if u["paused"]:
        tag += " <span class='tag bad'>PAUSED</span>"
    if s["throttled"]:
        tag += " <span class='tag bad'>THROTTLED</span>"
    macs = n.user_macs(u)
    cur = (next((d for d in leases if d["mac"] in macs and d["mac"] in (stations or {})), None)
           or next((d for d in leases if d["mac"] in macs), None))
    if cur and cur["ip"] != u["ip"]:
        tag += f" <span class='tag bad'>on {esc(cur['ip'])}: turn its Wi-Fi off and on to apply the limits</span>"
    if stations is not None:
        tag += " <span class='tag ok'>online</span>" if any(m in stations for m in macs) else " <span class=tag>offline</span>"
    if u.get("psk"):
        tag += " <span class=tag>personal Wi-Fi password</span>"
    qtxt = f"{fmt(s['used'])} of {q} GB" if q else f"{fmt(s['used'])} (no quota)"
    return (f"<b>{esc(u['name'])}</b>{tag} <span class=mut>{esc(u['ip'])} &middot; {u['down_mbps']}/{u['up_mbps']} Mbps &middot; {esc(', '.join(macs))}</span>"
            f"<div class='bar{' hot' if pct >= 90 else ''}'><i style='width:{pct:.0f}%'></i></div>"
            f"<div class=mut><b>{qtxt}</b> &middot; today {fmt(day.get(u['key'], 0))} &middot; now {s.get('rate', 0) / 1e6:.1f} Mbps</div>"
            + spark(st, u["key"]))


def devices_inner(cfg, leases, stations=None):
    reg = n.mac_owner_map(cfg)
    stations = stations or {}
    rows = []
    for d in sorted(leases, key=lambda d: (d["mac"] not in stations, d["ip"])):
        st_ = stations.get(d["mac"])
        who = ("<b>" + esc(reg[d["mac"]]["name"]) + "</b>") if d["mac"] in reg else "<span class=tag>guest</span>"
        if d["mac"] in reg and reg[d["mac"]]["ip"] != d["ip"]:
            who += " <span class='tag bad'>not on its fixed IP yet</span>"
        online = "<span class='tag ok'>online</span>" if st_ else "<span class=mut>offline</span>"
        sig = f"{st_['signal']} dBm" if st_ and st_["signal"] is not None else "-"
        rate = f"{st_['rate']:.0f} Mbps" if st_ and st_["rate"] else "-"
        rows.append(f"<tr><td>{online}</td><td>{esc(d['ip'])}</td><td>{esc(d['host'] or '-')}</td><td>{esc(d['mac'])}</td>"
                    f"<td>{who}</td><td>{sig}</td><td>{rate}</td></tr>")
    return ("<div class=wrap><table><tr><th>Now</th><th>IP</th><th>Device</th><th>MAC</th><th>Status</th>"
            f"<th>Signal</th><th>Link</th></tr>{''.join(rows)}</table></div>")


def suggest_owner(cfg, leases, d):
    """Best guess for who an unknown device belongs to: same DHCP hostname as one of a user's devices.
    Only a hint; the admin always confirms."""
    host = (d["host"] or "").lower()
    if not host:
        return None
    for u in cfg["users"]:
        names = {x["host"].lower() for x in leases if x["mac"] in n.user_macs(u) and x["host"]}
        if host in names:
            return u["name"]
    return None


def unknown_card(csrf, cfg, leases, stations):
    """Devices that are not a user. Private (random) MACs get a one-click 'belongs to' button."""
    reg = n.mac_owner_map(cfg)
    if not cfg["users"]:
        return ""
    rows = []
    for d in sorted((d for d in leases if d["mac"] not in reg), key=lambda d: (d["mac"] not in stations, d["ip"])):
        guess = suggest_owner(cfg, leases, d)
        opts = "".join(f"<option{' selected' if u['name'] == guess else ''}>{esc(u['name'])}</option>" for u in cfg["users"])
        priv = " <span class=tag>private MAC</span>" if n.is_private_mac(d["mac"]) else ""
        hint = f" <span class=mut>looks like {esc(guess)}</span>" if guess else ""
        online = "<span class='tag ok'>online</span>" if d["mac"] in stations else "<span class=mut>offline</span>"
        inner = (f"<input type=hidden name=mac value='{esc(d['mac'])}'>"
                 f"<select name=owner>{opts}</select>")
        rows.append(f"<tr><td>{online}</td><td>{esc(d['host'] or '-')}</td><td>{esc(d['mac'])}{priv}</td>"
                    f"<td>{esc(d['ip'])}</td><td>{hint}{form(csrf, 'adopt', inner, 'Belongs to this user', 'g')}</td></tr>")
    if not rows:
        return ""
    return ("<div class=card><h2>Unknown devices</h2><p class=mut>Not a user, so they use the guest pool. "
            "If it is a user's phone with a new private Wi-Fi address, attach it to that user (limits, fixed IP and usage are shared).</p>"
            "<div class=wrap><table><tr><th>Now</th><th>Device</th><th>MAC</th><th>IP</th><th>Attach to</th></tr>"
            + "".join(rows) + "</table></div></div>")


def fragments():
    """The live-updating parts of the page (the forms are left alone so typing is never interrupted)."""
    cfg, st = n.load(), n.load_state()
    n.monthly_reset(st)
    leases, stations = n.read_leases(), read_stations(cfg["interface"])
    out = {"uplink": uplink_inner(cfg, st), "usage": usage_inner(cfg, st), "devices": devices_inner(cfg, leases, stations)}
    for u in cfg["users"]:
        out["u_" + u["key"]] = user_stats(u, st, leases, stations)
    return out


def dashboard(sess, nonce=None):
    csrf = sess["csrf"]
    cfg, st = n.load(), n.load_state()
    n.monthly_reset(st)
    leases, stations = n.read_leases(), read_stations(cfg["interface"])
    reg = n.mac_owner_map(cfg)
    out = ["<h1>nek0 router</h1>"]
    for kind, text in sess.pop("flash", []):
        out.append(f"<div class='msg {kind}'>{esc(text)}</div>")
    p = pending()
    if p:
        left = max(0, int(p["deadline"] - time.time()))
        out.append("<meta http-equiv=refresh content=10><div class='msg err'><b>Wi-Fi settings changed.</b> "
                   f"Keep them within {left} s or they revert automatically.<div class=row>"
                   + form(csrf, "wifi_keep", "", "Keep new settings")
                   + form(csrf, "wifi_revert", "", "Revert now", "g") + "</div></div>")

    if cfg.get("uplink"):
        up_btns = ("<div class=row style='margin-top:8px'>"
                   + form(csrf, "uplink", "<input type=hidden name=mode value=auto>", "Automatic", "g")
                   + form(csrf, "uplink", "<input type=hidden name=mode value=hotspot>", "Use phone hotspot", "g")
                   + form(csrf, "uplink", "<input type=hidden name=mode value=ethernet>", "Use Ethernet", "g")
                   + "</div>")
        out.append("<div class=card><div id=uplink>" + uplink_inner(cfg, st) + "</div>" + up_btns + "</div>")
    # usage overview
    out.append("<div class=card><h2>Data used this month</h2><div id=usage>" + usage_inner(cfg, st) + "</div></div>")

    # users
    out.append("<div class=card><h2>Users</h2>")
    if not cfg["users"]:
        out.append("<p class=mut>No users yet. Everyone is in the guest pool. Add a connected device below.</p>")
    for u in cfg["users"]:
        s = n.S(st, u)
        used = s["used"] / n.GB
        q = u.get("quota_gb")
        inner = (f"<input type=hidden name=name_old value='{esc(u['name'])}'><input type=hidden name=used_orig value='{used:.3f}'><div class=row>"
                 f"<div><label>Name</label><input type=text name=name value='{esc(u['name'])}'></div>"
                 f"<div><label>Down Mbps</label><input type=number step=any name=down value='{esc(u['down_mbps'])}'></div>"
                 f"<div><label>Up Mbps</label><input type=number step=any name=up value='{esc(u['up_mbps'])}'></div>"
                 f"<div><label>Quota GB (blank = none)</label><input type=number step=any name=quota value='{esc(q if q else '')}'></div>"
                 f"<div><label>Used GB (edit to correct)</label><input type=number step=any min=0 name=used value='{used:.2f}'></div>"
                 f"<div><label>Device MAC (change if the phone got a new private address)</label><input type=text name=mac value='{esc(u['mac'])}'></div>"
                 f"<div><label>Other MACs of this person (comma separated; remove to detach)</label><input type=text name=extra_macs value='{esc(', '.join(u.get('extra_macs') or []))}'></div>"
                 f"<div><label>Personal Wi-Fi password (optional; auto-attaches new MACs)</label><input type=text name=psk value='{esc(u.get('psk') or '')}' placeholder='uses main Wi-Fi password'></div></div>")
        pause_btn = (form(csrf, "resume", f"<input type=hidden name=name value='{esc(u['name'])}'>", "Resume internet")
                     if u["paused"] else
                     form(csrf, "pause", f"<input type=hidden name=name value='{esc(u['name'])}'>", "Pause internet", "g",
                          f"Cut off {u['name']}'s internet?"))
        out.append(f"<div style='border-top:1px solid var(--line);padding:10px 0'><div id='u_{u['key']}'>{user_stats(u, st, leases, stations)}</div>"
                   "<details><summary>Edit settings &amp; actions</summary><div style='margin-top:8px'>"
                   + form(csrf, "set", inner, "Save changes")
                   + "<div class=row style='margin-top:6px'>" + pause_btn
                   + form(csrf, "reset", f"<input type=hidden name=name value='{esc(u['name'])}'>", "Reset usage", "g")
                   + form(csrf, "remove", f"<input type=hidden name=name value='{esc(u['name'])}'>", "Remove", "d",
                          f"Remove {u['name']}? Their usage is remembered if you add the same name again.")
                   + "</div></div></details></div>")
    if cfg["users"]:
        out.append(form(csrf, "reset", "<input type=hidden name=name value='*'>", "Reset everyone's usage", "g",
                        "Reset usage for all users?"))
    out.append("</div>")

    # connected devices + add
    free = sorted([d for d in leases if d["mac"] not in reg], key=lambda d: d["mac"] not in stations)
    out.append(unknown_card(csrf, cfg, leases, stations))
    out.append("<div class=card><h2>Devices</h2><div id=devices>" + devices_inner(cfg, leases, stations) + "</div>"
               "<p class=mut>\"online\" = connected to the Wi-Fi right now. Offline devices still hold a DHCP lease for up to 24 hours.</p></div>")
    opts = "".join(f"<option value='{esc(d['mac'])}'>{'online' if d['mac'] in stations else 'offline'}: {esc(d['host'] or 'no name')} - {esc(d['mac'])} ({esc(d['ip'])})</option>"
                   for d in free)
    inner = ("<div class=row><div><label>Connected device</label><select name=mac_pick><option value=''>- pick -</option>"
             f"{opts}</select></div><div><label>or MAC address</label><input type=text name=mac_manual placeholder='aa:bb:cc:dd:ee:ff'></div>"
             "<div><label>Name</label><input type=text name=name></div>"
             "<div><label>Down Mbps</label><input type=number step=any name=down value=10></div>"
             "<div><label>Up Mbps</label><input type=number step=any name=up value=5></div>"
             "<div><label>Quota GB</label><input type=number step=any name=quota placeholder='none'></div>"
             "<div><label>Personal Wi-Fi password (optional)</label><input type=text name=psk placeholder='8+ chars'></div></div>")
    out.append("<div class=card><h2>Add a user</h2>" + form(csrf, "add", inner, "Add user")
               + "<p class=mut>Phones: use the Wi-Fi MAC shown for this network, or give them a personal Wi-Fi password so rotated MACs attach automatically.</p></div>")

    # speed settings
    inner = (f"<div class=row><div><label>Total Mbps (Wi-Fi side)</label><input type=number step=any name=total value='{esc(cfg['total_mbps'])}'></div>"
             f"<div><label>Guest pool Mbps (shared)</label><input type=number step=any name=guest value='{esc(cfg['guest_mbps'])}'></div>"
             f"<div><label>Per-guest Mbps (each unknown device)</label><input type=number step=any name=guest_per value='{esc(cfg.get('guest_per_mbps', 5))}'></div>"
             f"<div><label>Over-quota speed (kbps)</label><input type=number name=throttle value='{esc(cfg['throttle_kbps'])}'></div></div>")
    out.append("<div class=card><h2>Speed settings</h2>" + form(csrf, "limits", inner, "Apply")
               + "<p class=mut>Guests are every device that is not a user; they share one pool and each guest is capped at Per-guest Mbps. Limits are caps, not guarantees: "
                 "the hostel link is the real ceiling.</p></div>")

    # wifi
    try:
        lines = conf_read()
    except OSError:
        lines = []
    ch = conf_get(lines, "channel", "149")
    wide = "HT40" in conf_get(lines, "ht_capab")
    sae = "SAE" in conf_get(lines, "wpa_key_mgmt")
    chopts = "".join(f"<option value={c}{' selected' if str(c) == ch else ''}>{c}</option>" for c in CHANNELS)
    inner = (f"<div class=row><div><label>Wi-Fi name (SSID)</label><input type=text name=ssid value='{esc(conf_get(lines, 'ssid'))}'></div>"
             "<div><label>New password (blank = keep)</label><input type=password name=password autocomplete=new-password></div>"
             "<div><label><input type=checkbox name=randpw> make a random one</label></div></div>"
             f"<div class=row><div><label>Channel (5 GHz)</label><select name=channel>{chopts}</select></div>"
             f"<div><label>Width</label><select name=width><option value=40{' selected' if wide else ''}>40 MHz (faster)</option>"
             f"<option value=20{'' if wide else ' selected'}>20 MHz</option></select></div>"
             f"<div><label>Security</label><select name=security><option value=wpa2{'' if sae else ' selected'}>WPA2 (recommended)</option>"
             f"<option value=wpa3t{' selected' if sae else ''}>WPA2 + WPA3 (experimental)</option></select></div>"
             f"<div><label><input type=checkbox name=hidden{' checked' if conf_get(lines, 'ignore_broadcast_ssid') == '1' else ''}> hide network name</label></div></div>")
    out.append("<div class=card><h2>Wi-Fi settings</h2>" + form(csrf, "wifi", inner, "Save and restart Wi-Fi", "", "The Wi-Fi restarts and everyone is disconnected. Continue?")
               + "<p class=mut>After saving, reconnect, open this page and press Keep. Without that, the old settings come back after "
                 f"{CONFIRM_SECONDS} s, so a mistake cannot lock you out. Channel 165 is 20 MHz only.</p></div>")

    # account
    inner = ("<div class=row><div><label>Current</label><input type=password name=current></div>"
             "<div><label>New</label><input type=password name=new></div><div><label>Repeat</label><input type=password name=new2></div></div>")
    out.append("<div class=card><h2>Admin password</h2>" + form(csrf, "password", inner, "Change")
               + "</div><div class=row>" + form(csrf, "logout", "", "Log out", "g")
               + "<a class=mut href='https://10.42.0.1/admin/'>Pi-hole</a> "
               "<a class=mut href='/backup'>Download backup</a></div>"
               f"<p class=mut>nek0 router {__version__}</p>")
    return page("nek0 router", "".join(out), nonce)


# ---------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    server_version = "nek0web"
    timeout = 20

    def setup(self):
        # TLS handshake happens HERE, in this connection's own thread, with a timeout.
        # (Doing it while accepting lets one idle browser connection freeze the whole server.)
        self.request.settimeout(15)
        self.request = CTX.wrap_socket(self.request, server_side=True)
        super().setup()

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, headers=(), ctype="text/html; charset=utf-8", nonce=None):
        data = body.encode()
        csp = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
        if nonce:
            csp += f"; script-src 'nonce-{nonce}'; connect-src 'self'"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", csp)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, where, headers=()):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        sid = c["sid"].value if "sid" in c else None
        key = sid_key(sid) if sid else None
        s = SESSIONS.get(key)
        if s and s["exp"] > time.time():
            return key, s
        return None, None

    def form_data(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size > 20000:
            raise ValueError("request too large")
        raw = self.rfile.read(size).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def do_GET(self):
        sid, s = self.session()
        path = self.path.split("?")[0]
        if path == "/login" or not s:
            return self.send(200, login_page()) if path == "/login" else self.redirect("/login")
        if path == "/stats":
            try:
                return self.send(200, json.dumps(fragments()), ctype="application/json")
            except Exception:
                return self.send(500, "{}", ctype="application/json")
        if path == "/backup":
            with open(n.CONF) as f:
                users_yaml = f.read()
            data = json.dumps({"version": __version__, "users_yaml": users_yaml, "state": n.load_state()}, indent=1)
            return self.send(200, data, [("Content-Disposition", "attachment; filename=nek0-backup.json")],
                             "application/json")
        if path != "/":
            return self.send(404, page("Not found", "<p>Not found</p>"))
        try:
            nonce = secrets.token_urlsafe(12)
            self.send(200, dashboard(s, nonce), nonce=nonce)
        except Exception as e:
            self.send(500, page("Error", f"<div class='msg err'>Could not build the page: {esc(e)}</div>"))

    def do_POST(self):
        try:
            f = self.form_data()
        except ValueError:
            return self.send(400, page("Bad request", "<p>Bad request</p>"))
        if self.path == "/login":
            ip = self.client_address[0]
            fails = FAILS.get(ip, [0, 0])
            if fails[1] > time.time():
                return self.send(429, login_page("Too many attempts. Wait a few minutes."))
            if check_pw(f.get("password", "")):
                FAILS.pop(ip, None)
                sid = secrets.token_urlsafe(32)
                SESSIONS[sid_key(sid)] = {"csrf": secrets.token_urlsafe(16), "exp": time.time() + SESSION_SECONDS, "flash": []}
                sessions_save()
                return self.redirect("/", [("Set-Cookie", f"sid={sid}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_SECONDS}")])
            time.sleep(1)
            fails[0] += 1
            FAILS[ip] = [0, time.time() + 600] if fails[0] >= 5 else fails
            return self.send(200, login_page("Wrong password."))
        sid, s = self.session()
        if not s:
            return self.redirect("/login")
        if self.path != "/act" or not hmac.compare_digest(f.get("csrf", ""), s["csrf"]):
            return self.send(403, page("Forbidden", "<p>Session expired. Reload the page.</p>"))
        act = f.get("act", "")
        if act == "logout":
            SESSIONS.pop(sid, None)
            sessions_save()
            return self.redirect("/login", [("Set-Cookie", "sid=; Max-Age=0; Path=/")])
        kind, text = perform(act, f)
        s.setdefault("flash", []).append((kind, text))
        self.redirect("/")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], OSError):  # failed handshakes, resets, idle timeouts: normal noise
            return
        super().handle_error(request, client_address)


def make_server():
    global CTX
    CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    CTX.load_cert_chain(CERT, KEY)
    return Server((BIND, PORT), Handler)


def guard_firewall():
    """Make the dashboard unreachable from the hostel side (eth0), whatever it is bound to."""
    rules = ("table ip nek0web\ndelete table ip nek0web\ntable ip nek0web {\n  chain input {\n"
             "    type filter hook input priority 0; policy accept;\n"
             f"    iifname != {{ \"wlan0\", \"lo\" }} tcp dport {PORT} drop\n  }}\n}}\n")
    n.run("nft -f -", stdin=rules)


def serve():
    global n
    sys.path.insert(0, HERE)
    import nek0ctl
    n = nek0ctl
    if not os.path.exists(AUTH):
        sys.exit("no admin password yet: run  sudo python3 nek0web.py setup")
    guard_firewall()
    threading.Thread(target=watcher, daemon=True).start()
    sessions_load()
    srv = make_server()
    print(f"nek0web listening on https://{BIND}:{PORT}", flush=True)
    srv.serve_forever()


def setup():
    import subprocess
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    if not os.path.exists("/opt/nek0/nek0ctl.py"):
        sys.exit("install nek0ctl first:  sudo python3 nek0ctl.py install")
    dst = "/opt/nek0/nek0web.py"
    if os.path.abspath(__file__) != dst:
        shutil.copy(os.path.abspath(__file__), dst)
    os.chmod(dst, 0o755)
    write_auth(ask_new_password())
    if not os.path.exists(CERT):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", KEY, "-out", CERT,
                        "-days", "3650", "-subj", "/CN=nek0.lan",
                        "-addext", "subjectAltName=IP:10.42.0.1,DNS:nek0.lan,DNS:router.lan,DNS:nek0.local"],
                       check=True, capture_output=True)
        os.chmod(KEY, 0o600)
    with open("/etc/systemd/system/nek0web.service", "w") as f:
        f.write(UNIT)
    subprocess.run("systemctl daemon-reload && systemctl enable nek0web.service && systemctl restart nek0web.service",
                   shell=True, check=True)
    print("done. Open https://nek0.lan:8443 or https://10.42.0.1:8443 from a device on the F-06 Wi-Fi (accept the certificate warning).")


def update():
    import subprocess
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    if not os.path.exists(AUTH):
        sys.exit("not set up yet: run  sudo python3 nek0web.py setup")
    dst = "/opt/nek0/nek0web.py"
    if os.path.abspath(__file__) != dst:
        shutil.copy(os.path.abspath(__file__), dst)
    os.chmod(dst, 0o755)
    with open("/etc/systemd/system/nek0web.service", "w") as f:
        f.write(UNIT)
    subprocess.run("systemctl daemon-reload && systemctl restart nek0web.service", shell=True, check=True)
    print(f"updated to nek0web {__version__}. Your password, certificate, users and usage are unchanged.")


def restore_wifi():
    global n
    sys.path.insert(0, HERE)
    import nek0ctl
    n = nek0ctl
    if not os.path.exists(GOOD):
        sys.exit("no saved Wi-Fi settings to restore")
    rollback("restored from the command line")


if __name__ == "__main__":
    cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
    if cmd == "serve":
        serve()
    elif cmd == "setup":
        setup()
    elif cmd == "update":
        update()
    elif cmd == "setpassword":
        write_auth(ask_new_password())
        print("admin password changed")
    elif cmd == "restore-wifi":
        restore_wifi()
    else:
        sys.exit(__doc__)
