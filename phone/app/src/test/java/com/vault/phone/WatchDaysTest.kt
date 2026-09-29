package com.vault.phone

import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The writer of a reading, in words, and the wake-up clock in the phone's zone. */
class WatchDaysTest {
    private val pacific: ZoneId = ZoneId.of("America/Los_Angeles")

    @Test
    fun `a device and its app read as one phrase`() {
        assertEquals("Galaxy Watch Ultra via Samsung Health",
                     WatchDays.source(listOf("Galaxy Watch Ultra"), listOf("com.sec.android.app.shealth")))
    }

    @Test
    fun `an app without a device says the device was not named`() {
        assertEquals("Samsung Health (device not named)", WatchDays.source(emptyList(), listOf("com.sec.android.app.shealth")))
    }

    @Test
    fun `nothing recorded is null, never a watch`() {
        assertNull(WatchDays.source(emptyList(), emptyList()))
    }

    @Test
    fun `several writers are all named, once each, unknown apps by package`() {
        assertEquals("Galaxy Watch Ultra, SM-G781V via Samsung Health, com.example.tracker",
                     WatchDays.source(listOf("SM-G781V", "Galaxy Watch Ultra", "SM-G781V"),
                                      listOf("com.sec.android.app.shealth", "com.example.tracker", "com.sec.android.app.shealth")))
    }

    @Test
    fun `the wake-up clock is the phone's own zone`() {
        assertEquals("07:05", WatchDays.clock(Instant.parse("2026-09-24T14:05:00Z"), pacific))
    }

    @Test
    fun `sleep is the union of sessions, so a night handed over twice is one night`() {
        val night = Instant.parse("2026-09-24T07:40:00Z") to Instant.parse("2026-09-24T13:25:00Z")   // 5 h 45 min
        assertEquals(Duration.ofMinutes(345), WatchDays.slept(listOf(night, night)))
        val nap = Instant.parse("2026-09-24T21:00:00Z") to Instant.parse("2026-09-24T21:40:00Z")
        assertEquals(Duration.ofMinutes(385), WatchDays.slept(listOf(nap, night)))
        val overlap = Instant.parse("2026-09-24T12:00:00Z") to Instant.parse("2026-09-24T14:00:00Z")
        assertEquals(Duration.ofMinutes(380), WatchDays.slept(listOf(night, overlap)))
        assertNull(WatchDays.slept(emptyList()))
    }

    @Test
    fun `a day with only a lone average is empty, a heart range is not`() {
        assertTrue(WatchReadings(heart_avg = 60).isEmpty)
        assertFalse(WatchReadings(heart_min = 42, heart_max = 87).isEmpty)
        assertFalse(WatchReadings(oxygen = 90.0).isEmpty)
    }
}
