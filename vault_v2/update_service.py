"""Installed Windows updater controller; construction has no IO or network effects.

The standalone updater holds no runtime marker. A receipt or Python result type
is not trust: core staging/handoff authenticate signed inputs and package again.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import json
from http.client import HTTPException
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .release_handoff import HandoffRefusal, InstallOffer, InstallerStarted, handoff_release, inspect_staged_release
from .installation import NAMESPACE
from .release_stage import HttpsReleaseSource, StageRefusal, StagedRelease, stage_release
from .releases import ReleaseRefusal, verify_release
from .runtime import installed_directory, worker_python


@dataclass(frozen=True, slots=True)
class UpdateConfiguration:
    version: str
    version_code: int
    api_version: int
    channel: str
    key: bytes
    staging_parent: Path
    installed_directory: Path


@dataclass(frozen=True, slots=True)
class ReadyUpdate:
    staged: StagedRelease
    offer: InstallOffer


@dataclass(frozen=True, slots=True)
class UpdateError:
    code: str
    message: str


_MESSAGES = {
    "updates_disabled": "Updates are disabled: this installation has no release key or update channel.",
    "invalid_configuration": "The installed update configuration is invalid. Repair the installation.",
    "configuration_changed": "Update configuration changed. Check for updates again.",
    "network_error": "The update channel could not be reached securely. Try again later.",
    "response_refused": "The update channel returned an invalid or oversized response.",
    "deadline_exceeded": "The update check timed out. Try again later.",
    "worker_failed": "The update download worker could not complete safely.",
    "not_newer": "No newer update is available.",
    "verification_refused": "The update signature or release requirements could not be verified.",
    "stage_refused": "The update package could not be downloaded and verified safely.",
    "quota_exceeded": "Saved updates reached the limit (4 packages or 2 GiB). Nothing was deleted automatically. Finish any running installer, then request a review of saved updates before retrying.",
    "staging_busy": "Another update download is running or its state is unknown. Nothing was deleted automatically. Wait for it to finish before retrying.",
    "staging_review_required": "An incomplete or unfamiliar saved update needs review. Nothing was deleted automatically. Do not remove files while an installer or download may be running.",
    "inspection_refused": "The staged update is no longer valid. Check for updates again.",
    "io_error": "The update staging directory could not be used safely.",
    "confirmation_required": "Check for updates and explicitly confirm the displayed release.",
    "runtime_busy_or_unknown": "Close all Vault windows and other installers, then try again.",
    "package_changed": "The staged package changed. Check for updates again.",
    "invalid_stage": "The staged update is no longer valid. Check for updates again.",
    "launch_failed": "The installer could not be started. Check for updates again.",
    "already_started": "The installer has already been started. Close this updater.",
}


def _error(code):
    return UpdateError(code, _MESSAGES[code])


def _plain(path, *, missing=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or str(path).startswith("\\\\"):
        raise ValueError("unsafe path")
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or (part != path and not stat.S_ISDIR(info.st_mode))):
            raise ValueError("unsafe path")
    return path


def _read(path, limit):
    _plain(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("not a file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversized file")
    return raw


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("non-JSON constant")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)


def _location(channel, filename):
    if (type(channel) is not str or not 0 < len(channel) <= 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in channel)
            or any(c in channel for c in "\\?#%")):
        raise ValueError("unsafe channel")
    parts = urlsplit(channel)
    if (parts.scheme != "https" or not parts.hostname or "@" in parts.netloc
            or parts.port == 0 or ".." in parts.path.split("/")
            or re.fullmatch(r"[A-Za-z0-9/._~-]*", parts.path) is None):
        raise ValueError("unsafe channel")
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/") + "/" + filename, "", ""))


def _valid_key(raw):
    if not 0 < len(raw) <= 1024:
        return False
    try:
        key = serialization.load_der_public_key(base64.b64decode(raw.strip(), validate=True))
        return isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)
    except (ValueError, UnsupportedAlgorithm):
        return False


class _DownloadFailure(Exception):
    def __init__(self, code):
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _download_metadata(channel):
    opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    bodies = []
    for filename, limit in (("vault-release.json", 16384), ("vault-release.json.sig", 1024)):
        url = _location(channel, filename)
        try:
            with opener.open(Request(url, headers={"Accept-Encoding": "identity"}), timeout=30) as response:
                lengths = response.headers.get_all("Content-Length", [])
                encodings = response.headers.get_all("Content-Encoding", [])
                transfers = response.headers.get_all("Transfer-Encoding", [])
                if (response.status != 200 or response.geturl() != url or len(lengths) > 1
                        or len(encodings) > 1 or len(transfers) > 1
                        or (encodings and encodings[0].strip().lower() != "identity")
                        or (transfers and (transfers[0].strip().lower() != "chunked" or lengths))):
                    raise _DownloadFailure("response_refused")
                advertised = None
                if lengths:
                    if re.fullmatch(r"[0-9]{1,10}", lengths[0].strip()) is None:
                        raise _DownloadFailure("response_refused")
                    advertised = int(lengths[0])
                    if not 0 < advertised <= limit:
                        raise _DownloadFailure("response_refused")
                body = bytearray()
                while len(body) <= limit:
                    chunk = response.read(min(4096, limit + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                if not 0 < len(body) <= limit or (advertised is not None and len(body) != advertised):
                    raise _DownloadFailure("response_refused")
                bodies.append(base64.b64encode(body).decode("ascii"))
        except HTTPError:
            raise _DownloadFailure("response_refused") from None
        except (URLError, OSError, HTTPException):
            raise _DownloadFailure("network_error") from None
    return bodies


def _fetch_metadata(channel, timeout_s=30.0):
    """External transport boundary; real child bounds DNS/TLS/read as one operation."""
    try:
        return _metadata_process(channel, timeout_s)
    except _DownloadFailure as failure:
        return _error(failure.code)


def _metadata_process(channel, timeout_s):
    """Own process/pipes; cleanup uncertainty overrides any successful result."""
    deadline = time.monotonic() + timeout_s
    process = None
    try:
        command = [worker_python(), "-I", "-B", "-c",
            "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
            "runpy.run_module('vault_v2.update_service',run_name='__main__')",
            str(Path(__file__).absolute().parent.parent)]
        env = {name: os.environ[name] for name in
               ("SystemRoot", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR") if name in os.environ}
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False, env=env, cwd=Path(__file__).absolute().parent,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout_s)
        output, _ = process.communicate(json.dumps({"channel": channel}).encode(), timeout=remaining)
        if time.monotonic() >= deadline:
            return _error("deadline_exceeded")
        if process.returncode or len(output) > 24576:
            return _error("worker_failed")
        result = _json(output)
        if type(result) is dict and result.get("error") in ("network_error", "response_refused"):
            return _error(result["error"])
        if type(result) is not list or len(result) != 2:
            return _error("worker_failed")
        values = tuple(base64.b64decode(value, validate=True) for value in result)
        if not 0 < len(values[0]) <= 16384 or not 0 < len(values[1]) <= 1024:
            return _error("response_refused")
        return values
    except subprocess.TimeoutExpired:
        return _error("deadline_exceeded")
    except (OSError, ValueError, TypeError, RuntimeError):
        return _error("worker_failed")
    finally:
        if process is not None:
            cleanup_failed = False
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failed = True
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        cleanup_failed = True
            if cleanup_failed:
                raise _DownloadFailure("worker_failed")


class UpdateService:
    """Call configuration/check/install serially; the GUI owns its busy-job gate.

    Production uses UpdateService() only. Explicit paths, launch/namespace and
    shortened metadata deadline are synthetic-test seams, never channel fields.
    """
    def __init__(self, *, installed_dir=None, staging_parent=None, _test_launch=None,
                 _test_namespace=None, _test_metadata_timeout_s=30.0):
        if type(_test_metadata_timeout_s) not in (int, float) or not 0 < _test_metadata_timeout_s <= 30:
            raise ValueError("Invalid metadata deadline")
        # Explicit directories are synthetic-test seams, never UI/user settings.
        self._installed_dir = installed_dir
        self._staging_parent = staging_parent
        self._issued = None
        self._launch = _test_launch
        self._namespace = _test_namespace or NAMESPACE
        self._started = False
        self._metadata_timeout = _test_metadata_timeout_s

    def configuration(self) -> UpdateConfiguration | UpdateError:
        try:
            directory = self._installed_dir or installed_directory()
            if directory is None:
                return _error("updates_disabled")
            directory = _plain(directory)
            marker = _json(_read(directory / "vault-install.json", 4096))
            if (type(marker) is not dict or set(marker) != {"schema", "worker_python", "program_root",
                    "version", "version_code", "api_version"}
                    or marker["schema"] != "vault-v2-install@1"
                    or marker["worker_python"] != "python/python.exe" or marker["program_root"] != "../.."
                    or type(marker["version"]) is not str
                    or re.fullmatch(r"[\x20-\x7e]{1,64}", marker["version"]) is None
                    or any(type(marker[k]) is not int or not 1 <= marker[k] <= 2147483647
                           for k in ("version_code", "api_version"))
                    or directory.parent.name != "versions" or directory.name != str(marker["version_code"])):
                raise ValueError("invalid identity")
            try:
                key = _read(directory / "vault_v2/release_key.pub", 1024)
            except (FileNotFoundError, ValueError):
                return _error("updates_disabled")
            if not _valid_key(key):
                return _error("updates_disabled")
            try:
                channel = _json(_read(directory / "vault_v2/update-channel.json", 4096))
            except FileNotFoundError:
                return _error("updates_disabled")
            if (type(channel) is not dict or set(channel) != {"schema", "url"}
                    or channel["schema"] != "vault-v2-update-channel@1"):
                raise ValueError("invalid channel")
            _location(channel["url"], "vault-release.json")
            parent = self._staging_parent
            if parent is None:
                parent = Path(os.environ["LOCALAPPDATA"]) / "VaultV2/updates"
            parent = _plain(parent, missing=True)
            program = directory.parent.parent
            if parent.is_relative_to(program) or program.is_relative_to(parent):
                raise ValueError("staging overlaps program")
            return UpdateConfiguration(marker["version"], marker["version_code"], marker["api_version"],
                                       channel["url"], key, parent, directory)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return _error("invalid_configuration")

    def check(self) -> ReadyUpdate | UpdateError:
        self._issued = None
        if self._started:
            return _error("already_started")
        configuration = self.configuration()
        if isinstance(configuration, UpdateError):
            return configuration
        metadata = _fetch_metadata(configuration.channel, self._metadata_timeout)
        if isinstance(metadata, UpdateError):
            return metadata
        if self.configuration() != configuration:
            return _error("configuration_changed")
        raw, signature = metadata
        release = verify_release(raw, signature, configuration.key, "windows",
                                 configuration.version_code, configuration.api_version)
        if isinstance(release, ReleaseRefusal):
            return _error("not_newer" if release.code == "not_newer" else "verification_refused")
        try:
            _plain(configuration.staging_parent, missing=True)
            configuration.staging_parent.mkdir(parents=True, exist_ok=True)
            _plain(configuration.staging_parent)
        except (OSError, ValueError):
            return _error("io_error")
        staged = stage_release(raw, signature, configuration.key, "windows", configuration.version_code,
            configuration.api_version, source=HttpsReleaseSource(configuration.channel),
            staging_parent=configuration.staging_parent)
        if isinstance(staged, StageRefusal):
            if staged.code in {"quota_exceeded", "staging_busy", "staging_review_required"}:
                return _error(str(staged.code))
            return _error("stage_refused")
        if self.configuration() != configuration:
            return _error("configuration_changed")
        offer = inspect_staged_release(staged, configuration.key, configuration.version_code,
                                       configuration.api_version, staging_parent=configuration.staging_parent)
        if isinstance(offer, HandoffRefusal):
            return _error("inspection_refused")
        ready = ReadyUpdate(staged, offer)
        self._issued = ready, configuration
        return ready

    def install(self, ready: ReadyUpdate, confirmed_digest: str) -> InstallerStarted | UpdateError:
        issued, self._issued = self._issued, None
        if self._started:
            return _error("already_started")
        configuration = self.configuration()
        if isinstance(configuration, UpdateError):
            return configuration
        if issued is None or ready is not issued[0]:
            return _error("confirmation_required")
        if configuration != issued[1]:
            return _error("configuration_changed")
        result = handoff_release(ready.staged, configuration.key, configuration.version_code,
            configuration.api_version, staging_parent=configuration.staging_parent,
            confirmed_digest=confirmed_digest, launch=self._launch, namespace=self._namespace)
        if isinstance(result, HandoffRefusal):
            return _error(str(result.code))
        self._started = True
        return result


if __name__ == "__main__":
    try:
        request = sys.stdin.buffer.read(8193)
        if len(request) > 8192:
            raise ValueError("oversized request")
        print(json.dumps(_download_metadata(_json(request)["channel"])))
    except _DownloadFailure as failure:
        print(json.dumps({"error": failure.code}))
    except (OSError, ValueError, TypeError, KeyError):
        sys.exit(1)
