"""Stage seam uses synthetic bytes and ephemeral release signing keys only."""

import base64
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import hashlib
import json
import multiprocessing
import os
import ssl
import sys
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from vault_v2.release_stage import HttpsReleaseSource, LocalReleaseSource, StagedRelease, stage_release
from vault_v2.releases import verify_release


@pytest.fixture(scope="module")
def signing_key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def stage_inputs(tmp_path, signing_key):
    source = tmp_path / "source"
    source.mkdir()
    (source / "vault.exe").write_bytes(b"abc")
    parent = tmp_path / "staging"
    parent.mkdir()
    manifest = {
        "schema": "vault-v2-release@1", "version": "synthetic",
        "version_code": 2, "platform": "windows", "file": "vault.exe", "size": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "min_api_version": 1,
    }
    raw = json.dumps(manifest).encode()
    signature = base64.b64encode(signing_key.sign(raw, ec.ECDSA(hashes.SHA256())))
    public_key = base64.b64encode(signing_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return dict(raw_manifest=raw, detached_signature=signature, installed_public_key=public_key,
                expected_platform="windows", installed_version_code=1, available_api_version=1,
                source=LocalReleaseSource(source), staging_parent=parent)


def test_stages_verified_local_bytes_with_signed_receipt(stage_inputs):
    result = stage_release(**stage_inputs)

    assert isinstance(result, StagedRelease)
    assert result.package_path.read_bytes() == b"abc"
    assert result.package_path.name == "vault.exe"
    assert result.package_path.parent.parent == stage_inputs["staging_parent"]
    assert result.raw_manifest == stage_inputs["raw_manifest"]
    assert result.detached_signature == stage_inputs["detached_signature"]
    assert result.release.version_code == 2
    assert result.attempt_id == result.package_path.parent.name
    assert list(result.package_path.parent.iterdir()) == [result.package_path]


def test_four_retained_packages_refuse_fifth_without_changing_files(stage_inputs):
    retained = [stage_release(**stage_inputs) for _ in range(4)]
    assert all(isinstance(item, StagedRelease) for item in retained)

    result = stage_release(**stage_inputs)

    assert result.code == "quota_exceeded"
    assert set(stage_inputs["staging_parent"].iterdir()) == {
        item.package_path.parent for item in retained}
    assert all(item.package_path.read_bytes() == b"abc" for item in retained)


def test_retained_byte_limit_includes_incoming_signed_size(stage_inputs):
    previous = stage_inputs["staging_parent"] / "release-previous"
    previous.mkdir()
    package = previous / "old.exe"
    with package.open("wb") as stream:
        stream.truncate(2 * 1024**3 - 3)
    assert isinstance(stage_release(**stage_inputs), StagedRelease)

    result = stage_release(**stage_inputs)

    assert result.code == "quota_exceeded"
    assert package.stat().st_size == 2 * 1024**3 - 3
    assert len(list(stage_inputs["staging_parent"].iterdir())) == 2


@pytest.mark.parametrize("state", ["empty", "partial", "extra", "unknown", "notes.txt", "other.part"])
def test_uncertain_retained_attempt_is_not_deleted_or_downloaded_over(stage_inputs, state):
    parent = stage_inputs["staging_parent"]
    attempt = parent / ("unfamiliar" if state == "unknown" else "release-old")
    attempt.mkdir()
    if state != "empty":
        filename = state if "." in state else ("package.part" if state == "partial" else "old.exe")
        (attempt / filename).write_bytes(b"keep")
    if state == "extra":
        (attempt / "extra.txt").write_bytes(b"keep also")
    before = {p.name: p.read_bytes() for p in attempt.iterdir()}
    result = stage_release(**stage_inputs)
    assert result.code == "staging_review_required"
    assert list(parent.iterdir()) == [attempt]
    assert {p.name: p.read_bytes() for p in attempt.iterdir()} == before


def _child_stage(inputs, results):
    result = stage_release(**inputs)
    results.put("staged" if isinstance(result, StagedRelease) else str(result.code))


def test_other_process_cannot_download_into_last_slot_while_transfer_is_active(stage_inputs, https_channel):
    for _ in range(3):
        assert isinstance(stage_release(**stage_inputs), StagedRelease)
    entered, release = threading.Event(), threading.Event()
    https_channel.entered, https_channel.release = entered, release
    stage_inputs["source"] = HttpsReleaseSource(https_channel.url)
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    child = context.Process(target=_child_stage, args=(stage_inputs, results))
    child.start()
    try:
        assert entered.wait(15), "child did not reach synthetic HTTPS server"
        assert stage_release(**stage_inputs).code == "staging_busy"
        assert len(https_channel.requests) == 1
        release.set()
        assert results.get(timeout=15) == "staged"
        child.join(10)
        assert child.exitcode == 0
        assert stage_release(**stage_inputs).code == "quota_exceeded"
        assert len(https_channel.requests) == 1
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)
        results.close()


@pytest.mark.parametrize(("payload", "code"), [
    (b"ab", "size_mismatch"), (b"abcd", "size_mismatch"), (b"abd", "hash_mismatch"),
])
def test_failed_attempt_never_publishes_and_preserves_earlier_release(stage_inputs, payload, code):
    previous = stage_release(**stage_inputs)
    (stage_inputs["source"].directory / "vault.exe").write_bytes(payload)

    result = stage_release(**stage_inputs)

    assert result.code == code
    assert previous.package_path.read_bytes() == b"abc"
    assert list(stage_inputs["staging_parent"].iterdir()) == [previous.package_path.parent]


def test_publication_disk_error_is_safe_and_cleans_only_new_attempt(stage_inputs, monkeypatch):
    previous = stage_release(**stage_inputs)

    def disk_full(*args, **kwargs):
        raise OSError("secret disk detail")

    monkeypatch.setattr(os, "link", disk_full)
    result = stage_release(**stage_inputs)

    assert result.code == "io_error"
    assert "secret" not in repr(result)
    assert list(stage_inputs["staging_parent"].iterdir()) == [previous.package_path.parent]
    assert previous.package_path.read_bytes() == b"abc"


@pytest.mark.parametrize("location", ["source", "package", "staging"])
def test_directory_junctions_are_refused(stage_inputs, tmp_path, location):
    if os.name != "nt":
        pytest.skip("Windows junction contract")
    import subprocess

    target = tmp_path / "junction"
    real = stage_inputs["source"].directory if location == "source" else stage_inputs["staging_parent"]
    if location == "package":
        (stage_inputs["source"].directory / "vault.exe").unlink()
        target = stage_inputs["source"].directory / "vault.exe"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(real)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if location == "source":
        stage_inputs["source"] = LocalReleaseSource(target)
    elif location == "staging":
        stage_inputs["staging_parent"] = target

    result = stage_release(**stage_inputs)

    assert result.code == "unsafe_path"
    assert target.is_junction()


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), "3", 301])
def test_invalid_deadline_is_refused_before_staging(stage_inputs, timeout):
    result = stage_release(**stage_inputs, timeout_s=timeout)

    assert result.code == "invalid_request"
    assert list(stage_inputs["staging_parent"].iterdir()) == []


def test_total_deadline_includes_worker_startup_and_removes_failed_attempt(stage_inputs):
    start = time.monotonic()
    result = stage_release(**stage_inputs, timeout_s=0.001)

    assert result.code == "deadline_exceeded"
    assert time.monotonic() - start < 3
    assert list(stage_inputs["staging_parent"].iterdir()) == []


@pytest.fixture
def https_channel(tmp_path, monkeypatch):
    # A throwaway loopback TLS identity, never a release signing identity.
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "local-stage-test")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                                  critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "tls-cert.pem", tmp_path / "tls-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    key_path.unlink()  # SSL has loaded this temporary, synthetic TLS key.
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.requests.append((self.path, dict(self.headers)))
            if hasattr(self.server, "entered"):
                self.server.entered.set()
                self.server.release.wait(20)
            self.send_response(self.server.status)
            for name, value in self.server.headers:
                self.send_header(name, value)
            self.end_headers()
            try:
                for chunk in self.server.chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if self.server.delay:
                        time.sleep(self.server.delay)
            except (OSError, ssl.SSLError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.status, server.headers, server.chunks, server.delay = 200, [("Content-Length", "3")], [b"abc"], 0
    server.requests = []
    server.url = f"https://127.0.0.1:{server.server_port}/releases"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        cert_path.unlink()


def test_https_channel_download_uses_validated_tls_and_fixed_manifest_basename(stage_inputs, https_channel, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    stage_inputs["source"] = HttpsReleaseSource(https_channel.url)

    result = stage_release(**stage_inputs)

    assert isinstance(result, StagedRelease)
    assert result.package_path.read_bytes() == b"abc"
    assert https_channel.requests[0][0] == "/releases/vault.exe"
    headers = {name.lower(): value for name, value in https_channel.requests[0][1].items()}
    assert "authorization" not in headers
    assert "cookie" not in headers


@pytest.mark.parametrize("suffix", ["?token=hidden", "#fragment", "/../other", "/bad path", "\\path", "/%2fother", "/" + "x" * 2048])
def test_ambiguous_channel_is_rejected_before_any_request(stage_inputs, https_channel, suffix):
    stage_inputs["source"] = HttpsReleaseSource(https_channel.url + suffix)

    result = stage_release(**stage_inputs)

    assert result.code == "invalid_source"
    assert https_channel.requests == []
    assert list(stage_inputs["staging_parent"].iterdir()) == []


@pytest.mark.parametrize(("defect", "code"), [
    ("redirect", "response_refused"), ("partial", "response_refused"),
    ("invalid_length", "response_refused"), ("duplicate_length", "response_refused"),
    ("encoded", "response_refused"), ("ambiguous_framing", "response_refused"),
    ("advertised_too_large", "size_mismatch"), ("truncated", "size_mismatch"),
    ("extra_without_length", "size_mismatch"), ("wrong_hash", "hash_mismatch"),
])
def test_http_response_must_match_one_complete_unencoded_package(stage_inputs, https_channel, defect, code):
    stage_inputs["source"] = HttpsReleaseSource(https_channel.url)
    if defect == "redirect":
        https_channel.status = 302
        https_channel.headers.append(("Location", https_channel.url + "/redirect"))
    elif defect == "partial":
        https_channel.status = 206
    elif defect == "invalid_length":
        https_channel.headers = [("Content-Length", "invalid")]
    elif defect == "duplicate_length":
        https_channel.headers *= 2
    elif defect == "encoded":
        https_channel.headers.append(("Content-Encoding", "gzip"))
    elif defect == "ambiguous_framing":
        https_channel.headers.append(("Transfer-Encoding", "chunked"))
        https_channel.chunks = [b"3\r\nabc\r\n0\r\n\r\n"]
    elif defect == "advertised_too_large":
        https_channel.headers = [("Content-Length", "4")]
    elif defect == "truncated":
        https_channel.chunks = [b"ab"]
    elif defect == "extra_without_length":
        https_channel.headers = []
        https_channel.chunks = [b"abcd"]
    elif defect == "wrong_hash":
        https_channel.chunks = [b"abd"]

    result = stage_release(**stage_inputs)

    assert result.code == code
    assert len(https_channel.requests) == 1
    assert list(stage_inputs["staging_parent"].iterdir()) == []


def test_untrusted_tls_is_refused_without_http_fallback(stage_inputs, https_channel, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    stage_inputs["source"] = HttpsReleaseSource(https_channel.url)

    result = stage_release(**stage_inputs)

    assert result.code == "network_error"
    assert https_channel.requests == []
    assert list(stage_inputs["staging_parent"].iterdir()) == []


def test_slow_trickle_cannot_extend_total_transfer_deadline(stage_inputs, https_channel):
    https_channel.headers = []
    https_channel.chunks, https_channel.delay = [b"a", b"b", b"c"], 0.5
    stage_inputs["source"] = HttpsReleaseSource(https_channel.url)
    start = time.monotonic()

    result = stage_release(**stage_inputs, timeout_s=0.9)

    assert result.code == "deadline_exceeded"
    assert time.monotonic() - start < 3
    assert len(https_channel.requests) == 1
    assert list(stage_inputs["staging_parent"].iterdir()) == []


def test_missing_packaged_runtime_fails_closed_and_cleans_attempt(stage_inputs, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    result = stage_release(**stage_inputs)

    assert result.code == "worker_failed"
    assert list(stage_inputs["staging_parent"].iterdir()) == []


def test_completion_after_deadline_is_not_reported_as_success(stage_inputs, monkeypatch):
    link = os.link

    def slow_publication(*args, **kwargs):
        time.sleep(2.1)
        return link(*args, **kwargs)

    monkeypatch.setattr(os, "link", slow_publication)
    result = stage_release(**stage_inputs, timeout_s=2)

    assert result.code == "deadline_exceeded"
    assert list(stage_inputs["staging_parent"].iterdir()) == []


def test_mapped_network_drive_is_not_a_local_development_source(stage_inputs, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows drive-type contract")
    import ctypes

    monkeypatch.setattr(ctypes.windll.kernel32, "GetDriveTypeW", lambda drive: 4)
    result = stage_release(**stage_inputs)

    assert result.code == "unsafe_path"
    assert list(stage_inputs["staging_parent"].iterdir()) == []


@pytest.mark.parametrize(("defect", "verification_code"), [
    ("key", "updates_disabled"), ("bytes", "invalid_signature"),
    ("description", "invalid_manifest"), ("version", "not_newer"),
])
def test_reverifies_original_inputs_before_touching_any_paths(stage_inputs, tmp_path, defect, verification_code):
    if defect == "key":
        stage_inputs["installed_public_key"] = None
    elif defect == "bytes":
        stage_inputs["raw_manifest"] += b" "
    elif defect == "description":
        stage_inputs["raw_manifest"] = verify_release(
            stage_inputs["raw_manifest"], stage_inputs["detached_signature"], stage_inputs["installed_public_key"],
            "windows", 1, 1)
    elif defect == "version":
        stage_inputs["installed_version_code"] = 2
    stage_inputs["source"] = LocalReleaseSource(tmp_path / "never-read")
    stage_inputs["staging_parent"] = tmp_path / "never-created"

    result = stage_release(**stage_inputs)

    assert result.code == "verification_refused"
    assert result.verification_code == verification_code
    assert not stage_inputs["staging_parent"].exists()


@pytest.mark.parametrize("channel", [
    None, "", "http://127.0.0.1:1/", "file:///tmp/", "https:///releases",
    "https://user:secret@127.0.0.1:1/", "https://127.0.0.1:0/", "https://127.0.0.1:70000/",
    "https://127.0.0.1:bad/", "https://127.0.0.1:1/\n", "https://127.0.0.1:1/?", "https://127.0.0.1:1/#",
])
def test_only_explicit_credential_free_https_channels_are_accepted(stage_inputs, channel):
    stage_inputs["source"] = HttpsReleaseSource(channel)

    assert stage_release(**stage_inputs).code == "invalid_source"
    assert list(stage_inputs["staging_parent"].iterdir()) == []


def test_successful_attempts_are_unique_and_receipt_is_immutable(stage_inputs):
    first, second = stage_release(**stage_inputs), stage_release(**stage_inputs)

    assert first.package_path != second.package_path
    assert first.package_path.read_bytes() == second.package_path.read_bytes() == b"abc"
    with pytest.raises(FrozenInstanceError):
        second.package_path = first.package_path


def test_cleanup_never_removes_a_file_created_by_someone_else(stage_inputs, monkeypatch):
    foreign = []

    def collision(temporary, final):
        final.write_bytes(b"belongs to another actor")
        foreign.append(final)
        raise FileExistsError("occupied")

    monkeypatch.setattr(os, "link", collision)
    result = stage_release(**stage_inputs)

    assert result.code == "cleanup_failed"
    assert foreign[0].read_bytes() == b"belongs to another actor"
    assert list(foreign[0].parent.iterdir()) == foreign


@pytest.mark.parametrize("framing", ["close", "chunked"])
def test_complete_https_body_without_length_still_passes_exact_size_and_hash(stage_inputs, https_channel, framing):
    https_channel.headers = []
    if framing == "chunked":
        https_channel.headers = [("Transfer-Encoding", "chunked")]
        https_channel.chunks = [b"1\r\na\r\n2\r\nbc\r\n0\r\n\r\n"]
    stage_inputs["source"] = HttpsReleaseSource(https_channel.url)

    result = stage_release(**stage_inputs)

    assert isinstance(result, StagedRelease)
    assert result.package_path.read_bytes() == b"abc"


def test_package_spanning_multiple_chunks_is_staged_without_truncation(stage_inputs, signing_key):
    payload = b"abc" * 50000
    (stage_inputs["source"].directory / "vault.exe").write_bytes(payload)
    manifest = json.loads(stage_inputs["raw_manifest"])
    manifest.update(size=150000, sha256=hashlib.sha256(payload).hexdigest())
    raw = json.dumps(manifest).encode()
    stage_inputs.update(raw_manifest=raw, detached_signature=base64.b64encode(
        signing_key.sign(raw, ec.ECDSA(hashes.SHA256()))))

    result = stage_release(**stage_inputs)

    assert isinstance(result, StagedRelease)
    assert result.package_path.read_bytes() == payload


def test_uncertain_worker_termination_preserves_attempt_for_safe_inspection(stage_inputs, monkeypatch):
    import subprocess

    class UnreapableProcess:
        stdin = None

        def communicate(self, *args, **kwargs):
            raise subprocess.TimeoutExpired("synthetic-worker", 0)

        def poll(self):
            return None

        def kill(self):
            raise PermissionError("synthetic OS cannot terminate worker")

    # Inject a process-lifecycle failure at the OS boundary; no real orphan runs.
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: UnreapableProcess())
    result = stage_release(**stage_inputs)

    assert result.code == "cleanup_failed"
    attempts = list(stage_inputs["staging_parent"].iterdir())
    assert len(attempts) == 1
    assert [path.name for path in attempts[0].iterdir()] == ["package.part"]
    assert stage_release(**stage_inputs).code == "staging_review_required"
