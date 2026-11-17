"""watch pushes through the configured backend and retries failed pushes."""

from cursor_saves import watch


class FakeBackend:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def is_initialized(self):
        return True

    def push(self, snapshots_dir, message=None):
        self.calls.append(message)
        return self.results.pop(0)


def run_ticks(monkeypatch, backend, fingerprints, saved_per_change, push=True):
    fps = iter(fingerprints)
    ticks = {"n": len(fingerprints) - 1}

    def fake_sleep(_):
        ticks["n"] -= 1
        if ticks["n"] < 0:
            raise KeyboardInterrupt

    monkeypatch.setattr(watch, "get_backend", lambda: backend)
    monkeypatch.setattr(watch, "_get_db_fingerprint", lambda p: next(fps))
    monkeypatch.setattr(watch.export, "checkpoint_project", lambda p: saved_per_change.pop(0))
    monkeypatch.setattr(watch.time, "sleep", fake_sleep)
    monkeypatch.setattr(watch.signal, "signal", lambda *a: None)
    try:
        watch.watch_loop("/tmp/proj", interval=0, push=push)
    except KeyboardInterrupt:
        pass


def test_push_after_checkpoint_and_retry_on_failure(monkeypatch):
    backend = FakeBackend([False, True])
    # initial fingerprint, one change, then two quiet ticks
    run_ticks(monkeypatch, backend, ["a", "b", "b", "b"], [["c1"]])
    assert len(backend.calls) == 2  # failed, then retried without a new change
    assert all("checkpoint proj" in m for m in backend.calls)


def test_no_push_when_nothing_saved(monkeypatch):
    backend = FakeBackend([])
    run_ticks(monkeypatch, backend, ["a", "b", "b"], [[]])
    assert backend.calls == []


def test_no_push_flag(monkeypatch):
    backend = FakeBackend([])
    run_ticks(monkeypatch, backend, ["a", "b"], [["c1"]], push=False)
    assert backend.calls == []
