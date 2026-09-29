"""Owner-approved archive Ask seams, using synthetic roots only."""
import json

import pytest

from vault_v2.agent_scope import AgentTextScope
from vault_v2.ops import VaultOps
from vault_v2.paths import VaultPaths


def test_owner_grant_is_persistent_recursive_receipted_and_revocable(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    folder = ops.paths.personal / "IMMIGRATION"
    child = folder / "nested"
    child.mkdir(parents=True)
    source = child / "form.txt"
    source.write_text("synthetic visa", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    assert scope.covering_folder(source) is None

    scope.set_folder(folder, True)
    reloaded = AgentTextScope(ops.paths, ops.log)
    assert reloaded.covering_folder(source) == folder
    assert ops.paths.settings()["agent_text_folders"] == ["personal/IMMIGRATION"]
    reloaded.set_folder(folder, False)
    assert scope.covering_folder(source) is None
    assert [row["op"] for row in ops.log.tail()] == ["agent_scope_granted", "agent_scope_revoked"]


def test_archive_cards_are_searchable_in_each_pane_without_reading_unmarked_text(tmp_path):
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    for pane in ("staging", "documents", "personal"):
        source = ops.paths.pane(pane) / "immigration.txt"
        source.write_text("ONLYINTEXT", encoding="utf-8")
    (ops.paths.personal / ".hidden.txt").write_text("immigration", encoding="utf-8")
    docs = collect_archive_docs(ops.paths, cards)
    result = ask("immigration", docs, None)
    assert {(doc.pane, doc.rel) for doc in result.docs} == {
        ("staging", "immigration.txt"), ("documents", "immigration.txt"), ("personal", "immigration.txt")}
    assert len(result.proposed_ids) == 3
    assert ask("ONLYINTEXT", collect_archive_docs(ops.paths, cards), None).proposed_ids == ()
    assert not (ops.paths.root / ".text").exists()


def test_only_marked_path_gets_cached_text_and_revocation_stops_the_next_ask(tmp_path):
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    folder = ops.paths.personal / "allowed"
    folder.mkdir()
    allowed, denied = folder / "one.txt", ops.paths.documents / "two.txt"
    for path in (allowed, denied):
        path.write_text("СИНТЕТИЧЕСКАЯВИЗА", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(folder, True)
    assert scope.warm(folder) == ()

    docs = collect_archive_docs(ops.paths, cards)
    by_path = {doc.path: doc for doc in docs}
    assert by_path[allowed].excerpt == "СИНТЕТИЧЕСКАЯВИЗА"
    assert by_path[denied].excerpt == ""
    assert ask("СИНТЕТИЧЕСКАЯВИЗА", docs, None).proposed_ids == (by_path[allowed].id,)
    reads = [row for row in ops.log.tail() if row["op"] == "agent_read"]
    assert len(reads) == 1 and reads[0]["src"] == str(allowed)

    scope.set_folder(folder, False)
    assert ask("СИНТЕТИЧЕСКАЯВИЗА", collect_archive_docs(ops.paths, cards), None).proposed_ids == ()
    assert len(list((ops.paths.root / ".text").glob("*.txt"))) == 1


def test_local_model_receives_only_authorized_cached_excerpt_and_rejects_stale_scope(tmp_path):
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    from vault_v2.agent_scope import ScopeChanged

    class LocalRecorder(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")

        def __init__(self):
            self.seen = []

        def chat(self, system, messages, on_chunk):
            self.seen.append((system, messages))
            return '{"documents": ["doc-001"], "recipient": "PERSONAL", "reason": "synthetic"}'

    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    folder = ops.paths.documents / "allowed"
    folder.mkdir()
    (folder / "form.txt").write_text("ALLOWED_EXCERPT", encoding="utf-8")
    (ops.paths.personal / "private.txt").write_text("FORBIDDEN_EXCERPT", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(folder, True)
    scope.warm(folder)
    docs, backend = collect_archive_docs(ops.paths, cards), LocalRecorder()
    result = ask("form", docs, backend)
    assert "ALLOWED_EXCERPT" in json.dumps(backend.seen)
    assert "FORBIDDEN_EXCERPT" not in json.dumps(backend.seen)
    assert "model saw 2 of 2 candidates" in result.notes
    scope.set_folder(folder, False)
    with pytest.raises(ScopeChanged):
        ask("form", docs, backend)
    assert len(backend.seen) == 1


def test_child_cannot_claim_revocation_when_parent_still_grants_text(tmp_path):
    from vault_v2.errors import VaultError
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    parent = ops.paths.personal / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(child, True)
    scope.set_folder(parent, True)
    before = ops.log.tail()
    with pytest.raises(VaultError, match="parent"):
        scope.set_folder(child, False)
    assert ops.log.tail() == before
    assert scope.covering_folder(child) == parent
    scope.set_folder(parent, False)
    assert scope.covering_folder(child) is None


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows case-insensitive pane aliases")
def test_windows_pane_case_is_saved_canonically(tmp_path):
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    folder = ops.paths.personal / "Grant"
    folder.mkdir()
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.root / "PERSONAL" / "Grant", True)
    assert AgentTextScope(ops.paths, ops.log).covering_folder(folder) == folder
    assert ops.paths.settings()["agent_text_folders"] == ["personal/Grant"]


def test_copy_archive_selection_is_unique_and_export_stays_staging_only(tmp_path):
    from vault_v2.ask import collect_archive_docs, copy_to_staging
    from vault_v2.cards import CardStore
    from vault_v2.gatekeeper import Gatekeeper
    from vault_v2.gatekeeper import GateError
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.personal / "report.txt"
    source.write_text("synthetic archive", encoding="utf-8")
    (ops.paths.staging / "report.txt").write_text("keep original", encoding="utf-8")
    selected = [doc for doc in collect_archive_docs(ops.paths, cards) if doc.pane == "personal"]
    copied = copy_to_staging(ops, selected)
    assert len(copied) == 1 and copied[0].dst == ops.paths.staging / "report (1).txt"
    assert copied[0].dst.read_text(encoding="utf-8") == "synthetic archive"
    assert (ops.paths.staging / "report.txt").read_text(encoding="utf-8") == "keep original"
    assert ops.log.tail()[-1]["op"] == "copy"
    assert source.exists()
    with pytest.raises(GateError, match="only Staging"):
        Gatekeeper(ops).prepare([source], tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_archive_budget_ranks_candidates_and_discloses_the_model_subset(tmp_path):
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.ask import AskCollection, AskDoc, ask
    from vault_v2.cards import Card

    class Recorder(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")
        seen = None

        def chat(self, system, messages, on_chunk):
            self.seen = json.loads(messages[0]["content"].split("Documents:\n", 1)[1])
            return '{"documents": ["doc-060"], "recipient": "PERSONAL", "reason": "supported"}'

    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    scope = AgentTextScope(ops.paths, ops.log)
    docs = []
    for index in range(1, 61):
        name = "priority-visa.txt" if index == 60 else f"other-{index:03d}.txt"
        card = Card.build(f"{index:064x}", name, "TEXT", shelf="INBOX", topics=["a" * 35 + str(i) for i in range(8)],
                          issuer="Synthetic " + "R" * 100, confirmed=True)
        docs.append(AskDoc(f"doc-{index:03d}", ops.paths.documents / name, card,
                           pane="documents", rel=name))
    backend = Recorder()
    result = ask("priority visa", AskCollection(docs, scope, scope.revision()), backend)
    assert 0 < len(backend.seen) < 60
    assert backend.seen[0]["id"] == "doc-060"
    assert f"model saw {len(backend.seen)} of 60 candidates" in result.notes
    assert result.proposed_ids == ("doc-060",)


def test_marking_another_folder_does_not_abort_an_authorized_warm_job(tmp_path, monkeypatch):
    from pathlib import Path
    from vault_v2.text_cache import DocumentTextCache
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    first, second = ops.paths.documents / "first", ops.paths.personal / "second"
    first.mkdir()
    second.mkdir()
    source = first / "one.txt"
    source.write_text("synthetic", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(first, True)
    original_open, triggered = Path.open, []

    def grant_during_source_read(path, *args, **kwargs):
        if path == source and args and args[0] == "rb" and not triggered:
            triggered.append(True)
            scope.set_folder(second, True)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grant_during_source_read)
    assert scope.warm(first) == ()
    assert DocumentTextCache(ops.paths, ops.log).read_cached_snapshot(source).text == "synthetic"


def test_ask_notes_count_only_real_pending_extractions(tmp_path, monkeypatch):
    from pathlib import Path
    from threading import Event, Thread, get_ident
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    folder = ops.paths.personal / "allowed"
    folder.mkdir()
    for name in ("one.txt", "two.txt"):
        (folder / name).write_text("synthetic visa", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(folder, True)
    entered, release = Event(), Event()
    caller, errors = get_ident(), []
    original_open = Path.open

    def slow_source(path, *args, **kwargs):
        if path == folder / "one.txt" and get_ident() != caller and args and args[0] == "rb":
            entered.set()
            assert release.wait(5)
        return original_open(path, *args, **kwargs)

    def warm():
        try:
            scope.warm(folder)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, "open", slow_source)
    worker = Thread(target=warm)
    worker.start()
    try:
        assert entered.wait(3)
        result = ask("visa", collect_archive_docs(ops.paths, cards), None)
        assert "2 documents in marked folders are still being read — ask again in a minute" in result.notes
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and errors == []
    assert not any("still being read" in note for note in ask("visa", collect_archive_docs(ops.paths, cards), None).notes)


@pytest.mark.parametrize("contents", ['[]', '{broken', '{"agent_text_folders":true}',
                                        '{"agent_text_folders":[],"agent_text_folders":[]}',
                                        '{"agent_text_folders":["personal/../documents"]}'])
def test_bad_scope_settings_refuse_without_overwriting_or_receipting(tmp_path, contents):
    from vault_v2.errors import VaultError
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    ops.paths.settings_file.write_text(contents, encoding="utf-8")
    with pytest.raises(VaultError):
        AgentTextScope(ops.paths, ops.log).set_folder(ops.paths.personal, True)
    assert ops.paths.settings_file.read_text(encoding="utf-8") == contents
    assert ops.log.tail() == []


def test_grant_preserves_other_settings_and_hidden_folder_is_refused(tmp_path):
    from vault_v2.errors import VaultError
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    initial = {"ollama_model": "synthetic", "titles": {"personal": "Private"}, "future_setting": [1, 2]}
    ops.paths.settings_file.write_text(json.dumps(initial), encoding="utf-8")
    hidden = ops.paths.personal / ".hidden"
    hidden.mkdir()
    scope = AgentTextScope(ops.paths, ops.log)
    with pytest.raises(VaultError):
        scope.set_folder(hidden, True)
    assert ops.paths.settings() == initial and ops.log.tail() == []
    scope.set_folder(ops.paths.personal, True)
    scope.set_folder(ops.paths.personal, False)
    assert all(ops.paths.settings()[key] == value for key, value in initial.items())


def test_revoked_cache_is_never_opened_and_warm_cannot_publish_after_revoke(tmp_path, monkeypatch):
    from pathlib import Path
    from vault_v2.agent_scope import ScopeChanged
    from vault_v2.ask import collect_archive_docs
    from vault_v2.cards import CardStore
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.personal / "one.txt"
    source.write_text("synthetic secret", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    original_open, revoke = Path.open, [True]

    def revoke_on_source_read(path, *args, **kwargs):
        if path == source and args and args[0] == "rb" and revoke:
            revoke.pop()
            scope.set_folder(ops.paths.personal, False)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", revoke_on_source_read)
    with pytest.raises(ScopeChanged):
        scope.warm(ops.paths.personal)
    assert not (ops.paths.root / ".text").exists()
    assert not scope.is_pending(source)
    scope.set_folder(ops.paths.personal, True)
    assert scope.warm(ops.paths.personal) == ()
    scope.set_folder(ops.paths.personal, False)

    def forbid_cache_open(path, *args, **kwargs):
        assert not path.is_relative_to(ops.paths.root / ".text"), "revoked cache was accessed"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_cache_open)
    assert collect_archive_docs(ops.paths, cards)[0].excerpt == ""


@pytest.mark.parametrize("reply", ['not json', '{"documents":["doc-001"]}'])
def test_inflight_reply_is_rejected_if_scope_changes(tmp_path, reply):
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.agent_scope import ScopeChanged
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    (ops.paths.personal / "one.txt").write_text("visa", encoding="utf-8")
    scope.warm(ops.paths.personal)

    class Revoker(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")

        def chat(self, system, messages, on_chunk):
            scope.set_folder(ops.paths.personal, False)
            return reply

    docs = collect_archive_docs(ops.paths, CardStore(ops.paths.root / ".cards", ops.log))
    with pytest.raises(ScopeChanged):
        ask("visa", docs, Revoker())


def test_cached_excerpt_has_6000_limit_coverage_receipt_and_card_year_matches(tmp_path):
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import Card, CardStore
    from vault_v2.receipts import sha256_file
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    source = ops.paths.personal / "neutral.txt"
    source.write_text("a" * 7000, encoding="utf-8")
    cards.confirm(Card.build(sha256_file(source), source.name, "TEXT", shelf="INBOX", topics=["synthetic"],
                             year=2025, confirmed=True), source)
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    scope.warm(ops.paths.personal)
    docs = collect_archive_docs(ops.paths, cards)
    assert len(docs[0].excerpt) == 6000 and docs[0].total_chars == 7000
    assert any("6 000 of 7 000" in note for note in docs[0].reading_notes)
    receipt = [row for row in ops.log.tail() if row["op"] == "agent_read"][-1]
    assert receipt["extra"]["read_chars"] == 6000 and receipt["extra"]["total_chars"] == 7000
    assert ask("2025", docs, None).proposed_ids == (docs[0].id,)


@pytest.mark.parametrize("change", ["move", "trash", "replace"])
def test_collected_excerpt_cannot_outlive_its_visible_authorized_source(tmp_path, change):
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.ask import ask, collect_archive_docs
    from vault_v2.cards import CardStore
    from vault_v2.errors import VaultError
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "one.txt"
    source.write_text("synthetic private visa", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    scope.warm(ops.paths.personal)
    docs = collect_archive_docs(ops.paths, CardStore(ops.paths.root / ".cards", ops.log))

    class Recorder(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")
        calls = 0

        def chat(self, system, messages, on_chunk):
            self.calls += 1
            return '{"documents": ["doc-001"]}'

    if change == "move":
        ops.move(source, ops.paths.documents)
    elif change == "trash":
        ops.trash(source)
    else:
        source.write_text("replacement file", encoding="utf-8")
    backend = Recorder()
    with pytest.raises(VaultError):
        ask("visa", docs, backend)
    assert backend.calls == 0


def test_candidates_not_sent_to_model_are_not_marked_omitted_by_agent(tmp_path):
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.ask import AskCollection, AskDoc, ask
    from vault_v2.cards import Card

    class Recorder(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")
        ids = []

        def chat(self, system, messages, on_chunk):
            records = json.loads(messages[0]["content"].split("Documents:\n", 1)[1])
            self.ids = [record["id"] for record in records]
            return json.dumps({"documents": self.ids + ["doc-060"], "recipient": "PERSONAL"})

    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    scope = AgentTextScope(ops.paths, ops.log)
    docs = []
    for index in range(1, 61):
        name = f"visa-{index:03d}.txt"
        card = Card.build(f"{index:064x}", name, "TEXT", shelf="INBOX",
                          topics=["a" * 35 + str(i) for i in range(8)], issuer="R" * 100, confirmed=True)
        docs.append(AskDoc(f"doc-{index:03d}", ops.paths.documents / name, card,
                           pane="documents", rel=name))
    backend = Recorder()
    result = ask("visa", AskCollection(docs, scope, scope.revision()), backend)
    assert 0 < len(backend.ids) < 60
    assert result.agent_ids == tuple(backend.ids)  # the model cannot claim an unseen id
    assert result.proposed_ids == tuple(doc.id for doc in docs)  # local hits survive its budget
    assert result.omitted_by_agent == ()
    assert len(result.not_seen_by_agent) == 60 - len(backend.ids)


def test_one_record_that_cannot_fit_with_question_refuses_before_model(tmp_path):
    from vault_v2.agent import Backend, BackendInfo
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    from vault_v2.document_budget import DocumentBudgetExceeded

    class Recorder(Backend):
        info = BackendInfo("ollama", "synthetic", "local test adapter")
        calls = 0

        def chat(self, system, messages, on_chunk):
            self.calls += 1
            return '{"documents": []}'

    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    source = ops.paths.personal / "synthetic.txt"
    source.write_text("я" * 6000, encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    scope.warm(ops.paths.personal)
    docs = collect_archive_docs(ops.paths, CardStore(ops.paths.root / ".cards", ops.log))
    backend = Recorder()
    with pytest.raises(DocumentBudgetExceeded) as exc:
        ask("question " * 1100, docs, backend)
    assert "personal/synthetic.txt" in str(exc.value)
    assert backend.calls == 0 and len(docs[0].excerpt) == 6000


def test_synthetic_pdf_and_text_are_ready_cache_inputs_not_fresh_ocr_in_ask(tmp_path, monkeypatch):
    from tools.synthetic_ocr_files import text_pdf
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    from pathlib import Path
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    folder = ops.paths.personal / "Marked"
    folder.mkdir()
    pdf = text_pdf(folder / "neutral.pdf", "Synthetic immigration supporting document")
    text = folder / "neutral.txt"
    text.write_text("Synthetic immigration supporting text", encoding="utf-8")
    denied = ops.paths.documents / "neutral.txt"
    denied.write_text("Synthetic immigration unsupported text", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(folder, True)
    assert scope.warm(folder) == ()
    receipts = [row for row in ops.log.tail() if row["op"] == "text_extracted"]
    original_open = Path.open

    def no_text_source_decode(path, mode="r", *args, **kwargs):
        if path in {text, denied}:
            assert "b" in mode, "Ask must only hash the source, not decode it"
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_text_source_decode)
    docs = collect_archive_docs(ops.paths, cards)
    result = ask("immigration", docs, None)
    assert {doc.path for doc in docs if doc.id in result.proposed_ids} == {pdf, text}
    assert [row for row in ops.log.tail() if row["op"] == "text_extracted"] == receipts
    assert not any(row["op"] == "agent_read" and row["src"] == str(denied) for row in ops.log.tail())


def test_new_marked_file_first_ask_queues_once_without_waiting_then_second_finds_text(tmp_path, monkeypatch):
    from pathlib import Path
    from threading import Event, get_ident
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    source = ops.paths.personal / "neutral.txt"
    source.write_text("NEWUNIQUESYNTHETICNEEDLE", encoding="utf-8")
    entered, release = Event(), Event()
    original_open, caller = Path.open, get_ident()
    background_readers = set()

    def block_background_read(path, mode="r", *args, **kwargs):
        if path == source and get_ident() != caller and mode == "rb":
            background_readers.add(get_ident())
            entered.set()
            assert release.wait(8)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", block_background_read)
    try:
        first = ask("NEWUNIQUESYNTHETICNEEDLE", collect_archive_docs(ops.paths, cards), None)
        assert first.proposed_ids == ()
        assert "1 documents in marked folders are still being read — ask again in a minute" in first.notes
        assert entered.wait(2)
        for _ in range(3):
            repeat = collect_archive_docs(ops.paths, cards)
            assert repeat[0].excerpt == ""
        assert len(background_readers) == 1
    finally:
        release.set()
    assert scope.queue.wait_idle(8)
    assert not scope.queue.busy
    second = ask("NEWUNIQUESYNTHETICNEEDLE", collect_archive_docs(ops.paths, cards), None)
    assert second.proposed_ids == (second.docs[0].id,)
    assert len([row for row in ops.log.tail() if row["op"] == "text_extracted"]) == 1


def test_folder_and_ask_preparation_share_serial_queue_and_revoke_cancels_pending_file(tmp_path, monkeypatch):
    from pathlib import Path
    from threading import Event, get_ident
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    first, second = ops.paths.personal / "first.txt", ops.paths.personal / "second.txt"
    first.write_text("first synthetic text", encoding="utf-8")
    second.write_text("second synthetic text", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    entered, release = Event(), Event()
    caller, original_open, background_sources = get_ident(), Path.open, []

    def blocked(path, mode="r", *args, **kwargs):
        if path in {first, second} and get_ident() != caller and mode == "rb":
            background_sources.append(path)
            if path == first:
                entered.set()
                assert release.wait(8)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", blocked)
    try:
        scope.request(first)
        assert entered.wait(2)
        other = AgentTextScope(ops.paths, ops.log)
        other.request(second)
        for _ in range(5):
            other.request(first)
            scope.request(second)
        assert set(background_sources) == {first}
        assert not other.queue.close_if_idle()
        scope.set_folder(ops.paths.personal, False)
    finally:
        release.set()
    assert scope.queue.wait_idle(8)
    assert not scope.is_pending(first) and not other.is_pending(second)
    assert not (ops.paths.root / ".text").exists()
    assert set(background_sources) == {first}  # revoked queued file was never opened


def test_read_only_scope_cannot_enqueue_on_an_existing_writer_queue(tmp_path):
    from vault_v2.errors import VaultError
    from vault_v2.receipts import ReceiptLog
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    source = ops.paths.personal / "one.txt"
    source.write_text("synthetic", encoding="utf-8")
    read_only = AgentTextScope(ops.paths, ReceiptLog(ops.paths.receipts, read_only=True))
    with pytest.raises(VaultError, match="read-only"):
        read_only.request(source)
    assert not scope.queue.busy
    assert not (ops.paths.root / ".text").exists()


def test_read_only_window_does_not_poison_later_writer_queue(tmp_path):
    from vault_v2.receipts import ReceiptLog
    paths = VaultPaths(tmp_path / "vault").ensure()
    read_only = AgentTextScope(paths, ReceiptLog(paths.receipts, read_only=True))
    ops = VaultOps(paths)
    scope = AgentTextScope(paths, ops.log)
    scope.set_folder(paths.personal, True)
    source = paths.personal / "one.txt"
    source.write_text("synthetic", encoding="utf-8")
    assert scope.warm(paths.personal) == ()
    assert not read_only.queue.busy
    assert len(list((paths.root / ".text").glob("*.txt"))) == 1


def test_finished_background_failure_is_not_reported_as_still_reading(tmp_path, monkeypatch):
    from pathlib import Path
    from threading import get_ident
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    source = ops.paths.personal / "one.txt"
    source.write_text("synthetic", encoding="utf-8")
    caller, original_open = get_ident(), Path.open
    main_reads = []

    def fail_background(path, mode="r", *args, **kwargs):
        if path == source and mode == "rb":
            if get_ident() != caller:
                raise OSError("synthetic preparation failure")
            main_reads.append(True)
            if len(main_reads) >= 3:
                assert scope.queue.wait_idle(3)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_background)
    result = ask("synthetic", collect_archive_docs(ops.paths, CardStore(ops.paths.root / ".cards", ops.log)), None)
    assert not any("still being read" in note for note in result.notes)
    assert any("text preparation failed" in note.casefold() for note in result.notes)
    assert not scope.queue.busy and not scope.is_pending(source)


def test_warm_failure_waits_for_jobs_it_already_accepted(tmp_path, monkeypatch):
    from pathlib import Path
    from threading import Event, Thread, get_ident
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    first, second = ops.paths.personal / "a.txt", ops.paths.personal / "b.txt"
    for source in (first, second):
        source.write_text("synthetic", encoding="utf-8")
    entered, release, returned = Event(), Event(), Event()
    original_open, original_lstat, caller = Path.open, Path.lstat, get_ident()
    failures = []

    def block_first(path, mode="r", *args, **kwargs):
        if path == first and mode == "rb" and get_ident() != caller:
            entered.set()
            assert release.wait(8)
        return original_open(path, mode, *args, **kwargs)

    def second_disappeared(path, *args, **kwargs):
        if path == second and scope.is_pending(first):
            assert entered.wait(3)
            raise FileNotFoundError("synthetic vanished file")
        return original_lstat(path, *args, **kwargs)

    def warm():
        try:
            scope.warm(ops.paths.personal)
        except Exception as exc:
            failures.append(exc)
        finally:
            returned.set()

    monkeypatch.setattr(Path, "open", block_first)
    monkeypatch.setattr(Path, "lstat", second_disappeared)
    worker = Thread(target=warm)
    worker.start()
    try:
        assert entered.wait(3)
        assert not returned.wait(0.2), "reported completion while accepted work is still running"
    finally:
        release.set()
        worker.join(8)
        scope.queue.wait_idle(8)
    assert returned.is_set() and failures


def test_identical_txt_and_md_share_ready_cache_without_displacing_each_other(tmp_path):
    from vault_v2.ask import collect_archive_docs, ask
    from vault_v2.cards import CardStore
    from vault_v2.text_cache import DocumentTextCache
    ops = VaultOps(VaultPaths(tmp_path / "vault"))
    sources = [ops.paths.personal / "one.txt", ops.paths.personal / "two.md"]
    for source in sources:
        source.write_text("synthetic sharedneedle", encoding="utf-8")
    scope = AgentTextScope(ops.paths, ops.log)
    scope.set_folder(ops.paths.personal, True)
    assert scope.warm(ops.paths.personal) == ()
    cache = DocumentTextCache(ops.paths, ops.log)
    assert all(cache.read_cached_snapshot(source) is not None for source in sources)
    extracted = [row for row in ops.log.tail() if row["op"] == "text_extracted"]
    assert len(extracted) == 1
    cards = CardStore(ops.paths.root / ".cards", ops.log)
    for _ in range(2):
        result = ask("sharedneedle", collect_archive_docs(ops.paths, cards), None)
        assert len(result.proposed_ids) == 2
        assert not any("still being read" in note for note in result.notes)
        assert not scope.queue.busy
    assert [row for row in ops.log.tail() if row["op"] == "text_extracted"] == extracted
