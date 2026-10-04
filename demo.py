#!/usr/bin/env python3
"""Run sysdash with made-up data, for screenshots and for trying the UI.

    python3 demo.py            # http://localhost:8766

Hostname, IP addresses, Wi-Fi, processes, history, alerts and the Claude report are all invented,
so nothing about your machine ends up in a screenshot. Data lives in a temporary folder that is
deleted on exit. The real sysdash service (if running) is not touched.
"""

import atexit
import datetime
import json
import math
import os
import random
import shutil
import tempfile
import time

DATA = tempfile.mkdtemp(prefix="sysdash-demo-")
atexit.register(shutil.rmtree, DATA, True)
os.environ["SYSDASH_DATA"] = DATA
os.environ.setdefault("SYSDASH_PORT", "8766")

import sysdash as sd  # noqa: E402  (must come after the environment is set)

GB = 2**30


def _bursts(t, period, width, seed):
    """Irregular bursts: each `period`-second bucket gets 0 or 1 burst at a random spot and size."""
    total = 0.0
    for b in (int(t // period) - 1, int(t // period), int(t // period) + 1):
        r = random.Random(b * 7919 + seed)
        if r.random() < 0.55:
            c = b * period + r.uniform(0.1, 0.9) * period
            total += r.uniform(0.25, 1.0) * math.exp(-((t - c) / width) ** 2)
    return total


def point(t):
    """Smooth, plausible metrics for time t (seconds): a calm baseline with the odd burst."""
    work, dl = _bursts(t, 420, 40, 1), _bursts(t, 700, 25, 2)
    cpu = max(2, 13 + 6 * math.sin(t / 170) + 3 * math.sin(t / 23) + 45 * work)
    return {"t": int(t), "cpu": round(cpu, 1), "mem": round(36 + 3 * math.sin(t / 900), 1),
            "rx": int(40e3 + 25e3 * math.sin(t / 90) ** 2 + 3.2e6 * dl), "tx": int(10e3 + 8e3 * math.sin(t / 70) ** 2 + 2.6e5 * dl + 1.2e5 * work),
            "dr": int(2e4 * math.sin(t / 50) ** 2 + 8e6 * work), "dw": int(5e4 + 4e4 * math.sin(t / 40) ** 2 + 2.5e6 * work + 4e6 * dl),
            "temp": round(48 + cpu * 0.42, 1)}


def seed():
    now = int(time.time())
    boot = sd.read("/proc/sys/kernel/random/boot_id")
    root = next((d for d in sd.disks() if d["mount"] == "/"), None) or {"used": 180 * GB, "total": 476 * GB}
    store = sd.Store()
    rnd = random.Random(7)
    rows, throttle = [], 0
    for i in range(26 * 60, 0, -1):
        ts = now - i * 60
        p = point(ts)
        hot = 3 * 3600 + 600 < i * 60 < 3 * 3600 + 840  # one hot spell ~3 h ago
        throttle += 40 if hot else 0
        rows.append((ts, boot, p["cpu"], min(100, p["cpu"] * 1.8), p["mem"], p["temp"] + (30 if hot else 0),
                     p["temp"] + 6 + (36 if hot else 0), 41 + rnd.random() * 3,
                     int(root["used"] - (now - ts) / 86400 * 0.9 * GB), root["total"],
                     int(p["rx"] * 60 * 0.2), int(p["tx"] * 60 * 0.2),
                     max(20, 100 - ((i % 480) / 480) * 70), "Discharging", round(p["cpu"] / 20, 2), throttle))
    with store.lock, store.db:
        store.db.executemany("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        ev = [("temp", "warning", "warning", "CPU has been at 93 °C for 30 s", now - 3 * 3600 - 800, now - 3 * 3600 - 560, now - 3 * 3600 - 560),
              ("unit:backup.service", "warning", "warning", "Service failed: backup.service", now - 20 * 3600, now - 20 * 3600 + 900, now - 20 * 3600 + 900)]
        store.db.executemany("INSERT INTO events(key, level, peak_level, text, start, last_seen, end) VALUES(?,?,?,?,?,?,?)", ev)
    store.meta("clean_stop", "")

    days = {}
    for d in range(30):
        day = (datetime.date.today() - datetime.timedelta(days=d)).isoformat()
        days[day] = {"wlan0": [int((1.2 + rnd.random() * 2.6) * GB), int((0.1 + rnd.random() * 0.3) * GB)]}
    days[datetime.date.today().isoformat()] = {"wlan0": [int(1.84 * GB), int(0.21 * GB)]}
    with open(sd.NET_FILE, "w") as f:
        json.dump({"days": days, "boot_id": boot, "last": {k: list(v) for k, v in sd.net_counters().items()}}, f)

    os.makedirs(sd.REPORTS_DIR, exist_ok=True)
    rid = datetime.datetime.fromtimestamp(now - 1500).strftime("%Y%m%d-%H%M%S")
    report = {
        "id": rid, "created": now - 1500, "finished": now - 1440, "model": "sonnet", "status": "done", "error": None,
        "session_id": None, "tools": ["sensors", "journalctl -b -k -p warning --since -4h", "systemctl --failed",
                                      "python3 sysdash.py report 24", "cat /sys/devices/system/cpu/intel_pstate/no_turbo"],
        "report": {
            "severity": "minor",
            "headline": "one short thermal spike, otherwise healthy",
            "summary": "The CPU hit 95 °C for about four minutes around three hours ago and throttled, during a short build. "
                       "It has been at 50–60 °C since. Memory, disk and services are fine; yesterday's backup.service failure recovered on the next run.",
            "findings": [
                {"title": "CPU reached 95 °C under a short burst", "severity": "minor",
                 "evidence": "Peak 95 °C, 4 min ≥ 90 °C, throttled 120×, average load 31% at the time. Now 54 °C, fan 2400 rpm.",
                 "cause": "Turbo boost on a thin laptop during a compile; the fan ramps up a few seconds late.",
                 "fix": "Nothing needed if it's rare. To cap it, switch to the balanced profile: `powerprofilesctl set balanced`."},
                {"title": "backup.service failed once", "severity": "ok",
                 "evidence": "Exit code 1 at 02:00 yesterday: network unreachable. The 03:00 retry succeeded.",
                 "cause": "Wi-Fi wasn't up yet after resume.", "fix": ""},
                {"title": "Disk is filling slowly", "severity": "ok",
                 "evidence": "+0.9 GB/day over the last day; full in about two months at this rate.",
                 "cause": "Mostly package cache.", "fix": "Clear old packages when convenient: `paccache -rk2`."},
            ],
            "next_steps": ["Check the report tab again after your next long build to see if the spike repeats."],
        },
        "chat": [
            {"role": "user", "text": "Is 95 °C dangerous for this CPU?", "ts": now - 1300},
            {"role": "claude", "ts": now - 1280, "tools": ["lscpu"],
             "text": "Not for a few minutes. The CPU's limit is 100 °C, and it slows itself down (throttles) before it gets there, which is what happened.\n\n"
                     "It's worth acting only if it sits there for long stretches. The quickest change is the balanced profile:\n\n```\npowerprofilesctl set balanced\n```"},
        ],
    }
    with open(os.path.join(sd.REPORTS_DIR, rid + ".json"), "w") as f:
        json.dump(report, f)


# ---- patch the live views with invented values

_full_stats = sd.full_stats
_build_report = sd.build_report


def demo_stats(*a, **k):
    s = _full_stats(*a, **k)
    if not s.get("time"):  # first moment after start, before the first sample
        return s
    now = time.time()
    p = point(now)
    rnd = random.Random(int(now / 2))
    s["host"].update(name="devbox", kernel="6.17.1-arch1-1", uptime_s=3 * 86400 + 4 * 3600 + 720, load=[0.84, 0.97, 1.02], procs=312)
    s["cpu"].update(pct=p["cpu"], mhz=2850, per_core=[max(0, p["cpu"] + rnd.uniform(-8, 8)) for _ in range(8)])
    s["mem"].update(pct=p["mem"], used=int(p["mem"] / 100 * 16 * GB), total=16 * GB)
    s["sensors"].update(cpu=p["temp"], nvme=42.0, fan_rpm=2400)
    s["net_rx"], s["net_tx"] = p["rx"], p["tx"]
    s["disk_io"] = {"read": p["dr"], "write": p["dw"]}
    s["battery"] = {"pct": 78, "status": "Discharging", "watts": 7.9, "remaining_s": 4 * 3600 + 12 * 60, "health": 94.2,
                    "full_wh": 53.7, "design_wh": 57.0, "cycles": 112, "start_threshold": 75, "stop_threshold": 80, "ac": False}
    s["wifi"] = {"iface": "wlan0", "dbm": -58, "ssid": "home-5g", "bitrate": "866.7 MBit/s"}
    s["addrs"] = [{"iface": "wlan0", "state": "UP", "ipv4": ["192.168.1.42"]}, {"iface": "tailscale0", "state": "UNKNOWN", "ipv4": ["100.64.0.12"]}]
    s["failed_units"] = []
    s["docker"] = {"ok": True, "containers": [{"name": "postgres", "image": "postgres:17", "status": "Up 3 days"},
                                              {"name": "redis", "image": "redis:8", "status": "Up 3 days"}]}
    s["updates"] = {"checked": now - 900, "count": 4, "packages": [], "error": None}
    s["top_cpu"] = [{"pid": 4182, "name": "firefox", "cpu": 11.0, "rss": 900 * 2**20},
                    {"pid": 2210, "name": "code", "cpu": 6.0, "rss": 700 * 2**20},
                    {"pid": 1033, "name": "Hyprland", "cpu": 3.0, "rss": 180 * 2**20}]
    s["alerts"] = []
    return s


def demo_report(store, hours=24):
    r = _build_report(store, hours)
    r["crashes"], r["oom"] = [], []
    r["errors"] = [{"source": "kernel", "count": 9, "last": time.time() - 4000, "message": "ACPI BIOS Error (bug): Could not resolve symbol [\\_SB.PC00.LPCB.EC0], AE_NOT_FOUND"},
                   {"source": "backup.service", "count": 1, "last": time.time() - 72000, "message": "rsync: failed to connect to nas.local: Network is unreachable"},
                   {"source": "bluetoothd", "count": 1, "last": time.time() - 30000, "message": "Failed to set mode: Failed (0x03)"}]
    r["error_total"] = 11
    return r


class DemoSampler(sd.Sampler):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        now = time.time()
        for t in range(int(now - 3600), int(now), 2):
            self.history.append(point(t))

    def sample(self):
        super().sample()
        with self.lock:
            if self.history:
                self.history[-1] = point(time.time())


sd.full_stats = demo_stats
sd.build_report = demo_report
sd.Sampler = DemoSampler

if __name__ == "__main__":
    seed()
    print(f"sysdash demo with made-up data: http://localhost:{sd.PORT}  (Ctrl+C to stop)")
    sd.serve()
