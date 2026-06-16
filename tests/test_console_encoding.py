# =============================================================================
# tests/test_console_encoding.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the console UTF-8 guard (cli._ensure_utf8_output and
#                  its per-stream helper) behaves correctly and, above all, never
#                  raises — including on the wrapped streams where it cannot act.
# Role in project: Phase 3.5 keeps the "⚠"/"✗" status glyphs but makes redirected
#                  output on Windows safe by switching the standard streams to
#                  UTF-8 at startup. Console encoding is the one genuinely new
#                  surface this pass introduces, so these are its safety tests:
#                  (a) it requests UTF-8 when the stream supports it, and (b) it
#                  is a silent no-op (never a crash) when the stream is a fake
#                  without reconfigure() or one that rejects the change — exactly
#                  the pytest-capture / embedded-interpreter cases the guard is for.
# =============================================================================

import sys

from orion import cli


class _StreamWithReconfigure:
    """A fake text stream that records the kwargs passed to reconfigure().

    Why:
        Stands in for a normal TextIOWrapper (sys.stdout/sys.stderr in a real
        run) so we can assert the guard asks for UTF-8 without touching the real
        process streams.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class _StreamWithoutReconfigure:
    """A fake stream that has no reconfigure() method.

    Why:
        Simulates the streams pytest's capture and embedded interpreters swap in,
        which are not TextIOWrapper and so lack reconfigure(). The guard must
        treat this as a no-op, not an AttributeError.
    """


def test_reconfigure_requests_utf8_when_supported():
    """A reconfigurable stream is asked for UTF-8 exactly once.

    Why:
        This is the happy path that prevents the redirected-output crash: the
        guard's whole job is to call reconfigure(encoding="utf-8").
    """
    stream = _StreamWithReconfigure()
    cli._reconfigure_stream_utf8(stream)
    assert stream.calls == [{"encoding": "utf-8"}]


def test_reconfigure_is_noop_without_method():
    """A stream lacking reconfigure() is left alone and does not raise.

    Why:
        Covers the pytest-capture / embedded-interpreter case. If this raised,
        every CLI invocation under those contexts would crash before doing work.
    """
    # No assertion needed beyond "did not raise" — reaching the next line is the
    # pass condition.
    cli._reconfigure_stream_utf8(_StreamWithoutReconfigure())


def test_reconfigure_swallows_stream_errors():
    """A stream whose reconfigure() raises is handled, not propagated.

    Why:
        An unusual stream can refuse reconfiguration (ValueError/OSError). The
        guard must degrade to "use the stream as-is" rather than abort the run.
    """

    class _Raises:
        def reconfigure(self, **kwargs):
            raise ValueError("stream is locked")

    # Must complete without raising.
    cli._reconfigure_stream_utf8(_Raises())


def test_reconfigure_swallows_oserror():
    """A stream whose reconfigure() raises OSError is also handled, not propagated.

    Why this matters: the guard catches (ValueError, OSError), but the other tests
    only exercise ValueError. An underlying OS handle can reject the encoding
    change with OSError; that must still degrade to "use the stream as-is" rather
    than abort the CLI before any work is done. This covers the second except arm.
    """

    class _RaisesOSError:
        def reconfigure(self, **kwargs):
            raise OSError("underlying handle rejected reconfigure")

    # Must complete without raising.
    cli._reconfigure_stream_utf8(_RaisesOSError())


def test_ensure_utf8_output_applies_to_both_streams(monkeypatch):
    """_ensure_utf8_output reconfigures BOTH stdout and stderr.

    Why:
        We print the "⚠" warning to stdout and the "✗" failure marker to stderr,
        so both must be made UTF-8 — missing either would leave one crash path
        open. We swap in fakes via monkeypatch (auto-restored after the test) so
        the real streams are untouched.
    """
    out = _StreamWithReconfigure()
    err = _StreamWithReconfigure()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    cli._ensure_utf8_output()

    assert out.calls == [{"encoding": "utf-8"}]
    assert err.calls == [{"encoding": "utf-8"}]
