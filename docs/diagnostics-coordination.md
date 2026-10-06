# Diagnostic publishing coordination

`pylsp/diagnostic_coordinator.py` coordinates every
`textDocument/publishDiagnostics` notification so asynchronous analyzer
results can never clobber newer state.

## Lifecycle

1. **Open** — `didOpen` calls `DiagnosticCoordinator.document_opened(uri)`,
   starting a new *generation* for the URI. Re-opening the same URI after a
   close starts the next generation, so results produced by tasks of a
   previous open/close cycle are discarded and cannot pollute the new one.
2. **Analyze** — each lint run calls `begin_analysis(uri, version)`, binding
   the run to the document version it started from and returning an
   `AnalysisTicket`. Only the latest begun ticket of the current generation
   may publish.
3. **Commit** — `commit(ticket, diagnostics)` publishes only if the ticket is
   still current; late results are discarded instead of overwriting
   newer-version diagnostics. `commit_error(ticket, exc)` records analyzer
   failures separately from empty diagnostic sets and never publishes.
4. **Close** — `didClose` calls `document_closed(uri)`, which cancels all
   future publishing for outstanding tickets and sends a single clear (empty
   diagnostics) notification if the client may still be showing diagnostics.
   The clear is sent **at most once** per close; a duplicate close is a no-op.

## Publish statuses

| Status | Meaning |
| --- | --- |
| `published` | The result was current and sent to the client. |
| `discarded-stale-version` | A newer analysis was begun; the late result was dropped. |
| `discarded-stale-generation` | The document was closed/re-opened since the analysis started. |
| `discarded-closed` | The document is closed; publishing was cancelled. |
| `error-recorded` | The analyzer raised; the error was recorded, nothing published. |

## Record kinds

Every commit is appended to a bounded per-URI history
(`DiagnosticCoordinator.records(uri)`), tagged with a `RecordKind`:

* `diagnostics` — a non-empty diagnostic set,
* `empty` — a successful analysis that found nothing,
* `error` — an analyzer failure (kept distinct from `empty`),
* `cleared` — the clear sent on close.

`DiagnosticCoordinator.status(uri)` summarizes generation, open state,
pending analyses and the last record. Use these for verification and
debugging — do **not** re-run the analyzer to reconcile state.

## Commands

Run the coordination tests (Event-barrier based, no real analyzer needed):

```
python -m pytest test/test_diagnostic_coordinator.py -v
```

Run the directly related existing suites:

```
python -m pytest test/test_python_lsp.py test/test_workspace.py -q
```
