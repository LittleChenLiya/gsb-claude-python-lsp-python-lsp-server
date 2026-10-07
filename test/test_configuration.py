# Copyright 2021- Python Language Server Contributors.

import os
from unittest.mock import patch

import pytest

from pylsp import IS_WIN, uris
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


def _document_paths(config):
    root_path = uris.to_fs_path(config.root_uri)
    return (
        os.path.join(root_path, "doc_a.py"),
        os.path.join(root_path, "doc_b.py"),
    )


def test_document_setting_override_and_remove(config) -> None:
    """Two documents can override the same setting independently, and
    removing one document's override makes it inherit the workspace value
    again without affecting the other document."""
    config.update({"plugins": {"pycodestyle": {"maxLineLength": 79}}})
    doc_a, doc_b = _document_paths(config)
    key = "plugins.pycodestyle.maxLineLength"

    config.set_document_setting(doc_a, key, 100)
    config.set_document_setting(doc_b, key, 120)

    assert config.settings(document_path=doc_a)["plugins"]["pycodestyle"][
        "maxLineLength"
    ] == 100
    assert config.settings(document_path=doc_b)["plugins"]["pycodestyle"][
        "maxLineLength"
    ] == 120
    # The workspace-wide settings are not affected by document overrides.
    assert config.settings()["plugins"]["pycodestyle"]["maxLineLength"] == 79

    # Removing doc_a's override makes it inherit the workspace value again...
    config.remove_document_setting(doc_a, key)
    assert config.settings(document_path=doc_a)["plugins"]["pycodestyle"][
        "maxLineLength"
    ] == 79
    # ...while doc_b keeps its own override.
    assert config.settings(document_path=doc_b)["plugins"]["pycodestyle"][
        "maxLineLength"
    ] == 120

    # Removing an override that does not exist is a no-op.
    config.remove_document_setting(doc_a, key)
    assert config.settings(document_path=doc_a)["plugins"]["pycodestyle"][
        "maxLineLength"
    ] == 79


def test_document_setting_none_differs_from_remove(config) -> None:
    """An explicit None override is kept as None (even across workspace
    updates), while removing the override re-inherits the workspace value."""
    config.update({"plugins": {"jedi": {"environment": "/usr/bin/python"}}})
    doc_a, doc_b = _document_paths(config)
    key = "plugins.jedi.environment"

    # doc_a explicitly sets the value to None, doc_b removes nothing.
    config.set_document_setting(doc_a, key, None)
    assert config.settings(document_path=doc_a)["plugins"]["jedi"][
        "environment"
    ] is None
    assert config.settings(document_path=doc_b)["plugins"]["jedi"][
        "environment"
    ] == "/usr/bin/python"

    # A later workspace configuration change does not wash out the explicit
    # None override...
    config.update({"plugins": {"jedi": {"environment": "/other/python"}}})
    assert config.settings(document_path=doc_a)["plugins"]["jedi"][
        "environment"
    ] is None
    assert config.settings(document_path=doc_b)["plugins"]["jedi"][
        "environment"
    ] == "/other/python"

    # ...but removing the override makes the document follow the workspace
    # value again.
    config.remove_document_setting(doc_a, key)
    assert config.settings(document_path=doc_a)["plugins"]["jedi"][
        "environment"
    ] == "/other/python"
