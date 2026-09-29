"""MCP server: the owner's phone, as tools an agent may use.

This is the PC half of the agent door. The phone holds the guard — the tiers,
the tap that has to happen, the receipts. This process only translates a tool
call into a knock and writes down that it knocked, so there are two logs to
compare rather than one that could be quietly rewritten.

Only the passport's tools exist here. Anything in its "never" list has no
function, not a disabled one: there is nothing to talk into calling.

Pairing: open the vault app on the phone, press the lock icon, open the door,
and copy the key and address into <vault root>/.door/config.json:

    {"address": "http://100.64.0.2:8779", "key": "<your phone door key>"}

The address above is an example, not a configured destination. Use the address
shown by your own phone; do not reuse an address from development notes.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import urllib.error
import urllib.request

from mcp.server.mcpserver import Image, MCPServer

# The owner-only tier waits for a tap on the phone; the phone's own window is
# 90 seconds, so this has to outlast it or a refusal looks like a timeout.
TIMEOUT_S = 120
CONFIG_NAME = "config.json"


def _root() -> Path:
    import os

    env = os.environ.get("VAULT_V2_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "data"


class Door:
    """The phone, reached over Tailscale."""

    def __init__(self, root: Path):
        self.dir = root / ".door"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.dir / "asked.jsonl"
        self._config = self.dir / CONFIG_NAME

    @property
    def paired(self) -> bool:
        return self._config.exists()

    def _settings(self) -> tuple[str, str]:
        if not self._config.exists():
            raise RuntimeError(
                "the phone is not paired yet — open the door in the phone app and put its "
                f"address and key into {self._config}"
            )
        data = json.loads(self._config.read_text(encoding="utf-8"))
        address = str(data.get("address", "")).rstrip("/")
        key = str(data.get("key", ""))
        if not address or not key:
            raise RuntimeError(f"{self._config} needs both an address and a key")
        return address, key

    def note(self, tool: str, tier: str, outcome: str, detail: str = "") -> None:
        """The PC's own record. Location is written down as asked, never as where."""
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool, "tier": tier, "outcome": outcome, "detail": detail,
        }
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def call(self, path: str, tier: str, *, post: bool = False, body: dict | None = None,
             detail: str = "") -> dict[str, Any]:
        try:
            address, key = self._settings()
            request = urllib.request.Request(
                address + path,
                data=json.dumps(body or {}).encode() if post else None,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST" if post else "GET",
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                payload = json.loads(response.read())
            self.note(path, tier, "answered", detail)
            return payload
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read() or b"{}")
            reason = payload.get("error", f"HTTP {exc.code}")
            self.note(path, tier, "refused", reason)
            return {"error": reason}
        except RuntimeError as exc:
            # Not paired yet: an agent should read a sentence, not a traceback.
            self.note(path, tier, "unreachable", str(exc))
            return {"error": str(exc)}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.note(path, tier, "unreachable", str(exc))
            return {"error": f"the phone did not answer: {exc}. Is Tailscale on, and the door open?"}


    def fetch_png(self, path: str, tier: str) -> bytes | str:
        """Same knock, but the answer is an image. Returns bytes, or a sentence
        explaining why not. The image is never written to disk here."""
        try:
            address, key = self._settings()
            request = urllib.request.Request(
                address + path, data=b"{}",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                payload = response.read()
            self.note(path, tier, "answered", f"{len(payload) // 1024} KB, not stored")
            return payload
        except urllib.error.HTTPError as exc:
            reason = json.loads(exc.read() or b"{}").get("error", f"HTTP {exc.code}")
            self.note(path, tier, "refused", reason)
            return reason
        except (RuntimeError, urllib.error.URLError, OSError, ValueError) as exc:
            self.note(path, tier, "unreachable", str(exc))
            return f"the phone did not answer: {exc}"


door = Door(_root())
server = MCPServer(
    "phone",
    instructions=(
        "The owner's phone. Battery and wifi answer straight away. Location is reserved: "
        "ask for it only when the owner has just asked in plain words where his phone is, "
        "never to enrich some other answer, and never record what comes back. Ringing and "
        "opening an app act on a device in someone's pocket — do them when asked, not to "
        "check that they work. Tapping and swiping happen inside a window the owner opens; "
        "close it with phone_stop_using_screen the moment you are done."
    ),
)


@server.tool()
def phone_status() -> dict:
    """Whether the phone's door is open, and whether its own log is intact."""
    return door.call("/door/status", "read")


@server.tool()
def phone_battery() -> dict:
    """Battery percentage and whether the phone is charging."""
    return door.call("/door/battery", "read")


@server.tool()
def phone_network() -> dict:
    """Whether the phone is on wifi and which network — enough to tell home from not home."""
    return door.call("/door/network", "read")


@server.tool()
def phone_location() -> dict:
    """Where the phone is. ONLY when the owner has just asked, in his own words, in this
    conversation. The phone will show him a prompt and refuse if he does not tap Allow.
    Never call this to improve another answer, and never write the coordinates anywhere."""
    return door.call("/door/location", "owner-only", post=True,
                     detail="requested by the owner; coordinates not recorded")


@server.tool()
def phone_ring() -> dict:
    """Make the phone ring, for finding it. The volume it had is restored when it stops."""
    return door.call("/door/ring", "act", post=True)


@server.tool()
def phone_stop_ringing() -> dict:
    """Stop the ringing started by phone_ring."""
    return door.call("/door/ring/stop", "act", post=True)


@server.tool()
def phone_open_app(package: str) -> dict:
    """Open an app on the phone, e.g. com.google.android.youtube. Opens outright if the
    owner has allowed the vault to display over other apps; otherwise leaves a
    notification he can tap, and the answer says which happened. Logged either way."""
    return door.call("/door/launch", "act", post=True, body={"package": package},
                     detail=package)


@server.tool()
def phone_screenshot() -> Image | str:
    """One picture of the phone's screen. The owner is asked twice — once by the
    vault app and once by Android itself — and a refusal or silence returns
    nothing. This phone can only capture the WHOLE screen, so whatever he has
    open will be in it: ask for it when he wants you to look at something, not
    to find out what he is doing. Do not save what comes back."""
    result = door.fetch_png("/door/screenshot", "owner-only")
    if isinstance(result, str):
        return result
    return Image(data=result, format="png")


@server.tool()
def phone_tap(x: int, y: int) -> dict:
    """Tap the phone's screen at x, y (the screen is 1080 by 2400). The first
    action asks the owner to open a five-minute window; after that it just
    works until the window closes. You cannot see the screen this way — take a
    phone_screenshot first if you need to know what you are tapping."""
    return door.call("/door/act", "owner-only", post=True,
                     body={"do": "tap", "x": x, "y": y}, detail=f"tap {x},{y}")


@server.tool()
def phone_swipe(x: int, y: int, to_x: int, to_y: int, ms: int = 250) -> dict:
    """Swipe from one point to another, for scrolling. Same window as phone_tap."""
    return door.call("/door/act", "owner-only", post=True,
                     body={"do": "swipe", "x": x, "y": y, "toX": to_x, "toY": to_y, "ms": ms},
                     detail=f"swipe {x},{y}->{to_x},{to_y}")


@server.tool()
def phone_press(button: str) -> dict:
    """Press back, home or recents. Same window as phone_tap."""
    return door.call("/door/act", "owner-only", post=True,
                     body={"do": button}, detail=button)


@server.tool()
def phone_stop_using_screen() -> dict:
    """Close the window early. Do this the moment you are finished — leaving it
    open is leaving a hand on someone's phone."""
    return door.call("/door/act/end", "owner-only", post=True)


@server.tool()
def phone_receipts(count: int = 40) -> dict:
    """The phone's own log of every knock, kept separately from this one."""
    return door.call("/door/receipts", "read", detail=f"n={count}")


def main() -> int:
    if not door.paired:
        print(f"phone not paired; expecting {door.dir / CONFIG_NAME}", file=sys.stderr)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
