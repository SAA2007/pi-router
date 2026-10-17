import json
import os

import pytest
import yaml

import nek0ctl as n
import nek0web as w

GOOD_USERS = {
    "interface": "wlan0", "total_mbps": 90, "guest_mbps": 30, "throttle_kbps": 256,
    "users": [
        {"name": "ali", "mac": "AA:BB:CC:00:11:22", "ip": "10.42.0.20", "down_mbps": 10, "up_mbps": 5, "quota_gb": 20},
        {"name": "sara-phone", "mac": "aa:bb:cc:00:11:33", "ip": "10.42.0.21", "down_mbps": 5, "up_mbps": 2},
    ],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(n, "BASE", str(tmp_path))
    monkeypatch.setattr(n, "CONF", str(tmp_path / "users.yaml"))
    monkeypatch.setattr(n, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(n, "LEASES", str(tmp_path / "leases"))
    monkeypatch.setattr(n, "PSK_FILE", str(tmp_path / "hostapd.psk"))
    cmds = []
    monkeypatch.setattr(n, "run", lambda c, check=True, stdin=None: (cmds.append(c), (0, ""))[1])
    w.n = n
    monkeypatch.setattr(w, "HOSTAPD", str(tmp_path / "hostapd.conf"))
    monkeypatch.setattr(w, "GOOD", str(tmp_path / "good.conf"))
    monkeypatch.setattr(w, "PENDING", str(tmp_path / "pending.json"))
    monkeypatch.setattr(w, "AUTH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(w, "apply_wifi_async", lambda: None)
    return tmp_path, cmds


def write_cfg(path, data=None):
    with open(path / "users.yaml", "w") as f:
        yaml.safe_dump(data or GOOD_USERS, f, sort_keys=False)


def clone(data):
    return json.loads(json.dumps(data))


def test_load_ok_and_normalises(env):
    tmp, _ = env
    write_cfg(tmp)
    cfg = n.load()
    assert cfg["users"][0]["mac"] == "aa:bb:cc:00:11:22"
    assert cfg["users"][1]["key"] == "sara_phone"
    assert cfg["users"][0]["paused"] is False


@pytest.mark.parametrize("change", [
    {"ip": "10.42.0.150"}, {"ip": "10.42.0.1"}, {"ip": "192.168.1.5"},
    {"mac": "nonsense"}, {"name": "bad name;rm"},
])
def test_load_rejects_bad_users(env, change):
    tmp, _ = env
    data = clone(GOOD_USERS)
    data["users"][0].update(change)
    write_cfg(tmp, data)
    with pytest.raises(SystemExit):
        n.load()


def test_load_rejects_missing_field_and_duplicates(env):
    tmp, _ = env
    data = clone(GOOD_USERS)
    del data["users"][0]["down_mbps"]
    write_cfg(tmp, data)
    with pytest.raises(SystemExit):
        n.load()
    data = clone(GOOD_USERS)
    data["users"][1]["ip"] = "10.42.0.20"
    write_cfg(tmp, data)
    with pytest.raises(SystemExit):
        n.load()


def test_usage_guests_and_counter_restart(env, monkeypatch):
    tmp, _ = env
    write_cfg(tmp)
    cfg, st = n.load(), {"month": n.month_now(), "users": {}}
    seq = [
        {"c_ali": 10 * n.GB, "c_sara_phone": 1 * n.GB, "c_total": 15 * n.GB},
        {"c_ali": 12 * n.GB, "c_sara_phone": 1 * n.GB, "c_total": 18 * n.GB},
        {"c_ali": 1 * n.GB, "c_sara_phone": 0, "c_total": 2 * n.GB},  # table rebuilt: counters restart
    ]
    for counters in seq:
        monkeypatch.setattr(n, "read_counters", lambda c=counters: c)
        n.collect(cfg, st)
    assert st["users"]["ali"]["used"] == 13 * n.GB          # 10 + 2 + (restart -> 1)
    assert st["users"]["sara_phone"]["used"] == 1 * n.GB
    assert st["total"]["used"] == 20 * n.GB                 # 15 + 3 + 2
    assert st["guest"]["used"] == 6 * n.GB                  # 4 + 1 + 1
    assert sum(st["days"][n.today()].values()) == 20 * n.GB


def test_quota_throttles_then_monthly_reset_restores(env, monkeypatch):
    tmp, cmds = env
    write_cfg(tmp)
    cfg, st = n.load(), {"month": n.month_now(), "users": {}}
    monkeypatch.setattr(n, "read_counters", lambda: {"c_ali": 21 * n.GB})
    n.collect(cfg, st)
    n.enforce(cfg, st)
    assert st["users"]["ali"]["throttled"] is True
    assert any("ceil 256kbit" in c for c in cmds)
    st["month"] = "1999-01"
    n.monthly_reset(st)
    n.enforce(cfg, st)
    assert st["users"]["ali"]["used"] == 0 and st["users"]["ali"]["throttled"] is False


def test_rename_keeps_usage_and_history():
    st = {"users": {"ali": {"used": 5, "last": 0, "throttled": False}},
          "days": {"2026-10-01": {"ali": 3}, "2026-10-02": {"ali": 2, "_guest": 1}}}
    n.rename_state(st, "ali", "ali_new")
    assert st["users"]["ali_new"]["used"] == 5 and "ali" not in st["users"]
    assert st["days"]["2026-10-01"] == {"ali_new": 3}


def test_firewall_text(env):
    tmp, _ = env
    data = clone(GOOD_USERS)
    data["users"][0]["paused"] = True
    write_cfg(tmp, data)
    text = n.accounting_text(n.load())
    assert "ip saddr 10.42.0.20 drop" in text and "ip daddr 10.42.0.20 drop" in text
    assert "ip saddr 10.42.0.21 drop" not in text
    assert "counter c_total" in text and "chain acct_fwd" in text and "chain fwd " not in text


def test_save_cfg_round_trip(env):
    tmp, _ = env
    write_cfg(tmp)
    cfg = n.load()
    cfg["users"][1]["paused"] = True
    n.save_cfg(cfg)
    again = n.load()
    assert again["users"][1]["paused"] is True and again["users"][0]["quota_gb"] == 20
    assert "quota_gb" not in yaml.safe_load(open(tmp / "users.yaml"))["users"][1]


def test_backup_and_restore(env):
    tmp, _ = env
    write_cfg(tmp)
    n.save_state({"month": "2026-10", "users": {"ali": {"used": 42}}})
    path = n.cmd_backup()
    os.remove(tmp / "users.yaml")
    os.remove(tmp / "state.json")
    n.cmd_restore(path)
    assert n.load_state()["users"]["ali"]["used"] == 42 and (tmp / "users.yaml").exists()


def test_web_hostapd_helpers():
    lines = ["ssid=F-06", "channel=149", "wpa_key_mgmt=WPA-PSK"]
    lines = w.conf_set(lines, "channel", "157")
    lines = w.conf_set(lines, "ieee80211w", "1")
    lines = w.conf_set(lines, "ssid", None)
    assert w.conf_get(lines, "channel") == "157" and w.conf_get(lines, "ieee80211w") == "1"
    assert w.conf_get(lines, "ssid") == ""


def test_web_wifi_validation_and_pending(env):
    tmp, _ = env
    (tmp / "hostapd.conf").write_text("ssid=F-06\nchannel=149\nht_capab=[HT40+]\nwpa_key_mgmt=WPA-PSK\nwpa_passphrase=oldoldoldold1\n")
    base = {"ssid": "F-06", "channel": "149", "width": "40", "security": "wpa2"}
    for bad in ({"password": "short"}, {"channel": "36"}, {"ssid": ""}, {"password": "has\nnewline-12345"}):
        with pytest.raises(ValueError):
            w.a_wifi({**base, **bad})
    w.a_wifi({**base, "password": "a-brand-new-pass-1", "channel": "165", "hidden": "on"})
    conf = (tmp / "hostapd.conf").read_text()
    assert "channel=165" in conf and "ht_capab=[SHORT-GI-20]" in conf and "ignore_broadcast_ssid=1" in conf
    assert "oldoldoldold1" in (tmp / "good.conf").read_text()   # fallback is the last confirmed config
    assert w.pending() is not None
    w.a_wifi_keep({})
    assert w.pending() is None and "brand-new" in (tmp / "good.conf").read_text()


def test_web_admin_password(env):
    w.write_auth("correct horse")
    assert w.check_pw("correct horse") and not w.check_pw("wrong")


def test_web_edit_user_keeps_usage_and_renames(env):
    tmp, _ = env
    write_cfg(tmp)
    n.save_state({"month": n.month_now(), "users": {"ali": {"used": 7 * n.GB, "last": 0, "throttled": False}}})
    kind, _msg = w.perform("set", {"name_old": "ali", "name": "ali2", "down": "12", "up": "6", "quota": "30",
                                   "used": "7.00", "used_orig": "7.000"})
    assert kind == "ok"
    st = n.load_state()
    assert st["users"]["ali2"]["used"] == 7 * n.GB and "ali" not in st["users"]
    assert n.load()["users"][0]["name"] == "ali2"
    w.perform("set", {"name_old": "ali2", "name": "ali2", "down": "12", "up": "6", "quota": "30",
                      "used": "1.50", "used_orig": "7.000"})
    assert n.load_state()["users"]["ali2"]["used"] == 1.5 * n.GB    # explicit correction
    kind, _ = w.perform("set", {"name_old": "ali2", "name": "sara-phone", "down": "1", "up": "1"})
    assert kind == "err"                                            # name clash refused, nothing lost
    w.perform("pause", {"name": "ali2"})
    assert n.load()["users"][0]["paused"] is True
    w.perform("resume", {"name": "ali2"})
    assert n.load()["users"][0]["paused"] is False


def test_web_rejects_hostile_input(env):
    tmp, _ = env
    write_cfg(tmp)
    for form in ({"mac_manual": "aa:bb:cc:dd:ee:ff", "name": "x; rm -rf /", "down": "1", "up": "1"},
                 {"mac_manual": "zz", "name": "ok", "down": "1", "up": "1"},
                 {"mac_manual": "aa:bb:cc:dd:ee:ff", "name": "ok", "down": "nan", "up": "1"},
                 {"mac_manual": "aa:bb:cc:dd:ee:ff", "name": "ok", "down": "-5", "up": "1"}):
        assert w.perform("add", form)[0] == "err"
    assert len(n.load()["users"]) == 2


def test_watchdog_restarts_a_dead_link_but_respects_a_stopped_ap(env, monkeypatch):
    tmp, cmds = env
    write_cfg(tmp)
    cfg, st = n.load(), {"month": n.month_now(), "users": {}}
    monkeypatch.setattr(n.time, "sleep", lambda s: None)
    monkeypatch.setattr(n, "iface_state", lambda dev: "down")
    n.ap_watchdog(cfg, st)
    assert not any("restart hostapd" in c for c in cmds)       # one poll is not enough
    n.ap_watchdog(cfg, st)
    assert any("restart hostapd" in c for c in cmds)           # two in a row: restart
    cmds.clear()
    n.ap_watchdog(cfg, st)
    n.ap_watchdog(cfg, st)
    assert not any("restart hostapd" in c for c in cmds)       # not more than once per 5 minutes
    monkeypatch.setattr(n, "iface_state", lambda dev: "up")
    n.ap_watchdog(cfg, st)
    assert st["wd_down"] == 0


def test_watchdog_can_be_switched_off(env, monkeypatch):
    tmp, cmds = env
    data = clone(GOOD_USERS)
    data["watchdog"] = False
    write_cfg(tmp, data)
    cfg, st = n.load(), {"month": n.month_now(), "users": {}}
    monkeypatch.setattr(n, "iface_state", lambda dev: "down")
    for _ in range(3):
        n.ap_watchdog(cfg, st)
    assert cmds == []


def test_dhcp_reservations_have_no_names_and_rename_does_not_restart_pihole(env):
    tmp, cmds = env
    write_cfg(tmp)
    st = {"month": n.month_now(), "users": {}}
    n.cmd_apply(n.load(), st)
    assert any("dhcp.hosts" in c and "ali" not in c and "aa:bb:cc:00:11:22,10.42.0.20" in c for c in cmds)
    assert sum("restart pihole-FTL" in c for c in cmds) == 1
    cmds.clear()
    cfg = n.load()
    n.cmd_set(cfg, st, ["ali", "name", "ali-renamed"])
    assert not any("restart pihole-FTL" in c or "dhcp.hosts" in c for c in cmds)


def test_web_edit_does_not_block_on_pihole_restart(env):
    tmp, cmds = env
    write_cfg(tmp)
    kind, _ = w.perform("add", {"mac_manual": "aa:bb:cc:dd:ee:77", "name": "newuser", "down": "5", "up": "2"})
    assert kind == "ok"
    import time as _t
    for _ in range(50):          # the restart runs in a background thread
        if any("restart pihole-FTL" in c for c in cmds):
            break
        _t.sleep(0.02)
    assert any("restart pihole-FTL" in c for c in cmds)


def test_everyone_is_users_plus_guests(env):
    tmp, _ = env
    write_cfg(tmp)
    w.n = n
    cfg = n.load()
    st = {"month": n.month_now(), "users": {"ali": {"used": 2 * n.GB, "last": 0, "throttled": False}},
          "guest": {"used": 1 * n.GB, "rate": 0}, "total": {"used": 1000, "last": 0, "rate": 0}}
    html = w.usage_inner(cfg, st)
    assert "3.00 GB" in html and "everyone" in html


def test_sessions_survive_a_restart(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setattr(w, "SESS_FILE", str(tmp / "sessions.json"))
    w.SESSIONS.clear()
    key = w.sid_key("token-abc")
    w.SESSIONS[key] = {"csrf": "c", "exp": __import__("time").time() + 100, "flash": []}
    w.SESSIONS["expired"] = {"csrf": "x", "exp": 1, "flash": []}
    w.sessions_save()
    assert "token-abc" not in (tmp / "sessions.json").read_text()   # only hashes are stored
    w.SESSIONS.clear()
    w.sessions_load()
    assert key in w.SESSIONS and "expired" not in w.SESSIONS


def test_stalled_connection_does_not_freeze_the_server(env, monkeypatch):
    import http.client
    import shutil
    import socket
    import ssl
    import subprocess
    import threading
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    tmp, _ = env
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp / "k.pem"),
                    "-out", str(tmp / "c.pem"), "-days", "2", "-subj", "/CN=test"], check=True, capture_output=True)
    monkeypatch.setattr(w, "CERT", str(tmp / "c.pem"))
    monkeypatch.setattr(w, "KEY", str(tmp / "k.pem"))
    monkeypatch.setattr(w, "BIND", "127.0.0.1")
    monkeypatch.setattr(w, "PORT", 0)
    srv = w.make_server()
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stalled = socket.create_connection(("127.0.0.1", port))        # connects, never starts TLS
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=ctx)
        conn.request("GET", "/login")
        assert conn.getresponse().status == 200
    finally:
        stalled.close()
        srv.shutdown()


IW_SAMPLE = """Station 76:51:82:53:d4:3d (on wlan0)
\tinactive time:\t2540 ms
\tsignal:  \t-52 [-52] dBm
\ttx bitrate:\t433.3 MBit/s VHT-MCS 9 40MHz short GI VHT-NSS 1
Station aa:3a:e2:7f:0c:5a (on wlan0)
\tinactive time:\t100 ms
"""


def test_stations_parse_and_wrong_ip_warning(env, monkeypatch):
    tmp, _ = env
    write_cfg(tmp)
    monkeypatch.setattr(n, "run", lambda c, check=True, stdin=None: (0, IW_SAMPLE))
    st_ = w.read_stations("wlan0")
    assert st_["76:51:82:53:d4:3d"] == {"signal": -52, "rate": 433.3}
    assert st_["aa:3a:e2:7f:0c:5a"] == {"signal": None, "rate": None}
    cfg = n.load()
    leases = [{"mac": "aa:bb:cc:00:11:22", "ip": "10.42.0.109", "host": "phone"}]   # user "ali" reserved .20
    assert n.user_ips(cfg, cfg["users"][0], leases) == ["10.42.0.20", "10.42.0.109"]
    html = w.user_stats(cfg["users"][0], {"month": "x", "users": {}}, leases, {})
    assert "on 10.42.0.109 (limits active)" in html and "offline" in html


UPLINK = {"backup_iface": "wlan1", "min_mbps": 3, "backup_cap_mbps": 10}


def uplink_env(env, monkeypatch, eth_ok=True, backup_ok=True, mbps=None):
    tmp, cmds = env
    data = clone(GOOD_USERS)
    data["uplink"] = dict(UPLINK)
    write_cfg(tmp, data)
    state = {"eth_ok": eth_ok, "backup_ok": backup_ok, "mbps": mbps}
    monkeypatch.setattr(n, "probe_ping", lambda iface: state["eth_ok"] if iface == "eth0" else state["backup_ok"])
    monkeypatch.setattr(n, "probe_speed", lambda iface, url: state["mbps"])
    monkeypatch.setattr(n, "backup_gw", lambda dev: "192.168.43.1")
    return n.load(), {"month": n.month_now(), "users": {}}, cmds, state


def test_uplink_config_validation_and_round_trip(env):
    tmp, _ = env
    data = clone(GOOD_USERS)
    data["uplink"] = {"backup_iface": "wlan1", "probe_url": "https://x.example/a b'; rm -rf /"}
    write_cfg(tmp, data)
    with pytest.raises(SystemExit):
        n.load()
    data["uplink"] = dict(UPLINK)
    write_cfg(tmp, data)
    cfg = n.load()
    n.save_cfg(cfg)
    assert n.load()["uplink"]["backup_cap_mbps"] == 10


def test_uplink_switches_after_two_failed_checks_and_caps_speed(env, monkeypatch):
    cfg, st, cmds, s = uplink_env(env, monkeypatch, eth_ok=False)
    n.uplink_watchdog(cfg, st)
    assert st["uplink"]["mode"] == "primary"                       # one failure is not enough
    n.uplink_watchdog(cfg, st)
    assert st["uplink"]["mode"] == "backup"
    assert any("route replace default via 192.168.43.1 dev wlan1 metric 50" in c for c in cmds)
    assert any("classid 1:1 htb rate 10000kbit" in c for c in cmds)  # mobile-data protection


def test_uplink_does_not_switch_to_a_dead_hotspot(env, monkeypatch):
    cfg, st, cmds, s = uplink_env(env, monkeypatch, eth_ok=False, backup_ok=False)
    for _ in range(4):
        n.uplink_watchdog(cfg, st)
    assert st["uplink"]["mode"] == "primary" and not any("route replace" in c for c in cmds)


def test_uplink_switches_when_ethernet_is_slow_twice(env, monkeypatch):
    cfg, st, cmds, s = uplink_env(env, monkeypatch, mbps=1.0)
    n.uplink_watchdog(cfg, st)                                      # first speed probe: slow (1/2)
    assert st["uplink"]["mode"] == "primary"
    st["uplink"]["n"] = 10                                          # next probe is due
    n.uplink_watchdog(cfg, st)
    assert st["uplink"]["mode"] == "backup"


def test_uplink_returns_after_recovery_and_dwell(env, monkeypatch):
    cfg, st, cmds, s = uplink_env(env, monkeypatch, eth_ok=False)
    n.uplink_watchdog(cfg, st)
    n.uplink_watchdog(cfg, st)
    assert st["uplink"]["mode"] == "backup"
    s["eth_ok"] = True
    for _ in range(4):
        n.uplink_watchdog(cfg, st)
    assert st["uplink"]["mode"] == "backup"                         # recovered, but minimum stay not over
    st["uplink"]["since"] -= 400
    n.uplink_watchdog(cfg, st)
    assert st["uplink"]["mode"] == "primary"
    assert any("route del default dev wlan1 metric 50" in c for c in cmds)
    assert any("classid 1:1 htb rate 90000kbit" in c for c in cmds)


def test_uplink_force_and_lost_hotspot(env, monkeypatch):
    cfg, st, cmds, s = uplink_env(env, monkeypatch)
    n.cmd_uplink(cfg, st, "hotspot")
    assert st["uplink"]["mode"] == "backup" and st["uplink"]["force"] == "backup"
    n.cmd_uplink(cfg, st, "auto")
    assert "force" not in st["uplink"]
    monkeypatch.setattr(n, "backup_gw", lambda dev: None)           # phone hotspot switched off
    n.uplink_watchdog(cfg, st)
    assert any("nmcli -w 10 device connect wlan1" in c for c in cmds)
    assert st["uplink"]["mode"] == "primary"


def test_uplink_line_in_dashboard(env):
    tmp, _ = env
    data = clone(GOOD_USERS)
    data["uplink"] = dict(UPLINK)
    write_cfg(tmp, data)
    cfg = n.load()
    assert "hostel Ethernet" in w.uplink_inner(cfg, {})
    assert "phone hotspot" in w.uplink_inner(cfg, {"uplink": {"mode": "backup", "mbps": 1.5}})


# ---------------------------------------------------------------- extra MACs (private addresses)
def test_extra_macs_load_and_helpers(env):
    tmp, _ = env
    data = clone(GOOD_USERS)
    data["users"][0]["extra_macs"] = ["DE:AD:BE:EF:00:01", "aa:bb:cc:00:11:22"]  # 2nd equals primary: dropped
    write_cfg(tmp, data)
    cfg = n.load()
    u = cfg["users"][0]
    assert n.user_macs(u) == ["aa:bb:cc:00:11:22", "de:ad:be:ef:00:01"]
    assert n.mac_owner_map(cfg)["de:ad:be:ef:00:01"]["name"] == "ali"
    assert n.is_private_mac("de:ad:be:ef:00:01") and not n.is_private_mac("a4:bb:cc:00:11:22")
    assert cfg["users"][1]["extra_macs"] == []


def test_extra_mac_duplicate_and_bad_rejected(env):
    tmp, _ = env
    data = clone(GOOD_USERS)
    data["users"][0]["extra_macs"] = ["aa:bb:cc:00:11:33"]  # belongs to sara
    write_cfg(tmp, data)
    with pytest.raises(SystemExit):
        n.load()
    data = clone(GOOD_USERS)
    data["users"][0]["extra_macs"] = ["nonsense"]
    write_cfg(tmp, data)
    with pytest.raises(SystemExit):
        n.load()


def test_set_addmac_delmac_and_reservation(env):
    tmp, cmds = env
    write_cfg(tmp)
    cfg, st = n.load(), n.load_state()
    n.cmd_set(cfg, st, ["ali", "addmac", "DE:AD:BE:EF:00:01"])
    cfg = n.load()
    assert cfg["users"][0]["extra_macs"] == ["de:ad:be:ef:00:01"]
    with pytest.raises(SystemExit):  # sara's MAC cannot be taken
        n.cmd_set(cfg, st, ["ali", "addmac", "aa:bb:cc:00:11:33"])
    with pytest.raises(SystemExit):  # primary cannot be deleted
        n.cmd_set(cfg, st, ["ali", "delmac", "aa:bb:cc:00:11:22"])
    n.cmd_set(cfg, st, ["ali", "delmac", "de:ad:be:ef:00:01"])
    assert n.load()["users"][0]["extra_macs"] == []
    with open(tmp / "users.yaml") as f:
        assert "extra_macs" not in f.read()


def test_web_adopt_and_extra_macs_field(env):
    tmp, _ = env
    write_cfg(tmp)
    kind, _msg = w.a_adopt({"mac": "de:ad:be:ef:00:01", "owner": "ali"})
    assert kind == "ok"
    assert n.load()["users"][0]["extra_macs"] == ["de:ad:be:ef:00:01"]
    for bad in ({"mac": "de:ad:be:ef:00:01", "owner": "sara-phone"},   # already owned
                {"mac": "zz", "owner": "ali"}, {"mac": "de:ad:be:ef:00:02", "owner": "nobody"}):
        with pytest.raises(Exception):
            w.a_adopt(bad)
    # edit form: replace the extra MAC list (and detach with a blank field)
    base = {"name_old": "ali", "name": "ali", "down": "10", "up": "5", "quota": "20", "used": "", "used_orig": "0"}
    w.a_set({**base, "extra_macs": "de:ad:be:ef:00:03, DE-AD-BE-EF-00-04"})
    assert n.load()["users"][0]["extra_macs"] == ["de:ad:be:ef:00:03", "de:ad:be:ef:00:04"]
    with pytest.raises(ValueError):
        w.a_set({**base, "extra_macs": "aa:bb:cc:00:11:33"})
    w.a_set({**base, "extra_macs": ""})
    assert n.load()["users"][0]["extra_macs"] == []


def test_web_unknown_card_and_suggestion(env):
    tmp, _ = env
    write_cfg(tmp)
    cfg = n.load()
    leases = [{"mac": "aa:bb:cc:00:11:22", "ip": "10.42.0.20", "host": "Redmi-Note"},
              {"mac": "de:ad:be:ef:00:09", "ip": "10.42.0.150", "host": "redmi-note"},
              {"mac": "de:ad:be:ef:00:0a", "ip": "10.42.0.151", "host": ""}]
    assert w.suggest_owner(cfg, leases, leases[1]) == "ali"
    assert w.suggest_owner(cfg, leases, leases[2]) is None
    html = w.unknown_card("tok", cfg, leases, {"de:ad:be:ef:00:09": {"signal": -50, "rate": 100}})
    assert "de:ad:be:ef:00:09" in html and "private MAC" in html and "looks like ali" in html
    assert "aa:bb:cc:00:11:22" not in html
    assert w.devices_inner(cfg, leases, {}).count("<b>ali</b>") == 1


def test_personal_psk_and_anti_sharing_adopt(env, monkeypatch):
    tmp, cmds = env
    write_cfg(tmp)
    cfg, st = n.load(), n.load_state()
    n.cmd_set(cfg, st, ["ali", "psk", "ali-secret-pass"])
    assert "keyid=ali 00:00:00:00:00:00 ali-secret-pass" in (tmp / "hostapd.psk").read_text()
    assert any("reload_wpa_psk" in c for c in cmds)
    with pytest.raises(SystemExit):  # duplicate psk rejected
        n.cmd_set(n.load(), st, ["sara-phone", "psk", "ali-secret-pass"])
    # 1) Ali's phone rotates MAC (old MAC offline, new MAC online with keyid=ali) -> auto-adopted
    sta_rotated = "de:ad:be:ef:11:22\nflags=[AUTH][ASSOC][AUTHORIZED]\nkeyid=ali\n"
    monkeypatch.setattr(n, "run", lambda c, check=True, stdin=None: (cmds.append(c), (0, sta_rotated if "all_sta" in c else ""))[1])
    n.psk_adopt(n.load(), st)
    assert "de:ad:be:ef:11:22" in n.load()["users"][0]["extra_macs"]
    # 2) Ali shares password while already online -> 2nd concurrent device is NOT auto-adopted
    sta_shared = (
        "de:ad:be:ef:11:22\nflags=[AUTH][ASSOC][AUTHORIZED]\nkeyid=ali\n"
        "de:ad:be:ef:99:99\nflags=[AUTH][ASSOC][AUTHORIZED]\nkeyid=ali\n"
    )
    monkeypatch.setattr(n, "run", lambda c, check=True, stdin=None: (cmds.append(c), (0, sta_shared if "all_sta" in c else ""))[1])
    n.psk_adopt(n.load(), st)
    assert "de:ad:be:ef:99:99" not in n.load()["users"][0]["extra_macs"]
    # clear psk
    n.cmd_set(n.load(), st, ["ali", "psk", "none"])
    assert "psk" not in n.load()["users"][0] and (tmp / "hostapd.psk").read_text() == ""


def test_per_guest_shaping(env):
    tmp, cmds = env
    data = clone(GOOD_USERS)
    data["guest_mbps"] = 15
    data["guest_per_mbps"] = 5
    write_cfg(tmp, data)
    n.setup_shaping(n.load())
    assert any("classid 1:999 htb rate 1000kbit ceil 15000kbit" in c for c in cmds)
    assert any("parent 1:999 classid 1:900 htb rate 500kbit ceil 5000kbit" in c for c in cmds)
    assert any("match u8 0 0x0f at 19 flowid 1:900" in c for c in cmds)


def test_cli_case_insensitive_and_web_uplink(env, monkeypatch):
    cfg, st, _cmds, _s = uplink_env(env, monkeypatch)
    n.cmd_set(cfg, st, ["SARA_PHONE", "down", "8"])
    assert n.load()["users"][1]["down_mbps"] == 8
    with pytest.raises(SystemExit):
        n.cmd_set(n.load(), st, ["ali", "down", "not-a-number"])
    assert w.perform("uplink", {"mode": "hotspot"})[0] == "ok"
    assert n.load_state()["uplink"]["force"] == "backup"
    assert w.perform("uplink", {"mode": "auto"})[0] == "ok"
    assert "force" not in n.load_state()["uplink"]
