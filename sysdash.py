#!/usr/bin/env python3
"""sysdash: a small, dependency-free system dashboard for Linux, served on http://localhost:8765.

    python3 sysdash.py             run the dashboard
    python3 sysdash.py report [H]  print a plain-text incident report for the last H hours (default 24)
    python3 sysdash.py open        open the dashboard window
    python3 sysdash.py --version   print the version
    python3 demo.py                run with made-up data on :8766 (for screenshots)

Settings (environment variables):
    SYSDASH_PORT   port to listen on (default 8765)
    SYSDASH_DATA   where history, network totals and Claude reports live (default ~/.local/state/sysdash)
    SYSDASH_MODEL  Claude model for scans and chat (default sonnet)
"""

__version__ = "1.0.0"

import collections
import datetime
import glob
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST, PORT = "127.0.0.1", int(os.environ.get("SYSDASH_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))  # code + static files
DATA = os.environ.get("SYSDASH_DATA") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "sysdash")
os.makedirs(DATA, exist_ok=True)
NET_FILE = os.path.join(DATA, "netusage.json")
DB_FILE = os.path.join(DATA, "history.db")
NOTES_FILE = os.path.join(DATA, "notes.md")  # optional: machine-specific hints for Claude (known noisy logs, quirks)
APP_URL = f"http://localhost:{PORT}"
APP_CLASS = "chrome-localhost__-Default"  # window class Chromium gives an --app=http://localhost window
HOME = os.path.realpath(os.path.expanduser("~"))
INTERVAL = 2.0
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1
REAL_FS = {"ext4", "ext3", "ext2", "btrfs", "xfs", "vfat", "exfat", "f2fs", "ntfs3", "ntfs", "zfs", "fuseblk"}
SKIP_IFACE = re.compile(r"^(lo|docker\d*|veth|br-|virbr)")
VIRTUAL_IFACE = re.compile(r"^(tailscale|wg|tun|tap)")
DISK_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|mmcblk\d+)$")


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def run(cmd, timeout=5):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


class Cached:
    """Run fn at most once per ttl seconds; fn runs in the caller's thread."""

    def __init__(self, fn, ttl):
        self.fn, self.ttl, self.t, self.v = fn, ttl, 0.0, None
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if time.time() - self.t >= self.ttl:
                try:
                    self.v = self.fn()
                except Exception as e:  # never let one probe break the page
                    self.v = {"error": str(e)}
                self.t = time.time()
            return self.v


# ---------------------------------------------------------------- probes

def cpu_times():
    out = {}
    for line in read("/proc/stat", "").splitlines():
        if line.startswith("cpu"):
            name, *vals = line.split()
            vals = list(map(int, vals[:8]))
            idle = vals[3] + vals[4]
            out[name] = (sum(vals), idle)
    return out


def meminfo():
    m = {}
    for line in read("/proc/meminfo", "").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    cache = m.get("Cached", 0) + m.get("Buffers", 0) + m.get("SReclaimable", 0)
    return {
        "total": total, "available": avail, "used": total - avail, "cache": cache,
        "pct": round(100 * (total - avail) / total, 1) if total else 0,
        "swap_total": m.get("SwapTotal", 0), "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0),
    }


def disks():
    seen, out = set(), []
    for line in read("/proc/mounts", "").splitlines():
        dev, mnt, fs = line.split()[:3]
        mnt = mnt.replace("\\040", " ")
        if fs not in REAL_FS or dev in seen:
            continue
        seen.add(dev)
        try:
            s = os.statvfs(mnt)
        except OSError:
            continue
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        used = total - s.f_bfree * s.f_frsize
        if total == 0:
            continue
        out.append({"mount": mnt, "device": dev, "fs": fs, "total": total, "used": used, "free": free,
                    "pct": round(100 * used / (used + free), 1) if used + free else 0})
    return out


def diskstats():
    rd = wr = 0
    for line in read("/proc/diskstats", "").splitlines():
        f = line.split()
        if DISK_RE.match(f[2]):
            rd += int(f[5]) * 512
            wr += int(f[9]) * 512
    return rd, wr


def hwmon():
    """Map hwmon chip name -> list of {label, value} (by name, numbers change across boots)."""
    chips = {}
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(f"{d}/name")
        if not name:
            continue
        temps, fans = [], []
        for p in sorted(glob.glob(f"{d}/temp*_input")):
            v = read_int(p)
            if v is None:
                continue
            label = read(p.replace("_input", "_label")) or os.path.basename(p).split("_")[0]
            temps.append({"label": label, "c": round(v / 1000, 1)})
        for p in sorted(glob.glob(f"{d}/fan*_input")):
            v = read_int(p)
            if v is not None:
                fans.append(v)
        chips[name] = {"temps": temps, "fans": fans}
    return chips


def sensors():
    chips = hwmon()
    # First CPU sensor found: Intel coretemp, AMD k10temp/zenpower, ARM boards, then the ACPI zone.
    cpu_chip = next((n for n in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "acpitz") if chips.get(n, {}).get("temps")), None)
    core = chips.get(cpu_chip, {}).get("temps", []) if cpu_chip else []
    pkg = next((t["c"] for t in core if t["label"].startswith(("Package", "Tctl", "Tdie"))), None)
    if pkg is None and core:
        pkg = max(t["c"] for t in core)
    nvme = chips.get("nvme", {}).get("temps", [])
    nvme_c = next((t["c"] for t in nvme if t["label"] == "Composite"), nvme[0]["c"] if nvme else None)
    wifi = next((c["temps"][0]["c"] for n, c in chips.items() if n.startswith(("iwlwifi", "mt7", "ath")) and c["temps"]), None)
    fans = [f for c in chips.values() for f in c["fans"]]
    return {"cpu": pkg, "cores": [t for t in core if t["label"].startswith("Core")],
            "nvme": nvme_c, "wifi": wifi, "fan_rpm": fans[0] if fans else None}


def cpu_freq():
    fs = [read_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    fs = [f for f in fs if f]
    return round(sum(fs) / len(fs) / 1000) if fs else None


def net_counters():
    out = {}
    for line in read("/proc/net/dev", "").splitlines()[2:]:
        name, data = line.split(":", 1)
        name = name.strip()
        if SKIP_IFACE.match(name):
            continue
        f = data.split()
        out[name] = (int(f[0]), int(f[8]))
    return out


def battery():
    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if not bats:
        return None
    b = bats[0]
    cap = read_int(f"{b}/capacity")
    status = read(f"{b}/status")
    p = read_int(f"{b}/power_now")
    e_now, e_full, e_design = (read_int(f"{b}/{k}") for k in ("energy_now", "energy_full", "energy_full_design"))
    remaining = None
    if p and e_now is not None:
        if status == "Discharging":
            remaining = e_now / p * 3600
        elif status == "Charging" and e_full:
            remaining = max(0, e_full - e_now) / p * 3600
    return {
        "pct": cap, "status": status, "watts": round(p / 1e6, 1) if p else 0,
        "remaining_s": round(remaining) if remaining else None,
        "health": round(100 * e_full / e_design, 1) if e_full and e_design else None,
        "full_wh": round(e_full / 1e6, 1) if e_full else None,
        "design_wh": round(e_design / 1e6, 1) if e_design else None,
        "cycles": read_int(f"{b}/cycle_count"),
        "start_threshold": read_int(f"{b}/charge_control_start_threshold"),
        "stop_threshold": read_int(f"{b}/charge_control_end_threshold"),
        "ac": any(read(f"{d}/type") == "Mains" and read_int(f"{d}/online") == 1
                  for d in glob.glob("/sys/class/power_supply/*")),
    }


def _wifi_link():
    info = {}
    for line in read("/proc/net/wireless", "").splitlines()[2:]:
        name, data = line.split(":", 1)
        f = data.split()
        info = {"iface": name.strip(), "dbm": int(float(f[2]))}
    if info:
        rc, out, _ = run(["iw", "dev", info["iface"], "link"], 3)
        m = re.search(r"SSID: (.+)", out) if rc == 0 else None
        info["ssid"] = m.group(1).strip() if m else None
        m = re.search(r"tx bitrate: ([\d.]+ \S+)", out) if rc == 0 else None
        info["bitrate"] = m.group(1) if m else None
    return info


def _addrs():
    rc, out, _ = run(["ip", "-j", "addr"], 3)
    res = []
    if rc != 0:
        return res
    for i in json.loads(out):
        if SKIP_IFACE.match(i["ifname"]):
            continue
        v4 = [a["local"] for a in i.get("addr_info", []) if a["family"] == "inet"]
        res.append({"iface": i["ifname"], "state": i.get("operstate"), "ipv4": v4})
    return res


def _failed_units():
    units = []
    for scope in ([], ["--user"]):
        rc, out, _ = run(["systemctl", *scope, "--failed", "--output=json", "--no-legend"], 5)
        if rc == 0 and out.strip():
            for u in json.loads(out):
                units.append({"unit": u.get("unit"), "scope": "user" if scope else "system",
                              "description": u.get("description")})
    return units


def _docker():
    rc, out, err = run(["docker", "ps", "--format", "{{json .}}"], 3)
    if rc != 0:
        reason = "permission denied" if "permission denied" in err.lower() else (err.strip().splitlines() or ["unavailable"])[-1]
        return {"ok": False, "reason": reason}
    rows = [json.loads(l) for l in out.splitlines() if l.strip()]
    return {"ok": True, "containers": [{"name": r.get("Names"), "image": r.get("Image"),
                                        "status": r.get("Status")} for r in rows]}


wifi_link = Cached(_wifi_link, 15)
addrs = Cached(_addrs, 30)
failed_units = Cached(_failed_units, 60)
docker = Cached(_docker, 30)


# ---------------------------------------------------------------- background jobs

class Updates(threading.Thread):
    """checkupdates uses a temp db, so it never touches the real pacman sync db."""

    def __init__(self):
        super().__init__(daemon=True)
        self.state = {"checked": None, "count": None, "packages": [], "error": None}

    def run(self):
        if not shutil.which("checkupdates"):  # Arch (pacman-contrib) only
            self.state = {**self.state, "error": "unsupported"}
            return
        time.sleep(20)  # let the network come up after login
        while True:
            rc, out, err = run(["checkupdates"], 180)
            pkgs = [l for l in out.splitlines() if l.strip()]
            if rc in (0, 2):  # 2 = no updates
                self.state = {"checked": time.time(), "count": len(pkgs), "packages": pkgs[:50], "error": None}
            else:
                self.state = {**self.state, "checked": time.time(), "error": (err.strip() or "checkupdates failed")[-200:]}
            time.sleep(1800)


class SpaceScan:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "path": None, "finished": None, "entries": [], "error": None}

    def start(self, path):
        path = os.path.realpath(path or HOME)
        if not (path == HOME or path.startswith(HOME + os.sep)) or not os.path.isdir(path):
            return False, "path must be a folder inside your home directory"
        with self.lock:
            if self.state["running"]:
                return False, "a scan is already running"
            self.state = {"running": True, "path": path, "finished": None, "entries": [], "error": None}
        threading.Thread(target=self._run, args=(path,), daemon=True).start()
        return True, None

    def _run(self, path):
        rc, out, err = run(["du", "-x", "-d1", "-B1", path], 600)
        entries = []
        for line in out.splitlines():
            size, p = line.split("\t", 1)
            if p != path:
                entries.append({"path": p, "name": os.path.basename(p), "bytes": int(size)})
        entries.sort(key=lambda e: -e["bytes"])
        total = next((int(l.split("\t")[0]) for l in out.splitlines() if l.split("\t", 1)[-1] == path), None)
        with self.lock:
            self.state = {"running": False, "path": path, "finished": time.time(), "total": total,
                          "entries": entries[:25], "error": None if out else (err.strip()[-200:] or "scan failed")}


# ---------------------------------------------------------------- network totals

class NetUsage:
    """Persist per-day, per-interface byte totals across restarts and reboots."""

    def __init__(self):
        self.lock = threading.Lock()
        self.boot_id = read("/proc/sys/kernel/random/boot_id")
        try:
            with open(NET_FILE) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("days", {})
        self.last = self.data.get("last", {}) if self.data.get("boot_id") == self.boot_id else {}
        self.dirty_since = time.time()

    def update(self, counters):
        day = datetime.date.today().isoformat()
        with self.lock:
            bucket = self.data["days"].setdefault(day, {})
            for iface, (rx, tx) in counters.items():
                prx, ptx = self.last.get(iface, (0, 0))
                drx = rx - prx if rx >= prx else rx  # counter reset
                dtx = tx - ptx if tx >= ptx else tx
                cur = bucket.setdefault(iface, [0, 0])
                cur[0] += drx
                cur[1] += dtx
                self.last[iface] = (rx, tx)
            if time.time() - self.dirty_since > 60:
                self._save()

    def _save(self):
        self.data["boot_id"], self.data["last"] = self.boot_id, self.last
        # keep ~13 months
        cutoff = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
        self.data["days"] = {d: v for d, v in self.data["days"].items() if d >= cutoff}
        tmp = NET_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, NET_FILE)
        self.dirty_since = time.time()

    def save(self):
        with self.lock:
            self._save()

    def summary(self):
        today = datetime.date.today()
        month = today.strftime("%Y-%m")
        out = {"today": {}, "month": {}, "daily": []}
        with self.lock:
            for d, ifaces in self.data["days"].items():
                for iface, (rx, tx) in ifaces.items():
                    kind = "vpn" if VIRTUAL_IFACE.match(iface) else "physical"
                    for key, match in (("today", d == today.isoformat()), ("month", d.startswith(month))):
                        if match:
                            t = out[key].setdefault(kind, [0, 0])
                            t[0] += rx
                            t[1] += tx
            for i in range(29, -1, -1):
                d = (today - datetime.timedelta(days=i)).isoformat()
                ifaces = self.data["days"].get(d, {})
                rx = sum(v[0] for k, v in ifaces.items() if not VIRTUAL_IFACE.match(k))
                tx = sum(v[1] for k, v in ifaces.items() if not VIRTUAL_IFACE.match(k))
                out["daily"].append({"date": d, "rx": rx, "tx": tx})
        return out


# ---------------------------------------------------------------- sampler

class Sampler(threading.Thread):
    def __init__(self, net_usage):
        super().__init__(daemon=True)
        self.net_usage = net_usage
        self.history = collections.deque(maxlen=int(3600 / INTERVAL))
        self.lock = threading.Lock()
        self.current = {}
        self.prev = None
        self.temp_win = collections.deque(maxlen=int(30 / INTERVAL))  # 30 s smoothing for alerts
        self.mem_win = collections.deque(maxlen=int(30 / INTERVAL))
        self.acc = []  # per-sample values, drained once a minute into the history db

    def take_acc(self):
        with self.lock:
            acc, self.acc = self.acc, []
        return acc

    def _procs(self):
        procs = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    s = f.read()
                with open(f"/proc/{d}/statm") as f:
                    rss = int(f.read().split()[1]) * PAGE_SIZE
            except (OSError, IndexError, ValueError):
                continue
            r = s.rfind(")")
            name = s[s.find("(") + 1:r]
            f = s[r + 2:].split()
            procs[int(d)] = (name, int(f[11]) + int(f[12]), rss)
        return procs

    def sample(self):
        now = time.time()
        cpu = cpu_times()
        net = net_counters()
        dio = diskstats()
        procs = self._procs()
        self.net_usage.update(net)
        cur = {"t": now, "cpu": cpu, "net": net, "dio": dio, "procs": procs}
        prev, self.prev = self.prev, cur
        if prev is None:
            return
        dt = now - prev["t"]

        def pct(name):
            (t1, i1), (t0, i0) = cpu[name], prev["cpu"].get(name, cpu[name])
            dtot = t1 - t0
            return round(100 * (1 - (i1 - i0) / dtot), 1) if dtot > 0 else 0.0

        per_core = [pct(f"cpu{i}") for i in range(NCPU) if f"cpu{i}" in cpu]
        rates = {}
        for iface, (rx, tx) in net.items():
            prx, ptx = prev["net"].get(iface, (rx, tx))
            rates[iface] = {"rx": max(0, rx - prx) / dt, "tx": max(0, tx - ptx) / dt}
        phys = [v for k, v in rates.items() if not VIRTUAL_IFACE.match(k)]
        rx_rate = sum(v["rx"] for v in phys)
        tx_rate = sum(v["tx"] for v in phys)
        dr = max(0, dio[0] - prev["dio"][0]) / dt
        dw = max(0, dio[1] - prev["dio"][1]) / dt

        plist = []
        for pid, (name, ticks, rss) in procs.items():
            p0 = prev["procs"].get(pid)
            c = (ticks - p0[1]) / CLK_TCK / dt * 100 if p0 and p0[0] == name else 0.0
            plist.append({"pid": pid, "name": name, "cpu": round(c, 1), "rss": rss})
        mem = meminfo()
        sens = sensors()
        if sens["cpu"] is not None:
            self.temp_win.append(sens["cpu"])
        if mem["total"]:
            self.mem_win.append(mem["available"] / mem["total"])
        sens["cpu_sustained"] = round(sum(self.temp_win) / len(self.temp_win), 1) if self.temp_win else None
        mem["avail_sustained"] = round(sum(self.mem_win) / len(self.mem_win), 3) if self.mem_win else None
        load = read("/proc/loadavg", "0 0 0").split()[:3]
        current = {
            "time": now,
            "host": {"name": os.uname().nodename, "kernel": os.uname().release,
                     "uptime_s": float(read("/proc/uptime", "0 0").split()[0]),
                     "load": [float(x) for x in load], "ncpu": NCPU, "procs": len(procs)},
            "cpu": {"pct": pct("cpu"), "per_core": per_core, "mhz": cpu_freq()},
            "mem": mem,
            "sensors": sens,
            "disk_io": {"read": dr, "write": dw},
            "net_rates": rates, "net_rx": rx_rate, "net_tx": tx_rate,
            "top_cpu": sorted(plist, key=lambda p: -p["cpu"])[:10],
            "top_mem": sorted(plist, key=lambda p: -p["rss"])[:10],
        }
        with self.lock:
            self.current = current
            self.history.append({"t": round(now), "cpu": current["cpu"]["pct"], "mem": mem["pct"],
                                 "rx": round(rx_rate), "tx": round(tx_rate), "dr": round(dr), "dw": round(dw),
                                 "temp": sens["cpu"]})
            self.acc.append({"cpu": current["cpu"]["pct"], "mem": mem["pct"], "temp": sens["cpu"],
                             "nvme": sens["nvme"], "rx": rx_rate * dt, "tx": tx_rate * dt})

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.sample()
            except Exception as e:
                print(f"sample failed: {e!r}", file=sys.stderr)
            time.sleep(max(0.1, INTERVAL - (time.time() - t0)))




# ---------------------------------------------------------------- history store

class Store:
    """Small SQLite log: one row per minute + alert events. Read it with `sysdash.py report`."""

    def __init__(self, path=DB_FILE, readonly=False):
        uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
        self.db = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10)
        self.lock = threading.Lock()
        if not readonly:
            with self.lock, self.db:
                self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS samples(
                  ts INTEGER PRIMARY KEY, boot_id TEXT,
                  cpu REAL, cpu_max REAL, mem REAL, temp REAL, temp_max REAL, nvme REAL,
                  disk_used INTEGER, disk_total INTEGER, rx INTEGER, tx INTEGER,
                  bat REAL, bat_status TEXT, load1 REAL, throttle INTEGER);
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY, key TEXT, level TEXT, peak_level TEXT, text TEXT,
                  start INTEGER, last_seen INTEGER, end INTEGER);
                CREATE INDEX IF NOT EXISTS events_start ON events(start);
                CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
                """)

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args)

    def meta(self, k, v=None):
        if v is None:
            r = self.q("SELECT v FROM meta WHERE k=?", (k,))
            return r[0][0] if r else None
        self.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


def throttle_count():
    return read_int("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count")


# ---------------------------------------------------------------- stats assembly

def full_stats(sampler, net_usage, updates, monitor):
    with sampler.lock:
        s = dict(sampler.current)
    s["disks"] = disks()
    s["battery"] = battery()
    s["wifi"] = wifi_link.get()
    s["addrs"] = addrs.get()
    s["failed_units"] = failed_units.get()
    s["docker"] = docker.get()
    s["updates"] = updates.state
    s["net_usage"] = net_usage.summary()
    s["alerts"] = monitor.alerts if monitor else []
    s["forecast"] = monitor.forecast.get() if monitor else None
    return s


def root_disk(s):
    ds = s.get("disks") or []
    return next((d for d in ds if d["mount"] == "/"), ds[0] if ds else None)


# ---------------------------------------------------------------- monitor: alerts, notifications, history

class Monitor(threading.Thread):
    """Every 5 s: evaluate alerts (with hysteresis), log start/end events, notify once per problem.
    Every 60 s: write a summary row to the history db."""

    CHECK = 5
    RENOTIFY = 1800  # never repeat a notification for the same problem within 30 min

    def __init__(self, sampler, net_usage, updates, store):
        super().__init__(daemon=True)
        self.sampler, self.net_usage, self.updates, self.store = sampler, net_usage, updates, store
        self.alerts, self.active, self.notified = [], {}, {}
        self.boot_id = read("/proc/sys/kernel/random/boot_id")
        self.forecast = Cached(lambda: disk_forecast(self.store), 600)
        self.last_flush = time.time()
        self._recover()

    def _recover(self):
        now = int(time.time())
        # Open events from a quick service restart carry over; older ones end at their last sighting.
        for eid, key, level, text, start, last_seen in self.store.q(
                "SELECT id, key, level, text, start, last_seen FROM events WHERE end IS NULL AND key NOT LIKE 'sys:%'"):
            if last_seen and now - last_seen < 600:
                self.active[key] = {"id": eid, "level": level, "text": text, "since": start}
                self.notified[key] = now
            else:
                self.store.x("UPDATE events SET end=? WHERE id=?", (last_seen or start, eid))
        # Did the machine go down without a clean stop since the last sample?
        last = self.store.q("SELECT ts, boot_id, bat, bat_status FROM samples ORDER BY ts DESC LIMIT 1")
        clean = self.store.meta("clean_stop")
        if last and last[0][1] != self.boot_id:
            ts, _, bat, bat_status = last[0]
            if clean and int(clean) >= ts:
                self._event("sys:boot", "info", "Restarted normally", now, now)
            else:
                hint = " (battery was at %d%% and discharging — it probably ran out)" % bat \
                    if bat is not None and bat <= 10 and bat_status == "Discharging" else \
                    " (crash, freeze, forced power-off or power loss)"
                self._event("sys:unclean", "warning", "Unexpected shutdown" + hint, ts, ts)
                self._event("sys:boot", "info", "Started again after unexpected shutdown", now, now)
        self.store.meta("clean_stop", "")

    def _event(self, key, level, text, start, end=None):
        cur = self.store.x("INSERT INTO events(key, level, peak_level, text, start, last_seen, end) VALUES(?,?,?,?,?,?,?)",
                           (key, level, level, text, start, start, end))
        return cur.lastrowid

    def mark_clean_stop(self):
        self.store.meta("clean_stop", int(time.time()))

    def evaluate(self, s):
        found = {}
        for d in s.get("disks", []):
            if d["pct"] >= 90:
                found[f"disk:{d['mount']}"] = ("critical" if d["pct"] >= 95 else "warning", f"Disk {d['mount']} is {d['pct']:.0f}% full")
        sens = s.get("sensors") or {}
        t = sens.get("cpu_sustained")
        if t is not None and (t >= 90 or ("temp" in self.active and t >= 85)):
            found["temp"] = ("critical" if t >= 97 else "warning", f"CPU has been at {t:.0f} °C for 30 s")
        n = sens.get("nvme")
        if n is not None and (n >= 70 or ("nvme" in self.active and n >= 65)):
            found["nvme"] = ("warning", f"SSD temperature is {n:.0f} °C")
        mem = s.get("mem") or {}
        av = mem.get("avail_sustained")
        if av is not None and (av < 0.10 or ("mem" in self.active and av < 0.15)):
            found["mem"] = ("critical" if av < 0.05 else "warning", f"Only {av * 100:.0f}% of memory free")
        bat = s.get("battery") or {}
        if bat.get("health") and bat["health"] < 70:
            found["bat:health"] = ("warning", f"Battery health is down to {bat['health']:.0f}%")
        if bat.get("status") == "Discharging" and bat.get("pct") is not None and \
                (bat["pct"] <= 15 or ("bat:low" in self.active and bat["pct"] <= 20)):
            found["bat:low"] = ("critical", f"Battery at {bat['pct']}%")
        for u in s.get("failed_units") or []:
            found[f"unit:{u['unit']}"] = ("warning", f"Service failed: {u['unit']}")
        return found

    def check(self):
        s = full_stats(self.sampler, self.net_usage, self.updates, None)
        if not s.get("time"):
            return
        found, now = self.evaluate(s), int(time.time())
        for key, (level, text) in found.items():
            a = self.active.get(key)
            if not a:
                eid = self._event(key, level, text, now)
                self.active[key] = {"id": eid, "level": level, "text": text, "since": now}
                self.notify(key, level, text)
            else:
                if level == "critical" and a["level"] != "critical":
                    self.store.x("UPDATE events SET peak_level='critical' WHERE id=?", (a["id"],))
                    self.notified.pop(key, None)
                    self.notify(key, level, text)
                a["level"], a["text"] = level, text
        for key in [k for k in self.active if k not in found]:
            a = self.active.pop(key)
            self.store.x("UPDATE events SET end=?, last_seen=? WHERE id=?", (now, now, a["id"]))
        order = {"critical": 0, "warning": 1}
        self.alerts = sorted(({"key": k, "level": a["level"], "text": a["text"], "since": a["since"]}
                              for k, a in self.active.items()), key=lambda a: (order.get(a["level"], 2), a["since"]))
        if time.time() - self.last_flush >= 60:
            self.flush(s, now)

    def flush(self, s, now):
        self.last_flush = time.time()
        acc = self.sampler.take_acc()
        if not acc:
            return
        def avg(k):
            v = [a[k] for a in acc if a[k] is not None]
            return round(sum(v) / len(v), 1) if v else None
        def mx(k):
            v = [a[k] for a in acc if a[k] is not None]
            return max(v) if v else None
        root, bat = root_disk(s), s.get("battery") or {}
        self.store.x("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            now, self.boot_id, avg("cpu"), mx("cpu"), avg("mem"), avg("temp"), mx("temp"), mx("nvme"),
            root["used"] if root else None, root["total"] if root else None,
            int(sum(a["rx"] for a in acc)), int(sum(a["tx"] for a in acc)),
            bat.get("pct"), bat.get("status"), (s.get("host") or {}).get("load", [None])[0], throttle_count()))
        self.store.x("UPDATE events SET last_seen=? WHERE end IS NULL", (now,))
        if now % 3600 < 60:  # hourly housekeeping: keep 30 days of samples, 180 days of events
            self.store.x("DELETE FROM samples WHERE ts < ?", (now - 30 * 86400,))
            self.store.x("DELETE FROM events WHERE start < ?", (now - 180 * 86400,))

    def notify(self, key, level, text):
        if time.time() - self.notified.get(key, 0) < self.RENOTIFY:
            return
        self.notified[key] = time.time()
        threading.Thread(target=self._notify, args=(level, text), daemon=True).start()

    @staticmethod
    def _notify(level, text):
        rc, out, _ = run(["notify-send", "-a", "sysdash", "-u", "critical" if level == "critical" else "normal",
                          "-i", "dialog-warning", "-A", "open=Open sysdash", "--wait", "sysdash", text], 900)
        if out.strip() == "open":
            open_app()

    def run(self):
        while True:
            try:
                self.check()
            except Exception as e:
                print(f"monitor failed: {e!r}", file=sys.stderr)
            time.sleep(self.CHECK)


def open_app():
    cmd = ["omarchy-launch-or-focus-webapp", APP_CLASS, APP_URL] if shutil.which("omarchy-launch-or-focus-webapp") \
        else ["xdg-open", APP_URL]
    subprocess.Popen(cmd,
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- disk forecast

def disk_forecast(store):
    now = time.time()
    rows = store.q("SELECT ts, disk_used, disk_total FROM samples WHERE ts > ? AND disk_used IS NOT NULL ORDER BY ts",
                   (int(now - 30 * 86400),))
    if len(rows) < 2:
        return {"status": "learning", "days": 0}
    span = rows[-1][0] - rows[0][0]
    if span < 86400:
        return {"status": "learning", "days": round(span / 86400, 1)}
    rows = rows[::max(1, len(rows) // 500)]  # least squares on ≤500 points is plenty
    xs = [r[0] for r in rows]
    ys = [r[1] for r in rows]
    slope = statistics.linear_regression(xs, ys).slope * 86400  # bytes per day
    free = rows[-1][2] - rows[-1][1]
    if slope < 50 * 2**20:
        return {"status": "stable", "per_day": slope, "days": round(span / 86400, 1)}
    return {"status": "growing", "per_day": slope, "days_left": free / slope, "days": round(span / 86400, 1)}


# ---------------------------------------------------------------- report

def _journal_json(args, limit):
    rc, out, _ = run(["journalctl", "-o", "json", "-q", "--no-pager", "-n", str(limit), *args], 20)
    res = []
    for line in out.splitlines():
        try:
            res.append(json.loads(line))
        except ValueError:
            pass
    return res


def _msg(e):
    m = e.get("MESSAGE", "")
    return bytes(m).decode(errors="replace") if isinstance(m, list) else str(m)


def build_report(store, hours=24):
    now = time.time()
    since = int(now - hours * 3600)
    rep = {"generated": now, "hours": hours, "since": since}

    ev = store.q("""SELECT key, level, peak_level, text, start, COALESCE(end, last_seen), end IS NULL
                    FROM events WHERE COALESCE(end, last_seen, start) >= ? ORDER BY start DESC""", (since,))
    rep["events"] = [{"key": k, "level": pk or lv, "text": t, "start": st, "end": en, "ongoing": bool(og)}
                     for k, lv, pk, t, st, en, og in ev]

    row = store.q("""SELECT COUNT(*), MIN(ts), MAX(temp_max), MAX(cpu), MAX(mem), MIN(bat), SUM(rx), SUM(tx), MAX(nvme)
                     FROM samples WHERE ts >= ?""", (since,))[0]
    n, first = row[0], row[1]
    peaks = {"minutes_logged": n, "first": first}
    if n:
        def at(col, agg):
            r = store.q(f"SELECT ts FROM samples WHERE ts >= ? ORDER BY {col} {agg} LIMIT 1", (since,))
            return r[0][0] if r else None
        peaks.update({"temp_max": row[2], "temp_at": at("temp_max", "DESC"), "cpu_max": row[3], "cpu_at": at("cpu", "DESC"),
                      "mem_max": row[4], "mem_at": at("mem", "DESC"), "bat_min": row[5],
                      "rx": row[6], "tx": row[7], "nvme_max": row[8]})
        thr = store.q("SELECT boot_id, MAX(throttle) - MIN(throttle) FROM samples WHERE ts >= ? GROUP BY boot_id", (since,))
        peaks["throttle_events"] = sum(r[1] or 0 for r in thr)
        hot = store.q("SELECT COUNT(*) FROM samples WHERE ts >= ? AND temp_max >= 90", (since,))[0][0]
        peaks["hot_minutes"] = hot
    rep["peaks"] = peaks

    rc, out, _ = run(["coredumpctl", "--json=short", "list", f"--since=@{since}", "--no-pager", "-q"], 15)
    try:
        crashes = json.loads(out) if rc == 0 and out.strip() else []
    except ValueError:
        crashes = []
    rep["crashes"] = [{"time": c.get("time", 0) / 1e6, "exe": c.get("exe"), "sig": c.get("sig"), "pid": c.get("pid")}
                      for c in crashes][-20:]

    errs = {}
    for e in _journal_json(["-p", "err", f"--since=@{since}"], 3000):
        who = e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or "unknown"
        g = errs.setdefault(who, {"source": who, "count": 0, "last": 0, "message": ""})
        g["count"] += 1
        ts = int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        if ts >= g["last"]:
            g["last"], g["message"] = ts, _msg(e)[:240]
    rep["errors"] = sorted(errs.values(), key=lambda g: -g["count"])[:8]
    rep["error_total"] = sum(g["count"] for g in errs.values())

    rep["oom"] = [{"time": int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6, "message": _msg(e)[:240]}
                  for e in _journal_json(["-k", f"--since=@{since}", "-g", "Killed process"], 50)]

    # headline: the few lines worth reading first
    head = []
    bad = [e for e in rep["events"] if e["level"] in ("warning", "critical")]
    unclean = [e for e in bad if e["key"] == "sys:unclean"]
    if unclean:
        head.append(("critical", f"{len(unclean)} unexpected shutdown{'s' * (len(unclean) > 1)}"))
    if rep["crashes"]:
        head.append(("critical", f"{len(rep['crashes'])} program crash{'es' * (len(rep['crashes']) > 1)}"))
    if rep["oom"]:
        head.append(("critical", f"{len(rep['oom'])} program{'s' * (len(rep['oom']) > 1)} killed for lack of memory"))
    others = [e for e in bad if e["key"] != "sys:unclean"]
    if others:
        head.append(("warning", f"{len(others)} alert{'s' * (len(others) > 1)} raised"))
    if peaks.get("throttle_events"):
        head.append(("warning", f"CPU slowed itself down {peaks['throttle_events']}× to cool off"))
    if not head:
        head.append(("ok", "Nothing unusual"))
    rep["headline"] = [{"level": l, "text": t} for l, t in head]
    return rep


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "—"


def human_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def report_text(rep):
    L = [f"sysdash report — last {rep['hours']} h (since {fmt_ts(rep['since'])}, generated {fmt_ts(rep['generated'])})", ""]
    L += [f"  [{h['level']}] {h['text']}" for h in rep["headline"]] + [""]
    L.append("events (newest first):")
    for e in rep["events"] or []:
        span = "ongoing" if e["ongoing"] else (f"until {fmt_ts(e['end'])}" if e["end"] and e["end"] != e["start"] else "")
        L.append(f"  {fmt_ts(e['start'])}  {e['level']:<8} {e['text']}  {span}")
    if not rep["events"]:
        L.append("  none")
    p = rep["peaks"]
    L += ["", f"peaks ({p['minutes_logged']} minutes logged):"]
    if p["minutes_logged"]:
        L += [f"  cpu temp max {p['temp_max']} °C at {fmt_ts(p['temp_at'])}; {p['hot_minutes']} minutes ≥ 90 °C; throttled {p['throttle_events']}×",
              f"  cpu max {p['cpu_max']}% (minute avg) at {fmt_ts(p['cpu_at'])}",
              f"  memory max {p['mem_max']}% at {fmt_ts(p['mem_at'])}",
              f"  ssd temp max {p['nvme_max']} °C; battery min {p['bat_min']}%",
              f"  network {human_bytes(p['rx'])} down / {human_bytes(p['tx'])} up"]
    L += ["", "crashes (coredumps):"] + ([f"  {fmt_ts(c['time'])}  {c['exe']}  signal {c['sig']}" for c in rep["crashes"]] or ["  none"])
    L += ["", "out-of-memory kills:"] + ([f"  {fmt_ts(o['time'])}  {o['message']}" for o in rep["oom"]] or ["  none"])
    L += ["", f"journal errors: {rep['error_total']} total"] + \
         [f"  {g['count']:>4}×  {g['source']}: {g['message']}" for g in rep["errors"]]
    return "\n".join(L)


# ---------------------------------------------------------------- claude: scan button + chat (manual only)

REPORTS_DIR = os.path.join(DATA, "reports")
CLAUDE_MODEL = os.environ.get("SYSDASH_MODEL", "sonnet")
CLAUDE_TIMEOUT = 600

# Claude only runs when the user presses "scan" or sends a chat message, and only read-only:
# these commands and paths are allowed, anything else is refused without asking (--permission-mode dontAsk).
CLAUDE_ALLOWED = [
    "Bash(journalctl:*)", "Bash(coredumpctl list:*)", "Bash(coredumpctl info:*)",
    "Bash(systemctl status:*)", "Bash(systemctl --failed:*)", "Bash(systemctl list-units:*)",
    "Bash(systemctl show:*)", "Bash(systemctl is-failed:*)", "Bash(systemctl --user status:*)",
    "Bash(systemctl --user --failed:*)", "Bash(systemctl --user list-units:*)",
    f"Bash(python3 {os.path.join(HERE, 'sysdash.py')} report:*)",
    "Bash(df:*)", "Bash(free:*)", "Bash(uptime)", "Bash(sensors:*)", "Bash(lsblk:*)", "Bash(ps:*)",
    "Bash(top -bn1:*)", "Bash(uname:*)", "Bash(lscpu:*)", "Bash(ip addr:*)", "Bash(ip -br:*)", "Bash(ip route:*)",
    "Bash(du:*)", "Bash(pacman -Q:*)", "Bash(powerprofilesctl get)", "Bash(powerprofilesctl list)",
    "Read(//proc/**)", "Read(//sys/**)", "Read(//var/log/**)", "Read(//etc/**)", f"Read(/{HERE}/**)", f"Read(/{DATA}/**)",
]

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["ok", "minor", "warning", "critical"]},
        "headline": {"type": "string", "description": "One short lowercase line, e.g. 'cpu overheating under sustained load'"},
        "summary": {"type": "string", "description": "2-4 plain sentences: what is going on, why, whether it needs action"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "severity": {"type": "string", "enum": ["ok", "minor", "warning", "critical"]},
            "evidence": {"type": "string", "description": "the concrete numbers or log lines that show it"},
            "cause": {"type": "string"},
            "fix": {"type": "string", "description": "what the user can do; exact commands if any; empty if nothing needed"}},
            "required": ["title", "severity", "evidence", "cause", "fix"]}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["severity", "headline", "summary", "findings", "next_steps"],
}

SYSTEM_PROMPT = """You are sysdash's diagnostic helper on the user's Linux machine.
You have READ-ONLY access: inspect, never change anything. Commands outside your allowlist are refused.
Voice: calm, brief, exact - numbers over adjectives, no filler, no emoji. If the system is fine, say so plainly; never invent problems.
Give fixes as concrete commands the user can run themselves. Useful: journalctl (-b, -b -1, -k, -p err, -u UNIT, --since),
coredumpctl info, systemctl status, /proc and /sys, and `python3 SYSDASH report HOURS` for logged history.
Many firmware and driver messages logged at error level are harmless; only call something a problem if it matters.
In follow-up chat, answer in a few short plain-text paragraphs or a short list; put commands on their own line.""".replace(
    "SYSDASH", os.path.join(HERE, "sysdash.py"))


def system_prompt():
    try:
        with open(NOTES_FILE) as f:
            notes = f.read().strip()[:4000]
    except OSError:
        notes = ""
    return SYSTEM_PROMPT + (f"\n\nNotes from the owner about this machine:\n{notes}" if notes else "")


def claude_bin():
    for p in (shutil.which("claude"), os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/.claude/local/claude")):
        if p and os.access(p, os.X_OK):
            return p
    return None


class Claude:
    """One job at a time: a scan (creates a report) or a chat turn (continues that report's session)."""

    def __init__(self, store, monitor):
        self.store, self.monitor = store, monitor
        self.lock = threading.Lock()
        self.job = None  # live state the page polls: kind, report id, activity (commands run), streamed text
        os.makedirs(REPORTS_DIR, exist_ok=True)

    # ---- saved reports
    def _path(self, rid):
        return os.path.join(REPORTS_DIR, rid + ".json")

    def get(self, rid):
        if not re.fullmatch(r"\d{8}-\d{6}", rid or ""):
            return None
        try:
            with open(self._path(rid)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self, rec):
        tmp = self._path(rec["id"]) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=1)
        os.replace(tmp, self._path(rec["id"]))

    def state(self):
        reports = []
        for f in sorted(glob.glob(os.path.join(REPORTS_DIR, "*.json")), reverse=True)[:30]:
            try:
                with open(f) as fh:
                    r = json.load(fh)
            except (OSError, ValueError):
                continue
            rep = r.get("report") or {}
            reports.append({"id": r["id"], "created": r["created"], "status": r["status"], "severity": rep.get("severity"),
                            "headline": rep.get("headline") or r.get("error"), "messages": len(r.get("chat", []))})
        with self.lock:
            job = dict(self.job, activity=list(self.job["activity"])) if self.job else None
        return {"available": bool(claude_bin()), "model": CLAUDE_MODEL, "job": job, "reports": reports}

    # ---- starting jobs
    def _begin(self, kind, rid):
        with self.lock:
            if self.job:
                return False
            self.job = {"kind": kind, "id": rid, "started": time.time(), "activity": [], "text": ""}
            return True

    def scan(self):
        rid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        if not self._begin("scan", rid):
            return False, "claude is busy"
        threading.Thread(target=self._scan, args=(rid,), daemon=True).start()
        return True, rid

    def chat(self, rid, message):
        rec = self.get(rid)
        if not rec or not rec.get("session_id"):
            return False, "that report has no conversation to continue"
        if not self._begin("chat", rid):
            return False, "claude is busy"
        rec.setdefault("chat", []).append({"role": "user", "text": message, "ts": time.time()})
        self._save(rec)
        threading.Thread(target=self._chat, args=(rid, message), daemon=True).start()
        return True, rid

    # ---- running claude
    def _run(self, prompt, resume=None, schema=None):
        exe = claude_bin()
        if not exe:
            raise RuntimeError("claude CLI not found")
        cmd = [exe, "-p", prompt, "--model", CLAUDE_MODEL, "--tools", "Bash,Read", "--allowedTools", *CLAUDE_ALLOWED,
               "--permission-mode", "dontAsk", "--append-system-prompt", system_prompt(), "--strict-mcp-config",
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if resume:
            cmd += ["--resume", resume]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=HERE)
        timer = threading.Timer(CLAUDE_TIMEOUT, proc.kill)
        timer.start()
        result, tools, texts = None, [], []
        try:
            for line in proc.stdout:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = e.get("type")
                if t == "stream_event":
                    ev = e.get("event") or {}
                    d = ev.get("delta") or {}
                    if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                        with self.lock:
                            self.job["text"] += d.get("text", "")
                elif t == "assistant":
                    for block in (e.get("message") or {}).get("content", []):
                        if block.get("type") == "tool_use":
                            inp = block.get("input") or {}
                            label = inp.get("command") or inp.get("file_path") or block.get("name")
                            tools.append(label)
                            with self.lock:
                                self.job["activity"].append(label)
                                self.job["text"] = ""  # text before a tool call was narration; keep only the answer
                        elif block.get("type") == "text":
                            texts.append(block.get("text", ""))
                elif t == "result":
                    result = e
        finally:
            timer.cancel()
            proc.wait()
        if not result:
            err = proc.stderr.read()[-300:] if proc.stderr else ""
            raise RuntimeError(f"claude stopped after {CLAUDE_TIMEOUT // 60} min" if proc.returncode == -9 else (err or "claude returned nothing"))
        if result.get("is_error"):
            raise RuntimeError(str(result.get("result") or result.get("subtype"))[-300:])
        return result, tools, (texts[-1] if texts else result.get("result") or "")

    def _scan(self, rid):
        rec = {"id": rid, "created": time.time(), "model": CLAUDE_MODEL, "status": "error", "error": None,
               "report": None, "session_id": None, "tools": [], "chat": []}
        cur = "\n".join(f"- [{a['level']}] {a['text']} (since {fmt_ts(a['since'])})" for a in self.monitor.alerts) or "- none"
        prompt = (f"The user pressed 'scan with claude'. Time now: {fmt_ts(time.time())}.\n\nActive alerts:\n{cur}\n\n"
                  f"sysdash's logged history (last 24 h):\n{report_text(build_report(self.store, 24))}\n\n"
                  "Check anything flagged above first, then do a general health check for anything wrong or trending badly. "
                  "Keep it under ~15 commands, then return the structured report.")
        try:
            result, tools, _ = self._run(prompt, schema=REPORT_SCHEMA)
            rep = result.get("structured_output")
            if rep is None and result.get("result"):
                rep = json.loads(result["result"])
            if not rep:
                raise RuntimeError("claude returned no report")
            rec.update(status="done", report=rep, session_id=result.get("session_id"), tools=tools)
        except Exception as e:
            rec["error"] = str(e)[-300:]
        rec["finished"] = time.time()
        self._save(rec)
        with self.lock:
            self.job = None

    def _chat(self, rid, message):
        rec = self.get(rid)
        try:
            result, tools, text = self._run(message, resume=rec["session_id"])
            rec["session_id"] = result.get("session_id") or rec["session_id"]
            rec["chat"].append({"role": "claude", "text": text.strip(), "tools": tools, "ts": time.time()})
        except Exception as e:
            rec["chat"].append({"role": "claude", "text": "", "error": str(e)[-300:], "ts": time.time()})
        self._save(rec)
        with self.lock:
            self.job = None


# ---------------------------------------------------------------- http

def make_handler(sampler, net_usage, updates, scan, monitor, store, claude):
    report_cache = {}
    # Every POST must carry this per-run token. Only the dashboard page knows it (it is injected into the HTML,
    # which other websites can't read), so a random site can't make the browser start Claude or a scan.
    token = secrets.token_urlsafe(24)
    origins = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _host_ok(self):
            # Blocks DNS-rebinding: only answer requests addressed to localhost.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("localhost", "127.0.0.1", "[::1]")

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body)
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _file(self, name, ctype):
            with open(os.path.join(HERE, name), "rb") as f:
                return self._send(200, f.read(), ctype)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html")) as f:
                    return self._send(200, f.read().replace("__SYSDASH_TOKEN__", token), "text/html; charset=utf-8")
            if path == "/tokens.css":
                return self._file("tokens.css", "text/css; charset=utf-8")
            if path == "/api/stats":
                s = full_stats(sampler, net_usage, updates, monitor)
                s["claude_busy"] = claude.job["kind"] if claude.job else None
                s["version"] = __version__
                return self._send(200, s)
            if path == "/api/health":
                lv = "critical" if any(a["level"] == "critical" for a in monitor.alerts) else \
                     "warning" if monitor.alerts else "ok"
                return self._send(200, {"level": lv, "alerts": monitor.alerts})
            if path == "/api/history":
                with sampler.lock:
                    return self._send(200, list(sampler.history))
            if path == "/api/report":
                try:
                    hours = max(1, min(24 * 30, int(qs.get("hours", ["24"])[0])))
                except ValueError:
                    hours = 24
                c = report_cache.get(hours)
                if not c or time.time() - c[0] > 30:
                    c = report_cache[hours] = (time.time(), build_report(store, hours))
                return self._send(200, c[1])
            if path == "/api/scan":
                return self._send(200, scan.state)
            if path == "/api/claude":
                return self._send(200, claude.state())
            if path == "/api/claude/report":
                r = claude.get(qs.get("id", [""])[0])
                if r:
                    r.pop("session_id", None)
                return self._send(200 if r else 404, r or {"error": "not found"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            origin = self.headers.get("Origin")
            if (origin and origin not in origins) or \
                    not secrets.compare_digest(self.headers.get("X-Sysdash-Token") or "", token):
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            if u.path == "/api/scan":
                p = parse_qs(u.query).get("path", [HOME])[0]
                ok, err = scan.start(p)
                return self._send(202 if ok else 409, {"ok": ok, "error": err})
            if u.path == "/api/claude/scan":
                ok, res = claude.scan()
                return self._send(202 if ok else 409, {"ok": ok, "id": res if ok else None, "error": None if ok else res})
            if u.path == "/api/claude/chat":
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 16384)) or b"{}")
                except ValueError:
                    body = {}
                msg = str(body.get("message") or "").strip()[:4000]
                if not msg:
                    return self._send(400, {"ok": False, "error": "empty message"})
                ok, res = claude.chat(str(body.get("id") or ""), msg)
                return self._send(202 if ok else 409, {"ok": ok, "error": None if ok else res})
            self._send(404, {"error": "not found"})

    return Handler


def serve():
    store = Store()
    net_usage = NetUsage()
    sampler = Sampler(net_usage)
    sampler.sample()  # prime deltas
    sampler.start()
    updates = Updates()
    updates.start()
    monitor = Monitor(sampler, net_usage, updates, store)
    monitor.start()
    scan = SpaceScan()
    claude = Claude(store, monitor)
    server = ThreadingHTTPServer((HOST, PORT), make_handler(sampler, net_usage, updates, scan, monitor, store, claude))

    def stop(*_):
        net_usage.save()
        monitor.mark_clean_stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"sysdash on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


def main():
    args = sys.argv[1:]
    if args and args[0] == "report":
        # Plain-text report straight from the history db (works while the service runs).
        hours = int(args[1]) if len(args) > 1 else 24
        if not os.path.exists(DB_FILE):
            Store()  # fresh install: create the empty history so the report can run
        print(report_text(build_report(Store(readonly=True), hours)))
    elif args and args[0] == "open":
        open_app()
    elif args and args[0] in ("-V", "--version", "version"):
        print(f"sysdash {__version__}")
    elif args and args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
    else:
        serve()


if __name__ == "__main__":
    main()
