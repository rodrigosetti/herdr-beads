#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="${HERDR_BEADS_PYTHON:-python3}"
exec "$python" "$source_dir/bin/beads_service_install.py" "$@"
