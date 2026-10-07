# Copyright 2026 Python Language Server Contributors.

"""In-process checks for versioned, encoding-aware textDocument/didChange.

These exercise the full server path (initialize negotiation -> didOpen ->
didChange) without spawning the language server as a subprocess.
"""

from io import StringIO
from unittest.mock import patch

from pylsp import lsp, uris
from test.fixtures import FakeEndpoint, FakePythonLSPServer

DOC_URI = "file:///versioned_sync_doc"
SOURCE = "cat: 猫\nemoji: \U0001f600\n"
EXPECTED = "cat: 猫\nemoji: ⭐\n"


def _server(tmpdir, position_encodings=None):
    server = FakePythonLSPServer(StringIO, StringIO, endpoint_cls=FakeEndpoint)
    capabilities = {}
    if position_encodings is not None:
        capabilities["general"] = {"positionEncodings": position_encodings}
    result = server.m_initialize(
        processId=1,
        rootUri=uris.from_fs_path(str(tmpdir)),
        initializationOptions={},
        capabilities=capabilities,
    )
    return server, result["capabilities"]


def _open(server):
    # Lint is disabled: these checks only assert document synchronisation,
    # and publishing diagnostics through the fake endpoint is irrelevant.
    with patch("pylsp.python_lsp.PythonLSPServer.lint"):
        server.m_text_document__did_open(
            textDocument={"uri": DOC_URI, "text": SOURCE, "version": 0}
        )


def _did_change(server, version, content_changes):
    with patch("pylsp.python_lsp.PythonLSPServer.lint") as lint:
        lint.reset_mock()
        server.m_text_document__did_change(
            textDocument={"uri": DOC_URI, "version": version},
            contentChanges=content_changes,
        )
        return lint


def test_initialize_defaults_to_utf16_without_client_preference(tmpdir):
    _, capabilities = _server(tmpdir)
    assert capabilities["positionEncoding"] == lsp.PositionEncodingKind.UTF16


def test_initialize_negotiates_utf8(tmpdir):
    _, capabilities = _server(
        tmpdir,
        position_encodings=[
            lsp.PositionEncodingKind.UTF8,
            lsp.PositionEncodingKind.UTF16,
        ],
    )
    assert capabilities["positionEncoding"] == lsp.PositionEncodingKind.UTF8


def test_initialize_falls_back_to_utf16(tmpdir):
    # An encoding the server does not support leaves the protocol default.
    _, capabilities = _server(tmpdir, position_encodings=["utf-32"])
    assert capabilities["positionEncoding"] == lsp.PositionEncodingKind.UTF16


def test_did_change_applies_utf16_batch_and_publishes_version(tmpdir):
    server, _ = _server(tmpdir)
    _open(server)
    _did_change(
        server,
        1,
        [
            {
                "text": "⭐",
                "range": {
                    "start": {"line": 1, "character": 7},
                    "end": {"line": 1, "character": 9},
                },
            }
        ],
    )
    doc = server.workspace.get_document(DOC_URI)
    assert doc.source == EXPECTED
    assert doc.version == 1


def test_did_change_with_utf8_negotiation(tmpdir):
    server, _ = _server(
        tmpdir,
        position_encodings=[
            lsp.PositionEncodingKind.UTF8,
            lsp.PositionEncodingKind.UTF16,
        ],
    )
    _open(server)
    _did_change(
        server,
        1,
        [
            {
                "text": "⭐",
                "range": {
                    "start": {"line": 1, "character": 7},
                    "end": {"line": 1, "character": 11},
                },
            }
        ],
    )
    assert server.workspace.get_document(DOC_URI).source == EXPECTED


def test_stale_did_change_is_ignored(tmpdir):
    server, _ = _server(tmpdir)
    _open(server)
    _did_change(server, 1, [{"text": EXPECTED}])
    # Replaying the same (now stale) version must not modify the document or
    # trigger diagnostics for the stale content.
    lint = _did_change(server, 1, [{"text": "STALE"}])
    lint.assert_not_called()
    doc = server.workspace.get_document(DOC_URI)
    assert doc.source == EXPECTED
    assert doc.version == 1


def test_half_surrogate_did_change_leaves_document_untouched(tmpdir):
    server, _ = _server(tmpdir)
    _open(server)
    lint = _did_change(
        server,
        1,
        [
            {
                "text": "?",
                "range": {
                    "start": {"line": 1, "character": 8},
                    "end": {"line": 1, "character": 8},
                },
            }
        ],
    )
    lint.assert_not_called()
    doc = server.workspace.get_document(DOC_URI)
    assert doc.source == SOURCE
    assert doc.version == 0
