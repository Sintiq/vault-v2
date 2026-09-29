package com.vault.phone

import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

/**
 * Dates on the phone: what the PC's flags mean in words, and a chooser for the
 * owner's own date. The PC counts the days; the phone only says what it was told.
 */
object DateText {
    /** One line for a date's origin and doubts, or null when there is nothing to say. */
    fun note(dueSource: String, flags: List<String>): String? {
        if (dueSource == "owner") return "set by you"
        if (dueSource == "device") return "measured by the device named on the line"
        val parts = flags.mapNotNull {
            when (it) {
                "date_from_quote" -> "date read from the quote"
                "date_not_in_quote" -> "the agent's date is not in the quote — set it yourself"
                "ambiguous_date" -> "month and day could be swapped — check"
                "several_dates" -> "several dates in the quote — set the right one yourself"
                "relative_deadline" -> "the quote has a date and a relative term — check and set the date yourself"
                "not_verified" -> "saved earlier, not verified against the quote"
                else -> null
            }
        }
        return parts.takeIf { it.isNotEmpty() }?.joinToString(" · ")
    }

    /** "in 23 d", "today", "2 d overdue" — from the PC's count, never recomputed here. */
    fun remaining(daysLeft: Int?): String? = when {
        daysLeft == null -> null
        daysLeft == 0 -> "today"
        daysLeft > 0 -> "in $daysLeft d"
        else -> "${-daysLeft} d overdue"
    }

    /** Whether a flag asks the owner to act, so the line is shown as a warning. */
    fun needsOwner(dueSource: String, flags: List<String>): Boolean =
        dueSource != "owner" && flags.any { it != "date_from_quote" }

    fun isoFromMillis(millis: Long): String =
        Instant.ofEpochMilli(millis).atZone(ZoneOffset.UTC).toLocalDate().toString()

    fun millisFromIso(iso: String?): Long? = runCatching {
        LocalDate.parse(iso).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()
    }.getOrNull()
}

/**
 * The owner's date: pick a day, or say "no date". Material's picker works in
 * UTC midnight millis, which is exactly a calendar day — no time zone guessing.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DateChooser(title: String, current: String?, onPick: (String?) -> Unit, onDismiss: () -> Unit) {
    val state = rememberDatePickerState(initialSelectedDateMillis = DateText.millisFromIso(current))
    DatePickerDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(
                onClick = { state.selectedDateMillis?.let { onPick(DateText.isoFromMillis(it)) } },
                enabled = state.selectedDateMillis != null,
            ) { Text("Save") }
        },
        dismissButton = {
            TextButton(onClick = { onPick(null) }) { Text("No date") }
        },
    ) {
        DatePicker(state = state, title = { Text(title, modifier = androidx.compose.ui.Modifier) })
    }
}
