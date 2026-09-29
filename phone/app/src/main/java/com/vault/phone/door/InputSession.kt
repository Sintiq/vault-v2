package com.vault.phone.door

import android.content.Context
import java.util.concurrent.atomic.AtomicLong

/**
 * When the agent may use the finger, and for how long.
 *
 * Asking for every tap would make the prompt meaningless — nobody reads the
 * tenth one. Asking once and never again would be worse. So the owner grants
 * a short window: he is asked once, the window is visible on the door screen
 * with the time left, he can end it with one tap, and it closes itself.
 *
 * Outside a window there is no acting at all, and the phone says so.
 */
object InputSession {

    /** Long enough to do something, short enough to be an event rather than a state. */
    const val WINDOW_MS = 5 * 60 * 1000L

    private val until = AtomicLong(0)
    private val actions = AtomicLong(0)
    private val lock = Any()
    private var generation = 0L

    val isOpen: Boolean
        get() = System.currentTimeMillis() < until.get()

    val secondsLeft: Long
        get() = maxOf(0, (until.get() - System.currentTimeMillis()) / 1000)

    val actionsThisWindow: Long
        get() = if (isOpen) actions.get() else 0

    fun close() {
        revoke { }
    }

    /** Revoke and publish a changed key together. No owner wait or network belongs here. */
    fun revoke(publish: () -> Unit) {
        synchronized(lock) {
            generation++
            until.set(0)
            actions.set(0)
            publish()
        }
    }

    /**
     * Capture before authentication. This epoch tracks explicit close/key revocation;
     * natural expiry still uses the existing deadline, not a distinct grant identity.
     */
    fun requestGeneration(): Long = synchronized(lock) { generation }

    /** Only a nonwaiting dispatch belongs here; already dispatched gestures cannot be recalled. */
    fun actIfOpen(expectedGeneration: Long, dispatch: () -> Boolean): Boolean? = synchronized(lock) {
        if (generation != expectedGeneration || !isOpen) return@synchronized null
        dispatch().also { actions.incrementAndGet() }
    }

    /**
     * Open a window if the owner allows one. Returns null when he may act, or
     * a sentence explaining why he may not.
     */
    fun ensureOpen(context: Context, expectedGeneration: Long): String? = ensureOpen(
        inputEnabled = InputService.current() != null, canAsk = Approval.canAsk(context),
        expectedGeneration = expectedGeneration
    ) {
        val minutes = WINDOW_MS / 60000
        Approval.ask(
            context,
            "Let the agent use the screen?",
            "The agent wants to tap and swipe on this phone for the next $minutes minutes. " +
                "It cannot read what is on the screen. You can end this at any time on the " +
                "door screen.",
        )
    }

    /** Same gate for Android and JVM callers; only owner interaction varies. */
    fun ensureOpen(
        inputEnabled: Boolean, canAsk: Boolean,
        expectedGeneration: Long = requestGeneration(), approve: () -> Boolean
    ): String? {
        if (!inputEnabled) return InputService.NOT_ENABLED
        synchronized(lock) {
            if (generation != expectedGeneration) return "no window open"
            if (isOpen) return null
        }
        if (!canAsk) return Approval.CANNOT_ASK
        val allowed = approve() // Never hold the lock while the owner is deciding.
        return synchronized(lock) {
            when {
                generation != expectedGeneration -> "no window open"
                !allowed -> "refused on the phone"
                else -> {
                    until.set(System.currentTimeMillis() + WINDOW_MS)
                    actions.set(0)
                    null
                }
            }
        }
    }
}
