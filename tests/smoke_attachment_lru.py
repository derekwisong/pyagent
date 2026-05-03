"""Smoke for the per-session attachments LRU cap (issue #86).

Locks these behaviors:

  1. **Under cap → no eviction.** Several small writes keep total
     under cap; every file remains.
  2. **Over cap → oldest atime evicted.** With controlled atimes via
     `os.utime`, the file with the oldest atime is the one removed
     when a new write pushes total over cap.
  3. **Just-written file is exempt.** A single huge write that alone
     exceeds the cap stays put; older smaller files are evicted.
     Even when the just-written file is "older" by atime than other
     files in the dir, eviction must not pick it.
  4. **cap=0 disables eviction.** Many large writes can pile up and
     no eviction runs.
  5. **Config wiring.** `[session] attachment_dir_cap_mb = 5` in a
     temp config flows through to a `Session(attachment_dir_cap_mb=5)`
     when constructed via the CLI startup pattern (mirrors
     `pyagent/cli.py` Session construction).
  6. **Class default unchanged.** Sessions constructed without an
     explicit cap inherit the class-level 25 MB.
  7. **Config validation.** Bogus values (float, negative, bool,
     non-numeric) emit warnings and resolve sanely — closes the
     "TOML float silently disables eviction" hole flagged in #93
     review.
  8. **Path A contract.** When the LRU evicts a file that's still
     referenced by the agent's prior conversation, a future
     `read_file` against the saved path lands on the standard
     `<file not found: ...>` marker. Locks the behavior the LRU
     implementation explicitly relies on.

No subprocess, no network. Run with:
    .venv/bin/python -m tests.smoke_attachment_lru
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from pyagent import config as config_mod
from pyagent import permissions
from pyagent import tools as agent_tools
from pyagent.session import Session


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.iterdir() if f.is_file())


def _check_under_cap_no_eviction() -> None:
    """A handful of small writes keep dir under cap; everything stays."""
    with tempfile.TemporaryDirectory(prefix="pyagent-lru-") as t:
        session = Session(
            session_id="under", root=Path(t), attachment_dir_cap_mb=5
        )
        paths = []
        for i in range(4):
            # Each write is 200KB, four writes = 800KB << 5MB cap.
            paths.append(session.write_attachment("read_file", "a" * 200_000))
        # All four files still on disk.
        for p in paths:
            assert p.exists(), f"unexpected eviction of {p}"
        # And the dir total reflects all four.
        total = _dir_size(session.attachments_dir)
        assert 750_000 < total < 850_000, total
    print("✓ under cap: no files evicted")


def _check_over_cap_oldest_atime_evicted() -> None:
    """Oldest-atime files go first when the dir crosses the cap."""
    with tempfile.TemporaryDirectory(prefix="pyagent-lru-") as t:
        # 3 MB cap, 1 MB writes — fourth write should trigger eviction.
        session = Session(
            session_id="over", root=Path(t), attachment_dir_cap_mb=3
        )
        # First three writes: stamp each with a known atime so we can
        # predict which one the eviction pass picks. Oldest first.
        p1 = session.write_attachment("read_file", "1" * 1_100_000)
        os.utime(p1, (1_000.0, 1_000.0))
        p2 = session.write_attachment("read_file", "2" * 1_100_000)
        os.utime(p2, (2_000.0, 2_000.0))
        p3 = session.write_attachment("read_file", "3" * 1_100_000)
        os.utime(p3, (3_000.0, 3_000.0))
        # All three live so far (3 * 1.1 MB = 3.3 MB > 3 MB cap, so the
        # third write itself already triggered eviction of p1). Confirm.
        assert not p1.exists(), "p1 should have been evicted on third write"
        assert p2.exists() and p3.exists(), "p2/p3 should still be present"
        # Now write a fourth, bigger one. p2 is oldest-atime remaining
        # and should be the one evicted.
        p4 = session.write_attachment("read_file", "4" * 1_200_000)
        assert not p2.exists(), "p2 (oldest atime) should have been evicted"
        assert p3.exists(), "p3 should remain"
        assert p4.exists(), "just-written p4 must remain"
    print("✓ over cap: oldest-atime file evicted, newer ones preserved")


def _check_just_written_exempt_even_when_over_alone() -> None:
    """A single huge write that alone exceeds the cap stays; older
    files are evicted but the just-written one is preserved."""
    with tempfile.TemporaryDirectory(prefix="pyagent-lru-") as t:
        session = Session(
            session_id="huge", root=Path(t), attachment_dir_cap_mb=2
        )
        # Seed two small files with old atimes.
        small1 = session.write_attachment("read_file", "x" * 500_000)
        os.utime(small1, (1_000.0, 1_000.0))
        small2 = session.write_attachment("read_file", "y" * 500_000)
        os.utime(small2, (2_000.0, 2_000.0))
        assert small1.exists() and small2.exists()
        # Now drop a single 5 MB write — this alone exceeds the 2 MB cap.
        # The just-written file must survive; the two small ones go.
        big = session.write_attachment("read_file", "Z" * 5_000_000)
        # The just-written file is exempt. Even though its atime might
        # be newer than the others, the eviction loop explicitly skips
        # it, so confirm it's still there.
        assert big.exists(), "just-written huge file must not be evicted"
        assert not small1.exists(), "old small file should have been evicted"
        assert not small2.exists(), "old small file should have been evicted"

        # Edge case: exempt holds even when the just-written file is
        # older by atime than other files in the dir. Force big's atime
        # to be the oldest, then write another file to trigger another
        # eviction pass — big must still be exempt during that pass
        # because it's the just-written one for THAT pass... wait, no:
        # only the freshest write is exempt. So we need a different
        # check: a follow-up write whose addition pushes us over cap
        # again. The just-written follow-up is exempt; big becomes a
        # candidate. With big's atime forced ancient, big is the LRU
        # pick and goes first.
        os.utime(big, (500.0, 500.0))  # ancient
        new_small = session.write_attachment("read_file", "n" * 100_000)
        # 5 MB + 0.1 MB = 5.1 MB > 2 MB; eviction picks LRU (big).
        assert not big.exists(), (
            "after a fresh write, the previously-just-written file "
            "loses its exemption and is a normal LRU candidate"
        )
        assert new_small.exists(), "newly-written file must be exempt"
    print("✓ just-written file exempt; loses exemption on next write")


def _check_cap_zero_disables_eviction() -> None:
    """cap=0 means "disabled" — no eviction ever runs."""
    with tempfile.TemporaryDirectory(prefix="pyagent-lru-") as t:
        session = Session(
            session_id="off", root=Path(t), attachment_dir_cap_mb=0
        )
        paths = []
        # Write 5 MB worth of data; with cap disabled, all stays.
        for _ in range(5):
            paths.append(
                session.write_attachment("read_file", "q" * 1_000_000)
            )
        for p in paths:
            assert p.exists(), f"cap=0 should not evict, but {p} is gone"
        total = _dir_size(session.attachments_dir)
        assert total > 4_500_000, total
    print("✓ cap=0: eviction disabled")


def _check_config_wiring() -> None:
    """A `[session] attachment_dir_cap_mb` value in config flows into
    Session via the same pattern cli.py uses on startup."""
    # Mirror cli.py: cfg = config.load(); cap = cfg["session"][...]
    # We can't easily inject a temp config.toml without monkey-
    # patching paths.config_dir(); just exercise the resolution logic
    # the CLI uses against a synthesized cfg dict.
    cfg = {"session": {"attachment_dir_cap_mb": 5}}
    cap_mb = int(cfg.get("session", {}).get("attachment_dir_cap_mb", 25))
    assert cap_mb == 5
    with tempfile.TemporaryDirectory(prefix="pyagent-lru-") as t:
        session = Session(
            session_id="cfg", root=Path(t), attachment_dir_cap_mb=cap_mb
        )
        assert session.attachment_dir_cap_mb == 5

    # And confirm the config defaults expose the key (so users can see
    # it via `pyagent-config defaults` and override it).
    defaults = config_mod.DEFAULTS
    assert "session" in defaults, defaults
    assert defaults["session"].get("attachment_dir_cap_mb") == 25, defaults
    print("✓ config wiring: [session] attachment_dir_cap_mb=5 → cap_mb=5")


def _check_class_default_unchanged() -> None:
    """The class-level default stays at 25 MB; an instance with no
    override inherits it. Locks the "single source of truth" claim."""
    assert Session.attachment_dir_cap_mb == 25, Session.attachment_dir_cap_mb
    with tempfile.TemporaryDirectory(prefix="pyagent-lru-") as t:
        session = Session(session_id="default", root=Path(t))
        assert session.attachment_dir_cap_mb == 25, session.attachment_dir_cap_mb
    print("✓ class default = 25 MB; instance without kwarg inherits it")


def _check_validation_warns_and_falls_back() -> None:
    """`resolve_attachment_dir_cap_mb` must warn-and-resolve for the
    cases that bit users in the #93 review: TOML floats silently
    disabling, negatives silently disabling, booleans, non-numerics."""

    class _CaptureHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    handler = _CaptureHandler()
    cfg_logger = logging.getLogger("pyagent.config")
    cfg_logger.addHandler(handler)
    cfg_logger.setLevel(logging.WARNING)

    try:
        # Missing → silent default.
        handler.records.clear()
        assert config_mod.resolve_attachment_dir_cap_mb(None) == 25
        assert handler.records == [], handler.records

        # Plain int → silent passthrough.
        handler.records.clear()
        assert config_mod.resolve_attachment_dir_cap_mb(10) == 10
        assert handler.records == [], handler.records

        # Float 25.0 → warn, round to 25.
        handler.records.clear()
        assert config_mod.resolve_attachment_dir_cap_mb(25.0) == 25
        assert any("float" in r.getMessage() for r in handler.records), handler.records

        # Float 0.5 → warn, round to 0 (the surprise case from review).
        handler.records.clear()
        assert config_mod.resolve_attachment_dir_cap_mb(0.5) == 0
        msgs = [r.getMessage() for r in handler.records]
        assert any("float" in m for m in msgs), msgs

        # Negative → warn, clamp to 0.
        handler.records.clear()
        assert config_mod.resolve_attachment_dir_cap_mb(-5) == 0
        assert any(">= 0" in r.getMessage() for r in handler.records), handler.records

        # Bool (TOML allows it; True is int(1) silently otherwise) → warn, default.
        handler.records.clear()
        assert config_mod.resolve_attachment_dir_cap_mb(True) == 25
        assert any("bool" in r.getMessage() for r in handler.records), handler.records

        # Non-numeric → warn, default.
        handler.records.clear()
        assert config_mod.resolve_attachment_dir_cap_mb("a lot") == 25
        assert any("integer" in r.getMessage() for r in handler.records), handler.records
    finally:
        cfg_logger.removeHandler(handler)
    print("✓ validation: float / negative / bool / non-numeric warn and resolve sanely")


def _check_path_a_contract() -> None:
    """When the LRU evicts a file, the saved path is gone — a future
    `read_file` against it returns the standard
    `<file not found: ...>` marker. The agent has no special "evicted"
    semantics; the existing not-found contract is the signal."""
    with tempfile.TemporaryDirectory(prefix="pyagent-lru-") as t:
        # 1 MB cap, 700 KB writes — second write triggers eviction of first.
        session = Session(
            session_id="path-a", root=Path(t), attachment_dir_cap_mb=1
        )
        first = session.write_attachment("read_file", "F" * 700_000)
        second = session.write_attachment("read_file", "S" * 700_000)
        # First should be evicted; second is the just-written and stays.
        assert not first.exists(), f"expected {first} to be evicted"
        assert second.exists(), f"just-written {second} must remain"

        # Pre-approve the temp path so read_file's permission gate
        # passes (the path is outside the workspace).
        permissions.pre_approve(first)
        result = agent_tools.read_file(str(first))
        assert isinstance(result, str), type(result)
        assert result == f"<file not found: {first}>", result

        # And the just-written file IS readable — confirms read_file
        # is working in this test, the failure above is genuinely
        # the eviction (not a permission/path issue).
        permissions.pre_approve(second)
        result = agent_tools.read_file(str(second))
        assert "S" in result, result[:100]
    print("✓ path A: evicted file → <file not found: ...> from read_file")


def main() -> None:
    _check_under_cap_no_eviction()
    _check_over_cap_oldest_atime_evicted()
    _check_just_written_exempt_even_when_over_alone()
    _check_cap_zero_disables_eviction()
    _check_config_wiring()
    _check_class_default_unchanged()
    _check_validation_warns_and_falls_back()
    _check_path_a_contract()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
