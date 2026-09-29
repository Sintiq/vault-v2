package com.vault.phone

import android.content.Context
import android.os.SystemClock
import android.provider.Settings
import java.security.SecureRandom
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

/**
 * The PIN rules, in one place and without a phone, so they are tested.
 * Contract §3: one PIN, 6–12 digits; delays from the 5th wrong try; the 9th wipes.
 */
object PinPolicy {
    const val MIN = 6
    const val MAX = 12
    const val WIPE_AT = 9
    const val EXPORT_WINDOW_MS = 5 * 60 * 1000L

    fun valid(pin: String): Boolean = pin.length in MIN..MAX && pin.all { it in '0'..'9' }

    /** Milliseconds to wait before the next try, after [failures] wrong PINs in a row. */
    fun delayAfter(failures: Int): Long = when (failures) {
        in 0..4 -> 0L
        5 -> 30_000L
        6 -> 60_000L
        7 -> 5 * 60_000L
        else -> 15 * 60_000L
    }

    /** One more wrong PIN erases Vault on this phone. */
    fun lastChance(failures: Int): Boolean = failures == WIPE_AT - 1

    fun wipes(failures: Int): Boolean = failures >= WIPE_AT

    fun waitWords(ms: Long): String {
        val s = (ms + 999) / 1000
        return if (s < 60) "$s s" else "${(s + 59) / 60} min"
    }
}

/** PBKDF2-HMAC-SHA256 of a PIN. Pure. */
object PinHash {
    const val ITERATIONS = 310_000

    fun salt(): ByteArray = ByteArray(16).also { SecureRandom().nextBytes(it) }

    fun hash(pin: String, salt: ByteArray, iterations: Int = ITERATIONS): ByteArray {
        val spec = PBEKeySpec(pin.toCharArray(), salt, iterations, 256)
        try {
            return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).encoded
        } finally {
            spec.clearPassword()
        }
    }

    fun matches(pin: String, salt: ByteArray, expected: ByteArray, iterations: Int = ITERATIONS): Boolean =
        java.security.MessageDigest.isEqual(hash(pin, salt, iterations), expected)
}

/**
 * The PIN on this phone: its hash, the count of wrong tries and the wait before the next.
 * The wait survives a restart and a reboot: elapsed time since boot is used within a boot,
 * and after a reboot the full remaining wait starts again, so moving the clock skips nothing.
 */
class PinStore(context: Context) {
    private val prefs = context.getSharedPreferences("pin", Context.MODE_PRIVATE)
    private val resolver = context.contentResolver

    sealed interface Check {
        data object Ok : Check
        data class Wrong(val failures: Int, val lastChance: Boolean) : Check
        data class Wait(val ms: Long) : Check
        data object Wipe : Check
    }

    val isSet: Boolean get() = prefs.contains("hash")
    val failures: Int get() = prefs.getInt("failures", 0)

    private fun bootCount(): Int =
        runCatching { Settings.Global.getInt(resolver, Settings.Global.BOOT_COUNT) }.getOrDefault(-1)

    fun set(pin: String) {
        require(PinPolicy.valid(pin))
        val salt = PinHash.salt()
        val hash = PinHash.hash(pin, salt)
        val b64 = java.util.Base64.getEncoder()
        prefs.edit().putString("salt", b64.encodeToString(salt)).putString("hash", b64.encodeToString(hash))
            .putInt("iterations", PinHash.ITERATIONS).putInt("failures", 0).remove("wait_until").commit()
            .let { saved -> check(saved) { "PIN not saved" } }
    }

    /** How long until a PIN may be tried again, or 0. */
    fun waitMs(): Long {
        val until = prefs.getLong("wait_until", 0L)
        if (until == 0L) return 0L
        val boot = prefs.getInt("wait_boot", -2)
        val now = SystemClock.elapsedRealtime()
        if (boot != bootCount()) {
            // Rebooted since: the remaining wait starts again from now, never shorter.
            val remaining = prefs.getLong("wait_remaining", 0L)
            prefs.edit().putLong("wait_until", now + remaining).putInt("wait_boot", bootCount()).apply()
            return remaining
        }
        return (until - now).coerceAtLeast(0L)
    }

    /** Check a PIN. Slow on purpose (PBKDF2); call off the main thread. */
    fun check(pin: String): Check {
        // Nine wrong in a row is final: no right PIN, no wait, undoes it.
        if (failures >= PinPolicy.WIPE_AT) return Check.Wipe
        val wait = waitMs()
        if (wait > 0) return Check.Wait(wait)
        val b64 = java.util.Base64.getDecoder()
        val salt = b64.decode(prefs.getString("salt", "") ?: "")
        val hash = b64.decode(prefs.getString("hash", "") ?: "")
        val iterations = prefs.getInt("iterations", PinHash.ITERATIONS)
        if (PinHash.matches(pin, salt, hash, iterations)) {
            check(prefs.edit().putInt("failures", 0).remove("wait_until").commit()) { "PIN state not saved" }
            return Check.Ok
        }
        val failures = failures + 1
        val delay = PinPolicy.delayAfter(failures)
        val now = SystemClock.elapsedRealtime()
        val saved = prefs.edit().putInt("failures", failures)
            .putLong("wait_until", if (delay > 0) now + delay else 0L)
            .putLong("wait_remaining", delay).putInt("wait_boot", bootCount()).commit()
        // A wrong try that could not be counted must not pass as a harmless one.
        check(saved) { "PIN state not saved" }
        if (PinPolicy.wipes(failures)) return Check.Wipe
        return Check.Wrong(failures, PinPolicy.lastChance(failures))
    }
}
