"""Cancel-watcher: detects Escape on stdin in a daemon thread and
signals agent loops to stop at the next safe point.

Active during `agent.run`; paused around any code that needs cooked-
mode stdin (the REPL prompt, the y/N skill activation, the y/n/a
permissions prompt). Posix only — on platforms without `termios`
the watcher is a no-op and Ctrl+C remains the way to interrupt.
"""

from __future__ import annotations

import sys
import threading

try:
    import select
    import termios
    import tty

    _SUPPORTED = True
except ImportError:  # Windows
    _SUPPORTED = False


class CancelWatcher:
    """Background Esc-key watcher with start/stop semantics.

    Call `start()` before the agent does work; `stop()` before any
    code that needs cooked-mode stdin. `cancel_event` is set when the
    user presses Esc standalone — agent loops should poll between
    safe points and return early. Multi-byte escape sequences from
    arrow keys / function keys are detected and ignored.
    """

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._saved_termios: list | None = None

    def start(self) -> None:
        if not _SUPPORTED or not sys.stdin.isatty():
            return
        if self._thread is not None:
            return
        fd = sys.stdin.fileno()
        try:
            self._saved_termios = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (termios.error, OSError):
            self._saved_termios = None
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=0.5)
        self._thread = None
        if self._saved_termios is not None:
            fd = sys.stdin.fileno()
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._saved_termios)
            except (termios.error, OSError):
                pass
            self._saved_termios = None

    def reset(self) -> None:
        self.cancel_event.clear()

    def _watch(self) -> None:
        while not self._stop_event.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not r:
                continue
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                return
            if ch != "\x1b":
                continue
            # Disambiguate standalone Esc from an escape sequence
            # (arrows / function keys send "\x1b[A" etc. instantly).
            follow, _, _ = select.select([sys.stdin], [], [], 0.05)
            if follow:
                # Drain the rest of the sequence and ignore.
                while True:
                    try:
                        sys.stdin.read(1)
                    except (OSError, ValueError):
                        return
                    more, _, _ = select.select([sys.stdin], [], [], 0.005)
                    if not more:
                        break
                continue
            self.cancel_event.set()
            return
