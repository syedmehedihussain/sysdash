# Contributing to sysdash

Thanks for helping. sysdash stays small on purpose, so a few ground rules keep it that way.

## Principles

1. **No dependencies.** The server is Python standard library only. The page is vanilla JS and CSS, with no build step.
2. **Glanceable over complete.** A new number has to earn its place on the screen. Put detail in the report tab, a tooltip or the CLI report instead.
3. **Local and private.** Nothing leaves the machine unless the user explicitly starts it (the Claude scan).
4. **Read-only Claude.** Changes to `CLAUDE_ALLOWED` must keep Claude unable to modify the system or reach the network.
5. **Calm UI.** Use the design tokens in `tokens.css` rather than raw colors, keep motion short and subtle, and respect `prefers-reduced-motion`.

## Getting started

```bash
git clone https://github.com/syedmehedihussain/sysdash.git
cd sysdash
python3 demo.py          # made-up data on http://localhost:8766
python3 sysdash.py       # your real machine on http://localhost:8765
```

The server reads `index.html` on every request, so UI changes show up on refresh. Restart Python after backend changes.

## Before you open a pull request

- `python3 -m py_compile sysdash.py demo.py` passes.
- `python3 sysdash.py report 1` runs.
- You've checked the page in both themes (`?theme=light`, `?theme=dark`) and at phone width.
- New hardware support falls back gracefully when the sensor or tool isn't there.
- If it's user-facing, add a line to `CHANGELOG.md` under **Unreleased**.

CI runs the same checks on every pull request.

## Reporting bugs

Open an issue using the **bug report** template. Including the output of `python3 sysdash.py report 24` helps a lot. Read it first and remove anything you don't want public.

## Style

- Python: PEP 8-ish with a 120-column limit, small functions, and comments that explain *why*.
- Commit messages: imperative mood (`Add AMD GPU temperature`, not `Added…`).

## Code of conduct

Be kind and assume good intent. Harassment or personal attacks aren't tolerated in issues, pull requests or discussions.
