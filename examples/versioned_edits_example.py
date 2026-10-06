# Copyright 2026 Python Language Server Contributors.

"""Runnable example of Document's explicit versioned edit entry point.

Run it with a UTF-8 capable console::

    python -X utf8 examples/versioned_edits_example.py

The document has two lines (a cat and an emoji) with CRLF line endings::

    "cat \U0001f431\r\ndog \U0001f600"

The same logical edit (replace the cat face \U0001f431 with the dog face
\U0001f436) is expressed with UTF-16 code units in one session and UTF-8
code units in another. Both produce identical text. It then demonstrates
the two failure modes: a stale document version, and a range that cuts a
UTF-16 surrogate pair in half. In every failure case the whole batch is
rejected and the document is left untouched.
"""

import os
import sys

# Allow running straight from a checkout without installing pylsp.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pylsp import lsp
from pylsp.workspace import (
    Document,
    InvalidRangeError,
    StaleDocumentVersionError,
)

SOURCE = "cat \U0001f431\r\ndog \U0001f600"
EXPECTED = "cat \U0001f436\r\ndog \U0001f600"


class _FakeWorkspace:
    # Document only needs a config object for editing; None is fine here.
    _config = None


def _replace_emoji_change(encoding):
    # The cat face sits after "cat " (4 characters) on line 0. It occupies
    # two UTF-16 code units but four UTF-8 code units.
    if encoding == lsp.PositionEncodingKind.UTF16:
        end_character = 6
    else:
        end_character = 8
    return [
        {
            "range": {
                "start": {"line": 0, "character": 4},
                "end": {"line": 0, "character": end_character},
            },
            "text": "\U0001f436",
        }
    ]


def main():
    # --- Same logical character edit, two session encodings ----------------
    for encoding in (lsp.PositionEncodingKind.UTF16, lsp.PositionEncodingKind.UTF8):
        doc = Document(
            "file:///example.py",
            _FakeWorkspace(),
            SOURCE,
            version=1,
            position_encoding=encoding,
        )
        doc.apply_versioned_changes(_replace_emoji_change(encoding), version=2)
        print(f"{encoding}: {doc.source!r}")
        assert doc.source == EXPECTED
        assert doc.version == 2

    # --- Stale version is rejected, document untouched ---------------------
    doc = Document("file:///example.py", _FakeWorkspace(), SOURCE, version=3)
    try:
        doc.apply_versioned_changes(
            [
                {
                    "text": "C",
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                }
            ],
            version=2,  # older than 3
        )
    except StaleDocumentVersionError as exc:
        print(f"stale version rejected: {exc}")
    assert doc.source == SOURCE and doc.version == 3

    # --- Half a surrogate pair fails the entire batch atomically -----------
    doc = Document(
        "file:///example.py",
        _FakeWorkspace(),
        SOURCE,
        version=1,
        position_encoding=lsp.PositionEncodingKind.UTF16,
    )
    batch = [
        # Valid first edit...
        {
            "text": "X",
            "range": {
                "start": {"line": 1, "character": 0},
                "end": {"line": 1, "character": 1},
            },
        },
        # ...then a range starting at UTF-16 unit 5, inside the cat face.
        {
            "text": "Y",
            "range": {
                "start": {"line": 0, "character": 5},
                "end": {"line": 0, "character": 6},
            },
        },
    ]
    try:
        doc.apply_versioned_changes(batch, version=2)
    except InvalidRangeError as exc:
        print(f"invalid range rejected, batch failed: {exc}")
    assert doc.source == SOURCE and doc.version == 1

    # --- CRLF positioning: insert right before the terminator --------------
    doc = Document(
        "file:///example.py",
        _FakeWorkspace(),
        SOURCE,
        version=1,
        position_encoding=lsp.PositionEncodingKind.UTF16,
    )
    doc.apply_versioned_changes(
        [
            {
                "text": ";",
                "range": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 6},
                },
            }
        ],
        version=2,
    )
    print(f"CRLF insertion: {doc.source!r}")
    assert doc.source == "cat \U0001f431;\r\ndog \U0001f600"

    print("all examples passed")


if __name__ == "__main__":
    main()
