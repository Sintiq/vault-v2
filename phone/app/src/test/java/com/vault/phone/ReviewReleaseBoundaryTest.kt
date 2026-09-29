package com.vault.phone

import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import org.junit.Assert.assertFalse
import org.junit.Test

/** Review-only synthetic tests against pinned 1bc6e1c; no real release material. */
class ReviewReleaseBoundaryTest {
    private val key = KeyPairGenerator.getInstance("EC").apply {
        initialize(ECGenParameterSpec("secp256r1"))
    }.generateKeyPair()
    private val raw = """{"schema":"vault-v2-release@1","version":"0.5","version_code":5,"platform":"android","file":"vault.apk","size":10,"sha256":"${"a".repeat(64)}","min_api_version":2}"""

    private fun check(text: String, pair: java.security.KeyPair = key): ReleaseCheck {
        val bytes = text.toByteArray()
        val sig = Signature.getInstance("SHA256withECDSA").run {
            initSign(pair.private); update(bytes); sign()
        }
        return ReleaseRules.decide(Base64.getEncoder().encodeToString(pair.public.encoded), bytes,
            Base64.getEncoder().encodeToString(sig), 1, 2)
    }

    @Test fun `duplicate signed keys are refused`() {
        assertFalse(check(raw.replace("\"version_code\":5", "\"version_code\":4,\"version_code\":5")) is ReleaseCheck.Available)
    }

    @Test fun `negative API version is refused`() {
        assertFalse(check(raw.replace("\"min_api_version\":2", "\"min_api_version\":-1")) is ReleaseCheck.Available)
    }

    @Test fun `missing API version is refused`() {
        assertFalse(check(raw.replace(",\"min_api_version\":2", "")) is ReleaseCheck.Available)
    }

    @Test fun `empty display version is refused`() {
        assertFalse(check(raw.replace("\"version\":\"0.5\"", "\"version\":\"\"")) is ReleaseCheck.Available)
    }

    @Test fun `double dot filename is refused`() {
        assertFalse(check(raw.replace("vault.apk", "x..apk")) is ReleaseCheck.Available)
    }

    @Test fun `wrong curve public key is refused`() {
        val otherCurve = KeyPairGenerator.getInstance("EC").apply {
            initialize(ECGenParameterSpec("secp384r1"))
        }.generateKeyPair()
        assertFalse(check(raw, otherCurve) is ReleaseCheck.Available)
    }

    @Test fun `string is not an integer version code`() {
        assertFalse(check(raw.replace("\"version_code\":5", "\"version_code\":\"5\"")) is ReleaseCheck.Available)
    }
}
