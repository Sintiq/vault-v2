package com.vault.phone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The phone says what the PC decided about a date — in words, without recounting. */
class DateTextTest {

    @Test
    fun `the owner's date is just his, whatever flags were there before`() {
        assertEquals("set by you", DateText.note("owner", listOf("date_not_in_quote")))
        assertFalse(DateText.needsOwner("owner", listOf("date_not_in_quote")))
    }

    @Test
    fun `a date read from the quote is not a warning`() {
        assertEquals("date read from the quote", DateText.note("quote", listOf("date_from_quote")))
        assertFalse(DateText.needsOwner("quote", listOf("date_from_quote")))
    }

    @Test
    fun `doubts ask the owner to act`() {
        val note = DateText.note("none", listOf("date_not_in_quote"))!!
        assertTrue(note, note.contains("set it yourself"))
        assertTrue(DateText.needsOwner("none", listOf("relative_deadline")))
        assertTrue(DateText.needsOwner("none", listOf("several_dates")))
    }

    @Test
    fun `a measured date names the watch and asks nothing of the owner`() {
        assertEquals("measured by the device named on the line", DateText.note("device", emptyList()))
        assertFalse(DateText.needsOwner("device", emptyList()))
    }

    @Test
    fun `nothing to say is said as nothing`() {
        assertNull(DateText.note("none", emptyList()))
        assertNull(DateText.note("none", listOf("some_future_flag")))
    }

    @Test
    fun `remaining days come from the PC and read naturally`() {
        assertEquals("in 23 d", DateText.remaining(23))
        assertEquals("today", DateText.remaining(0))
        assertEquals("2 d overdue", DateText.remaining(-2))
        assertNull(DateText.remaining(null))
    }

    @Test
    fun `a picked day round-trips as the same calendar date`() {
        val millis = DateText.millisFromIso("2026-10-15")!!
        assertEquals("2026-10-15", DateText.isoFromMillis(millis))
        assertEquals("2028-02-29", DateText.isoFromMillis(DateText.millisFromIso("2028-02-29")!!))
        assertNull(DateText.millisFromIso(null))
        assertNull(DateText.millisFromIso("not a date"))
    }
}
