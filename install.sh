#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="${HERDR_BEADS_PYTHON:-python3}"
command="${1:-install}"
case "$command" in
  status|restart|stop|--help|-h)
    exec "$python" "$source_dir/bin/beads_service_install.py" "$@"
    ;;
esac
"$python" "$source_dir/bin/beads_service_install.py" "$@"
app_home="${HERDR_APP_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/herdr-app}"
bin_home="$HOME/.local/bin"

for mod in beads-dashboard bead-detail beads-sidebar; do
  mkdir -p "$app_home/mods/$mod"
  cp -f "$source_dir/mods/$mod/"*.js "$app_home/mods/$mod/"
  cp -f "$source_dir/mods/$mod/mod.json" "$app_home/mods/$mod/mod.json"
done
mkdir -p "$bin_home"
cp -f "$source_dir/bin/go-bd" "$bin_home/go-bd"
cp -f "$source_dir/bin/go-bd-session.mjs" "$bin_home/go-bd-session.mjs"
cp -f "$source_dir/bin/beads_epics.py" "$bin_home/beads_epics.py"
chmod +x "$bin_home/go-bd"
mkdir -p "$app_home/layouts"
if [[ ! -e "$app_home/layouts/beads-workspace.json" ]]; then
  cp -f "$source_dir/layouts/beads-workspace.json" "$app_home/layouts/beads-workspace.json"
fi
cp -f "$source_dir/layouts/beads-workspace.svg" "$app_home/layouts/beads-workspace.svg"
printf 'Installed Beads mods in %s\nLauncher: %s/go-bd\nSidebar button: herdr-app do layout.switch --name beads-workspace\nOpen from a project: go-bd --dashboard\n' "$app_home" "$bin_home"
