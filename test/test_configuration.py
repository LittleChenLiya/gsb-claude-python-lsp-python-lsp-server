# Copyright 2021- Python Language Server Contributors.

import os
from unittest.mock import patch

import pytest

from pylsp import IS_WIN, uris
from pylsp.config.config import Config
from test.test_notebook_document import wait_for_condition
from test.test_utils import send_initialize_request

INITIALIZATION_OPTIONS = {
    "pylsp": {
        "plugins": {
            "flake8": {"enabled": True},
            "pycodestyle": {"enabled": False},
            "pyflakes": {"enabled": False},
        },
    }
}


@pytest.mark.skipif(IS_WIN, reason="Flaky on Windows")
def test_set_flake8_using_init_opts(client_server_pair) -> None:
    client, server = client_server_pair
    send_initialize_request(client, INITIALIZATION_OPTIONS)
    for key, value in INITIALIZATION_OPTIONS["pylsp"]["plugins"].items():
        assert server.workspace._config.settings().get("plugins").get(key).get(
            "enabled"
        ) == value.get("enabled")


@pytest.mark.skipif(IS_WIN, reason="Flaky on Windows")
def test_set_flake8_using_workspace_did_change_configuration(
    client_server_pair,
) -> None:
    client, server = client_server_pair
    send_initialize_request(client, None)
    assert (
        server.workspace._config.settings().get("plugins").get("flake8").get("enabled")
        is False
    )

    with patch.object(server.workspace, "update_config") as mock_update_config:
        client._endpoint.notify(
            "workspace/didChangeConfiguration",
            {"settings": INITIALIZATION_OPTIONS},
        )
        wait_for_condition(lambda: mock_update_config.call_count >= 1)

        for key, value in INITIALIZATION_OPTIONS["pylsp"]["plugins"].items():
            assert server.workspace._config.settings().get("plugins").get(key).get(
                "enabled"
            ) == value.get("enabled")


@pytest.fixture()
def config(tmp_path):
    """A Config rooted in a temporary directory, without any LSP client."""
    return Config(uris.from_fs_path(str(tmp_path)), {}, 0, {})


def test_document_settings_override_and_remove(config) -> None:
    """Two documents get independent overrides; removing one re-inherits the
    workspace value without affecting the other document."""
    config.update({"plugins": {"pycodestyle": {"enabled": True, "maxLineLength": 88}}})

    doc_a = os.path.join(config._root_path, "a.py")
    doc_b = os.path.join(config._root_path, "b.py")
    config.update_document_settings(
        doc_a, {"plugins": {"pycodestyle": {"maxLineLength": 100}}}
    )
    config.update_document_settings(
        doc_b, {"plugins": {"pycodestyle": {"maxLineLength": 120}}}
    )

    def max_line_length(document_path):
        return config.settings(document_path=document_path)["plugins"]["pycodestyle"][
            "maxLineLength"
        ]

    assert max_line_length(doc_a) == 100
    assert max_line_length(doc_b) == 120

    # Revoking doc_a's override makes it inherit the workspace value again,
    # while doc_b's override is unaffected.
    assert config.remove_document_settings(doc_a) is True
    assert max_line_length(doc_a) == 88
    assert max_line_length(doc_b) == 120

    # Removing a non-existent override is a no-op.
    assert config.remove_document_settings(doc_a) is False
    assert max_line_length(doc_b) == 120


def test_document_settings_explicit_none_vs_remove(config) -> None:
    """An explicit None override nulls out a setting and survives later
    workspace updates, while removing the override re-inherits (and follows)
    the workspace value."""
    config.update({"plugins": {"flake8": {"config": "workspace.cfg", "enabled": True}}})

    doc_none = os.path.join(config._root_path, "none.py")
    doc_removed = os.path.join(config._root_path, "removed.py")
    override = {"plugins": {"flake8": {"config": None}}}
    config.update_document_settings(doc_none, override)
    config.update_document_settings(doc_removed, override)

    def flake8_config(document_path):
        return config.settings(document_path=document_path)["plugins"]["flake8"][
            "config"
        ]

    # Explicit None overrides the workspace value (unlike merge_dicts' default
    # behaviour of ignoring Nones).
    assert flake8_config(doc_none) is None
    assert flake8_config(doc_removed) is None

    # A later workspace settings change does not revive the nulled value for
    # the document that keeps its explicit None override...
    config.update(
        {"plugins": {"flake8": {"config": "updated.cfg", "enabled": True}}}
    )
    assert flake8_config(doc_none) is None

    # ...but the document whose override was removed follows the workspace.
    config.remove_document_settings(doc_removed)
    assert flake8_config(doc_removed) == "updated.cfg"

    # Other keys from the workspace update still flow through the None override.
    assert config.settings(document_path=doc_none)["plugins"]["flake8"][
        "enabled"
    ] is True
