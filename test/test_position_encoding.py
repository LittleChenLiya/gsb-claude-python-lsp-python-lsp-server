# Copyright 2026 Python Language Server Contributors.

"""Session-level position encoding negotiation and didChange integration.

These tests drive ``PythonLSPServer`` the same way a client would: the
position encoding is negotiated from ``general.positionEncodings`` during
``initialize`` and then used to interpret didChange ranges, while the
ordered contentChanges array is applied as one versioned batch.
"""

from io import StringIO

import pytest

from pylsp import lsp, uris
from pylsp.python_lsp import PythonLSPServer
from pylsp.workspace import InvalidRangeError, StaleDocumentVersionError
from test.fixtures import FakeEditorMethodsMixin, FakeEndpoint

CAT_DOC = "cat \U0001f431\r\ndog \U0001f600"


class _FakeServer(FakeEditorMethodsMixin, PythonLSPServer):
    pass


def _server(tmpdir, client_encodings=None):
    server = _FakeServer(StringIO, StringIO, endpoint_cls=FakeEndpoint)
    # didChange schedules a debounced lint on a timer thread; these tests
    # only care about document state, so neutralise it to avoid touching the
    # already-closed StringIO streams after the test finishes.
    server.lint = lambda *args, **kwargs: None
    capabilities = {}
    if client_encodings is not None:
        capabilities["general"] = {"positionEncodings": client_encodings}
    result = server.m_initialize(
        processId=1,
        rootUri=uris.from_fs_path(str(tmpdir)),
        initializationOptions={},
        capabilities=capabilities,
    )
    return server, result["capabilities"]


def _open(server, uri, source, version=1):
    server.m_text_document__did_open(
        textDocument={
            "uri": uri,
            "text": source,
            "version": version,
            "languageId": "python",
        }
    )


@pytest.mark.parametrize(
    "client_encodings,expected",
    [
        (None, lsp.PositionEncodingKind.UTF16),
        ([lsp.PositionEncodingKind.UTF16], lsp.PositionEncodingKind.UTF16),
        (
            [lsp.PositionEncodingKind.UTF8, lsp.PositionEncodingKind.UTF16],
            lsp.PositionEncodingKind.UTF8,
        ),
        # Prefer the first mutually supported encoding.
        (
            [lsp.PositionEncodingKind.UTF32, lsp.PositionEncodingKind.UTF8],
            lsp.PositionEncodingKind.UTF32,
        ),
        # Unknown encodings only: fall back to UTF-16.
        (["unicode-pizza"], lsp.PositionEncodingKind.UTF16),
    ],
)
def test_position_encoding_negotiation(tmpdir, client_encodings, expected):
    server, capabilities = _server(tmpdir, client_encodings)
    assert capabilities["positionEncoding"] == expected
    assert server.workspace.position_encoding == expected


def test_did_change_uses_negotiated_utf8_offsets(tmpdir):
    server, _ = _server(
        tmpdir, [lsp.PositionEncodingKind.UTF8, lsp.PositionEncodingKind.UTF16]
    )
    uri = "file:///cat.py"
    _open(server, uri, CAT_DOC, version=1)

    # Replace the cat face: it starts at UTF-8 offset 4 and is 4 bytes.
    server.m_text_document__did_change(
        textDocument={"uri": uri, "version": 2},
        contentChanges=[
            {
                "range": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 8},
                },
                "text": "\U0001f436",
            }
        ],
    )
    assert server.workspace.get_document(uri).source == (
        "cat \U0001f436\r\ndog \U0001f600"
    )


def test_did_change_applies_ordered_batch_as_one_version(tmpdir):
    server, _ = _server(tmpdir)
    uri = "file:///cat.py"
    _open(server, uri, CAT_DOC, version=1)
    server.m_text_document__did_change(
        textDocument={"uri": uri, "version": 2},
        contentChanges=[
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 3},
                },
                "text": "fish",
            },
            # Depends on the first change already being applied.
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 4},
                },
                "text": "FISH",
            },
        ],
    )
    doc = server.workspace.get_document(uri)
    assert doc.source == "FISH \U0001f431\r\ndog \U0001f600"
    assert doc.version == 2


def test_did_change_half_surrogate_leaves_document_unchanged(tmpdir):
    server, _ = _server(tmpdir)
    uri = "file:///cat.py"
    _open(server, uri, CAT_DOC, version=1)
    with pytest.raises(InvalidRangeError):
        server.m_text_document__did_change(
            textDocument={"uri": uri, "version": 2},
            contentChanges=[
                {
                    "range": {
                        "start": {"line": 0, "character": 5},
                        "end": {"line": 0, "character": 6},
                    },
                    "text": "X",
                }
            ],
        )
    doc = server.workspace.get_document(uri)
    assert doc.source == CAT_DOC
    assert doc.version == 1


def test_did_change_stale_version_rejected(tmpdir):
    server, _ = _server(tmpdir)
    uri = "file:///cat.py"
    _open(server, uri, CAT_DOC, version=5)
    with pytest.raises(StaleDocumentVersionError):
        server.m_text_document__did_change(
            textDocument={"uri": uri, "version": 4},
            contentChanges=[
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                    "text": "C",
                }
            ],
        )
    assert server.workspace.get_document(uri).source == CAT_DOC
