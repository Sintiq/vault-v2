package com.vault.phone

import java.time.Duration
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** The Watch tab turns Health Connect numbers into words; the words are tested here. */
class HealthTextTest {
    private val now = Instant.parse("2026-09-23T15:00:00Z")

    @Test
    fun `sleep reads as hours and minutes`() {
        assertEquals("7 h 25 min", HealthText.duration(Duration.ofMinutes(445)))
        assertEquals("40 min", HealthText.duration(Duration.ofMinutes(40)))
        assertNull(HealthText.duration(null))
    }

    @Test
    fun `a reading says how old it is`() {
        assertEquals("just now", HealthText.ago(now.minusSeconds(20), now))
        assertEquals("12 min ago", HealthText.ago(now.minusSeconds(12 * 60), now))
        assertEquals("3 h ago", HealthText.ago(now.minusSeconds(3 * 3600 + 100), now))
        assertEquals("2 d ago", HealthText.ago(now.minusSeconds(2 * 86400 + 5), now))
        assertNull(HealthText.ago(null, now))
    }

    @Test
    fun `heart range needs both ends, average is optional`() {
        assertEquals("today 52–131 bpm, average 74", HealthText.heart(52, 74, 131))
        assertEquals("today 52–131 bpm", HealthText.heart(52, null, 131))
        assertNull(HealthText.heart(null, 74, 131))
    }

    @Test
    fun `oxygen is a whole percent`() {
        assertEquals("97%", HealthText.oxygen(96.6))
        assertNull(HealthText.oxygen(null))
    }
}
