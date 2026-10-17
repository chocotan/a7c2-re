#!/usr/bin/env python3
"""Replay successful button taps from an earlier console; save before-Enter images."""
import argparse
import json
from pathlib import Path
import re
import tempfile
import time

from menu_control import BASE, VM
from menu_live import KEYS, SINGLE_STATUS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('console', type=Path)
    parser.add_argument('--after', nargs='*', choices=sorted(KEYS), default=[],
                        help='additional fixed controls to verify navigation after the reproduced entry')
    args = parser.parse_args()
    reverse = {code: name for name, code in KEYS.items() if name not in SINGLE_STATUS}
    codes = re.findall(r'INJECT_KEY_SEND[^\n]+key_code=(0x[0-9a-f]+) sub=0 status=0x1 ex=0 from=0x1 result=0',
                       args.console.read_text(errors='replace'))
    if not codes or any(code not in reverse for code in codes):
        parser.error('expected a nonempty sequence of supported button taps')
    destination = BASE / 'input-replay-check'
    destination.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='replay-', dir=destination))
    vm = VM(native_display='none')

    def wait(predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = vm.status()
            if status['state'] == 'error':
                raise RuntimeError(status['error'])
            if predicate(status):
                return status
            time.sleep(.02)
        raise TimeoutError('replay operation timed out')

    steps = []
    try:
        vm.start()
        wait(lambda status: status['state'] == 'running', 180)
        for number, key in enumerate([reverse[code] for code in codes] + args.after, 1):
            if key == 'ENTER':
                old = (vm.status()['snapshot'] or {}).get('number', 0)
                vm.enqueue({'action': 'snapshot'})
                wait(lambda status: (status['snapshot'] or {}).get('number', 0) > old)
                with vm.lock:
                    (out / f'before-enter-{number:03d}.png').write_bytes(vm.png)
            event_id = vm.event_id
            began = time.monotonic()
            vm.enqueue({'action': 'key', 'key': key})
            wait(lambda status: any(event['id'] > event_id and event.get('kind') == 'input'
                                    for event in status['events']))
            steps.append({'number': number, 'key': key, 'seconds': time.monotonic() - began})
            print('REPLAY', number, key, flush=True)
            time.sleep(.1)
        # An ACK can precede an application assertion; observe it without another key.
        until = time.monotonic() + 3
        wait(lambda status: time.monotonic() >= until, 5)
        old = (vm.status()['snapshot'] or {}).get('number', 0)
        vm.enqueue({'action': 'snapshot'})
        wait(lambda status: (status['snapshot'] or {}).get('number', 0) > old)
        with vm.lock:
            (out / 'after-sequence.png').write_bytes(vm.png)
        print('REPLAY_COMPLETED', out)
        return 0
    except Exception as error:
        print('REPLAY_FAILED', str(error), out, flush=True)
        return 1
    finally:
        (out / 'result.json').write_text(json.dumps({'source': str(args.console), 'steps': steps,
                                                   'state': vm.status()}, ensure_ascii=False, indent=2) + '\n')
        vm.close()


if __name__ == '__main__':
    raise SystemExit(main())
