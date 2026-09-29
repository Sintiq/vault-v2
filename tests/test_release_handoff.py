"""Explicit installation seam, synthetic bytes and process-launch adapter only."""
import base64
import json
import os
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from vault_v2.release_stage import LocalReleaseSource, StagedRelease, stage_release
from vault_v2.release_handoff import InstallOffer, inspect_staged_release, handoff_release


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handoff lock semantics")


@pytest.fixture
def staged(tmp_path):
    source = tmp_path / "channel"
    source.mkdir()
    (source / "setup.exe").write_bytes(b"abc")  # never executed
    key = ec.generate_private_key(ec.SECP256R1())
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))
    raw = json.dumps({"schema": "vault-v2-release@1", "version": "Test 2", "version_code": 2,
        "platform": "windows", "file": "setup.exe", "size": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "min_api_version": 1}).encode()
    sig = base64.b64encode(key.sign(raw, ec.ECDSA(hashes.SHA256())))
    parent = tmp_path / "staged"
    parent.mkdir()
    result = stage_release(raw, sig, pub, "windows", 1, 1, source=LocalReleaseSource(source),
                           staging_parent=parent)
    assert isinstance(result, StagedRelease), result
    return result, pub, parent


def test_handoff_requires_confirmation_of_the_shown_release_before_any_launch(staged):
    package, pub, parent = staged
    offer = inspect_staged_release(package, pub, 1, 1, staging_parent=parent)
    assert isinstance(offer, InstallOffer)
    assert offer.release.version == "Test 2"
    def forbidden(_):
        pytest.fail("nothing may launch without confirmation")
    result = handoff_release(package, pub, 1, 1, staging_parent=parent,
        confirmed_digest="", launch=forbidden, namespace="Local\\VaultV2-handoff-test-" + uuid.uuid4().hex)
    assert result.code == "confirmation_required"


def test_confirmed_handoff_rechecks_bytes_and_retains_read_lock_until_launch(staged):
    from vault_v2.installation import InstallationSession, InstallationBusy
    package, pub, parent = staged
    offer = inspect_staged_release(package, pub, 1, 1, staging_parent=parent)
    namespace = "Local\\VaultV2-handoff-test-" + uuid.uuid4().hex
    def launch(path):
        assert path == package.package_path
        with pytest.raises(OSError):
            path.write_bytes(b"bad")
        with pytest.raises(InstallationBusy):
            with InstallationSession("runtime", namespace=namespace):
                pytest.fail("handoff admission must be held during launch")
        return 123  # synthetic process adapter; never execute the fixture
    result = handoff_release(package, pub, 1, 1, staging_parent=parent,
                             confirmed_digest=offer.approval_digest, launch=launch, namespace=namespace)
    assert result.state == "started_unconfirmed"
    assert result.pid == 123
    assert package.package_path.read_bytes() == b"abc"


@pytest.mark.parametrize("replacement", [b"abd", b"ab", b"abcd"])
def test_changed_package_after_display_is_refused_without_launch(staged, replacement):
    package, pub, parent = staged
    offer = inspect_staged_release(package, pub, 1, 1, staging_parent=parent)
    package.package_path.write_bytes(replacement)
    result = handoff_release(package, pub, 1, 1, staging_parent=parent,
        confirmed_digest=offer.approval_digest, launch=lambda _: pytest.fail("changed bytes cannot run"),
        namespace="Local\\VaultV2-handoff-test-" + uuid.uuid4().hex)
    assert result.code == "package_changed"


def test_active_window_refuses_handoff_without_killing_or_releasing_the_window(staged):
    from vault_v2.installation import InstallationSession, RuntimeState, runtime_state
    package, pub, parent = staged
    offer = inspect_staged_release(package, pub, 1, 1, staging_parent=parent)
    namespace = "Local\\VaultV2-handoff-test-" + uuid.uuid4().hex
    with InstallationSession("runtime", namespace=namespace):
        result = handoff_release(package, pub, 1, 1, staging_parent=parent,
            confirmed_digest=offer.approval_digest, namespace=namespace,
            launch=lambda _: pytest.fail("active window must prevent launch"))
        assert result.code == "runtime_busy_or_unknown"
        assert runtime_state(namespace) == RuntimeState.ACTIVE


@pytest.mark.parametrize("change", ["signature", "key", "downgrade", "path", "confirmation"])
def test_forged_or_stale_receipt_does_not_confer_authority(staged, tmp_path, change):
    from dataclasses import replace
    package, pub, parent = staged
    offer = inspect_staged_release(package, pub, 1, 1, staging_parent=parent)
    version, digest = 1, offer.approval_digest
    expected = "verification_refused"
    if change == "signature":
        package = replace(package, detached_signature=b"ZmFrZQ==")
    elif change == "key":
        pub = b"not-a-trust-anchor"
    elif change == "downgrade":
        version = 2
    elif change == "path":
        outside = tmp_path / "setup.exe"
        outside.write_bytes(b"abc")
        package = replace(package, package_path=outside)
        expected = "invalid_stage"
    else:
        digest = "0" * 64
        expected = "confirmation_required"
    result = handoff_release(package, pub, version, 1, staging_parent=parent,
        confirmed_digest=digest, launch=lambda _: pytest.fail("must refuse before launch"),
        namespace="Local\\VaultV2-handoff-test-" + uuid.uuid4().hex)
    assert result.code == expected


def test_receipt_description_is_not_trust_and_launch_failure_is_not_install_success(staged):
    from dataclasses import replace
    from vault_v2.installation import InstallationSession
    package, pub, parent = staged
    package = replace(package, release=replace(package.release, version="FORGED"))
    offer = inspect_staged_release(package, pub, 1, 1, staging_parent=parent)
    assert offer.release.version == "Test 2"
    namespace = "Local\\VaultV2-handoff-test-" + uuid.uuid4().hex
    def fail(_):
        raise OSError("synthetic launch failure")
    result = handoff_release(package, pub, 1, 1, staging_parent=parent,
                             confirmed_digest=offer.approval_digest, launch=fail, namespace=namespace)
    assert result.code == "launch_failed"
    with InstallationSession("runtime", namespace=namespace):
        pass  # no stuck install marker after failure
