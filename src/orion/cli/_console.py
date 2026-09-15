# =============================================================================
# cli/_console.py
# -----------------------------------------------------------------------------
# Responsible for: Making stdout/stderr UTF-8 capable before the CLI prints anything.
# Role in project: The first thing main() does; every command's status glyphs depend on it.
# Assumptions: Windows may hand the process a legacy-codepage stream; the reconfiguration is a
#              no-op elsewhere.
# =============================================================================
from __future__ import annotations


import sys


def _reconfigure_stream_utf8(stream) -> None:
    """Switch one text stream to UTF-8 output when it supports reconfiguration.

    Args:
        stream: A text stream, typically sys.stdout or sys.stderr.

    Returns:
        None. The stream is reconfigured in place when possible; otherwise this
        is a silent no-op.

    Why:
        On Windows, when output is redirected to a pipe or file, Python encodes
        with the locale ANSI code page (often cp1252), which cannot represent the
        status glyphs we print ("⚠", "✗") — printing one then raises
        UnicodeEncodeError and aborts the run. (To an interactive Windows console
        Python 3.6+ writes via WriteConsoleW, so the glyphs are already fine
        there; the crash is specifically the redirected case.) Forcing UTF-8
        removes that crash while keeping the glyphs on capable terminals. We guard
        two ways because neither should ever break the CLI: `reconfigure` is
        absent on non-TextIOWrapper streams (pytest's capture, embedded
        interpreters like IDLE), and on an unusual stream it can raise. A real end
        user always has a reconfigurable stream; the guards exist for those
        wrapped contexts.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (ValueError, OSError):
        # ValueError: the stream is in a state that forbids reconfiguration.
        # OSError: the underlying handle rejected the change. Either way, fall
        # back to the stream as-is rather than crash before any work is done.
        pass


def _ensure_utf8_output() -> None:
    """Make stdout and stderr emit UTF-8 so status glyphs never crash the CLI.

    Returns:
        None. Applies the UTF-8 reconfiguration to both standard streams.

    Why:
        Called once at the top of main(), so it covers every command and both
        invocations (the `orion` console script and `python -m orion`). Applied
        uniformly on all OSes — a harmless no-op where the stream is already UTF-8
        (macOS/Linux, and modern Windows terminals) — so there is no platform
        branch, matching the project's "cross-compat-minded, not one-OS-at-a-time"
        principle.
    """
    _reconfigure_stream_utf8(sys.stdout)
    _reconfigure_stream_utf8(sys.stderr)
