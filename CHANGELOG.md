# Changelog

All notable changes to sysdash are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-04

First public release.

### Added

- **Overview:** CPU, memory, battery and temperature tiles with block meters; a network card with live speed, a one-hour trace and daily/monthly data totals; a storage card with free space, disk activity, SSD temperature and a fill-up forecast; a system report with uptime, load, failed services, pending updates (Arch), Docker and top processes.
- **Health line** at the top: all systems nominal, or the active alerts.
- **Alerts** for disk space, CPU and SSD temperature, memory, battery health, low battery and failed services, smoothed over 30 seconds with hysteresis. One desktop notification per problem.
- **Incident log:** per-minute history in SQLite, an alert timeline, detection of unexpected shutdowns, coredump and out-of-memory tracking, and journal error summaries.
- **Report tab** with 24-hour, 7-day and 30-day views.
- **`sysdash.py report`** for a plain-text incident report on the command line.
- **Claude scan & chat** (optional): a read-only diagnostic scan through the Claude Code CLI, with structured reports and follow-up chat. It runs only when you click.
- **"What's filling my disk"** scan with drill-down.
- **Light and dark modes**, a layout that works on phones, and reduced-motion support.
- **Security:** binds to localhost only, checks the Host header, and requires a per-run token for every action.
- **`install.sh` / `uninstall.sh`** for a systemd user service.
- **`demo.py`** to run with made-up data for screenshots and trying it out.
- **Omarchy extras:** a bar status dot, a launcher entry and a keybinding.

[Unreleased]: https://github.com/syedmehedihussain/sysdash/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/syedmehedihussain/sysdash/releases/tag/v1.0.0
