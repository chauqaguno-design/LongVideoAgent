"""Run one local media tool, cancelling the process and draining its pipes before returning."""

from pathlib import Path
import subprocess
import time


class MediaStopped(InterruptedError):
    """A job lost ownership or its worker is shutting down."""


def check_running(stopped):
    if stopped is not None and stopped.is_set():
        raise MediaStopped("媒体处理已中止")


def command(args, stopped=None, *, cwd=None, timeout=900):
    check_running(stopped)
    with subprocess.Popen(
        [str(a) for a in args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_running(stopped)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    out, err = process.communicate(timeout=min(0.25, left) if stopped is not None else left)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.kill()
            process.communicate()
            raise
        if process.returncode:
            raise ValueError(f"{Path(args[0]).name}: {err.decode('utf8', 'replace').strip()[-1000:]}")
        check_running(stopped)
        return out, err
