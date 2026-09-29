"""Public release-verification seam; all signing identities are ephemeral.

These fixtures exercise protocol verification, not a production signing identity,
the provenance of an installed key, package contents, staging or installation.
"""

import base64
import json
from dataclasses import FrozenInstanceError, asdict

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from vault_v2.releases import RefusalCode, ReleaseDescription, ReleaseRefusal, verify_release


@pytest.fixture(scope="module")
def signing_key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def manifest():
    return {
        "schema": "vault-v2-release@1",
        "version": "Release candidate 0.1",
        "version_code": 2,
        "platform": "windows",
        "file": "vault-v2.exe",
        "size": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "min_api_version": 1,
    }


def public_key_bytes(key):
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))


def signature_bytes(key, raw):
    return base64.b64encode(key.sign(raw, ec.ECDSA(hashes.SHA256())))


def verify_signed(key, raw, **context):
    return verify_release(
        raw, signature_bytes(key, raw), public_key_bytes(key),
        context.get("expected_platform", "windows"),
        context.get("installed_version_code", 1),
        context.get("available_api_version", 1),
    )


def test_accepts_signature_over_original_manifest_bytes(signing_key, manifest):
    raw = b" \n" + json.dumps(manifest, indent=2).encode("utf-8") + b"\n"
    result = verify_signed(signing_key, raw)

    assert isinstance(result, ReleaseDescription)
    assert asdict(result) == {
        "schema": "vault-v2-release@1",
        "version": "Release candidate 0.1",
        "version_code": 2,
        "platform": "windows",
        "file": "vault-v2.exe",
        "size": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "min_api_version": 1,
    }


@pytest.mark.parametrize("installed_key", [None, b"", b"not base64!", b"YQ==", b"A" * 1025, b"\xff"])
def test_missing_or_malformed_installed_key_disables_updates(signing_key, manifest, installed_key):
    raw = json.dumps(manifest).encode()
    result = verify_release(raw, signature_bytes(signing_key, raw), installed_key, "windows", 1, 1)

    assert result.code == "updates_disabled"


@pytest.mark.parametrize("curve", [ec.SECP384R1(), ec.SECP256K1()])
def test_other_curves_cannot_act_as_installed_release_key(curve, manifest):
    key = ec.generate_private_key(curve)
    result = verify_signed(key, json.dumps(manifest).encode())

    assert result.code == "updates_disabled"


@pytest.mark.parametrize("defect", [
    "missing", "text", "empty", "oversized", "embedded_space", "non_ascii_space",
    "invalid_base64", "invalid_der", "raw_rs", "wrong_key", "sha384", "changed_bytes",
])
def test_untrusted_signature_is_a_safe_refusal(signing_key, manifest, defect):
    raw = json.dumps(manifest).encode()
    signature = signature_bytes(signing_key, raw)
    if defect == "missing":
        signature = None
    elif defect == "text":
        signature = signature.decode()
    elif defect == "empty":
        signature = b""
    elif defect == "oversized":
        signature = signature + b" " * 1024
    elif defect == "embedded_space":
        signature = signature[:8] + b" " + signature[8:]
    elif defect == "non_ascii_space":
        signature = b"\xc2\xa0" + signature
    elif defect == "invalid_base64":
        signature = b"!" + signature
    elif defect == "invalid_der":
        signature = base64.b64encode(b"invalid DER")
    elif defect == "raw_rs":
        r, s = decode_dss_signature(base64.b64decode(signature))
        signature = base64.b64encode(r.to_bytes(32) + s.to_bytes(32))
    elif defect == "wrong_key":
        signature = signature_bytes(ec.generate_private_key(ec.SECP256R1()), raw)
    elif defect == "sha384":
        signature = base64.b64encode(signing_key.sign(raw, ec.ECDSA(hashes.SHA384())))
    elif defect == "changed_bytes":
        raw = b" " + raw

    result = verify_release(raw, signature, public_key_bytes(signing_key), "windows", 1, 1)

    assert result.code == "invalid_signature"


@pytest.mark.parametrize("defect", [
    "empty", "oversized", "bom", "invalid_utf8", "utf16", "trailing_json",
    "duplicate", "nested_duplicate", "nan", "infinity", "negative_infinity",
    "list", "null", "scalar", "deep", "missing_field", "unknown_field",
])
def test_signed_manifest_still_requires_bounded_strict_json(signing_key, manifest, defect):
    raw = json.dumps(manifest).encode()
    if defect == "empty":
        raw = b""
    elif defect == "oversized":
        raw = raw + b" " * (16385 - len(raw))
    elif defect == "bom":
        raw = b"\xef\xbb\xbf" + raw
    elif defect == "invalid_utf8":
        raw = raw.replace(b"Release candidate", b"\xff candidate")
    elif defect == "utf16":
        raw = raw.decode().encode("utf-16")
    elif defect == "trailing_json":
        raw += b" {}"
    elif defect == "duplicate":
        raw = raw[:-1] + b', "version_code": 2}'
    elif defect == "nested_duplicate":
        raw = raw.replace(b'"Release candidate 0.1"', b'{"a":1,"a":2}')
    elif defect in {"nan", "infinity", "negative_infinity"}:
        value = {"nan": b"NaN", "infinity": b"Infinity", "negative_infinity": b"-Infinity"}[defect]
        raw = raw.replace(b'"Release candidate 0.1"', value)
    elif defect == "list":
        raw = b"[" + raw + b"]"
    elif defect == "null":
        raw = b"null"
    elif defect == "scalar":
        raw = b"1"
    elif defect == "deep":
        raw = b"[" * 1500 + b"0" + b"]" * 1500
    elif defect == "missing_field":
        del manifest["sha256"]
        raw = json.dumps(manifest).encode()
    elif defect == "unknown_field":
        manifest["public_key"] = "must not replace installed trust"
        raw = json.dumps(manifest).encode()

    assert verify_signed(signing_key, raw).code == "invalid_manifest"


@pytest.mark.parametrize(("field", "value"), [
    ("schema", "vault-v2-release@2"), ("schema", None),
    ("version", ""), ("version", "x" * 65), ("version", "v\n2"),
    ("version", "v\x7f2"), ("version", "версия"), ("version", "\ud800"),
    ("version", 2), ("version", []), ("version", {}),
    ("platform", "Windows"), ("platform", "linux"), ("platform", None),
    ("sha256", "A" * 64), ("sha256", "g" * 64), ("sha256", "a" * 63),
    ("sha256", "a" * 65), ("sha256", 123), ("sha256", "a" * 63 + "\n"),
    *[(field, value) for field in ("version_code", "min_api_version")
      for value in (True, False, 2.0, "2", None, [], {}, 0, -1, 2147483648)],
    *[("size", value) for value in (True, False, 3.0, "3", None, [], {}, 0, -1, 1073741825)],
])
def test_signed_fields_must_match_the_v1_types_and_bounds(signing_key, manifest, field, value):
    manifest[field] = value

    assert verify_signed(signing_key, json.dumps(manifest).encode()).code == "invalid_manifest"


@pytest.mark.parametrize("file", [
    "", None, 3, [], {}, ".exe", "-vault.exe", "_vault.exe",
    "a" * 117 + ".exe", "vault..exe", "../vault.exe", "folder/vault.exe",
    "folder\\vault.exe", "C:vault.exe", "https://example.test/vault.exe",
    "\\\\server\\vault.exe", "vault.exe:stream", "vault.exe.", "vault.exe ",
    "vault .exe", "vault\x00.exe", "vault\n.exe", "вклад.exe", "vault%20.exe",
    "vault.apk", "vault.EXE", "vault.exe.zip",
    *[name + ".exe" for name in ("CON", "prn", "Aux", "NUL")],
    *[f"{prefix}{number}.extra.exe" for prefix in ("com", "LPT") for number in range(1, 10)],
])
def test_manifest_cannot_supply_an_unsafe_windows_package_name(signing_key, manifest, file):
    manifest["file"] = file

    assert verify_signed(signing_key, json.dumps(manifest).encode()).code == "invalid_manifest"


@pytest.mark.parametrize(("change", "context", "code"), [
    ({}, {"expected_platform": "android"}, "platform_mismatch"),
    ({}, {"installed_version_code": 2}, "not_newer"),
    ({"version": "9999.0"}, {"installed_version_code": 3}, "not_newer"),
    ({"min_api_version": 2}, {"available_api_version": 1}, "incompatible_api"),
])
def test_valid_signature_does_not_bypass_update_compatibility(signing_key, manifest, change, context, code):
    manifest.update(change)

    assert verify_signed(signing_key, json.dumps(manifest).encode(), **context).code == code


@pytest.mark.parametrize(("field", "value"), [
    *[("expected_platform", value) for value in ("Windows", "linux", "", None, [], {})],
    *[("installed_version_code", value) for value in (True, False, 1.0, "1", None, [], {}, -1, 2147483648)],
    *[("available_api_version", value) for value in (True, False, 1.0, "1", None, [], {}, 0, -1, 2147483648)],
])
def test_invalid_caller_context_is_refused_without_coercion(signing_key, manifest, field, value):
    result = verify_signed(signing_key, json.dumps(manifest).encode(), **{field: value})

    assert result.code == "invalid_context"


@pytest.mark.parametrize(("platform", "suffix", "size"), [
    ("windows", ".exe", 1073741824), ("android", ".apk", 268435456),
])
def test_accepts_exact_protocol_limits(signing_key, manifest, platform, suffix, size):
    manifest.update(platform=platform, file="A" * 116 + suffix, size=size,
                    version="!" * 63 + "~", version_code=2147483647, min_api_version=2147483647)
    raw = json.dumps(manifest).encode()
    raw += b" " * (16384 - len(raw))
    signature = signature_bytes(signing_key, raw)
    signature = b"\t\r\n\v\f" + signature + b" " * (1019 - len(signature))
    key = public_key_bytes(signing_key)
    key = b"\n" + key + b" " * (1023 - len(key))

    result = verify_release(raw, signature, key, platform, 2147483646, 2147483647)

    assert isinstance(result, ReleaseDescription)
    assert result.platform == platform
    assert result.size == size
    assert result.version_code == 2147483647
    assert len(result.file) == 120
    assert len(result.version) == 64


@pytest.mark.parametrize("platform", ["windows", "android"])
def test_accepts_first_version_and_minimum_package_size(signing_key, manifest, platform):
    manifest.update(platform=platform, file="a.exe" if platform == "windows" else "a.apk",
                    version="0", version_code=1, size=1)

    result = verify_signed(signing_key, json.dumps(manifest).encode(),
                           expected_platform=platform, installed_version_code=0)

    assert isinstance(result, ReleaseDescription)
    assert result.version_code == 1
    assert result.min_api_version == 1
    assert result.size == 1


@pytest.mark.parametrize("change", [
    {"size": 268435457}, {"file": "a.exe"}, {"file": "a.APK"},
    {"file": "CON.apk"}, {"file": "a..apk"},
])
def test_android_manifest_has_its_own_package_limit_and_apk_suffix(signing_key, manifest, change):
    manifest.update(platform="android", file="vault.apk")
    manifest.update(change)

    result = verify_signed(signing_key, json.dumps(manifest).encode(), expected_platform="android")

    assert result.code == "invalid_manifest"


@pytest.mark.parametrize("encoding", ["pem", "private", "point", "trailing_der", "embedded_whitespace", "ed25519"])
def test_installed_key_requires_only_p256_spki_der(signing_key, manifest, encoding):
    raw = json.dumps(manifest).encode()
    key = public_key_bytes(signing_key)
    if encoding == "pem":
        key = signing_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
    elif encoding == "private":
        key = base64.b64encode(signing_key.private_bytes(serialization.Encoding.DER,
                               serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    elif encoding == "point":
        key = base64.b64encode(signing_key.public_key().public_bytes(serialization.Encoding.X962,
                               serialization.PublicFormat.UncompressedPoint))
    elif encoding == "trailing_der":
        key = base64.b64encode(base64.b64decode(key) + b"untrusted suffix")
    elif encoding == "embedded_whitespace":
        key = key[:8] + b"\n" + key[8:]
    elif encoding == "ed25519":
        key = public_key_bytes(ed25519.Ed25519PrivateKey.generate())

    result = verify_release(raw, signature_bytes(signing_key, raw), key, "windows", 1, 1)

    assert result == ReleaseRefusal(RefusalCode.UPDATES_DISABLED)


@pytest.mark.parametrize("raw", [None, "{}", bytearray(b"{}"), memoryview(b"{}")])
def test_manifest_input_must_be_immutable_bytes(signing_key, raw):
    result = verify_release(raw, b"", public_key_bytes(signing_key), "windows", 1, 1)

    assert result == ReleaseRefusal(RefusalCode.INVALID_MANIFEST)


def test_authenticated_description_and_refusals_are_immutable(signing_key, manifest):
    result = verify_signed(signing_key, json.dumps(manifest).encode())
    with pytest.raises(FrozenInstanceError):
        result.size = 100
    with pytest.raises(FrozenInstanceError):
        result.file = "other.exe"

    refusal = verify_release(b"private marker", b"secret marker", None, "windows", 1, 1)
    assert refusal == ReleaseRefusal(RefusalCode.UPDATES_DISABLED)
    with pytest.raises(FrozenInstanceError):
        refusal.code = RefusalCode.INVALID_SIGNATURE
    assert "marker" not in repr(refusal)
