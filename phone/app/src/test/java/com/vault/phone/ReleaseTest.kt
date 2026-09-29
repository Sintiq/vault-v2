package com.vault.phone

import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Signed releases, checked with a throwaway key made here; the real key never enters a test. */
class ReleaseTest {
    private val pair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
    private val other = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
    private val pub = Base64.getEncoder().encodeToString(pair.public.encoded)

    private fun manifest(code: Int = 5, platform: String = "android", api: Int = 2, sha: String = "a".repeat(64)) =
        """{"schema":"vault-v2-release@1","version":"0.5","version_code":$code,"platform":"$platform",""" +
            """"file":"vault.apk","size":10,"sha256":"$sha","min_api_version":$api}"""

    private fun sign(bytes: ByteArray, key: java.security.PrivateKey = pair.private): String =
        Base64.getEncoder().encodeToString(Signature.getInstance("SHA256withECDSA").run {
            initSign(key); update(bytes); sign()
        })

    private fun decide(text: String, sig: String = sign(text.toByteArray()), installed: Int = 1) =
        ReleaseRules.decide(pub, text.toByteArray(), sig, installed, 2)

    @Test
    fun `a newer release signed by the built-in key is offered`() {
        val r = decide(manifest())
        assertTrue(r is ReleaseCheck.Available)
        assertEquals(5, (r as ReleaseCheck.Available).manifest.version_code)
    }

    @Test
    fun `no key in the build means no update check, said plainly`() {
        assertEquals(ReleaseCheck.NotConfigured, ReleaseRules.decide("", manifest().toByteArray(), "", 1, 2))
    }

    @Test
    fun `refusals`() {
        val m = manifest()
        assertTrue(decide(m, sign(m.toByteArray(), other.private)) is ReleaseCheck.Refused)
        assertTrue(decide(m.replace("0.5", "0.6"), sign(m.toByteArray())) is ReleaseCheck.Refused)
        assertTrue(decide(manifest(platform = "windows")) is ReleaseCheck.Refused)
        assertTrue(decide(manifest(api = 3)) is ReleaseCheck.Refused)
        assertTrue(decide(manifest(sha = "zz")) is ReleaseCheck.Refused)
        val extra = manifest().dropLast(1) + ""","extra":1}"""
        assertTrue("an unknown field is a refusal", decide(extra) is ReleaseCheck.Refused)
        val path = manifest().replace("\"vault.apk\"", "\"../vault.apk\"")
        assertTrue("a path is not a file name", decide(path) is ReleaseCheck.Refused)
    }

    @Test
    fun `same or older is up to date, never a downgrade`() {
        assertEquals(ReleaseCheck.UpToDate, decide(manifest(code = 5), installed = 5))
        assertEquals(ReleaseCheck.UpToDate, decide(manifest(code = 3), installed = 5))
    }

    @Test
    fun `an invalid manifest is refused even when it is not newer`() {
        val broken = manifest(code = 1).replace("\"size\":10", "\"size\":0")
        assertTrue(decide(broken) is ReleaseCheck.Refused)
    }

    // Parity with the PC verifier (vault_v2/releases.py, docs/release-format-v1.md).

    @Test
    fun `a display version may hold a space, as printable ASCII does`() {
        val m = manifest().replace("\"0.5\"", "\"0.5 beta\"")
        assertTrue(decide(m) is ReleaseCheck.Available)
    }

    @Test
    fun `a file name may be 120 characters, not 121`() {
        val name120 = "v".repeat(116) + ".apk"
        val name121 = "v".repeat(117) + ".apk"
        assertTrue(decide(manifest().replace("\"vault.apk\"", "\"$name120\"")) is ReleaseCheck.Available)
        assertTrue(decide(manifest().replace("\"vault.apk\"", "\"$name121\"")) is ReleaseCheck.Refused)
    }

    @Test
    fun `a unicode escape takes exactly four hex digits`() {
        val plus = manifest().replace("\"0.5\"", "\"\\u+035\"")
        assertTrue(decide(plus) is ReleaseCheck.Refused)
        val fine = manifest().replace("\"0.5\"", "\"\\u0035\"")
        assertTrue(decide(fine) is ReleaseCheck.Available)
    }

    @Test
    fun `an older release is up to date even when it would need a newer PC`() {
        assertEquals(ReleaseCheck.UpToDate, decide(manifest(code = 3, api = 3), installed = 5))
    }

    @Test
    fun `only ASCII whitespace may surround the signature`() {
        val m = manifest()
        val sig = sign(m.toByteArray())
        assertTrue(decide(m, " \r\n$sig\n") is ReleaseCheck.Available)
        assertTrue(decide(m, "\u2003$sig") is ReleaseCheck.Refused)
    }

    @Test
    fun `a built-in key over 1 KiB counts as damaged`() {
        val padded = pub + " ".repeat(1025 - pub.length)
        val r = ReleaseRules.decide(padded, manifest().toByteArray(), sign(manifest().toByteArray()), 1, 2)
        assertTrue(r is ReleaseCheck.Refused)
    }
}
