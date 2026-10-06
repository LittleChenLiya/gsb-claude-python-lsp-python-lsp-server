# Versioned, encoding-aware document edits

`Document.apply_versioned_changes(changes, version)` is pylsp's explicit
entry point for applying a batch of incremental `textDocument/didChange`
edits to a document.

## Contract

1. **Explicit version.** The caller passes the document version the edits
   are based on. A version older than the document's current version is
   stale and rejected with `StaleDocumentVersionError`; the document is not
   modified.
2. **Session encoding, never Python indices.** `position.character` values
   are interpreted in the position encoding negotiated during the
   `initialize` handshake (`utf-16`, `utf-8` or `utf-32` code units). They
   are never treated as Python string indices.
3. **Ordered application.** The `contentChanges` array is applied in the
   order the client sent it; every change is resolved against the text
   produced by the previous one. Client change events are never reordered.
4. **Atomic batch.** Before publishing, every range is validated. A range
   that is out of bounds, ends before it starts, lands inside a line
   terminator, or cuts an encoding code unit in half (e.g. half of a UTF-16
   surrogate pair, or a non-boundary UTF-8 byte offset) raises
   `InvalidRangeError`. The whole batch fails and the document keeps its
   previous source and version.
5. **Text and version together.** On success the new source text and the new
   version are committed at once.
6. **Line endings.** LSP line terminators are `\r\n`, `\r` and `\n`. A
   position on a terminated line may point at the end of its content (the
   start of the terminator) but not into the middle of the terminator. A
   document ending with a terminator has an empty virtual line after it.

## Negotiation

The client lists its preferred encodings in
`capabilities.general.positionEncodings`; pylsp picks the first one it also
supports and echoes it back in `serverCapabilities.positionEncoding`.
Clients without the capability use the protocol default, UTF-16.

```python
from pylsp import lsp
# capabilities sent by the client
client = {"general": {"positionEncodings": ["utf-8", "utf-16"]}}
# server answers "utf-8"; didChange ranges are then UTF-8 byte offsets
```

See `examples/versioned_edits_example.py` for a runnable walk-through and
`test/test_document_versioned_edits.py` for the acceptance tests.
