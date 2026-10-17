#!/bin/busybox sh
# Guest diagnostic script for qemu-menu-probe.py. Baked into the initramfs so the
# probe sends one short command instead of a giant line that guest tty input drops.
emit_count() {
  count=$(/bin/busybox grep -a -c -E "$2" /tmp/menu-load.log)
  printf '%s=%s\n' "$1" "$count"
}
cat /tmp/im.log
/bin/busybox tail -c 131072 /tmp/menu-load.log
echo FULL_LOG_LENS_VERSION
/bin/busybox grep -a 'LENS_VERSION\|VERSION_TRANSPORT_HIT\|VERSION_VALLOC_HIT\|LENS_POOL\|BACKUP_SYNC\|BACKUP_POOL\|HEAPSCAN\|VTSCAN\|VTWALK' /tmp/menu-load.log | /bin/busybox tail -30
echo FULL_LOG_VERSION_SERVICE
/bin/busybox grep -a 'CURRENT_VERSION\|TRANSPORT_UNAVAILABLE' /tmp/menu-load.log | /bin/busybox tail -10
echo FULL_LOG_SYNC_DEST_COUNTS
/bin/busybox grep -a 'SND_SYNC_OTHER' /tmp/menu-load.log | /bin/busybox grep -a -oE 'dest=0x[0-9a-f]+' | /bin/busybox sort | /bin/busybox uniq -c
echo FULL_LOG_APPFW_LOAD
/bin/busybox grep -a -E '(BEGIN|LOADED|FAILED).*(appFw|shared\.)' /tmp/menu-load.log
echo FULL_LOG_LIFECYCLE
echo -n 'log_lines='; /bin/busybox wc -l < /tmp/menu-load.log
echo -n 'enter='; /bin/busybox grep -a -c 'ENTER Application_System_Init' /tmp/menu-load.log
echo -n 'return_zero='; /bin/busybox grep -a -c 'RETURN_ZERO Application_System_Init' /tmp/menu-load.log
echo -n 'return_nonzero='; /bin/busybox grep -a -c 'RETURN_NONZERO Application_System_Init' /tmp/menu-load.log
echo -n 'uxengine_init2='; /bin/busybox grep -a -c 'uxengine_init2' /tmp/menu-load.log
echo FULL_LOG_LIFECYCLE_LINES
/bin/busybox grep -a -n -E 'Application_System_Init|uxengine_init2|uxengine_init ' /tmp/menu-load.log | /bin/busybox tail -40
echo FULL_LOG_ENTER_RETURN
/bin/busybox grep -a -E '^(ENTER|RETURN|RETURN_ZERO|RETURN_NONZERO) ' /tmp/menu-load.log
echo FULL_LOG_CENSUS
emit_count resolved_asi 'RESOLVED Application_System_Init'
emit_count enter_asi 'ENTER Application_System_Init'
emit_count return_asi 'RETURN_(ZERO|NONZERO) Application_System_Init'
emit_count ux2_enter 'ENTER uxengine_init2'
emit_count ux2_return 'RETURN uxengine_init2'
emit_count ux2_hook 'UX2_HOOK result=1'
emit_count appfw_loaded 'LOADED appFw.so'
emit_count gui_loaded 'LOADED gui.so'
echo CENSUS_REPEAT
emit_count resolved_asi 'RESOLVED Application_System_Init'
emit_count enter_asi 'ENTER Application_System_Init'
emit_count return_asi 'RETURN_(ZERO|NONZERO) Application_System_Init'
emit_count ux2_enter 'ENTER uxengine_init2'
emit_count ux2_return 'RETURN uxengine_init2'
emit_count ux2_hook 'UX2_HOOK result=1'
emit_count appfw_loaded 'LOADED appFw.so'
emit_count gui_loaded 'LOADED gui.so'
echo -n 'reg_55='; /bin/busybox grep -a -c '^REG_ASYNC id=0x55' /tmp/menu-load.log
echo -n 'reg_sync_55='; /bin/busybox grep -a -c '^REG_SYNC id=0x55' /tmp/menu-load.log
echo -n 'dynid_55='; /bin/busybox grep -a -c '^DYNAMIC_ID .* id=0x55' /tmp/menu-load.log
echo -n 'rcv_55='; /bin/busybox grep -a -c '^RCV_SITE .*queue=0x55' /tmp/menu-load.log
echo SUBSCRIPTION_LINES
/bin/busybox grep -a -E '^SUBSCRIPTION_(SEEN|UNHANDLED|REPLY|REPLY_FAILED) ' /tmp/menu-load.log | /bin/busybox tail -25
echo -n 'subscription_seen='; /bin/busybox grep -a -c '^SUBSCRIPTION_SEEN ' /tmp/menu-load.log
echo -n 'seq_event_delivered='; /bin/busybox grep -a -c '^SEQ_EVENT_DELIVERED ' /tmp/menu-load.log
echo -n 'seq_event_failed='; /bin/busybox grep -a -c '^SEQ_EVENT_FAILED ' /tmp/menu-load.log
echo SUBSCRIPTION_LINES
echo -n 'subscription_event='; /bin/busybox grep -a -c '^SUBSCRIPTION_EVENT ' /tmp/menu-load.log
echo -n 'subscription_event_failed='; /bin/busybox grep -a -c '^SUBSCRIPTION_EVENT_FAILED ' /tmp/menu-load.log
echo -n 'subscription_unhandled='; /bin/busybox grep -a -c '^SUBSCRIPTION_UNHANDLED ' /tmp/menu-load.log
echo -n 'subscription_reply='; /bin/busybox grep -a -c '^SUBSCRIPTION_REPLY ' /tmp/menu-load.log
echo -n 'subscription_reply_failed='; /bin/busybox grep -a -c '^SUBSCRIPTION_REPLY_FAILED ' /tmp/menu-load.log
echo -n 'registry_sends='; /bin/busybox grep -a -c 'dest=0x811006e9' /tmp/menu-load.log
echo -n 'ux_render_pokes='; /bin/busybox grep -a -c '^UX_RENDER_POKE ' /tmp/menu-load.log
/bin/busybox grep -a '^UX_RENDER_POKE ' /tmp/menu-load.log | /bin/busybox tail -8
echo TASK_MAIN_LINES
/bin/busybox grep -a -E '^(TASK_MAIN_EVENT|APP_STATE|CREATE_SCREEN_POST) ' /tmp/menu-load.log | /bin/busybox tail -20
echo VIEW_SLOTS_LINES
/bin/busybox grep -a -E '^VIEW_SLOT' /tmp/menu-load.log | /bin/busybox tail -20
echo SCREEN_REGISTRY_LINES
/bin/busybox grep -a -E '^SCREEN_REGISTRY' /tmp/menu-load.log | /bin/busybox tail -20
echo N/A 2>/dev/null
echo -n 'menu_init_attempts='; /bin/busybox grep -a -c '^MENU_INIT_' /tmp/menu-load.log
/bin/busybox grep -a -E '^MENU_INIT_' /tmp/menu-load.log | /bin/busybox tail -8
echo -n 'force_set_calls='; /bin/busybox grep -a -c '^FORCE_SET ' /tmp/menu-load.log
/bin/busybox grep -a '^FORCE_SET ' /tmp/menu-load.log | /bin/busybox tail -8
echo FACTORY_CALL_LINES
/bin/busybox grep -a -E '^FACTORY_(HOOK_ARMED|CALL|HOOK_FAILED) ' /tmp/menu-load.log | /bin/busybox tail -25
echo FACTORY_TABLE_LINES
/bin/busybox grep -a -E '^FACTORY_(TABLE|ENTRY) ' /tmp/menu-load.log | /bin/busybox tail -40
/bin/busybox grep -a '^FACTORY_IDS ' /tmp/menu-load.log | /bin/busybox tail -80
echo CREATE_SCREEN_STATE_LINES
/bin/busybox grep -a -E '^CREATE_SCREEN_(ARGS|STATE|RETURN) ' /tmp/menu-load.log | /bin/busybox tail -10
echo -n 'create_screen_calls='; /bin/busybox grep -a -c '^CREATE_SCREEN_CALLED ' /tmp/menu-load.log
echo -n 'create_screen_sentinel='; /bin/busybox grep -a -c 'CREATE_SCREEN_SENTINEL ' /tmp/menu-load.log
echo -n 'create_screen_called='; /bin/busybox grep -a -c 'CREATE_SCREEN_CALLED ' /tmp/menu-load.log
echo -n 'input_reg='; /bin/busybox grep -a -c -E '^REG_(SYNC|ASYNC) id=0x(7a|807a)' /tmp/menu-load.log
echo CENSUS_END
echo FULL_LOG_RECORD_CENSUS
/bin/busybox grep -a -oE '^[A-Z][A-Z_]{2,} ' /tmp/menu-load.log | /bin/busybox sort | /bin/busybox uniq -c | /bin/busybox sort -n | /bin/busybox tail -30
echo FULL_LOG_RCV_SITES
/bin/busybox grep -a 'RCV_SITE' /tmp/menu-load.log
echo FULL_LOG_5503e0
/bin/busybox grep -a '0x5503e0' /tmp/menu-load.log
echo FULL_LOG_REGSYNC
/bin/busybox grep -a 'REG_' /tmp/menu-load.log | /bin/busybox tail -40
echo FULL_LOG_REGSYNC_ALL
/bin/busybox grep -a '^REG_SYNC ' /tmp/menu-load.log
echo FULL_LOG_INPUT_REG
echo -n 'input_reg='; /bin/busybox grep -a -c -E '^REG_(SYNC|ASYNC) id=0x(7a|807a)' /tmp/menu-load.log
/bin/busybox grep -a -E '^REG_(SYNC|ASYNC) id=0x(7a|807a)' /tmp/menu-load.log | /bin/busybox tail -20
echo KIKILOG_DUMP
# Only report the size and the management header; decoding happens on the host. No arithmetic and
# no applets that this busybox lacks (an earlier version aborted the whole diag right here).
if [ -f /tmp/kikilog.dat ]; then
  echo -n 'kikilog_size='; /bin/busybox wc -c < /tmp/kikilog.dat
  echo -n 'kikilog_hdr='
  /bin/busybox dd if=/tmp/kikilog.dat bs=1 skip=2113472 count=32 2>/dev/null | /bin/busybox xxd -p
else
  echo kikilog_missing
fi
echo FULL_LOG_GPE_CAPTURE
/bin/busybox grep -a -E '^GPE_(REQUEST |PARAMETERS |CAPTURE_LIMIT )' /tmp/menu-load.log
echo FULL_LOG_RESOURCE_OPEN
/bin/busybox grep -a '^RESOURCE_' /tmp/menu-load.log
echo OSD_UIPC_PROC_READONLY
/bin/busybox ls -l /proc/osal
if [ -d /proc/osal/uipc ]; then
  /bin/busybox ls -l /proc/osal/uipc
elif [ -r /proc/osal/uipc ]; then
  /bin/busybox dd if=/proc/osal/uipc bs=4096 count=1 2>/dev/null
fi
printf '\nMENU_PROBE_FINISHED\n'
