# Copyright 2021- Python Language Server Contributors.

"""Tests for the diagnostic publishing coordination layer.

The concurrency tests use ``threading.Event`` barriers to deterministically
reproduce the "slow v1 analysis vs fast v2 analysis" interleaving without
re-running any real analyzer: all assertions are made against the
coordinator's recorded history and the notifications it emitted.
"""

import threading

import pytest

from pylsp.diagnostic_coordinator import (
    DiagnosticCoordinator,
    PublishStatus,
    RecordKind,
)

DOC_URI = "file:///test/diag_coord.py"

DIAG_V1 = [{"message": "from-v1", "source": "test"}]
DIAG_V2 = [{"message": "from-v2", "source": "test"}]

BARRIER_TIMEOUT_S = 10


@pytest.fixture
def published():
    """Every (uri, diagnostics, version) notification sent to the client."""
    return []


@pytest.fixture
def coordinator(published):
    return DiagnosticCoordinator(
        publish=lambda uri, diagnostics, version: published.append(
            (uri, diagnostics, version)
        )
    )


def test_latest_analysis_wins(coordinator, published):
    coordinator.document_opened(DOC_URI)
    ticket_v1 = coordinator.begin_analysis(DOC_URI, 1)
    ticket_v2 = coordinator.begin_analysis(DOC_URI, 2)

    # v2 finishes first and is published.
    assert coordinator.commit(ticket_v2, DIAG_V2) is PublishStatus.PUBLISHED
    # The late v1 result must be discarded, never overwriting v2.
    assert (
        coordinator.commit(ticket_v1, DIAG_V1) is PublishStatus.DISCARDED_STALE_VERSION
    )

    assert published == [(DOC_URI, DIAG_V2, 2)]


def test_slow_v1_never_overwrites_v2_with_event_barrier(coordinator, published):
    """v1 analysis is stuck while v2 completes: only v2 may be published."""
    coordinator.document_opened(DOC_URI)
    ticket_v1 = coordinator.begin_analysis(DOC_URI, 1)
    ticket_v2 = coordinator.begin_analysis(DOC_URI, 2)

    v1_analysis_started = threading.Event()
    v1_may_finish = threading.Event()
    v2_committed = threading.Event()
    results = {}

    def slow_analyzer_v1():
        # Simulates an analyzer that gets stuck mid-run.
        v1_analysis_started.set()
        assert v1_may_finish.wait(timeout=BARRIER_TIMEOUT_S)
        results["v1"] = coordinator.commit(ticket_v1, DIAG_V1)

    def fast_analyzer_v2():
        # Only finish once v1 is known to be stuck in-flight.
        assert v1_analysis_started.wait(timeout=BARRIER_TIMEOUT_S)
        results["v2"] = coordinator.commit(ticket_v2, DIAG_V2)
        v2_committed.set()

    thread_v1 = threading.Thread(target=slow_analyzer_v1)
    thread_v2 = threading.Thread(target=fast_analyzer_v2)
    thread_v1.start()
    thread_v2.start()

    try:
        assert v2_committed.wait(timeout=BARRIER_TIMEOUT_S)
        # v1 is still stuck; only v2 has been published so far.
        assert published == [(DOC_URI, DIAG_V2, 2)]
    finally:
        v1_may_finish.set()
    thread_v1.join(timeout=BARRIER_TIMEOUT_S)
    thread_v2.join(timeout=BARRIER_TIMEOUT_S)

    assert results["v2"] is PublishStatus.PUBLISHED
    assert results["v1"] is PublishStatus.DISCARDED_STALE_VERSION
    # The late v1 result was discarded; the client still only saw v2.
    assert published == [(DOC_URI, DIAG_V2, 2)]

    # The discard is visible in the recorded history (no analyzer re-run).
    records = coordinator.records(DOC_URI)
    assert [(r.kind, r.status) for r in records] == [
        (RecordKind.DIAGNOSTICS, PublishStatus.PUBLISHED),
        (RecordKind.DIAGNOSTICS, PublishStatus.DISCARDED_STALE_VERSION),
    ]


def test_close_cancels_pending_and_clears_once(coordinator, published):
    coordinator.document_opened(DOC_URI)
    ticket = coordinator.begin_analysis(DOC_URI, 1)
    assert coordinator.commit(ticket, DIAG_V1) is PublishStatus.PUBLISHED

    pending_ticket = coordinator.begin_analysis(DOC_URI, 2)
    coordinator.document_closed(DOC_URI)

    # Exactly one clear was sent for the close.
    assert published == [(DOC_URI, DIAG_V1, 1), (DOC_URI, [], None)]

    # Results from the cancelled analysis are discarded, not published.
    assert coordinator.commit(pending_ticket, DIAG_V2) is PublishStatus.DISCARDED_CLOSED
    assert published == [(DOC_URI, DIAG_V1, 1), (DOC_URI, [], None)]

    # A duplicate close must not clear again: at most one clear per close.
    coordinator.document_closed(DOC_URI)
    assert published == [(DOC_URI, DIAG_V1, 1), (DOC_URI, [], None)]

    status = coordinator.status(DOC_URI)
    assert status["is_open"] is False
    assert status["pending_analyses"] == 0
    assert status["has_visible_diagnostics"] is False


def test_close_without_published_diagnostics_sends_no_clear(coordinator, published):
    coordinator.document_opened(DOC_URI)
    coordinator.document_closed(DOC_URI)
    assert published == []


def test_reopen_same_uri_uses_new_generation(coordinator, published):
    coordinator.document_opened(DOC_URI)
    stale_ticket = coordinator.begin_analysis(DOC_URI, 1)
    coordinator.document_closed(DOC_URI)

    generation = coordinator.document_opened(DOC_URI)
    assert generation == 2
    fresh_ticket = coordinator.begin_analysis(DOC_URI, 1)

    # A result from the previous open/close cycle cannot pollute the new one.
    assert (
        coordinator.commit(stale_ticket, DIAG_V1)
        is PublishStatus.DISCARDED_STALE_GENERATION
    )
    assert coordinator.commit(fresh_ticket, DIAG_V2) is PublishStatus.PUBLISHED
    assert published == [(DOC_URI, DIAG_V2, 1)]

    assert coordinator.status(DOC_URI)["generation"] == 2


def test_error_and_empty_diagnostics_recorded_separately(coordinator, published):
    coordinator.document_opened(DOC_URI)

    error_ticket = coordinator.begin_analysis(DOC_URI, 1)
    boom = RuntimeError("analyzer exploded")
    assert coordinator.commit_error(error_ticket, boom) is (
        PublishStatus.ERROR_RECORDED
    )

    empty_ticket = coordinator.begin_analysis(DOC_URI, 2)
    assert coordinator.commit(empty_ticket, []) is PublishStatus.PUBLISHED

    # The error did not publish anything; the empty result published a clear.
    assert published == [(DOC_URI, [], 2)]

    records = coordinator.records(DOC_URI)
    assert [(r.kind, r.status) for r in records] == [
        (RecordKind.ERROR, PublishStatus.ERROR_RECORDED),
        (RecordKind.EMPTY, PublishStatus.PUBLISHED),
    ]
    assert "analyzer exploded" in records[0].detail
    # An error record is distinct from an empty-diagnostics record.
    assert records[0].kind is not records[1].kind


def test_error_from_stale_ticket_is_recorded_as_discarded(coordinator, published):
    coordinator.document_opened(DOC_URI)
    stale_ticket = coordinator.begin_analysis(DOC_URI, 1)
    coordinator.document_closed(DOC_URI)

    assert (
        coordinator.commit_error(stale_ticket, RuntimeError("late failure"))
        is PublishStatus.DISCARDED_CLOSED
    )
    assert published == []
    last = coordinator.last_record(DOC_URI)
    assert last.kind is RecordKind.ERROR
    assert last.status is PublishStatus.DISCARDED_CLOSED


def test_concurrent_commits_are_thread_safe(coordinator, published):
    coordinator.document_opened(DOC_URI)
    tickets = [coordinator.begin_analysis(DOC_URI, i) for i in range(8)]

    def commit(ticket):
        return coordinator.commit(ticket, DIAG_V1)

    threads = [threading.Thread(target=commit, args=(t,)) for t in tickets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=BARRIER_TIMEOUT_S)

    # Only the last begun ticket may publish; every other commit is discarded.
    assert published == [(DOC_URI, DIAG_V1, 7)]
    records = coordinator.records(DOC_URI)
    assert sum(r.status is PublishStatus.PUBLISHED for r in records) == 1
    assert sum(r.status is PublishStatus.DISCARDED_STALE_VERSION for r in records) == 7


def test_workspace_close_publishes_single_clear(workspace):
    """The workspace routes didClose through the coordination layer."""
    from unittest.mock import Mock

    workspace._endpoint.notify = Mock()
    coordinator = workspace.diagnostic_coordinator

    coordinator.document_opened(DOC_URI)
    ticket = coordinator.begin_analysis(DOC_URI, 3)
    assert coordinator.commit(ticket, DIAG_V1) is PublishStatus.PUBLISHED

    workspace._endpoint.notify.reset_mock()
    coordinator.document_closed(DOC_URI)

    # One single clear notification was emitted for the close.
    assert workspace._endpoint.notify.call_count == 1
    notify_call = workspace._endpoint.notify.call_args
    assert notify_call[0][0] == workspace.M_PUBLISH_DIAGNOSTICS
    assert notify_call[1]["params"] == {"uri": DOC_URI, "diagnostics": []}


def test_server_routes_open_close_through_coordinator(pylsp):
    workspace = pylsp.workspace
    coordinator = workspace.diagnostic_coordinator

    pylsp.m_text_document__did_open(
        textDocument={
            "uri": DOC_URI,
            "languageId": "python",
            "version": 1,
            "text": "x = 1\n",
        }
    )
    status = coordinator.status(DOC_URI)
    assert status["is_open"] is True
    assert status["generation"] == 1

    pylsp.m_text_document__did_close(textDocument={"uri": DOC_URI})
    status = coordinator.status(DOC_URI)
    assert status["is_open"] is False

    # Re-opening the same URI starts a new, isolated generation.
    pylsp.m_text_document__did_open(
        textDocument={
            "uri": DOC_URI,
            "languageId": "python",
            "version": 1,
            "text": "x = 2\n",
        }
    )
    assert coordinator.status(DOC_URI)["generation"] == 2
    pylsp.m_text_document__did_close(textDocument={"uri": DOC_URI})


def test_server_lint_late_v1_result_discarded(pylsp):
    """End-to-end through ``_lint_text_document`` with a fake analyzer.

    The v1 lint is stuck behind an Event barrier while the v2 lint completes;
    only v2 diagnostics may reach the client. Verification uses the
    coordinator's records, not a re-run of the analyzer.
    """
    workspace = pylsp.workspace
    coordinator = workspace.diagnostic_coordinator
    notifications = []
    workspace._endpoint.notify = lambda method, params=None: notifications.append(
        (method, params)
    )

    workspace.put_document(DOC_URI, "x = 1\n", version=1)
    coordinator.document_opened(DOC_URI)

    v1_analysis_started = threading.Event()
    v1_may_finish = threading.Event()
    hook_calls = []

    def fake_hook(hook_name, doc_uri, **_kwargs):
        assert hook_name == "pylsp_lint"
        hook_calls.append(doc_uri)
        if len(hook_calls) == 1:
            # The v1 analyzer gets stuck mid-run.
            v1_analysis_started.set()
            assert v1_may_finish.wait(timeout=BARRIER_TIMEOUT_S)
            return [DIAG_V1]
        return [DIAG_V2]

    pylsp._hook = fake_hook

    stuck_v1 = threading.Thread(
        target=pylsp._lint_text_document, args=(DOC_URI, workspace, False, 1)
    )
    stuck_v1.start()
    assert v1_analysis_started.wait(timeout=BARRIER_TIMEOUT_S)
    try:
        # The v2 analysis runs to completion while v1 is still stuck.
        pylsp._lint_text_document(DOC_URI, workspace, False, 2)
    finally:
        v1_may_finish.set()
    stuck_v1.join(timeout=BARRIER_TIMEOUT_S)

    # Only the v2 diagnostics were published; the late v1 result was dropped.
    assert notifications == [
        (
            workspace.M_PUBLISH_DIAGNOSTICS,
            {"uri": DOC_URI, "diagnostics": DIAG_V2, "version": 2},
        )
    ]
    records = coordinator.records(DOC_URI)
    assert [(r.kind, r.status) for r in records] == [
        (RecordKind.DIAGNOSTICS, PublishStatus.PUBLISHED),
        (RecordKind.DIAGNOSTICS, PublishStatus.DISCARDED_STALE_VERSION),
    ]
