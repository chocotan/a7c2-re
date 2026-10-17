#!/bin/sh
# 相机工具箱统一入口：终端 / 命令 / 内存 / 整机镜像转储。
# 相机就绪检查、相机卷自动卸载、"需断电重启"提示都已在各工具内。
set -e
cd "$(dirname "$0")"
PY=./.venv/bin/python

usage() {
    cat <<'USAGE'
用法: ./cam.sh <子命令> [参数...]

  shell            在相机内开常驻终端（cd/export 保留）—— 默认子命令
  cmd '命令'        一次会话内执行命令；多条用分号，或加 --file/--interactive
  mem ...          读相机内存到本机，例如：
                     ./cam.sh mem --phys 0xffff0000 --size 0x6000 --save brom.bin
                     ./cam.sh mem --pid 1 --addr 0x400000 --size 0x1000
  dump             整机分区镜像写入 SD 卡（需卡在相机里）
  install-card     重建 SD 卡布局（会清空整张卡）
  fetch            把卡上的镜像取回本机并校验
  verify           校验本机镜像、生成清单
  help             显示本帮助
USAGE
}

case "${1:-shell}" in
    -h|--help|help) usage ;;
    shell)          exec sudo $PY run_command.py --shell ;;
    cmd)            shift; exec sudo $PY run_command.py "$@" ;;
    mem)            shift; exec sudo $PY memread.py "$@" ;;
    dump)           shift; exec sudo $PY dump_system.py --stage dump "$@" ;;
    install-card)   shift; exec sudo $PY dump_system.py --format-card "$@" ;;
    fetch)          shift; exec sudo $PY dump_system.py --stage fetch "$@" ;;
    verify)         shift; exec sudo $PY dump_system.py --stage verify "$@" ;;
    *)              echo "未知子命令: $1" >&2; usage; exit 2 ;;
esac
