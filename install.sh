#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_home="${HERDR_APP_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/herdr-app}"
bin_home="$HOME/.local/bin"

for mod in beads-dashboard bead-detail; do
  mkdir -p "$app_home/mods/$mod"
  cp -f "$source_dir/mods/$mod/"*.js "$app_home/mods/$mod/"
  cp -f "$source_dir/mods/$mod/mod.json" "$app_home/mods/$mod/mod.json"
done
mkdir -p "$bin_home"
cp -f "$source_dir/bin/go-bd" "$bin_home/go-bd"
chmod +x "$bin_home/go-bd"
printf 'Installed Beads dashboard and detail mods in %s\nLauncher: %s/go-bd\nOpen from a project: go-bd --dashboard\n' "$app_home" "$bin_home"
