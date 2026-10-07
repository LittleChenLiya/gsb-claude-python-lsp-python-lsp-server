# Copyright 2026 Python Language Server Contributors.

"""Tests for the explicit, versioned, encoding-aware document edit entry.

Positions in a ``textDocument/didChange`` batch are interpreted in the
position encoding negotiated for the session (UTF-8 code points or UTF-16
code units), never as plain Python character indices. The acceptance
fixtures below use a two-line document containing both a Chinese cat (猫)
and an emoji (\U0001f600).
"""

import pytest

from pylsp import lsp
from pylsp.workspace import (
    Document,
    InvalidRangeError,
    StaleVersionError,
)

# Two lines: a Chinese cat on the first one and an emoji on the second one.
SOURCE = "cat: 猫\nemoji: \U0001f600\n"
EXPECTED = "cat: 猫\nemoji: ⭐\n"

DOC_URI = "file:///versioned_edit_doc"

# On the "emoji: \U0001f600\n" line the emoji follows seven ASCII characters.
# It occupies 4 UTF-8 code units (7..11) and 2 UTF-16 code units (7..9).
UTF8_EMOJI_RANGE = {
    "start": {"line": 1, "character": 7},
    "end": {"line": 1, "character": 11},
}
UTF16_EMOJI_RANGE = {
    "start": {"line": 1, "character": 7},
    "end": {"line": 1, "character": 9},
}


def _doc(workspace, encoding):
    return Document(
        DOC_URI,
        workspace,
        SOURCE,
        version=0,
        position_encoding=encoding,
    )


def _replace_emoji(range_):
    return {"text": "⭐", "range": range_}


@pytest.mark.parametrize(
    "encoding,range_",
    [
        (lsp.PositionEncodingKind.UTF8, UTF8_EMOJI_RANGE),
        (lsp.PositionEncodingKind.UTF16, UTF16_EMOJI_RANGE),
    ],
)
def test_same_edit_same_result_for_both_encodings(workspace, encoding, range_):
    """Changing the emoji yields the same document under UTF-8 and UTF-16."""
    doc = _doc(workspace, encoding)
    result = doc.apply_changes([_replace_emoji(range_)], version=1)
    assert result == EXPECTED
    # Source and version are published together.
    assert doc.source == EXPECTED
    assert doc.version == 1


def test_stale_version_is_rejected_and_leaves_document_untouched(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    doc.apply_changes([_replace_emoji(UTF16_EMOJI_RANGE)], version=1)

    # Re-sending version 1 must be rejected, and version 0 cannot be replayed.
    for stale_version in (1, 0):
        with pytest.raises(StaleVersionError):
            doc.apply_changes([{"text": "x"}], version=stale_version)
        assert doc.source == EXPECTED
        assert doc.version == 1


def test_half_surrogate_range_rejects_whole_batch(workspace):
    """A UTF-16 range cutting a surrogate pair must not modify the document."""
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    half_surrogate = {
        "start": {"line": 1, "character": 8},
        "end": {"line": 1, "character": 8},
    }
    with pytest.raises(InvalidRangeError):
        doc.apply_changes(
            [
                # This one points at the low surrogate of \U0001f600...
                {"text": "?", "range": half_surrogate},
                # ...and even a valid later change cannot save the batch.
                _replace_emoji(UTF16_EMOJI_RANGE),
            ],
            version=1,
        )
    assert doc.source == SOURCE
    assert doc.version == 0


def test_half_utf8_code_point_range_rejects_whole_batch(workspace):
    """A UTF-8 range in the middle of a 4-byte code point is rejected."""
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF8)
    half_code_point = {
        "start": {"line": 1, "character": 8},
        "end": {"line": 1, "character": 8},
    }
    with pytest.raises(InvalidRangeError):
        doc.apply_changes([{"text": "?", "range": half_code_point}], version=1)
    assert doc.source == SOURCE
    assert doc.version == 0


@pytest.mark.parametrize(
    "encoding,character",
    [
        (lsp.PositionEncodingKind.UTF8, 13),
        (lsp.PositionEncodingKind.UTF16, 11),
    ],
)
def test_out_of_range_offsets_are_rejected(workspace, encoding, character):
    doc = _doc(workspace, encoding)
    bad_range = {
        "start": {"line": 1, "character": character},
        "end": {"line": 1, "character": character},
    }
    with pytest.raises(InvalidRangeError):
        doc.apply_changes([{"text": "x", "range": bad_range}], version=1)
    assert doc.source == SOURCE
    assert doc.version == 0


def test_out_of_range_line_is_rejected(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    with pytest.raises(InvalidRangeError):
        doc.apply_changes(
            [
                {
                    "text": "x",
                    "range": {
                        "start": {"line": 5, "character": 0},
                        "end": {"line": 5, "character": 0},
                    },
                }
            ],
            version=1,
        )
    assert doc.version == 0


def test_reversed_range_is_rejected(workspace):
    doc = _doc(workspace, lsp.PositionEncodingKind.UTF16)
    with pytest.raises(InvalidRangeError):
        doc.apply_changes(
            [
                {
                    "text": "x",
                    "range": {
                        "start": {"line": 1, "character": 3},
                        "end": {"line": 1, "character": 1},
                    },
                }
            ],
            version=1,
        )
    assert doc.version == 0


def test_changes_apply_in_received_order(workspace):
    """Each change builds on the result of the previous one, never reordered."""
    doc = Document(DOC_URI, workspace, "ac", version=0)
    doc.apply_changes(
        [
            # Insert "b" between "a" and "c".
            {
                "text": "b",
                "range": {
                    "start": {"line": 0, "character": 1},
                    "end": {"line": 0, "character": 1},
                },
            },
            # The next position addresses the text produced by the previous
            # change: insert "d" right after the newly inserted "b".
            {
                "text": "d",
                "range": {
                    "start": {"line": 0, "character": 2},
                    "end": {"line": 0, "character": 2},
                },
            },
        ],
        version=1,
    )
    assert doc.source == "abdc"


def test_astral_characters_on_preceding_lines(workspace):
    """Offsets must not confuse encoding units of earlier lines with chars."""
    source = "😀😀\n🐱 cat\nemoji: 🎉 end"
    edit = {
        "text": "*",
        "range": {
            "start": {"line": 2, "character": 7},
            "end": {"line": 2, "character": 9},
        },
    }
    expected = "😀😀\n🐱 cat\nemoji: * end"

    doc_utf16 = Document(DOC_URI, workspace, source, version=0)
    doc_utf16.apply_changes([edit], version=1)
    assert doc_utf16.source == expected

    doc_utf8 = Document(
        DOC_URI, workspace, source, version=0, position_encoding="utf-8"
    )
    doc_utf8.apply_changes(
        [
            {
                "text": "*",
                "range": {
                    "start": {"line": 2, "character": 7},
                    "end": {"line": 2, "character": 11},
                },
            }
        ],
        version=1,
    )
    assert doc_utf8.source == expected


def test_crlf_line_endings_are_positioned(workspace):
    """CRLF terminators are addressed as a single line ending.

    The carriage return stays part of the line, so inserting after "a"
    targets character 1 of line 0. Replacing "b" on line 1 keeps the CRLF
    ending in place. A range between CR and LF is refused rather than
    splitting the ending.
    """
    doc = Document(DOC_URI, workspace, "a\r\nb\r\n", version=0)
    doc.apply_changes(
        [
            {
                "text": "X",
                "range": {
                    "start": {"line": 0, "character": 1},
                    "end": {"line": 0, "character": 1},
                },
            }
        ],
        version=1,
    )
    assert doc.source == "aX\r\nb\r\n"

    doc.apply_changes(
        [
            {
                "text": "Y",
                "range": {
                    "start": {"line": 1, "character": 0},
                    "end": {"line": 1, "character": 1},
                },
            }
        ],
        version=2,
    )
    assert doc.source == "aX\r\nY\r\n"

    # Character 3 on the "aX\r\n" line sits between the carriage return and
    # the line feed, which must not be cut by an edit. Character 2 is the
    # valid end-of-content position, right before the CRLF terminator.
    with pytest.raises(InvalidRangeError):
        doc.apply_changes(
            [
                {
                    "text": "Z",
                    "range": {
                        "start": {"line": 0, "character": 3},
                        "end": {"line": 0, "character": 3},
                    },
                }
            ],
            version=3,
        )
    assert doc.source == "aX\r\nY\r\n"
    assert doc.version == 2


def test_empty_document_insertion(workspace):
    doc = Document(DOC_URI, workspace, "", version=0)
    doc.apply_changes(
        [
            {
                "text": "f",
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 0},
                },
            }
        ],
        version=1,
    )
    assert doc.source == "f"


def test_whole_file_change(workspace):
    doc = Document(DOC_URI, workspace, SOURCE, version=0)
    doc.apply_changes([{"text": "replaced"}], version=1)
    assert doc.source == "replaced"
    assert doc.version == 1


def test_workspace_applies_versioned_batch(workspace):
    workspace.put_document(DOC_URI, SOURCE, version=0)
    workspace.apply_document_changes(
        DOC_URI, [_replace_emoji(UTF16_EMOJI_RANGE)], version=1
    )
    assert workspace.get_document(DOC_URI).source == EXPECTED
    assert workspace.get_document(DOC_URI).version == 1

    with pytest.raises(StaleVersionError):
        workspace.apply_document_changes(DOC_URI, [{"text": "newer"}], version=1)
