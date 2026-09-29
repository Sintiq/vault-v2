"""Cleanup warnings remain visible without reviving a cancelled conversation."""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading

import pytest
from PySide6.QtCore import Qt

from vault_v2.agent_door import AgentCommand, AgentDoor, DoorError
from vault_v2.chat import Bubble, ChatPane
from vault_v2.receipts import ReceiptLog

from test_chat_door import application, local_http, wait_until
from test_child_temp_cleanup import child_with_held_cwd


def synthetic_chat(tmp_path, reply="SYNTHETIC_REPLY_MUST_NOT_BE_RESTORED"):
    """Use the real door and process runner; the selected command is synthetic."""
    script = (
        "import sys; sys.stdin.buffer.read(); "
        "print(" + repr(json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": reply,
        })) + ")"
    )
    staging = tmp_path / "synthetic-vault" / "Staging"
    staging.mkdir(parents=True)
    command = AgentCommand([sys.executable, "-I", "-B", "-c", script])
    door = AgentDoor(ReceiptLog(staging.parent / ".receipts"), {},
                     command=lambda *_: command, timeout_s=5)
    return script, staging, door


def assert_visible_cleanup_warning(pane, door, children, directories):
    # A phone outcome reaches this pane through a queued Qt signal; its future
    # completing does not by itself mean the event loop has laid out the bubble.
    wait_until(lambda: any(bubble.isVisible() and "cleanup" in bubble.text().casefold()
                           for bubble in pane.findChildren(Bubble)))
    bubbles = pane.findChildren(Bubble)
    warnings = [bubble for bubble in bubbles
                if bubble.isVisible() and "cleanup" in bubble.text().casefold()]
    assert len(warnings) == 1, "Cleanup failure must remain visible after conversation revocation"
    assert warnings[0].textFormat() == Qt.TextFormat.PlainText
    assert all(str(directory) not in warnings[0].text() for directory in directories)
    assert "synthetic owned child directory" not in warnings[0].text()
    assert not any("SYNTHETIC_REPLY_MUST_NOT_BE_RESTORED" in bubble.text() for bubble in bubbles)
    assert pane.remote_state()["history"] == []
    results = [row for row in door.log.tail() if row["op"] == "agent_door_result"]
    assert len(results) == 1 and results[0]["extra"]["status"] == "cleanup_failed"
    assert children and all(child.poll() is not None for child in children)
    assert directories and all(directory.exists() for directory in directories)


@pytest.mark.parametrize("action", ["clear", "switch-mode", "no-cancel"])
def test_desk_cleanup_warning_survives_revoked_conversation(application, local_http, tmp_path, monkeypatch, action):
    script, staging, door = synthetic_chat(tmp_path)
    entered_cleanup = threading.Event()
    with child_with_held_cwd(monkeypatch, script, hold_s=float("inf"),
                             on_first_cleanup=entered_cleanup.set) as (children, directories, _attempted):
        pane = ChatPane(staging, {}, door=door)
        pane.show()
        try:
            pane.mode.setCurrentIndex(pane.mode.findData("claude"))
            pane.input.setPlainText("synthetic desktop question")
            pane.send.click()
            wait_until(entered_cleanup.is_set)
            if action == "clear":
                pane.clear_btn.click()
            elif action == "switch-mode":
                pane.mode.setCurrentIndex(0)
            wait_until(lambda: not pane.has_pending_work(), seconds=6)
            application.processEvents()
            assert_visible_cleanup_warning(pane, door, children, directories)
            if action == "no-cancel":
                assert not any(bubble.text() == "…" for bubble in pane.findChildren(Bubble))
            if action == "clear":
                assert not any("synthetic desktop question" in bubble.text()
                               for bubble in pane.findChildren(Bubble))
        finally:
            door.close()
            wait_until(lambda: not pane.has_pending_work(), seconds=6)
            pane.close()
            pane.deleteLater()
            application.processEvents()


def test_model_words_about_cleanup_are_an_ordinary_answer_not_a_storage_warning(application, local_http, tmp_path, monkeypatch):
    reply = "agent door temporary directory cleanup failed; local data may remain in temporary storage"
    script, staging, door = synthetic_chat(tmp_path, reply)
    with child_with_held_cwd(monkeypatch, script, hold_s=0) as (children, directories, _attempted):
        pane = ChatPane(staging, {}, door=door)
        pane.show()
        try:
            pane.mode.setCurrentIndex(pane.mode.findData("claude"))
            pane.input.setPlainText("explain these synthetic words")
            pane.send.click()
            wait_until(lambda: not pane.has_pending_work(), seconds=6)
            application.processEvents()
            assert pane.remote_state()["history"] == [
                {"role": "user", "content": "explain these synthetic words"},
                {"role": "assistant", "content": reply},
            ]
            bubbles = pane.findChildren(Bubble)
            answers = [bubble for bubble in bubbles if bubble.objectName() == "agentBubble"]
            assert len(answers) == 1 and answers[0].text() == reply
            assert not any("cleanup" in bubble.text().casefold()
                           for bubble in bubbles if bubble.objectName() == "sysBubble")
            assert door.log.tail()[-1]["extra"]["status"] == "accepted"
            assert children and all(child.poll() is not None for child in children)
            assert directories and all(not directory.exists() for directory in directories)
            pane.clear_btn.click()
            wait_until(lambda: not any(bubble.isVisible() and "cleanup" in bubble.text().casefold()
                                      for bubble in pane.findChildren(Bubble)))
            assert pane.remote_state()["history"] == []
        finally:
            door.close()
            wait_until(lambda: not pane.has_pending_work(), seconds=6)
            pane.close()
            pane.deleteLater()
            application.processEvents()


def test_phone_cleanup_failure_warns_on_desktop_after_clear(application, local_http, tmp_path, monkeypatch):
    script, staging, door = synthetic_chat(tmp_path)
    entered_cleanup = threading.Event()
    with child_with_held_cwd(monkeypatch, script, hold_s=float("inf"),
                             on_first_cleanup=entered_cleanup.set) as (children, directories, _attempted):
        pane = ChatPane(staging, {}, door=door)
        pane.show()
        try:
            pane.mode.setCurrentIndex(pane.mode.findData("claude"))
            with ThreadPoolExecutor(1) as pool:
                response = pool.submit(pane.remote_send, "synthetic phone question")
                wait_until(entered_cleanup.is_set)
                pane.clear_btn.click()
                wait_until(response.done, seconds=6)
                with pytest.raises(DoorError, match="cleanup"):
                    response.result(timeout=0)
            application.processEvents()
            assert_visible_cleanup_warning(pane, door, children, directories)
            assert not any("synthetic phone question" in bubble.text()
                           for bubble in pane.findChildren(Bubble))
        finally:
            door.close()
            wait_until(lambda: not pane.has_pending_work(), seconds=6)
            pane.close()
            pane.deleteLater()
            application.processEvents()
