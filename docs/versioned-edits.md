# Versioned, encoding-aware document edits

`Document.apply_changes(changes, version=None, encoding=None)` is the
explicit entry point for a batch of LSP `textDocument/didChange` content
changes. Positions in the changes are interpreted in the position encoding
negotiated for the session (`utf-8` code units or `utf-16` code units), not
as Python character indices.

The encoding is negotiated during `initialize` from the client's
`general.positionEncodings` capability (pylsp prefers `utf-8`, falls back to
`utf-16`) and is advertised in the server capabilities as `positionEncoding`.
Every document created by the workspace inherits it; `Workspace` exposes the
same batch through `apply_document_changes`.

## Batch semantics

* Changes are applied in the order received, each one based on the result of
  the previous one. pylsp never reorders client change events.
* A change whose `version` is not strictly newer than the current document
  version raises `StaleVersionError`.
* A range that is out of bounds, that ends before it starts, or that cuts an
  encoding unit in half (half a UTF-16 surrogate pair or half a UTF-8 code
  point) raises `InvalidRangeError`.
* Any failure rejects the whole batch: neither the source nor the version is
  modified. On success the new source and the new version are published
  together.
* CRLF and CR documents are supported: lines are split on `\n` only and the
  line terminator cannot be split by a position (there is no valid position
  between the `\r` and the `\n` of a CRLF ending).

A runnable demonstration lives at
[`examples/versioned_edits.py`](../examples/versioned_edits.py), and the
behaviour is covered by `test/test_document_versioned_edits.py` and
`test/test_versioned_document_sync.py`.
