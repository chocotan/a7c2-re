"""Run arbitrary commands inside the camera through one service session.

The deployed shell payload exposes a mailbox: the host writes a command, the worker
runs it with `/bin/sh -c`, and the reply carries the command's output plus its exit
status. That already permits any command, but a session is expensive — the camera is
armed at the start and left needing a physical reconnect at the end — so this driver
runs *many* commands through one session instead of one command per session.

A reply has this shape (see `a7c2_shell.c`):

    A7C2-OUT-1
    CMD echo A7C2-c001; id
    A7C2-c001
    uid=0(root) gid=0(root)
    EXIT 0
    A7C2-END

Results go to stderr because the service CLI prints its JSON event stream on stdout;
keeping the two apart means `--run-command` can be used in a pipeline without the
command output being buried in protocol events.
"""
import json
import os
import sys
from pathlib import Path

from jiritsu_preload_deploy import READ_ATTEMPTS, submit_command

MARKER_PREFIX = "A7C2-"
END_MARKER = "A7C2-END"
EXIT_PREFIX = "EXIT "
DEFAULT_ATTEMPTS = READ_ATTEMPTS


def parse_reply(raw, marker):
    """Split a worker reply into (output, exit_status, error).

    The trailer is found from the end: a command may legitimately print something
    that looks like `EXIT 3` or even `A7C2-END` on its own, so only the final
    trailer lines are removed.
    """
    text = raw.decode("ascii", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    lines = text.splitlines()
    start = None
    token = f"{MARKER_PREFIX}{marker}"
    for index, line in enumerate(lines):
        if line.strip() == token:
            start = index + 1
            break
    if start is None:
        return None, None, f"reply carries no {token} line"

    body = lines[start:]
    end = len(body)
    for index in range(len(body) - 1, -1, -1):
        if body[index].strip() == END_MARKER:
            end = index
            break
    trailer = body[:end]

    exit_status = None
    if trailer and trailer[-1].strip().startswith(EXIT_PREFIX):
        tail = trailer[-1].strip()[len(EXIT_PREFIX):].strip()
        trailer = trailer[:-1]
        exit_status = None if tail == "unknown" else int(tail) if tail.isdigit() else None
    return "\n".join(trailer), exit_status, None


def _stderr(text=""):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def command_runner_driver(commands=None, interactive=False, json_path=None,
                          attempts=DEFAULT_ATTEMPTS, out_stream=None):
    """Build a deploy driver that runs the given commands, or reads them interactively."""
    commands = list(commands or ())
    if not commands and not interactive:
        raise ValueError("provide at least one command or enable interactive mode")
    emit = out_stream.write if out_stream is not None else None

    def report(text):
        if emit is not None:
            emit(text)
        else:
            _stderr(text)

    def read_command(index):
        report(f"a7c2[{index}]> ")
        line = sys.stdin.readline()
        if not line:
            report("(标准输入结束，停止)")
            return None
        text = line.strip()
        if text in ("", "exit", "quit"):
            return None
        return text

    def driver(transport, requests, log, record):
        pending = list(commands)
        results = []
        index = 0
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
            marker = f"c{index:03d}"
            log("command_sending", index=index, command=command[:160])
            raw = submit_command(transport, requests, command, marker, log, attempts=attempts)
            output, exit_status, error = parse_reply(raw, marker)
            if error:
                raise ValueError(f"command {index} ({command[:60]!r}) failed: {error}")
            results.append({"index": index, "command": command, "exit_status": exit_status,
                            "output": output})
            report(f"$ {command}")
            report(output if output else "(无输出)")
            report(f"[exit {exit_status if exit_status is not None else 'unknown'}]\n")
        record["commands"] = results
        record["payload_ran"] = True
        record["mailbox_ok"] = True
        payload = json.dumps(results, indent=2, ensure_ascii=False).encode()
        if json_path:
            Path(json_path).write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
            hand_back(json_path)
            report(f"结果已写入 {json_path}")
        return payload

    return driver


def hand_back(path):
    """Give a root-created result file back to the invoking user.

    The session runs under sudo, so `--json` would otherwise leave a file the
    operator cannot read — the same trap as reading any other root-owned artifact
    afterwards.
    """
    target = Path(path)
    if not target.exists():
        return None
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None or gid is None or os.geteuid() != 0:
        return None
    try:
        os.chown(target, int(uid), int(gid))
    except OSError:
        return None
    return target
