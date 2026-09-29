"""Pure verification of the release-format-v1 manifest and detached signature.

The caller supplies an installed, out-of-band trusted key. A result describes
authenticated metadata only; it is not a package check or installation authority.
Future staging/install boundaries must reverify their inputs, never trust merely
the Python type of a caller-constructible ReleaseDescription.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


class RefusalCode(StrEnum):
    UPDATES_DISABLED = "updates_disabled"
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_MANIFEST = "invalid_manifest"
    PLATFORM_MISMATCH = "platform_mismatch"
    NOT_NEWER = "not_newer"
    INCOMPATIBLE_API = "incompatible_api"
    INVALID_CONTEXT = "invalid_context"


@dataclass(frozen=True, slots=True)
class ReleaseRefusal:
    """Safe machine-readable reason; contains no untrusted input or crypto error."""

    code: RefusalCode


@dataclass(frozen=True, slots=True)
class ReleaseDescription:
    schema: str
    version: str
    version_code: int
    platform: str
    file: str
    size: int
    sha256: str
    min_api_version: int


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate field")
        result[name] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-JSON constant")


def verify_release(
    raw_manifest: bytes,
    detached_signature: bytes,
    installed_public_key: bytes | None,
    expected_platform: str,
    installed_version_code: int,
    available_api_version: int,
) -> ReleaseDescription | ReleaseRefusal:
    """Verify original bytes without filesystem, network or process access.

    ``installed_public_key`` is base64 SPKI DER from the installed app, capped
    at 1 KiB including surrounding ASCII whitespace. Missing/invalid trust
    disables updates. ``installed_version_code=0`` means no prior release;
    otherwise caller version/API values are exact signed-32-bit-range integers.

    Success authenticates the manifest's claims, not the bytes of any package.
    Ephemeral test-key success cannot establish production signing-key trust.
    """
    if type(installed_public_key) is not bytes or not 0 < len(installed_public_key) <= 1024:
        return ReleaseRefusal(RefusalCode.UPDATES_DISABLED)
    try:
        key = serialization.load_der_public_key(base64.b64decode(
            installed_public_key.strip(b" \t\r\n\v\f"), validate=True,
        ))
    except (ValueError, UnsupportedAlgorithm):
        return ReleaseRefusal(RefusalCode.UPDATES_DISABLED)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        return ReleaseRefusal(RefusalCode.UPDATES_DISABLED)
    if (
        type(expected_platform) is not str or expected_platform not in ("windows", "android")
        or type(installed_version_code) is not int or not 0 <= installed_version_code <= 2147483647
        or type(available_api_version) is not int or not 1 <= available_api_version <= 2147483647
    ):
        return ReleaseRefusal(RefusalCode.INVALID_CONTEXT)
    if type(raw_manifest) is not bytes or not 0 < len(raw_manifest) <= 16384:
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    if type(detached_signature) is not bytes or not 0 < len(detached_signature) <= 1024:
        return ReleaseRefusal(RefusalCode.INVALID_SIGNATURE)
    try:
        signature = base64.b64decode(detached_signature.strip(b" \t\r\n\v\f"), validate=True)
        key.verify(signature, raw_manifest, ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature):
        return ReleaseRefusal(RefusalCode.INVALID_SIGNATURE)
    try:
        manifest = json.loads(
            raw_manifest.decode("utf-8"), object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    if type(manifest) is not dict or set(manifest) != {
        "schema", "version", "version_code", "platform", "file", "size",
        "sha256", "min_api_version",
    }:
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    if manifest["schema"] != "vault-v2-release@1":
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    version = manifest["version"]
    if type(version) is not str or re.fullmatch(r"[\x20-\x7e]{1,64}", version) is None:
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    if manifest["platform"] not in ("windows", "android"):
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    for field in ("version_code", "min_api_version"):
        value = manifest[field]
        if type(value) is not int or not 1 <= value <= 2147483647:
            return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    size = manifest["size"]
    size_limit = 1073741824 if manifest["platform"] == "windows" else 268435456
    if type(size) is not int or not 1 <= size <= size_limit:
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    digest = manifest["sha256"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    filename = manifest["file"]
    suffix = ".exe" if manifest["platform"] == "windows" else ".apk"
    if (
        type(filename) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", filename) is None
        or ".." in filename
        or not filename.endswith(suffix)
        or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", filename.split(".", 1)[0].upper())
    ):
        return ReleaseRefusal(RefusalCode.INVALID_MANIFEST)
    if manifest["platform"] != expected_platform:
        return ReleaseRefusal(RefusalCode.PLATFORM_MISMATCH)
    if manifest["version_code"] <= installed_version_code:
        return ReleaseRefusal(RefusalCode.NOT_NEWER)
    if manifest["min_api_version"] > available_api_version:
        return ReleaseRefusal(RefusalCode.INCOMPATIBLE_API)
    return ReleaseDescription(**manifest)
