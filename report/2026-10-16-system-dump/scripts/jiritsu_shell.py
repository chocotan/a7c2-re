"""Drive a persistent shell inside the camera, so a session behaves like a terminal.

Running one command per request starts a fresh `/bin/sh -c` every time: `cd` and
`export` do not survive, and the reply only arrives once the command has finished.
A shell that stays alive reading from a FIFO keeps the session's state and lets output
be collected as it appears.

The device constrains the implementation (verified in the dumped rootfs):

* no `mkfifo` and no `printf` — so the FIFO is made with `mknod <path> p`, and
  commands are fed through a **quoted heredoc**, which needs no escaping at all;
* no `tail`, so the output file is read whole and the new part is sliced here;
* `/tmp` is a 12 MiB vfat partition, so the output file is truncated once read.

A FIFO reader sees EOF as soon as the last writer closes, which would kill the
shell after the first command, so a long-lived `sleep` holds the FIFO open for
writing. If that shell dies anyway, the next command finds no completion marker and
the session reports it instead of hanging.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from jiritsu_preload_deploy import READ_ATTEMPTS, submit_command
from jiritsu_run_command import parse_reply

# FIFOs need a filesystem that supports them: /tmp is vfat on this camera and
# `mknod ... p` fails there with "Operation not supported", so the candidates are a
# tmpfs mount, a devtmpfs path, and the card's ext2 partition — whichever accepts a
# FIFO first is used, and the choice is reported back.
SHELL_DIRS = ("/tmp_wbshd/a7c2-sh", "/dev/a7c2-sh", "/tmp/sd/a7c2-sh", "/tmp/jiritsu")
STATE_DIR_MARKER = "SHELL_DIR="
DONE_PREFIX = "__A7C2_DONE__"
# The read command carries its own end marker; reusing the completion
# prefix made the host mistake it for a status ("[exit READ]").
READ_MARKER = "__A7C2_READ__"
DONE_PATTERN = re.compile(rf"{re.escape(DONE_PREFIX)}(\d+)")
HEREDOC = "A7C2-IN"
HOLD_SECONDS = 86400


def setup_scripts():
    """One command that picks a FIFO-capable directory, starts the shell, and holds it open."""
    candidates = " ".join(SHELL_DIRS)
    script = (
        f'for D in {candidates}; do '
        f'mkdir -p "$D" 2>/dev/null; rm -f "$D/in" "$D/out"; '
        f'if mknod "$D/in" p 2>/dev/null; then '
        f'echo "{STATE_DIR_MARKER}$D"; '
        f'sh < "$D/in" > "$D/out" 2>&1 & echo SHELL_PID=$!; '
        f'sleep {HOLD_SECONDS} > "$D/in" & echo HOLD_PID=$!; '
        f'break; fi; done; sleep 1; echo SHELL_READY'
    )
    return (script,)


def shell_paths(state_dir):
    return f"{state_dir}/in", f"{state_dir}/out"


def feed_script(command, state_dir):
    """Feed one command line plus a completion marker through the FIFO.

    The heredoc is quoted, so the command text is passed through literally: no
    escaping, and the marker is echoed by the persistent shell itself, which makes
    it a reliable end-of-output signal for that command.
    """
    fifo, _ = shell_paths(state_dir)
    return (f"cat > {fifo} <<'{HEREDOC}'\n"
            f"{command}\n"
            f"echo {DONE_PREFIX}$?\n"
            f"{HEREDOC}\necho FED")


def read_script(state_dir, offset=0):
    """Read the output file from a byte offset.

    The shell holds the file open, so truncating it between commands leaves a hole at
    the shell's unchanged write offset — that is where the runs of blanks and NUL
    bytes came from. `tail` does not exist on the device, but `dd` does, so the host
    keeps its own offset and never truncates.
    """
    _, out = shell_paths(state_dir)
    return f"dd if={out} bs=1 skip={int(offset)} 2>/dev/null; echo {READ_MARKER}"


def alive_script(pid):
    return f"kill -0 {pid} 2>/dev/null && echo SHELL_ALIVE || echo SHELL_DEAD"


def exit_script(state_dir):
    fifo, _ = shell_paths(state_dir)
    return f"cat > {fifo} <<'{HEREDOC}'\nexit\n{HEREDOC}\necho EXIT_SENT"


def truncate_script(state_dir):
    _, out = shell_paths(state_dir)
    return f": > {out} 2>/dev/null; echo TRUNCATED"


def parse_marker(text, prefix, suffix=""):
    """Return the text before `prefix<n>` plus the value, or (None, None)."""
    marker = f"{prefix}{suffix}"
    index = text.rfind(marker)
    if index < 0:
        return None, None
    value = text[index + len(marker):].split("\n", 1)[0].strip()
    return text[:index], value


def _stderr(text=""):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def shell_driver(lines=None, interactive=False, json_path=None, attempts=READ_ATTEMPTS,
                 out_stream=None, poll_seconds=0.6, poll_attempts=60):
    """Build a deploy driver that runs a persistent in-camera shell."""
    lines = list(lines or ())
    if not lines and not interactive:
        raise ValueError("provide a script or enable interactive mode")
    emit = out_stream.write if out_stream is not None else None

    def report(text):
        if emit is not None:
            emit(text)
        else:
            _stderr(text)

    def read_command(index):
        report(f"a7c2[{index}]$ ")
        line = sys.stdin.readline()
        if not line:
            return None
        text = line.rstrip("\n")
        return None if text.strip() in ("exit", "quit") else text

    def collect(transport, requests, log, state_dir, offset):
        """Read new output since `offset`; return (output, status, new_offset).

        Everything is counted in bytes: `dd bs=1 skip=` counts bytes, so the text is
        handled as latin-1 (one byte per character) and only decoded for display.
        """
        for _ in range(poll_attempts):
            raw = submit_command(transport, requests, read_script(state_dir, offset), "read", log,
                                 attempts=attempts)
            body, _status, error = parse_reply(raw, "read")
            if error:
                time.sleep(poll_seconds)
                continue
            text = body.encode("latin-1", "replace").decode("latin-1")
            found = DONE_PATTERN.search(text)
            if found:
                consumed = text.index("\n", found.end()) + 1 if "\n" in text[found.end():] else len(text)
                output = text[:found.start()].rstrip("\n")
                return output, found.group(1), offset + len(text[:consumed].encode("latin-1"))
            time.sleep(poll_seconds)
        return None, None, offset

    def driver(transport, requests, log, record):
        results = []
        setup = []
        for script in setup_scripts():
            raw = submit_command(transport, requests, script, "setup", log, attempts=attempts)
            setup.append(raw.decode("ascii", "replace"))
        text = "\n".join(setup)
        body, _status, _error = parse_reply(text.encode(), "setup")
        text = body if body is not None else text
        _, state_dir = parse_marker(text, STATE_DIR_MARKER)
        _, pid = parse_marker(text, "SHELL_PID=")
        if "SHELL_READY" not in text or not state_dir:
            raise ValueError(f"in-camera shell did not start (FIFO needs a non-vfat path): "
                             f"{text[-240:]!r}")
        report(f"常驻 shell 已启动：pid={pid or '未知'}，FIFO 目录 {state_dir}")
        record["shell_pid"] = pid
        record["shell_state_dir"] = state_dir

        index = 0
        offset = 0
        pending = list(lines)
        while True:
            if pending:
                command = pending.pop(0)
            elif interactive:
                command = read_command(index + 1)
                if command is None:
                    break
            else:
                break
            index += 1
            submit_command(transport, requests, feed_script(command, state_dir), "feed", log,
                           attempts=attempts)
            body, status, offset = collect(transport, requests, log, state_dir, offset)
            if body is None:
                submit_command(transport, requests, alive_script(pid or 0), "alive", log)
                raise ValueError(f"命令 {index} 没有返回完成标记：相机内 shell 可能已退出")
            # The output file is truncated after every command, so what arrives here is
            # exactly this command's output; parse_marker already removed the marker.
            output = body.rstrip("\n")
            report(f"$ {command}")
            for line in output.splitlines():
                report(f"  {line}")
            report(f"  [exit {status if status not in (None, '') else 'unknown'}]\n")
            results.append({"index": index, "command": command, "exit_status": status,
                            "output": output})
            report(f"  [已读入至偏移 {offset} 字节]")

        submit_command(transport, requests, exit_script(state_dir), "exit", log, attempts=attempts)
        record["shell_commands"] = results
        record["payload_ran"] = True
        record["mailbox_ok"] = True
        payload = json.dumps(results, indent=2, ensure_ascii=False).encode()
        if json_path:
            Path(json_path).write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
            uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
            if uid is not None and gid is not None and os.geteuid() == 0:
                try:
                    os.chown(json_path, int(uid), int(gid))
                except OSError:
                    pass
            report(f"结果已写入 {json_path}")
        return payload

    return driver
