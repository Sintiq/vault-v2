package com.vault.phone.door

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Brings the door back after the phone restarts — but only if the owner had
 * left it open. A reboot must not open a door he closed, and must not close
 * one he deliberately left open and expects to reach.
 *
 * Also fires when the app is replaced, so reinstalling a new build does not
 * quietly leave the phone unreachable.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val relevant = intent.action in setOf(
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
        )
        if (!relevant) return
        if (!DoorKey(context).isOpen) return
        DoorService.start(context)
    }
}

/**
 * At boot the door is usually ready before Tailscale is, and an address that
 * does not exist yet is not a failure — it is a few seconds early. So it
 * waits, briefly and with a widening gap, and then stops rather than spinning
 * on a phone in someone's pocket for the rest of the day.
 */
object StartupPolicy {

    const val MAX_ATTEMPTS = 12
    private const val FIRST_DELAY_MS = 3_000L
    private const val CEILING_MS = 30_000L

    fun shouldKeepTrying(attempt: Int): Boolean = attempt < MAX_ATTEMPTS

    fun delayMs(attempt: Int): Long =
        minOf(FIRST_DELAY_MS * (1L shl minOf(attempt, 5)), CEILING_MS)

    /** Roughly how long the door will keep waiting for Tailscale, in seconds. */
    fun totalWaitSeconds(): Long =
        (0 until MAX_ATTEMPTS).sumOf { delayMs(it) } / 1000
}
