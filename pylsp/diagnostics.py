# Copyright 2021- Python Language Server Contributors.

"""Diagnostic publish coordination layer.

This module implements the coordination layer that sits between the lint
analyzers and the textDocument/publishDiagnostics notifications sent to
the client. Its job is to make sure that diagnostics published for a
document always reflect the *newest* completed analysis of the *current*
lifecycle of that document.

Status model (per document URI)
-------------------------------

The coordinator tracks, for every document URI, a small amount of state:

* ``generation``: a monotonically increasing counter. ``document_opened``
  bumps the generation, so a URI that is closed and later reopened gets a
  fresh generation and can never be polluted by results of analyses that
  were started before the close.
* ``open``: whether the URI currently accepts diagnostic publishes.
  ``document_closed`` flips this to ``False``, cancels every in-flight
  analysis, and publishes a single empty-diagnostics *clear*. The clear is
  sent **at most once** per close; repeated closes are no-ops.
* ``last_version``: the document version of the most recently published
  diagnostics within the current generation.

Lifecycle of one analysis
-------------------------

Every lint run calls :meth:`DiagnosticsCoordinator.begin_analysis` *before*
invoking the analyzers and receives an :class:`AnalysisToken` that binds the
run to the current ``(generation, version)``. When the analyzers finish, the
run calls :meth:`DiagnosticsCoordinator.commit`, which publishes the result
only if the token is still current:

* the document was closed (or closed and reopened) since the analysis
  started -> the late result is **discarded** (recorded as
  ``discarded-closed``); it can never overwrite newer state;
* the bound version is older than ``last_version`` -> the stale result is
  **discarded** (recorded as ``discarded-stale``), so a slow analysis of an
  old document version can never overwrite diagnostics of a newer version.

Analyzer errors are recorded separately (outcome ``error``) from empty
diagnostic sets (outcome ``published`` with ``diagnostic_count == 0``), so
"the analyzer crashed" is never confused with "the analyzer found nothing".

Every decision is appended to :attr:`DiagnosticsCoordinator.records` and can
be inspected through :meth:`DiagnosticsCoordinator.status`; the coordinator
never re-runs analyzers to double-check a result.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass(frozen=True)
class AnalysisToken:
    """Binding of one analysis run to a document generation and version."""

    uri: str
    generation: int
    version: Optional[int]


@dataclass
class PublishRecord:
    """One entry in the coordinator's decision log."""

    uri: str
    generation: int
    version: Optional[int]
    outcome: str
    diagnostic_count: int = 0
    error: Optional[str] = None


class DiagnosticsCoordinator:
    """Coordinates diagnostic publishing per document URI.

    Parameters
    ----------
    publish:
        Callable invoked as ``publish(uri, diagnostics, version)`` to emit
        the actual textDocument/publishDiagnostics notification. It is
        always called while holding the coordinator lock, so publishes are
        atomic with the staleness checks and totally ordered.
    """

    #: Outcome constants used in PublishRecord.
    PUBLISHED = "published"
    DISCARDED_STALE = "discarded-stale"
    DISCARDED_CLOSED = "discarded-closed"
    CLEARED = "cleared"
    ERROR = "error"

    def __init__(self, publish: Callable[[str, list, Optional[int]], None]) -> None:
        self._publish = publish
        self._lock = threading.Lock()
        self._generation: dict[str, int] = {}
        self._open: set[str] = set()
        self._last_version: dict[str, Optional[int]] = {}
        self._cleared: dict[str, int] = {}
        self.records: List[PublishRecord] = []

    # -- lifecycle ------------------------------------------------------

    def document_opened(self, doc_uri: str) -> int:
        """Start a new generation for *doc_uri* and return it.

        Reopening the same URI yields a fresh generation, isolating the new
        document lifecycle from any results of analyses started earlier.
        """
        with self._lock:
            generation = self._generation.get(doc_uri, 0) + 1
            self._generation[doc_uri] = generation
            self._open.add(doc_uri)
            self._last_version.pop(doc_uri, None)
            return generation

    def document_closed(self, doc_uri: str) -> bool:
        """Cancel future publishes for *doc_uri* and clear its diagnostics.

        Publishes a single empty-diagnostics clear; the clear is sent at
        most once per close (repeated closes are no-ops). Any analysis
        result that commits afterwards for a previous generation is
        discarded. Returns True if a clear was published.
        """
        with self._lock:
            generation = self._generation.get(doc_uri, 0)
            if doc_uri not in self._open and self._cleared.get(doc_uri) == generation:
                # Already closed and already cleared: do not clear twice.
                return False
            generation += 1
            self._generation[doc_uri] = generation
            self._open.discard(doc_uri)
            self._last_version.pop(doc_uri, None)
            self._cleared[doc_uri] = generation
            self._record(doc_uri, generation, None, self.CLEARED)
            self._publish(doc_uri, [], None)
            return True

    # -- analysis runs ----------------------------------------------------

    def begin_analysis(
        self, doc_uri: str, version: Optional[int] = None
    ) -> AnalysisToken:
        """Bind a new analysis run to the current generation and *version*."""
        with self._lock:
            generation = self._generation.get(doc_uri)
            if generation is None:
                # Implicitly track documents we never saw an open for
                # (e.g. notebook cells or external files).
                generation = 1
                self._generation[doc_uri] = generation
                self._open.add(doc_uri)
        return AnalysisToken(uri=doc_uri, generation=generation, version=version)

    def commit(
        self,
        token: AnalysisToken,
        diagnostics: Optional[list] = None,
        error: Optional[BaseException] = None,
    ) -> bool:
        """Publish the result of an analysis run if it is still current.

        Late results (document closed/reopened, or a newer version already
        published) are discarded, never overwritten on top of newer state.
        Analyzer *error* is recorded separately from an empty diagnostic
        set. Returns True if the diagnostics were published.
        """
        with self._lock:
            if (
                token.uri not in self._open
                or self._generation.get(token.uri) != token.generation
            ):
                self._record(
                    token.uri, token.generation, token.version, self.DISCARDED_CLOSED
                )
                return False
            if error is not None:
                self._record(
                    token.uri,
                    token.generation,
                    token.version,
                    self.ERROR,
                    error=str(error),
                )
                return False
            last_version = self._last_version.get(token.uri)
            if (
                token.version is not None
                and last_version is not None
                and token.version < last_version
            ):
                self._record(
                    token.uri, token.generation, token.version, self.DISCARDED_STALE
                )
                return False
            diagnostics = list(diagnostics or [])
            if token.version is not None:
                self._last_version[token.uri] = token.version
            self._record(
                token.uri,
                token.generation,
                token.version,
                self.PUBLISHED,
                diagnostic_count=len(diagnostics),
            )
            self._publish(token.uri, diagnostics, token.version)
            return True

    # -- introspection ----------------------------------------------------

    def status(self) -> dict:
        """Return a snapshot of the coordinator state (see module docstring)."""
        with self._lock:
            return {
                "generations": dict(self._generation),
                "open": sorted(self._open),
                "last_versions": dict(self._last_version),
                "records": list(self.records),
            }

    # -- internals ----------------------------------------------------------

    def _record(
        self,
        doc_uri: str,
        generation: int,
        version: Optional[int],
        outcome: str,
        diagnostic_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        self.records.append(
            PublishRecord(
                uri=doc_uri,
                generation=generation,
                version=version,
                outcome=outcome,
                diagnostic_count=diagnostic_count,
                error=error,
            )
        )
