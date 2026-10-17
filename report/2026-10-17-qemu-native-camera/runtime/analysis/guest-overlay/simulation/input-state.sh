#!/bin/busybox sh
# Read globals from the running app only; never load or call a second appFw.
p=
for candidate in /proc/[0-9]*; do
  if /bin/busybox grep -q '/usr/lib/appFw.so' "$candidate/smaps" 2>/dev/null; then
    if [ -n "$p" ]; then echo INPUT_STATE_AMBIGUOUS_PROCESS; exit 3; fi
    p="$candidate"
  fi
done
if [ -z "$p" ]; then
  echo INPUT_STATE_APP_NOT_MAPPED
  exit 3
fi
echo "INPUT_STATE_PID=${p#/proc/}"
for field in product:0x7e504d8:4 index:0x5bc35b0:4 mode_raw:0x7ecad08:8 main_raw:0x7ecad30:8 sub_raw:0x7ecad58:8 dial:0x7ee4750:4 switch:0x7ee4778:4 saved:0x7ee4798:4 rear_l_setting:0x7e72220:4 \
             c1_still:0x7e72298:4 c1_movie:0x7e72550:4 c1_play:0x7e72730:4 \
             sequence_enqueue_enabled:0x7e4e608:1 sequence_requests:0x7e4e5d0:24 sequence_group2_list:0x910a548:24 sequence_group2_current:0x910a5c0:8 \
             boot_seen:0x7ecfe20:1 controller_vptr:0x7603190:8 boot_target:0x7603494:12 model16e:0x7e5d0f8:224 flag16f:0x7e5d3a8:1 flag170:0x7e5d3c8:1; do
  name=${field%%:*}
  rest=${field#*:}
  offset=${rest%%:*}
  count=${rest#*:}
  echo "INPUT_STATE_FIELD=$name offset=$offset"
  /simulation/menu-readmem "$p/mem" "$offset" "$count" appFw.so || exit 4
done
echo INPUT_STATE_READ_PASS
