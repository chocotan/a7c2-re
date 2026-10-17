#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
entry="$here/runtime/analysis/menu-native.py"
[[ -f "$entry" ]] || { echo '运行程序尚未装配完成，请查看本目录 README 的当前状态。' >&2; exit 2; }
exec python3 "$entry" "$@"
