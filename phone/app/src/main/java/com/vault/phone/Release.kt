package com.vault.phone

import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.PublicKey
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.X509EncodedKeySpec
import java.util.Base64

/**
 * A release the PC offers, as the signed manifest describes it (contract §7,
 * docs/release-format-v1.md). The manifest is trusted only if its ECDSA P-256
 * signature checks out against the public key built into this app — never a key
 * that came with the update — and only if every field is exactly as the format says.
 */
data class ReleaseManifest(
    val schema: String,
    val version: String,
    val version_code: Int,
    val platform: String,
    val file: String,
    val size: Long,
    val sha256: String,
    val min_api_version: Int,
)

sealed interface ReleaseCheck {
    /** This build carries no release key: it does not check for updates, and says so. */
    data object NotConfigured : ReleaseCheck
    data object UpToDate : ReleaseCheck
    data class Available(val manifest: ReleaseManifest) : ReleaseCheck
    data class Refused(val why: String) : ReleaseCheck
}

/**
 * One update at a time in this app, from the owner's «Update» until the checked bytes are in
 * Android's installer. A second press while one runs is refused, not queued.
 */
object UpdateJob {
    private val running = java.util.concurrent.atomic.AtomicBoolean(false)

    fun <T> run(block: () -> T): T {
        if (!running.compareAndSet(false, true))
            throw VaultClient.VaultException("an update is already on its way — wait for it")
        try {
            return block()
        } finally {
            running.set(false)
        }
    }
}

object ReleaseRules {
    const val MANIFEST_MAX = 16 * 1024
    const val SIGNATURE_MAX = 1024
    const val KEY_MAX = 1024
    const val APK_MAX = 256L * 1024 * 1024
    private val FIELDS = setOf("schema", "version", "version_code", "platform", "file", "size", "sha256",
                               "min_api_version")
    private val FILE = Regex("""[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*\.apk""")
    private val DEVICE_NAMES = setOf("con", "prn", "aux", "nul") + (1..9).flatMap { listOf("com$it", "lpt$it") }
    private val VERSION = Regex("""[\x20-\x7E]{1,64}""")
    private const val ASCII_SPACE = " \t\r\n\u000B\u000C"
    private val SHA = Regex("[0-9a-f]{64}")
    private val P256_ORDER =
        java.math.BigInteger("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)

    /** A P-256 public key only (the curve's order is checked, not just «EC»). */
    fun publicKey(base64Der: String): PublicKey? = runCatching {
        val key = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(Base64.getDecoder().decode(asciiTrim(base64Der))))
        check((key as ECPublicKey).params.order == P256_ORDER) { "not P-256" }
        key
    }.getOrNull()

    fun signatureValid(key: PublicKey, manifest: ByteArray, signatureBase64: String): Boolean = runCatching {
        Signature.getInstance("SHA256withECDSA").run {
            initVerify(key)
            update(manifest)
            verify(Base64.getDecoder().decode(asciiTrim(signatureBase64)))
        }
    }.getOrDefault(false)

    /** Surrounding ASCII whitespace only, as the PC strips it; anything else stays and fails to decode. */
    private fun asciiTrim(s: String) = s.trim { it in ASCII_SPACE }

    private fun strictUtf8(bytes: ByteArray): String? = runCatching {
        Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
    }.getOrNull()

    /**
     * Copy [input] to [out] and prove on the way that these are the signed bytes: exactly
     * [m]'s size and SHA-256. Used where the bytes are handed to Android's installer, so the
     * check covers what is actually installed, not an earlier read of the network.
     */
    fun copyVerified(input: java.io.InputStream, out: java.io.OutputStream, m: ReleaseManifest) {
        val digest = MessageDigest.getInstance("SHA-256")
        val buf = ByteArray(64 * 1024)
        var total = 0L
        while (true) {
            val r = input.read(buf)
            if (r < 0) break
            total += r
            if (total > m.size) throw VaultClient.VaultException("the update changed after it was checked")
            digest.update(buf, 0, r)
            out.write(buf, 0, r)
        }
        val sha = digest.digest().joinToString("") { "%02x".format(it) }
        if (total != m.size || sha != m.sha256) throw VaultClient.VaultException("the update changed after it was checked")
    }

    /** Every field present, of its exact kind, within its bounds — or null. */
    fun parse(bytes: ByteArray): ReleaseManifest? {
        val text = strictUtf8(bytes) ?: return null
        val m = runCatching { StrictJson.flatObject(text) }.getOrNull() ?: return null
        if (m.keys != FIELDS) return null
        fun str(k: String) = m[k] as? String
        fun num(k: String) = m[k] as? Long
        val code = num("version_code") ?: return null
        val size = num("size") ?: return null
        val api = num("min_api_version") ?: return null
        val file = str("file") ?: return null
        val version = str("version") ?: return null
        val sha = str("sha256") ?: return null
        val schema = str("schema") ?: return null
        val platform = str("platform") ?: return null
        if (code !in 1..Int.MAX_VALUE.toLong() || api !in 1..Int.MAX_VALUE.toLong() || size !in 1..APK_MAX) return null
        if (!VERSION.matches(version) || !SHA.matches(sha)) return null
        if (file.length > 120 || !FILE.matches(file) || ".." in file ||
            file.substringBefore('.').lowercase() in DEVICE_NAMES) return null
        return ReleaseManifest(schema, version, code.toInt(), platform, file, size, sha, api.toInt())
    }

    /**
     * Decide about a manifest the PC served. Pure, so every refusal is tested. A manifest
     * is validated in full before «up to date» is said, so an invalid one never hides; the
     * order after that (platform, then newer, then API) is the PC verifier's order.
     */
    fun decide(keyBase64: String, manifest: ByteArray, signature: String,
               installedCode: Int, apiVersion: Int): ReleaseCheck {
        if (keyBase64.isBlank()) return ReleaseCheck.NotConfigured
        val key = keyBase64.takeIf { it.length <= KEY_MAX }?.let(::publicKey) ?: return ReleaseCheck.Refused("this build's release key is damaged")
        if (manifest.size > MANIFEST_MAX || signature.length > SIGNATURE_MAX) return ReleaseCheck.Refused("the release description is too large")
        if (!signatureValid(key, manifest, signature)) return ReleaseCheck.Refused("the release is not signed by Vault's key")
        val m = parse(manifest) ?: return ReleaseCheck.Refused("the release description is not valid")
        if (m.schema != "vault-v2-release@1") return ReleaseCheck.Refused("unknown release format")
        if (m.platform != "android") return ReleaseCheck.Refused("this release is not for Android")
        if (m.version_code <= installedCode) return ReleaseCheck.UpToDate
        if (m.min_api_version > apiVersion) return ReleaseCheck.Refused("this release needs a newer PC")
        return ReleaseCheck.Available(m)
    }
}
