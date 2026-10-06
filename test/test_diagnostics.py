# Copyright 2021- Python Language Server Contributors.

"""Tests for the diagnostic publish coordination layer.

Run with:

    python -m pytest test/test_diagnostics.py -v

Status model (see pylsp/diagnostics.py for the full description): each
document URI has a generation (bumped on every open), an open/closed flag
and the last published document version. Every analysis is bound to a
(generation, version) token via ``begin_analysis``; ``commit`` only
publishes while the token is still current. Late results are discarded,
never overwritten on top of newer diagnostics; closing a document emits a
single clear and cancels future publishes; analyzer errors are recorded
separately from empty diagnostic sets.

The concurrency tests use threading.Event barriers to deterministically
model a stuck (v1) analysis racing a fast (v2) one; no analyzer is ever
re-run to double-check a result.
"""

import threading

import pytest

from pylsp.diagnostics import DiagnosticsCoordinator

DOC_URI = "file:///test_doc.py"

V1_DIAGNOSTICS = [{"message": "v1"}]
V2_DIAGNOSTICS = [{"message": "v2"}]


@pytest.fixture()
def published():
    """Chronological log of (uri, diagnostics, version) publishes."""
    return []


@pytest.fixture()
def coordinator(published):
    return DiagnosticsCoordinator(
        lambda uri, diagnostics, version: published.append(
            (uri, list(diagnostics), version)
        )
    )


def outcomes(coordinator):
    return [record.outcome for record in coordinator.records]


def test_stuck_v1_only_v2_is_published(coordinator, published):
    """v1 analysis is stuck, v2 finishes first: only v2 may be published."""
    coordinator.document_opened(DOC_URI)

    v1_started = threading.Event()
    release_v1 = threading.Event()
    token_v1 = coordinator.begin_analysis(DOC_URI, version=1)

    def stuck_v1_analysis():
        v1_started.set()
        assert release_v1.wait(timeout=5)
        # The v1 result arrives late and must be discarded.
        return coordinator.commit(token_v1, diagnostics=V1_DIAGNOSTICS)

    thread = threading.Thread(target=stuck_v1_analysis)
    thread.start()
    assert v1_started.wait(timeout=5)

    # v2 analysis finishes first and wins.
    token_v2 = coordinator.begin_analysis(DOC_URI, version=2)
    assert coordinator.commit(token_v2, diagnostics=V2_DIAGNOSTICS)

    release_v1.set()
    thread.join(timeout=5)

    assert published == [(DOC_URI, V2_DIAGNOSTICS, 2)]
    assert outcomes(coordinator) == [
        DiagnosticsCoordinator.PUBLISHED,
        DiagnosticsCoordinator.DISCARDED_STALE,
    ]


def test_close_cancels_inflight_result_and_clears_at_most_once(coordinator, published):
    """Closing publishes one clear and invalidates old results."""
    coordinator.document_opened(DOC_URI)
    token = coordinator.begin_analysis(DOC_URI, version=1)

    assert coordinator.document_closed(DOC_URI)
    # The clear is sent at most once: repeated closes are no-ops.
    assert not coordinator.document_closed(DOC_URI)
    assert published == [(DOC_URI, [], None)]

    # The in-flight result from before the close is invalid now.
    assert not coordinator.commit(token, diagnostics=V1_DIAGNOSTICS)
    assert published == [(DOC_URI, [], None)]
    assert outcomes(coordinator) == [
        DiagnosticsCoordinator.CLEARED,
        DiagnosticsCoordinator.DISCARDED_CLOSED,
    ]


def test_reopen_same_uri_uses_new_generation(coordinator, published):
    """Reopening the same URI isolates it from old task results."""
    coordinator.document_opened(DOC_URI)
    old_token = coordinator.begin_analysis(DOC_URI, version=1)
    coordinator.document_closed(DOC_URI)
    published.clear()

    coordinator.document_opened(DOC_URI)
    # A result of an analysis started before the close cannot pollute the
    # reopened document.
    assert not coordinator.commit(old_token, diagnostics=V1_DIAGNOSTICS)
    assert published == []

    new_token = coordinator.begin_analysis(DOC_URI, version=1)
    assert coordinator.commit(new_token, diagnostics=V2_DIAGNOSTICS)
    assert published == [(DOC_URI, V2_DIAGNOSTICS, 1)]
    assert outcomes(coordinator) == [
        DiagnosticsCoordinator.CLEARED,
        DiagnosticsCoordinator.DISCARDED_CLOSED,
        DiagnosticsCoordinator.PUBLISHED,
    ]


def test_analyzer_error_recorded_separately_from_empty_diagnostics(
    coordinator, published
):
    """Analyzer errors and empty diagnostic sets are distinct records."""
    coordinator.document_opened(DOC_URI)

    error_token = coordinator.begin_analysis(DOC_URI, version=1)
    assert not coordinator.commit(error_token, error=RuntimeError("analyzer boom"))

    empty_token = coordinator.begin_analysis(DOC_URI, version=2)
    assert coordinator.commit(empty_token, diagnostics=[])

    error_records = [
        r for r in coordinator.records if r.outcome == DiagnosticsCoordinator.ERROR
    ]
    empty_records = [
        r
        for r in coordinator.records
        if r.outcome == DiagnosticsCoordinator.PUBLISHED and r.diagnostic_count == 0
    ]
    assert len(error_records) == 1
    assert "analyzer boom" in error_records[0].error
    assert len(empty_records) == 1
    # An analyzer error publishes nothing; the empty set is published.
    assert published == [(DOC_URI, [], 2)]


def test_status_snapshot(coordinator):
    """The coordinator exposes its state for inspection."""
    coordinator.document_opened(DOC_URI)
    token = coordinator.begin_analysis(DOC_URI, version=7)
    coordinator.commit(token, diagnostics=V1_DIAGNOSTICS)

    status = coordinator.status()
    assert status["generations"][DOC_URI] == 1
    assert status["open"] == [DOC_URI]
    assert status["last_versions"][DOC_URI] == 7
    assert [r.outcome for r in status["records"]] == [DiagnosticsCoordinator.PUBLISHED]


# -- Server-level integration (PythonLSPServer + coordinator) ----------------


@pytest.fixture()
def recording_coordinator(pylsp, published):
    """Give the server a coordinator with a recording publish callback."""
    coordinator = DiagnosticsCoordinator(
        lambda uri, diagnostics, version: published.append(
            (uri, list(diagnostics), version)
        )
    )
    pylsp._diagnostics_coordinator = coordinator
    return pylsp


def test_server_stuck_v1_only_v2_is_published(recording_coordinator, published):
    """v1 lint stuck in the analyzer, v2 lint finishes first: only v2 wins."""
    pylsp = recording_coordinator
    coordinator = pylsp._diagnostics_coordinator
    coordinator.document_opened(DOC_URI)

    v1_entered = threading.Event()
    release_v1 = threading.Event()
    calls = []

    def fake_analyzer(hook_name, doc_uri=None, **kwargs):
        calls.append(doc_uri)
        if len(calls) == 1:
            # The v1 analysis gets stuck inside the analyzer.
            v1_entered.set()
            assert release_v1.wait(timeout=5)
            return [V1_DIAGNOSTICS]
        return [V2_DIAGNOSTICS]

    pylsp._hook = fake_analyzer

    thread = threading.Thread(
        target=pylsp._lint_text_document, args=(DOC_URI, None, True, 1)
    )
    thread.start()
    assert v1_entered.wait(timeout=5)

    # The v2 lint completes while v1 is still stuck in the analyzer.
    pylsp._lint_text_document(DOC_URI, None, True, 2)

    release_v1.set()
    thread.join(timeout=5)

    assert published == [(DOC_URI, V2_DIAGNOSTICS, 2)]
    assert outcomes(coordinator) == [
        DiagnosticsCoordinator.PUBLISHED,
        DiagnosticsCoordinator.DISCARDED_STALE,
    ]


def test_server_close_cancels_inflight_and_reopen_isolated(
    recording_coordinator, published, monkeypatch
):
    """Close cancels an in-flight lint; reopening the same URI is isolated."""
    pylsp = recording_coordinator
    coordinator = pylsp._diagnostics_coordinator
    # Keep the test deterministic: drive linting manually.
    monkeypatch.setattr(pylsp, "lint", lambda *args, **kwargs: None)

    entered = threading.Event()
    release = threading.Event()

    def stuck_analyzer(hook_name, doc_uri=None, **kwargs):
        if hook_name != "pylsp_lint":
            return []
        entered.set()
        assert release.wait(timeout=5)
        return [V1_DIAGNOSTICS]

    pylsp._hook = stuck_analyzer

    pylsp.m_text_document__did_open(
        textDocument={"uri": DOC_URI, "text": "x = 1", "version": 1}
    )
    thread = threading.Thread(
        target=pylsp._lint_text_document, args=(DOC_URI, None, True, 1)
    )
    thread.start()
    assert entered.wait(timeout=5)

    # Closing publishes exactly one clear and cancels the in-flight lint.
    pylsp.m_text_document__did_close(textDocument={"uri": DOC_URI})
    assert published == [(DOC_URI, [], None)]

    release.set()
    thread.join(timeout=5)
    # The old in-flight result is invalid after the close: nothing more.
    assert published == [(DOC_URI, [], None)]
    assert outcomes(coordinator) == [
        DiagnosticsCoordinator.CLEARED,
        DiagnosticsCoordinator.DISCARDED_CLOSED,
    ]

    # Reopening the same URI starts a new generation; the old task's result
    # cannot pollute it and fresh lints publish normally.
    pylsp._hook = lambda hook_name, doc_uri=None, **kwargs: (
        [V2_DIAGNOSTICS] if hook_name == "pylsp_lint" else []
    )
    pylsp.m_text_document__did_open(
        textDocument={"uri": DOC_URI, "text": "x = 2", "version": 1}
    )
    pylsp._lint_text_document(DOC_URI, None, True, 1)
    assert published == [(DOC_URI, [], None), (DOC_URI, V2_DIAGNOSTICS, 1)]


def test_server_analyzer_error_recorded_separately(recording_coordinator, published):
    """An analyzer exception is recorded as an error, not as empty diagnostics."""
    pylsp = recording_coordinator
    coordinator = pylsp._diagnostics_coordinator
    coordinator.document_opened(DOC_URI)

    def boom(hook_name, doc_uri=None, **kwargs):
        raise RuntimeError("analyzer boom")

    pylsp._hook = boom
    with pytest.raises(RuntimeError, match="analyzer boom"):
        pylsp._lint_text_document(DOC_URI, None, True, 1)

    assert published == []
    assert outcomes(coordinator) == [DiagnosticsCoordinator.ERROR]
    assert "analyzer boom" in coordinator.records[0].error
