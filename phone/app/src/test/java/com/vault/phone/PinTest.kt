package com.vault.phone

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The PIN rules of contract §3, without a phone. */
class PinTest {

    @Test
    fun `a PIN is six to twelve digits`() {
        assertTrue(PinPolicy.valid("123456"))
        assertTrue(PinPolicy.valid("123456789012"))
        assertFalse(PinPolicy.valid("12345"))
        assertFalse(PinPolicy.valid("1234567890123"))
        assertFalse(PinPolicy.valid("12345a"))
    }

    @Test
    fun `delays start at the fifth wrong try and the ninth wipes`() {
        assertEquals(listOf(0L, 0L, 0L, 0L, 30_000L, 60_000L, 300_000L, 900_000L),
                     (1..8).map { PinPolicy.delayAfter(it) })
        assertFalse(PinPolicy.lastChance(7))
        assertTrue(PinPolicy.lastChance(8))
        assertFalse(PinPolicy.wipes(8))
        assertTrue(PinPolicy.wipes(9))
    }

    @Test
    fun `waits read as seconds or minutes`() {
        assertEquals("30 s", PinPolicy.waitWords(30_000))
        assertEquals("1 s", PinPolicy.waitWords(1))
        assertEquals("5 min", PinPolicy.waitWords(300_000))
        assertEquals("15 min", PinPolicy.waitWords(899_001))
    }

    @Test
    fun `the hash is PBKDF2 and only the right PIN matches`() {
        val salt = ByteArray(16) { it.toByte() }
        val h = PinHash.hash("482913", salt, 1_000)
        assertArrayEquals(h, PinHash.hash("482913", salt, 1_000))
        assertTrue(PinHash.matches("482913", salt, h, 1_000))
        assertFalse(PinHash.matches("482914", salt, h, 1_000))
        assertFalse(PinHash.matches("482913", ByteArray(16), h, 1_000))
        assertEquals(32, h.size)
    }
}
