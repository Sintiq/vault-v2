"""The MCP door: what it does, what it refuses, and what it writes down."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.phone_mcp.server import Door


@pytest.fixture()
def door(tmp_path: Path) -> Door:
    return Door(tmp_path)


def test_it_refuses_to_knock_before_it_is_paired(door: Door) -> None:
    assert not door.paired
    result = door.call("/door/battery", "read")
    assert "not paired" in result["error"]
    assert [json.loads(l)["outcome"] for l in door.log_file.read_text().splitlines()] == ["unreachable"]


def test_an_unreachable_phone_says_so_instead_of_guessing(door: Door) -> None:
    (door.dir / "config.json").write_text(
        json.dumps({"address": "http://127.0.0.1:1", "key": "k"}), encoding="utf-8")
    result = door.call("/door/battery", "read")
    assert "did not answer" in result["error"] and "Tailscale" in result["error"]


def test_a_location_call_is_logged_without_saying_where(door: Door) -> None:
    door.note("/door/location", "owner-only", "answered",
              "requested by the owner; coordinates not recorded")
    written = door.log_file.read_text(encoding="utf-8")
    assert "coordinates not recorded" in written
    for forbidden in ("latitude", "longitude", "accuracy"):
        assert forbidden not in written


def test_only_the_passport_tools_exist() -> None:
    from tools.phone_mcp import server as mod

    exposed = {
        name for name in dir(mod)
        if name.startswith("phone_") and callable(getattr(mod, name))
    }
    assert exposed == {
        "phone_status", "phone_battery", "phone_network", "phone_location",
        "phone_ring", "phone_stop_ringing", "phone_open_app", "phone_receipts",
        "phone_screenshot", "phone_tap", "phone_swipe", "phone_press",
        "phone_stop_using_screen",
    }
    # The passport's "never" list must have no implementation to be talked
    # into. Tapping is allowed now, by the owner; reading his screen is not,
    # and neither is typing, which cannot be done without reading.
    source = Path(mod.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("sms", "messages", "contacts", "call_log", "uninstall",
                      "type_text", "read_screen", "screen_text"):
        assert f"def phone_{forbidden}" not in source


def test_a_refused_picture_comes_back_as_words_not_as_bytes(door: Door) -> None:
    """A tool that returns an image must not return a JSON error as an image."""
    (door.dir / "config.json").write_text(
        json.dumps({"address": "http://127.0.0.1:1", "key": "k"}), encoding="utf-8")
    result = door.fetch_png("/door/screenshot", "owner-only")
    assert isinstance(result, str) and "did not answer" in result
    assert json.loads(door.log_file.read_text().splitlines()[-1])["outcome"] == "unreachable"


def test_a_picture_is_logged_by_size_and_never_by_content(door: Door) -> None:
    door.note("/door/screenshot", "owner-only", "answered", "412 KB, not stored")
    written = door.log_file.read_text(encoding="utf-8")
    assert "not stored" in written and "KB" in written
    assert "data:image" not in written and "iVBOR" not in written
