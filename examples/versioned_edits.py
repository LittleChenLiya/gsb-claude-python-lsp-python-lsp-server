#!/usr/bin/env python3
"""Runnable example: versioned, encoding-aware document editing.

Documents expose ``apply_changes``, an explicit entry point for a batch of
``textDocument/didChange`` content changes. Instead of blindly indexing the
Python string, every position is interpreted using the position encoding
negotiated for the LSP session (UTF-8 code units or UTF-16 code units).

The batch rules demonstrated here are:

* changes are applied in the order received, each one on top of the previous
  result, and client change events are never reordered;
* a version that is not newer than the current one rejects the whole batch;
* a range that is out of bounds or cuts an encoding unit (half a UTF-16
  surrogate pair / half a UTF-8 code point) rejects the whole batch;
* on success the new source text and the new version are published together.

Run it directly with ``python examples/versioned_edits.py``.
"""

import os
import sys

# Allow running straight from a repository checkout without installing pylsp.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The demonstration prints cat and emoji characters, so use UTF-8 even on
# consoles whose default encoding cannot represent them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# A tiny stand-in workspace is enough for the document API.
from types import SimpleNamespace

from pylsp import lsp
from pylsp.workspace import (
    Document,
    InvalidRangeError,
    StaleVersionError,
)

# Two lines: a Chinese cat on the first one and an emoji on the second one.
SOURCE = "cat: 猫\nemoji: \U0001f600\n"
REPLACED = "cat: 猫\nemoji: ⭐\n"


def make_document(encoding):
    workspace = SimpleNamespace(_config=None)
    return Document(
        "file:///example.py",
        workspace,
        source=SOURCE,
        version=0,
        position_encoding=encoding,
    )


def main():
    # On the "emoji: \U0001f600\n" line the emoji follows seven ASCII
    # characters. It spans 4 UTF-8 units (7..11) but only 2 UTF-16 units
    # (7..9) - the positions differ, yet both edits describe the same
    # character and therefore produce the same document.
    utf8_range = {
        "start": {"line": 1, "character": 7},
        "end": {"line": 1, "character": 11},
    }
    utf16_range = {
        "start": {"line": 1, "character": 7},
        "end": {"line": 1, "character": 9},
    }

    utf8_doc = make_document(lsp.PositionEncodingKind.UTF8)
    utf16_doc = make_document(lsp.PositionEncodingKind.UTF16)

    utf8_doc.apply_changes([{"text": "⭐", "range": utf8_range}], version=1)
    utf16_doc.apply_changes([{"text": "⭐", "range": utf16_range}], version=1)
    assert utf8_doc.source == utf16_doc.source == REPLACED
    print("UTF-8 and UTF-16 edits agree:", repr(utf8_doc.source))

    # Sending a version that is not newer (1 when the document is already at
    # version 1) is rejected and leaves source and version untouched.
    try:
        utf16_doc.apply_changes([{"text": "late"}], version=1)
    except StaleVersionError:
        print("Stale version 1 rejected; document stays at", utf16_doc.version)

    # A range that lands between the two UTF-16 code units of the emoji
    # (character 8, the low surrogate) cuts an encoding unit and fails.
    half_surrogate = {
        "start": {"line": 1, "character": 8},
        "end": {"line": 1, "character": 8},
    }
    fresh = make_document(lsp.PositionEncodingKind.UTF16)
    try:
        fresh.apply_changes([{"text": "?", "range": half_surrogate}], version=1)
    except InvalidRangeError:
        print("Half-surrogate range rejected; source unchanged:", fresh.version)

    # CRLF line endings are positioned just like LF: the carriage return is
    # part of the line, so inserting after "a" is character 1 of line 0 and
    # an edit between CR and LF (character 2 of "a\r\n") would split the
    # ending and is rejected.
    crlf = Document(
        "file:///crlf.txt",
        SimpleNamespace(_config=None),
        source="a\r\nb\r\n",
        version=0,
    )
    crlf.apply_changes(
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
    print("CRLF-aware edit:", repr(crlf.source))


if __name__ == "__main__":
    main()
