# Omarchy extras

Optional pieces for [Omarchy](https://omarchy.org) (Arch + Hyprland). sysdash works without them.

## Bar dot

A small dot in the Omarchy bar: dim when everything is fine, sand on a warning, clay on a critical alert, hollow when sysdash isn't running. Hover to see the alerts, click to open the dashboard.

```bash
mkdir -p ~/.config/omarchy/bar/modules
cp sysdash.qml ~/.config/omarchy/bar/modules/
```

Then add it to the bar layout in `~/.config/omarchy/shell.json`, e.g. in `bar.layout.right`:

```json
{ "id": "sysdash", "type": "qml" }
```

The shell reloads on save. If you changed `SYSDASH_PORT`, update the two URLs in `sysdash.qml`.

## App launcher entry

```bash
omarchy webapp install "sysdash" "http://localhost:8765" "$PWD/../../assets/icon.png" \
  "omarchy-launch-or-focus-webapp chrome-localhost__-Default http://localhost:8765"
```

## Keybinding

Add to `~/.config/hypr/bindings.lua` (check `omarchy menu keybindings --print` first so you don't clash with an existing binding):

```lua
o.bind("SUPER + CTRL + M", "System dashboard", "omarchy-launch-or-focus-webapp chrome-localhost__-Default http://localhost:8765")
```
