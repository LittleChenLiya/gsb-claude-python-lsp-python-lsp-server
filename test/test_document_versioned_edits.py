# Copyright 2026 Python Language Server Contributors.

"""Tests for Document's explicit versioned, encoding-aware edit entry point.

The fixture document contains a cat line and an emoji line with CRLF line
endings::

    "cat \U0001f431\r\ndog \U0001f600"

Positions are interpreted in the encoding negotiated for the session
(UTF-8, UTF-16 or UTF-32 code units), never as Python string indices.
"""

import pytest

from pylsp import lsp
from pylsp.workspace import (
    Document,
    InvalidRangeError,
    StaleDocumentVersionError,
)

CAT_DOC = "cat \U0001f431\r\ndog \U0001f600"


def _doc(workspace, encoding=lsp.PositionEncodingKind.UTF16, source=CAT_DOC, version=1):
    return Document(
        "file:///uri",
        workspace,
        source,
        version=version,
        position_encoding=encoding,
    )


def _rng(sl, sc, el, ec):
    return {
        "start": {"line": sl, "character": sc},
        "end": {"line": el, "character": ec},
    }


@pytest.mark.parametrize(
    "encoding",
    [lsp.PositionEncodingKind.UTF16, lsp.PositionEncodingKind.UTF8],
)
def test_same_character_edit_matches_across_encodings(workspace, encoding):
    """Editing the same character ('c') yields the same text in both encodings."""
    doc = _doc(workspace, encoding)
    doc.apply_versioned_changes([{"range": _rng(0, 0, 0, 1), "text": "C"}], version=2)
    assert doc.source == "Cat \U0001f431\r\ndog \U0001f600"
    assert doc.version == 2


def _emoji_change(encoding, replacement="\U0001f436"):
    # The cat face is the 5th character on line 0 and occupies two UTF-16
    # code units but four UTF-8 code units.
    width = 2 if encoding == lsp.PositionEncodingKind.UTF16 else 4
    return {"range": _rng(0, 4, 0, 4 + width), "text": replacement}


@pytest.mark.parametrize(
    "encoding",
    [lsp.PositionEncodingKind.UTF16, lsp.PositionEncodingKind.UTF8],
)
def test_emoji_edit_matches_across_encodings(workspace, encoding):
    """Replacing the emoji with encoding-correct units is the same edit."""
    doc = _doc(workspace, encoding)
    doc.apply_versioned_changes([_emoji_change(encoding)], version=2)
    assert doc.source == "cat \U0001f436\r\ndog \U0001f600"


def test_utf32_uses_code_point_offsets(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF32)
    doc.apply_versioned_changes(
        [{"range": _rng(0, 4, 0, 5), "text": "\U0001f436"}], version=2
    )
    assert doc.source == "cat \U0001f436\r\ndog \U0001f600"


def test_stale_version_rejected_and_document_untouched(workspace):
    doc = _doc(workspace, version=3)
    with pytest.raises(StaleDocumentVersionError):
        doc.apply_versioned_changes(
            [{"range": _rng(0, 0, 0, 1), "text": "C"}], version=2
        )
    # Original text and version are still published together.
    assert doc.source == CAT_DOC
    assert doc.version == 3


def test_half_surrogate_range_fails_whole_batch(workspace):
    """A range cutting a surrogate pair in half fails the *entire* batch."""
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    changes = [
        # A perfectly valid first change...
        {"range": _rng(1, 0, 1, 1), "text": "X"},
        # ...followed by a range that starts inside the cat-face surrogate
        # pair (emoji spans UTF-16 units 4 and 5 on line 0).
        {"range": _rng(0, 5, 0, 6), "text": "Y"},
    ]
    with pytest.raises(InvalidRangeError, match="surrogate"):
        doc.apply_versioned_changes(changes, version=2)
    assert doc.source == CAT_DOC
    assert doc.version == 1


def test_utf8_non_boundary_range_fails_whole_batch(workspace):
    """A UTF-8 offset inside the 4-byte emoji cuts a code unit."""
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF8)
    with pytest.raises(InvalidRangeError):
        doc.apply_versioned_changes(
            [{"range": _rng(0, 5, 0, 7), "text": "?"}], version=2
        )
    assert doc.source == CAT_DOC
    assert doc.version == 1


def test_out_of_bounds_range_fails(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    with pytest.raises(InvalidRangeError):
        doc.apply_versioned_changes(
            [{"range": _rng(9, 0, 9, 0), "text": "Z"}], version=2
        )
    assert doc.source == CAT_DOC
    assert doc.version == 1


def test_end_before_start_range_fails(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    with pytest.raises(InvalidRangeError):
        doc.apply_versioned_changes(
            [{"range": _rng(0, 3, 0, 2), "text": "Z"}], version=2
        )
    assert doc.source == CAT_DOC


def test_changes_apply_in_received_order_based_on_previous_result(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    doc.apply_versioned_changes(
        [
            # Replace 'cat' by 'fish', then uppercase the *new* text. The
            # second range only resolves correctly against the first result.
            {"range": _rng(0, 0, 0, 3), "text": "fish"},
            {"range": _rng(0, 0, 0, 4), "text": "FISH"},
        ],
        version=2,
    )
    assert doc.source == "FISH \U0001f431\r\ndog \U0001f600"


def test_crlf_line_ending_is_positioned(workspace):
    """Positions honour the CRLF terminator and the line after it."""
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    # Insert right before the CRLF (after the emoji, unit 6 on line 0).
    doc.apply_versioned_changes([{"range": _rng(0, 6, 0, 6), "text": ";"}], version=2)
    assert doc.source == "cat \U0001f431;\r\ndog \U0001f600"

    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    # Append at the end of the unterminated second line (6 UTF-16 units).
    doc.apply_versioned_changes([{"range": _rng(1, 6, 1, 6), "text": "!"}], version=2)
    assert doc.source == "cat \U0001f431\r\ndog \U0001f600!"


def test_position_inside_crlf_terminator_rejected(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    # Unit 7 on line 0 is the '\n' half of '\r\n' - not a valid position.
    with pytest.raises(InvalidRangeError):
        doc.apply_versioned_changes(
            [{"range": _rng(0, 7, 0, 7), "text": "z"}], version=2
        )
    assert doc.source == CAT_DOC


def test_cr_only_line_ending_is_positioned(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16, source="a\rb", version=0)
    doc.apply_versioned_changes([{"range": _rng(1, 0, 1, 1), "text": "X"}], version=1)
    assert doc.source == "a\rX"


def test_virtual_line_after_trailing_terminator(workspace):
    doc = Document(
        "file:///uri",
        workspace,
        "x\r\n",
        version=0,
        position_encoding=lsp.PositionEncodingKind.UTF16,
    )
    doc.apply_versioned_changes([{"range": _rng(1, 0, 1, 0), "text": "y"}], version=1)
    assert doc.source == "x\r\ny"


def test_empty_document_edit(workspace):
    doc = Document(
        "file:///uri",
        workspace,
        "",
        version=0,
        position_encoding=lsp.PositionEncodingKind.UTF16,
    )
    doc.apply_versioned_changes([{"range": _rng(0, 0, 0, 0), "text": "f"}], version=1)
    assert doc.source == "f"


def test_full_document_change(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    doc.apply_versioned_changes([{"text": "brand new"}], version=2)
    assert doc.source == "brand new"
    assert doc.version == 2


def test_source_and_version_published_together(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    # A failing batch must not leave source and version out of sync.
    with pytest.raises(InvalidRangeError):
        doc.apply_versioned_changes(
            [
                {"range": _rng(0, 0, 0, 1), "text": "C"},
                {"range": _rng(5, 0, 5, 0), "text": "D"},
            ],
            version=2,
        )
    assert doc.source == CAT_DOC
    assert doc.version == 1


def test_workspace_update_document_applies_ordered_batch(workspace):
    uri = "file:///uri"
    workspace.put_document(uri, CAT_DOC, version=1)
    workspace.position_encoding = lsp.PositionEncodingKind.UTF16
    workspace.update_document(
        uri,
        [
            {"range": _rng(0, 0, 0, 3), "text": "fish"},
            {"range": _rng(0, 0, 0, 4), "text": "FISH"},
        ],
        version=2,
    )
    assert workspace.get_document(uri).source == ("FISH \U0001f431\r\ndog \U0001f600")
    assert workspace.get_document(uri).version == 2


def test_workspace_update_document_rejects_stale_batch(workspace):
    uri = "file:///uri"
    workspace.put_document(uri, CAT_DOC, version=5)
    workspace.position_encoding = lsp.PositionEncodingKind.UTF16
    with pytest.raises(StaleDocumentVersionError):
        workspace.update_document(
            uri,
            [{"range": _rng(0, 0, 0, 1), "text": "C"}],
            version=4,
        )
    assert workspace.get_document(uri).source == CAT_DOC
    assert workspace.get_document(uri).version == 5
