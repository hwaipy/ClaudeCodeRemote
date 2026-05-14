"""Fixture overrides so tests in this dir hit the snapshot-backed server.

`page` is auto-provided by pytest-playwright; it picks up `base_url` to use
as the default origin. Override that and everything (page, fresh_page,
logged_in_page) routes to the live server.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def base_url(live_server_env):
    return live_server_env["base_url"]


@pytest.fixture(scope="session")
def test_token(live_server_env):
    return live_server_env["token"]
