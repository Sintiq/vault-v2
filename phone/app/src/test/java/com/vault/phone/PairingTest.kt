package com.vault.phone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** The QR link and the connection dot, without a phone. */
class PairingTest {
    private val ticket = "AbCdEfGhIjKlMnOpQrStUv"

    @Test
    fun `a QR link gives the address and the ticket`() {
        val link = PairLink.parse("vault-pair://pair?u=https%3A%2F%2Fvault-pc.example.ts.net%2F&t=$ticket")
        assertEquals(PairLink("https://vault-pc.example.ts.net", ticket), link)
    }

    @Test
    fun `anything else is not a pairing link`() {
        assertNull(PairLink.parse("https://vault-pc.example.ts.net/?key=abc"))
        assertNull(PairLink.parse("vault-pair://pair?u=ftp%3A%2F%2Fx&t=$ticket"))
        assertNull(PairLink.parse("vault-pair://pair?u=https%3A%2F%2Fx&t=short"))
        assertNull(PairLink.parse("vault-pair://pair?t=$ticket"))
        assertNull(PairLink.parse("vault-pair://pair?u=https%3A%2F%2Fx&t=bad%20ticket%20with%20spaces"))
    }

    @Test
    fun `green only for a fresh authorised answer from the same vault`() {
        val now = 100_000L
        assertEquals(Link.GREEN, LinkState.of(now - 5_000, now, 2, true))
        assertEquals(Link.RED, LinkState.of(now - 31_000, now, 2, true))
        assertEquals(Link.RED, LinkState.of(null, now, null, true))
        assertEquals(Link.RED, LinkState.of(now - 1_000, now, 2, false))
        assertEquals(Link.AMBER, LinkState.of(now - 1_000, now, 3, true))
        assertEquals("PC online", LinkState.words(Link.GREEN))
    }
}
