#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
tool="$here/runtime/analysis/prepare-assets.py"
[[ -f "$tool" ]] || { echo '资产导入工具尚未装配完成，请查看 README。' >&2; exit 2; }
exec python3 "$tool" --runtime "$here/runtime" "$@"
