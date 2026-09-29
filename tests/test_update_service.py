"""Installed update service; synthetic identities and no installer execution."""
import base64
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import ssl
import time
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from vault_v2.update_service import ReadyUpdate, UpdateConfiguration, UpdateError, UpdateService
from test_release_stage import https_channel  # shared external loopback-TLS fixture


@pytest.fixture
def installed(tmp_path):
    directory = tmp_path / "Programs/VaultV2/versions/1"
    (directory / "vault_v2").mkdir(parents=True)
    marker = {"schema": "vault-v2-install@1", "worker_python": "python/python.exe",
              "program_root": "../..", "version": "Installed 1", "version_code": 1,
              "api_version": 2}
    (directory / "vault-install.json").write_text(json.dumps(marker))
    private = ec.generate_private_key(ec.SECP256R1())
    key = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    (directory / "vault_v2/release_key.pub").write_bytes(key)
    (directory / "vault_v2/update-channel.json").write_text(json.dumps(
        {"schema": "vault-v2-update-channel@1", "url": "https://example.invalid/releases"}))
    return directory, tmp_path / "updates", private, key


def service(inputs, **kwargs):
    return UpdateService(installed_dir=inputs[0], staging_parent=inputs[1], **kwargs)


def test_configuration_is_installed_read_only_and_immutable(installed):
    updater = service(installed)
    assert not installed[1].exists()
    result = updater.configuration()
    assert isinstance(result, UpdateConfiguration)
    assert (result.version, result.version_code, result.api_version) == ("Installed 1", 1, 2)
    assert result.channel == "https://example.invalid/releases"
    assert result.key == installed[3]
    assert result.staging_parent == installed[1]
    assert result.installed_directory == installed[0]
    assert not installed[1].exists()
    with pytest.raises(FrozenInstanceError):
        result.version_code = 99


@pytest.mark.parametrize("name", ["release_key.pub", "update-channel.json"])
def test_missing_installed_trust_or_channel_visibly_disables_updates(installed, name):
    (installed[0] / "vault_v2" / name).unlink()
    result = service(installed).configuration()
    assert isinstance(result, UpdateError)
    assert result.code == "updates_disabled"
    assert result.message
    assert not installed[1].exists()


@pytest.mark.parametrize("field,value", [
    ("version", ""), ("version", "x" * 65), ("version", "private\ntext"),
    ("version_code", True), ("version_code", 1.0), ("version_code", 0),
    ("version_code", 2), ("api_version", True), ("api_version", 0),
    ("api_version", "2"), ("program_root", "../../elsewhere"),
    ("worker_python", "elsewhere.exe"), ("unknown", "secret"),
])
def test_invalid_installed_identity_refuses_without_leaking_input(installed, field, value):
    path = installed[0] / "vault-install.json"
    marker = json.loads(path.read_bytes())
    marker[field] = value
    path.write_text(json.dumps(marker))
    result = service(installed).configuration()
    assert result.code == "invalid_configuration"
    assert "private" not in result.message and "secret" not in result.message
    assert not installed[1].exists()


@pytest.mark.parametrize("raw", [b"x" * 4097, b'\xef\xbb\xbf{}', b'{"schema":1,"schema":2}',
                                  b'{"url":NaN}', b'\xff'])
def test_installed_json_is_strict_and_bounded(installed, raw):
    (installed[0] / "vault_v2/update-channel.json").write_bytes(raw)
    assert service(installed).configuration().code == "invalid_configuration"


@pytest.mark.parametrize("url", ["http://example.invalid", "https://user:secret@example.invalid",
    "https://example.invalid/?token=secret", "https://example.invalid/#", "https://example.invalid/%2f",
    "https://example.invalid/../escape", "https://example.invalid:0", "file:///C:/channel"])
def test_channel_is_only_credential_free_explicit_https(installed, url):
    (installed[0] / "vault_v2/update-channel.json").write_text(json.dumps(
        {"schema": "vault-v2-update-channel@1", "url": url}))
    assert service(installed).configuration().code == "invalid_configuration"


@pytest.mark.parametrize("raw", [b"", b"x" * 1025, b"not a key"])
def test_malformed_installed_key_disables_updates(installed, raw):
    (installed[0] / "vault_v2/release_key.pub").write_bytes(raw)
    assert service(installed).configuration().code == "updates_disabled"


def test_channel_unknown_fields_and_program_staging_are_refused(installed):
    path = installed[0] / "vault_v2/update-channel.json"
    channel = json.loads(path.read_bytes())
    channel["token"] = "secret"
    path.write_text(json.dumps(channel))
    assert service(installed).configuration().code == "invalid_configuration"
    del channel["token"]
    path.write_text(json.dumps(channel))
    updater = UpdateService(installed_dir=installed[0], staging_parent=installed[0] / "updates")
    assert updater.configuration().code == "invalid_configuration"


@pytest.fixture
def release_channel(installed, https_channel):
    raw = json.dumps({"schema": "vault-v2-release@1", "version": "Available 2", "version_code": 2,
        "platform": "windows", "file": "setup.exe", "size": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "min_api_version": 2}).encode()
    signature = base64.b64encode(installed[2].sign(raw, ec.ECDSA(hashes.SHA256())))
    server = https_channel
    server.bodies = {"/releases/vault-release.json": raw,
                     "/releases/vault-release.json.sig": signature, "/releases/setup.exe": b"abc"}
    server.metadata_headers = None

    class Handler(server.RequestHandlerClass):
        def do_GET(self):
            server.requests.append((self.path, dict(self.headers)))
            body = server.bodies.get(self.path, b"")
            self.send_response(server.status)
            headers = server.metadata_headers
            for name, value in (headers if headers is not None else [("Content-Length", str(len(body)))]):
                self.send_header(name, value)
            self.end_headers()
            try:
                if server.delay:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(server.delay)
                else:
                    self.wfile.write(body)
            except (OSError, ssl.SSLError):
                pass

    server.RequestHandlerClass = Handler
    (installed[0] / "vault_v2/update-channel.json").write_text(json.dumps(
        {"schema": "vault-v2-update-channel@1", "url": server.url}))
    return server


def test_check_fetches_fixed_metadata_then_verified_package_without_launch(installed, release_channel):
    updater = service(installed)
    ready = updater.check()
    assert isinstance(ready, ReadyUpdate), ready
    assert ready.offer.release.version == "Available 2"
    assert ready.staged.package_path.read_bytes() == b"abc"
    assert len(ready.offer.approval_digest) == 64
    assert [path for path, _ in release_channel.requests] == [
        "/releases/vault-release.json", "/releases/vault-release.json.sig", "/releases/setup.exe"]
    for _, headers in release_channel.requests:
        assert not {name.lower() for name in headers} & {"authorization", "cookie"}


def test_explicit_confirmed_install_rechecks_and_returns_only_started_unconfirmed(installed, release_channel):
    launched = []
    updater = service(installed, _test_launch=lambda path: launched.append(path) or 123,
                      _test_namespace="Local\\VaultV2-update-test-" + uuid.uuid4().hex)
    ready = updater.check()
    result = updater.install(ready, ready.offer.approval_digest)
    assert result.state == "started_unconfirmed"
    assert result.pid == 123 and launched == [ready.staged.package_path]
    assert updater.install(ready, ready.offer.approval_digest).code == "already_started"


@pytest.mark.parametrize("change,code", [("key", "configuration_changed"),
    ("channel", "configuration_changed"), ("api", "configuration_changed"),
    ("package", "package_changed"), ("digest", "confirmation_required"),
    ("constructed", "confirmation_required"), ("active", "runtime_busy_or_unknown")])
def test_install_refuses_changed_context_or_unconfirmed_receipt(installed, release_channel, change, code):
    from contextlib import nullcontext
    from vault_v2.installation import InstallationSession
    namespace = "Local\\VaultV2-update-test-" + uuid.uuid4().hex
    updater = service(installed, _test_launch=lambda _: pytest.fail("refused release must not launch"),
                      _test_namespace=namespace)
    ready = updater.check()
    confirmation = ready.offer.approval_digest
    if change == "key":
        other = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        (installed[0] / "vault_v2/release_key.pub").write_bytes(base64.b64encode(other))
    elif change == "channel":
        (installed[0] / "vault_v2/update-channel.json").write_text(json.dumps(
            {"schema": "vault-v2-update-channel@1", "url": "https://example.invalid/other"}))
    elif change == "api":
        path = installed[0] / "vault-install.json"
        marker = json.loads(path.read_bytes())
        marker["api_version"] = 3
        path.write_text(json.dumps(marker))
    elif change == "package":
        ready.staged.package_path.write_bytes(b"bad")
    elif change == "digest":
        confirmation = "0" * 64
    elif change == "constructed":
        ready = ReadyUpdate(ready.staged, ready.offer)
    with InstallationSession("runtime", namespace=namespace) if change == "active" else nullcontext():
        result = updater.install(ready, confirmation)
    assert result.code == code
    assert result.message and "example.invalid" not in result.message


@pytest.mark.parametrize("defect", ["redirect", "oversized_manifest", "oversized_signature",
                                   "unknown_size_extra", "truncated", "duplicate_length"])
def test_metadata_response_refusal_never_downloads_or_stages_package(installed, release_channel, defect):
    channel = release_channel
    if defect == "redirect":
        channel.status = 302
        channel.metadata_headers = [("Location", channel.url + "/elsewhere")]
    elif defect == "oversized_manifest":
        channel.bodies["/releases/vault-release.json"] = b"x" * 16385
    elif defect == "oversized_signature":
        channel.bodies["/releases/vault-release.json.sig"] = b"x" * 1025
    elif defect == "unknown_size_extra":
        channel.metadata_headers = []
        channel.bodies["/releases/vault-release.json"] = b"x" * 16385
    elif defect == "truncated":
        channel.metadata_headers = [("Content-Length", "1000")]
    elif defect == "duplicate_length":
        channel.metadata_headers = [("Content-Length", "3"), ("Content-Length", "3")]
    result = service(installed).check()
    assert result.code == "response_refused"
    assert not installed[1].exists()
    assert all(path != "/releases/setup.exe" for path, _ in channel.requests)


def test_slow_metadata_is_bounded_by_one_parent_deadline(installed, release_channel):
    release_channel.delay = 0.2
    updater = service(installed, _test_metadata_timeout_s=0.7)
    started = time.monotonic()
    result = updater.check()
    assert result.code == "deadline_exceeded"
    assert time.monotonic() - started < 3
    assert not installed[1].exists()


def test_metadata_certificate_validation_cannot_fall_back_to_http(installed, release_channel, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    result = service(installed).check()
    assert result.code == "network_error"
    assert release_channel.requests == []
    assert not installed[1].exists()


def test_changed_configuration_during_network_check_invalidates_fetched_metadata(installed, monkeypatch):
    from vault_v2 import update_service
    def fetched(channel, timeout_s):
        (installed[0] / "vault_v2/update-channel.json").unlink()
        return b"untrusted", b"untrusted"
    monkeypatch.setattr(update_service, "_fetch_metadata", fetched)  # external transport boundary
    assert service(installed).check().code == "configuration_changed"
    assert not installed[1].exists()


def test_constructor_has_no_io_even_with_unavailable_installation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("constructor must not touch filesystem")
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    UpdateService()


@pytest.mark.parametrize("state,code", [("full", "quota_exceeded"),
                                        ("partial", "staging_review_required")])
def test_staging_limits_have_actionable_refusals_before_package_request(installed, release_channel, state, code):
    parent = installed[1]
    parent.mkdir()
    for index in range(4 if state == "full" else 1):
        attempt = parent / f"release-old-{index}"
        attempt.mkdir()
        (attempt / ("old.exe" if state == "full" else "package.part")).write_bytes(b"abc")
    result = service(installed).check()
    assert result.code == code
    assert "automatically" in result.message
    assert all(path != "/releases/setup.exe" for path, _ in release_channel.requests)


@pytest.mark.parametrize("defect,code", [("signature", "verification_refused"),
    ("not_newer", "not_newer"), ("api", "verification_refused"), ("package", "stage_refused")])
def test_core_refusals_never_become_ready(installed, release_channel, defect, code):
    channel = release_channel
    if defect == "package":
        channel.bodies["/releases/setup.exe"] = b"bad"
    elif defect == "signature":
        channel.bodies["/releases/vault-release.json"] += b" "
    else:
        manifest = json.loads(channel.bodies["/releases/vault-release.json"])
        manifest["version_code" if defect == "not_newer" else "min_api_version"] = 1 if defect == "not_newer" else 3
        raw = json.dumps(manifest).encode()
        channel.bodies["/releases/vault-release.json"] = raw
        channel.bodies["/releases/vault-release.json.sig"] = base64.b64encode(
            installed[2].sign(raw, ec.ECDSA(hashes.SHA256())))
    assert service(installed).check().code == code
    assert not installed[1].exists() or list(installed[1].iterdir()) == []


def test_launch_failure_is_safe_and_consumes_confirmation(installed, release_channel):
    def fail(_):
        raise OSError("private filesystem detail")
    updater = service(installed, _test_launch=fail,
                      _test_namespace="Local\\VaultV2-update-test-" + uuid.uuid4().hex)
    ready = updater.check()
    result = updater.install(ready, ready.offer.approval_digest)
    assert result.code == "launch_failed" and "private" not in result.message
    assert updater.install(ready, ready.offer.approval_digest).code == "confirmation_required"


@pytest.mark.parametrize("failure", ["kill", "reap"])
def test_uncertain_metadata_worker_termination_refuses_and_closes_owned_pipes(monkeypatch, failure):
    import io
    import subprocess
    from vault_v2.update_service import _fetch_metadata  # agreed external transport seam

    class UncertainProcess:
        stdin = io.BytesIO()
        stdout = io.BytesIO()

        def communicate(self, *args, **kwargs):
            raise subprocess.TimeoutExpired("synthetic worker", 0)

        def poll(self):
            return None

        def kill(self):
            if failure == "kill":
                raise PermissionError("private process detail")

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("synthetic worker", timeout)

    process = UncertainProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    result = _fetch_metadata("https://example.invalid/releases")
    assert result.code == "worker_failed"
    assert "private" not in result.message
    assert process.stdin.closed and process.stdout.closed
