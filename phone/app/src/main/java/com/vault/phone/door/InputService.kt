package com.vault.phone.door

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.view.accessibility.AccessibilityEvent

/**
 * Acting on the screen — and deliberately never looking at it.
 *
 * An accessibility service is the only way an app can tap, and it is a blunt
 * instrument: Android grants it once, in settings, and from then on it could
 * read every screen in every app without asking again. The passport forbids
 * exactly that, so the restraint has to live in this file rather than in the
 * platform.
 *
 * So: no event types are subscribed to, `onAccessibilityEvent` is empty, and
 * nothing here ever calls `rootInActiveWindow` or touches a node. There is no
 * code path from this service to the contents of anyone's screen. Typing text
 * is absent for the same reason — it would require finding the focused field,
 * which means reading.
 *
 * What is left is a finger: taps, swipes, back, home. Where to put it comes
 * from the agent, and whether it may is decided in InputSession.
 */
class InputService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    /** Required by the platform. Intentionally empty: nothing is observed. */
    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    private fun gesture(path: Path, durationMs: Long): Boolean {
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val description = GestureDescription.Builder().addStroke(stroke).build()
        return dispatchGesture(description, null, null)
    }

    fun tap(x: Float, y: Float): Boolean =
        gesture(Path().apply { moveTo(x, y) }, 60)

    fun swipe(fromX: Float, fromY: Float, toX: Float, toY: Float, durationMs: Long): Boolean =
        gesture(Path().apply { moveTo(fromX, fromY); lineTo(toX, toY) }, durationMs)

    fun press(action: String): Boolean = when (action) {
        "back" -> performGlobalAction(GLOBAL_ACTION_BACK)
        "home" -> performGlobalAction(GLOBAL_ACTION_HOME)
        "recents" -> performGlobalAction(GLOBAL_ACTION_RECENTS)
        else -> false
    }

    companion object {
        @Volatile
        private var instance: InputService? = null

        /** Null when the owner has not enabled the service, or has turned it off. */
        fun current(): InputService? = instance

        const val NOT_ENABLED =
            "the vault app is not allowed to act on the screen; turn it on in " +
                "Settings, Accessibility, Vault — and off again whenever you like"
    }
}
