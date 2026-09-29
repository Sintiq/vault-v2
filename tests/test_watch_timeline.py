"""The watch's day in the Health timeline: measured lines, the watch named as the source."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vault_v2.api import ApiServer, VaultAPI, load_or_create_key
from vault_v2.cards import CardStore
from vault_v2.date_dialog import date_evidence
from vault_v2.health import WATCH_FALLBACK_SOURCE, HealthStore, watch_entries, watch_sha256
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths
from vault_v2.tasks import TaskStore

DAY = "2026-09-24"
READINGS = {"steps": 4245, "heart_min": 42, "heart_avg": 54, "heart_max": 87,
            "sleep_minutes": 345, "sleep_end": "07:05", "oxygen": 90.0,
            "steps_source": "Galaxy Watch Ultra via Samsung Health",
            "heart_source": "Galaxy Watch Ultra via Samsung Health",
            "sleep_source": "Galaxy Watch Ultra via Samsung Health",
            "oxygen_source": "Galaxy Watch Ultra via Samsung Health"}
WATCH = "Galaxy Watch Ultra via Samsung Health"


@pytest.fixture()
def env(tmp_path: Path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    tasks = TaskStore(ops.paths.root / ".tasks", ops.log)
    health = HealthStore(ops.paths.root / ".health", ops.log)
    return ops, health, VaultAPI(ops, cards, tasks, health)


# -- the lines themselves ------------------------------------------------------


def test_a_full_day_becomes_four_measured_lines_naming_the_watch() -> None:
    entries = watch_entries(DAY, READINGS)
    assert [e.label for e in entries] == [f"Steps {DAY}", f"Heart rate {DAY}", f"Sleep {DAY}", f"Blood oxygen {DAY}"]
    assert [e.quote for e in entries] == ["4245 steps", "heart rate 42–87 bpm, average 54",
                                          "sleep 5 h 45 min, woke 07:05", "blood oxygen 90%"]
    for e in entries:
        assert e.doc_name == WATCH and e.origin == "WATCH" and e.kind == "WATCH"
        assert e.date == DAY and e.due_source == "device" and e.flags == ()


def test_each_line_names_what_health_connect_named_or_says_it_could_not() -> None:
    mixed = {"steps": 10, "steps_source": "SM-G781V via Samsung Health", "oxygen": 95}
    steps, oxygen = watch_entries(DAY, mixed)
    assert steps.doc_name == "SM-G781V via Samsung Health"
    assert oxygen.doc_name == WATCH_FALLBACK_SOURCE, "no source named: no watch is claimed"
    for junk in (42, "", "   ", "\x00\x01", ["Galaxy"]):
        assert watch_entries(DAY, {"steps": 1, "steps_source": junk})[0].doc_name == WATCH_FALLBACK_SOURCE
    long = watch_entries(DAY, {"steps": 1, "steps_source": "x" * 300})[0].doc_name
    assert len(long) == 80
    assert watch_entries(DAY, {"steps": 1, "steps_source": "  Galaxy\n Watch  "})[0].doc_name == "Galaxy Watch"


def test_readings_must_cohere_not_merely_fit_their_ranges() -> None:
    with pytest.raises(ValueError):
        watch_entries(DAY, {"heart_min": 120, "heart_max": 60})
    with pytest.raises(ValueError):
        watch_entries(DAY, {"heart_min": 60, "heart_avg": 200, "heart_max": 120})
    assert watch_entries(DAY, {"heart_min": 60, "heart_avg": 60, "heart_max": 60})[0].quote == "heart rate 60–60 bpm, average 60"
    assert watch_entries(DAY, {"sleep_minutes": 30, "sleep_end": "99:99"})[0].quote == "sleep 30 min"
    assert watch_entries(DAY, {"sleep_minutes": 30, "sleep_end": "23:59"})[0].quote == "sleep 30 min, woke 23:59"


def test_absent_metrics_are_simply_not_lines() -> None:
    assert [e.label for e in watch_entries(DAY, {"steps": 12})] == [f"Steps {DAY}"]
    assert watch_entries(DAY, {}) == []
    # Heart needs both ends; the average alone is not a range.
    assert watch_entries(DAY, {"heart_avg": 60}) == []
    assert watch_entries(DAY, {"sleep_minutes": 40})[0].quote == "sleep 40 min"
    assert watch_entries(DAY, {"sleep_minutes": 60, "sleep_end": "7am"})[0].quote == "sleep 1 h 0 min"


@pytest.mark.parametrize("day", ["24.09.2026", "2026-13-01", "2026-09", 20260924, None])
def test_the_day_must_be_a_real_calendar_date(day) -> None:
    with pytest.raises(ValueError):
        watch_entries(day, READINGS)


@pytest.mark.parametrize("bad", [{"steps": -1}, {"steps": "4245"}, {"steps": True}, {"oxygen": 101},
                                 {"heart_min": 10, "heart_max": 80}, {"sleep_minutes": 0}, "not an object"])
def test_readings_outside_the_body_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        watch_entries(DAY, bad)


def test_the_same_day_maps_to_the_same_entry_ids() -> None:
    first = watch_entries(DAY, READINGS)
    again = watch_entries(DAY, {**READINGS, "steps": 9000})
    assert [e.id for e in first] == [e.id for e in again]
    assert watch_sha256(DAY, "steps") != watch_sha256("2026-09-25", "steps")


# -- in the store --------------------------------------------------------------


def test_sending_a_day_twice_replaces_rather_than_duplicates(env) -> None:
    _ops, health, _api = env
    health.add_watch(DAY, READINGS)
    health.add_watch(DAY, {**READINGS, "steps": 9000})
    entries = health.all()
    assert len(entries) == 4
    steps = next(e for e in entries if e.label == f"Steps {DAY}")
    assert steps.quote == "9000 steps"
    # Read back through the date check: a device date is kept as measured.
    assert steps.date == DAY and steps.due_source == "device" and steps.flags == ()
    assert date_evidence(steps.due_source, steps.flags, steps.quote) == "measured by the device named on the line"


def test_each_watch_line_is_receipted_like_any_health_add(env) -> None:
    ops, health, _api = env
    health.add_watch(DAY, {"steps": 100, "oxygen": 95, "steps_source": WATCH, "oxygen_source": WATCH})
    rows = [r for r in ops.log.tail(10) if r["op"] == "health_add"]
    assert len(rows) == 2 and all(Path(r["src"]).name == WATCH for r in rows)
    assert {json.loads(r["extra"])["kind"] if isinstance(r["extra"], str) else r["extra"]["kind"] for r in rows} == {"WATCH"}


def test_a_day_is_one_write_and_one_generation_however_many_lines(env) -> None:
    _ops, health, _api = env
    before = health.generation
    assert len(health.add_watch(DAY, READINGS)) == 4
    assert health.generation == before + 1, "one guard, one save: nothing can land between two lines of a day"
    assert health.add_watch(DAY, {}) == [] and health.generation == before + 1, "nothing to write takes no guard"


def test_an_owner_set_date_on_a_watch_line_survives_the_next_send(env) -> None:
    _ops, health, _api = env
    [entry] = health.add_watch(DAY, {"steps": 100})
    health.set_date(entry.id, "2026-09-23")
    health.add_watch(DAY, {"steps": 200})
    [seen] = health.all()
    assert seen.quote == "200 steps" and seen.date == "2026-09-23" and seen.due_source == "owner"


# -- through the API -----------------------------------------------------------


def test_the_api_adds_the_day_and_lists_it_under_its_year(env) -> None:
    _ops, _health, api = env
    result = api.add_watch_readings({"day": DAY, "readings": READINGS})
    assert result["added"] == 4 and result["labels"][0] == f"Steps {DAY}"
    listed = api.list_health()
    assert listed["years"][0]["year"] == "2026"
    line = listed["years"][0]["entries"][0]
    assert line["doc"] == WATCH and line["due_source"] == "device" and line["kind"] == "WATCH"
    assert "4 watch" in listed["summary"]


def test_the_api_refuses_what_is_not_a_day(env) -> None:
    _ops, _health, api = env
    with pytest.raises(ValueError):
        api.add_watch_readings({"day": "yesterday", "readings": READINGS})
    with pytest.raises(Exception):
        api.add_watch_readings(["not", "an", "object"])


@pytest.fixture()
def server(env, tmp_path: Path):
    ops, _health, api = env
    web = tmp_path / "web"
    web.mkdir()
    page = web / "index.html"
    page.write_text("<html>vault</html>", encoding="utf-8")
    (web / "manifest.webmanifest").write_text('{"name": "Vault"}', encoding="utf-8")
    (web / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (web / "icon-512.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    srv = ApiServer(api, load_or_create_key(ops.paths.root), page,
                    lambda text: "answer: " + text,
                    lambda: {"backend": "fake", "ready": True, "history": []},
                    "127.0.0.1", 0)
    srv.start()
    yield srv
    srv.stop()


def _post(srv, path: str, body: object, key: str | None):
    req = urllib.request.Request(srv.url.rstrip("/") + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_over_the_wire_the_day_needs_the_key_and_a_real_body(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/health/watch", {"day": DAY, "readings": READINGS}, None)
    assert exc.value.code == 401
    assert _post(server, "/api/health/watch", {"day": DAY, "readings": READINGS}, server.key)["added"] == 4
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/health/watch", {"day": DAY, "readings": {"steps": -5}}, server.key)
    assert exc.value.code == 400


def test_the_manifest_and_icons_are_served_without_a_key_so_the_page_can_be_pinned(server) -> None:
    for path, ctype in (("/manifest.webmanifest", "application/manifest+json"), ("/icon-192.png", "image/png"),
                        ("/icon-512.png", "image/png")):
        with urllib.request.urlopen(server.url.rstrip("/") + path, timeout=5) as r:
            assert r.status == 200 and r.headers["Content-Type"] == ctype
            assert len(r.read()) > 0
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server.url.rstrip("/") + "/icon-64.png", timeout=5)
    assert exc.value.code == 401, "anything else still needs the key"
