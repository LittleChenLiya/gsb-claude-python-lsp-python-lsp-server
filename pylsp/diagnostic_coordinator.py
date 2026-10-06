# Copyright 2021- Python Language Server Contributors.

"""Diagnostic publishing coordination layer.

This module owns the lifecycle of ``textDocument/publishDiagnostics``
notifications so that asynchronous lint/analyzer results can never clobber
newer state:

* Every analysis run is bound to the document version it started from via an
  :class:`AnalysisTicket` (``begin_analysis``). Only the most recently begun
  ticket for the current generation may publish; late results are discarded
  instead of overwriting newer-version diagnostics.
* Closing a document publishes a single clear (empty diagnostics, at most
  once per close) and cancels all future publishing for outstanding tickets.
* Re-opening the same URI starts a new generation, so results produced by
  tasks belonging to a previous open/close cycle are discarded and cannot
  pollute the new session.
* Analyzer errors and empty diagnostic sets are recorded separately
  (:class:`RecordKind`), so an analyzer failure is never confused with a
  successful run that happened to find nothing.

The recorded history doubles as the authoritative status of what was (or was
not) published: callers can inspect ``DiagnosticCoordinator.records`` /
``DiagnosticCoordinator.status`` instead of re-running the analyzer to
reconcile state.
"""

import enum
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# Keep the per-document history bounded; records exist for status inspection
# and debugging, not as an audit log.
MAX_RECORDS_PER_DOCUMENT = 50


class PublishStatus(enum.Enum):
    """Outcome of committing an analysis result."""

    #: The result was current and has been sent to the client.
    PUBLISHED = "published"
    #: A newer analysis was begun for this document; the late result was
    #: discarded instead of overwriting newer-version diagnostics.
    DISCARDED_STALE_VERSION = "discarded-stale-version"
    #: The document was closed (and possibly re-opened) since the analysis
    #: started; the result belongs to a dead generation.
    DISCARDED_STALE_GENERATION = "discarded-stale-generation"
    #: The document is closed; publishing for it has been cancelled.
    DISCARDED_CLOSED = "discarded-closed"
    #: The analyzer raised; the error was recorded and nothing was published.
    ERROR_RECORDED = "error-recorded"


class RecordKind(enum.Enum):
    """What a committed analysis result represented."""

    #: A non-empty diagnostic set.
    DIAGNOSTICS = "diagnostics"
    #: A successful analysis that produced no diagnostics.
    EMPTY = "empty"
    #: An analyzer failure (recorded separately from empty diagnostics).
    ERROR = "error"
    #: A clear (empty diagnostics) sent because the document was closed.
    CLEARED = "cleared"


@dataclass(frozen=True)
class AnalysisTicket:
    """Binds one analysis run to a document generation, sequence and version."""

    uri: str
    generation: int
    sequence: int
    version: Optional[int]


@dataclass
class DiagnosticRecord:
    """One entry in the per-document publish history."""

    uri: str
    generation: int
    sequence: int
    version: Optional[int]
    kind: RecordKind
    status: PublishStatus
    detail: Optional[str] = None


@dataclass
class _DocumentState:
    generation: int
    is_open: bool = True
    latest_sequence: int = 0
    pending: set = field(default_factory=set)
    has_visible_diagnostics: bool = False


class DiagnosticCoordinator:
    """Coordinates diagnostic publishing for all documents of a workspace.

    Parameters
    ----------
    publish:
        Callable invoked as ``publish(uri, diagnostics, version)`` whenever a
        notification may actually be sent to the client.
    """

    def __init__(self, publish: Callable[[str, List, Optional[int]], None]) -> None:
        self._publish = publish
        self._lock = threading.RLock()
        self._states: Dict[str, _DocumentState] = {}
        self._records: Dict[str, List[DiagnosticRecord]] = {}

    # -- lifecycle -----------------------------------------------------------

    def document_opened(self, uri: str) -> int:
        """Start a new generation for ``uri``; returns the generation number."""
        with self._lock:
            previous = self._states.get(uri)
            generation = previous.generation + 1 if previous is not None else 1
            self._states[uri] = _DocumentState(generation=generation)
            if previous is not None and previous.pending:
                log.debug(
                    "Document %s re-opened; cancelling %d pending analysis(es) "
                    "from generation %d",
                    uri,
                    len(previous.pending),
                    previous.generation,
                )
            return generation

    def document_closed(self, uri: str) -> None:
        """Cancel future publishing for ``uri`` and clear its diagnostics once.

        Outstanding and future tickets for the current generation can no
        longer publish. A single clear (empty diagnostics) notification is
        sent if the client may still be showing diagnostics for the document;
        the clear is sent at most once per close.
        """
        with self._lock:
            state = self._states.get(uri)
            if state is None:
                return
            if state.pending:
                log.debug(
                    "Closing %s; cancelling %d pending analysis(es)",
                    uri,
                    len(state.pending),
                )
            state.pending.clear()
            state.is_open = False
            if state.has_visible_diagnostics:
                state.has_visible_diagnostics = False
                self._publish(uri, [], None)
                self._record(
                    DiagnosticRecord(
                        uri=uri,
                        generation=state.generation,
                        sequence=state.latest_sequence,
                        version=None,
                        kind=RecordKind.CLEARED,
                        status=PublishStatus.PUBLISHED,
                        detail="document closed",
                    )
                )

    # -- analysis lifecycle --------------------------------------------------

    def begin_analysis(self, uri: str, version: Optional[int]) -> AnalysisTicket:
        """Bind a new analysis run to ``version`` of ``uri``.

        The returned ticket must be passed to :meth:`commit` or
        :meth:`commit_error` exactly once. Only the latest begun ticket of the
        current generation may publish.
        """
        with self._lock:
            state = self._states.get(uri)
            if state is None:
                # Document was never registered (e.g. lint triggered without
                # didOpen): adopt it implicitly so results can still flow.
                self.document_opened(uri)
                state = self._states[uri]
            state.latest_sequence += 1
            ticket = AnalysisTicket(
                uri=uri,
                generation=state.generation,
                sequence=state.latest_sequence,
                version=version,
            )
            state.pending.add(ticket.sequence)
            return ticket

    def commit(self, ticket: AnalysisTicket, diagnostics: List) -> PublishStatus:
        """Publish ``diagnostics`` for ``ticket`` if it is still current.

        Late tickets (superseded by a newer analysis, or belonging to a
        closed/re-opened document) are discarded and never overwrite newer
        diagnostics.
        """
        with self._lock:
            state = self._states.get(ticket.uri)
            stale = self._staleness(ticket, state)
            kind = RecordKind.EMPTY if not diagnostics else RecordKind.DIAGNOSTICS
            if stale is not None:
                self._record_for(ticket, kind, stale)
                log.debug(
                    "Discarding diagnostics for %s (generation %d, sequence %d): %s",
                    ticket.uri,
                    ticket.generation,
                    ticket.sequence,
                    stale.value,
                )
                return stale
            self._publish(ticket.uri, list(diagnostics), ticket.version)
            state.has_visible_diagnostics = bool(diagnostics)
            self._record_for(ticket, kind, PublishStatus.PUBLISHED)
            return PublishStatus.PUBLISHED

    def commit_error(self, ticket: AnalysisTicket, error: Exception) -> PublishStatus:
        """Record an analyzer failure for ``ticket``.

        Errors are recorded separately from empty diagnostic sets and never
        result in a publish, so a failing analyzer cannot wipe out the
        diagnostics the client currently shows.
        """
        with self._lock:
            state = self._states.get(ticket.uri)
            stale = self._staleness(ticket, state)
            status = stale if stale is not None else PublishStatus.ERROR_RECORDED
            self._record_for(ticket, RecordKind.ERROR, status, detail=repr(error))
            if stale is None:
                log.warning(
                    "Analyzer for %s (version %s) failed: %r",
                    ticket.uri,
                    ticket.version,
                    error,
                )
            return status

    # -- introspection -------------------------------------------------------

    def status(self, uri: str) -> Optional[dict]:
        """Return the coordination status for ``uri`` (or None if unknown)."""
        with self._lock:
            state = self._states.get(uri)
            if state is None:
                return None
            records = self._records.get(uri, [])
            return {
                "uri": uri,
                "generation": state.generation,
                "is_open": state.is_open,
                "pending_analyses": len(state.pending),
                "has_visible_diagnostics": state.has_visible_diagnostics,
                "last_record": records[-1] if records else None,
            }

    def records(self, uri: str) -> List[DiagnosticRecord]:
        """Return the recorded publish history for ``uri``."""
        with self._lock:
            return list(self._records.get(uri, []))

    def last_record(self, uri: str) -> Optional[DiagnosticRecord]:
        """Return the most recent record for ``uri`` (or None)."""
        with self._lock:
            records = self._records.get(uri)
            return records[-1] if records else None

    # -- internals -----------------------------------------------------------

    def _staleness(
        self, ticket: AnalysisTicket, state: Optional[_DocumentState]
    ) -> Optional[PublishStatus]:
        """Return why ``ticket`` may not publish, or None if it is current."""
        if state is not None:
            state.pending.discard(ticket.sequence)
        if state is None or ticket.generation != state.generation:
            return PublishStatus.DISCARDED_STALE_GENERATION
        if not state.is_open:
            return PublishStatus.DISCARDED_CLOSED
        if ticket.sequence != state.latest_sequence:
            return PublishStatus.DISCARDED_STALE_VERSION
        return None

    def _record_for(
        self,
        ticket: AnalysisTicket,
        kind: RecordKind,
        status: PublishStatus,
        detail: Optional[str] = None,
    ) -> None:
        self._record(
            DiagnosticRecord(
                uri=ticket.uri,
                generation=ticket.generation,
                sequence=ticket.sequence,
                version=ticket.version,
                kind=kind,
                status=status,
                detail=detail,
            )
        )

    def _record(self, record: DiagnosticRecord) -> None:
        history = self._records.setdefault(record.uri, [])
        history.append(record)
        del history[:-MAX_RECORDS_PER_DOCUMENT]
