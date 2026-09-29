package com.vault.phone.door

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * The gate for anything the passport says needs the owner himself.
 *
 * The agent's call blocks here while a notification with two buttons waits on
 * the phone. No answer inside the window is a refusal — the door fails closed,
 * so a phone in a pocket never says yes by accident.
 */
object Approval {

    private const val CHANNEL = "door-approval"
    const val ACTION_ANSWER = "com.vault.phone.door.ANSWER"
    private const val EXTRA_ID = "id"
    private const val EXTRA_ALLOW = "allow"

    /** Long enough to fish the phone out of a pocket, short enough that a call is not left hanging. */
    private val WINDOW_SECONDS = 60L

    private val pending = ConcurrentHashMap<Long, Gate>()
    private val nextId = AtomicLong(1)

    private class Gate(val latch: CountDownLatch) {
        @Volatile var allowed = false
    }

    /**
     * Can the phone actually reach the owner?
     *
     * With notifications off there is no way to ask him, and the owner-only
     * tier would quietly answer "refused" for a reason that has nothing to do
     * with his wishes. A door that cannot ask should say so, not pretend it
     * asked and was turned down.
     */
    fun canAsk(context: Context): Boolean =
        androidx.core.app.NotificationManagerCompat.from(context).areNotificationsEnabled()

    const val CANNOT_ASK =
        "the phone cannot ask you: notifications for the vault app are turned off"

    fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL, "Agent asks permission", NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Shown when the agent asks for something the passport reserves for you"
            enableVibration(true)
        }
        manager.createNotificationChannel(channel)
    }

    /**
     * Ask the owner. Returns true only if he tapped Allow inside the window.
     * Called on a server thread; blocks that thread and nothing else.
     */
    fun ask(context: Context, what: String, why: String): Boolean {
        if (!canAsk(context)) return false
        val id = nextId.getAndIncrement()
        val gate = Gate(CountDownLatch(1))
        pending[id] = gate

        val manager = context.getSystemService(NotificationManager::class.java)
        manager.notify(id.toInt(), build(context, id, what, why))
        try {
            gate.latch.await(WINDOW_SECONDS, TimeUnit.SECONDS)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        } finally {
            pending.remove(id)
            manager.cancel(id.toInt())
        }
        return gate.allowed
    }

    private fun build(context: Context, id: Long, what: String, why: String): Notification =
        NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_lock_idle_lock)
            .setContentTitle(what)
            .setContentText(why)
            .setStyle(NotificationCompat.BigTextStyle().bigText(why))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            // The lock screen shows that something was asked, not what.
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(
                NotificationCompat.Builder(context, CHANNEL)
                    .setSmallIcon(android.R.drawable.ic_lock_idle_lock)
                    .setContentTitle("The agent is asking for something")
                    .build()
            )
            .setAutoCancel(false)
            .setOngoing(false)
            .addAction(0, "Allow once", answerIntent(context, id, true))
            .addAction(0, "Deny", answerIntent(context, id, false))
            .build()

    private fun answerIntent(context: Context, id: Long, allow: Boolean): PendingIntent =
        PendingIntent.getBroadcast(
            context,
            (id * 2 + if (allow) 1 else 0).toInt(),
            Intent(ACTION_ANSWER).setPackage(context.packageName)
                .putExtra(EXTRA_ID, id).putExtra(EXTRA_ALLOW, allow),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    fun answer(id: Long, allow: Boolean) {
        pending[id]?.let {
            it.allowed = allow
            it.latch.countDown()
        }
    }

    /** Registered by the service; turns a button tap into an answer. */
    class Receiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != ACTION_ANSWER) return
            answer(intent.getLongExtra(EXTRA_ID, -1), intent.getBooleanExtra(EXTRA_ALLOW, false))
        }
    }

    fun register(context: Context): Receiver = Receiver().also {
        ContextCompat.registerReceiver(
            context, it, IntentFilter(ACTION_ANSWER), ContextCompat.RECEIVER_NOT_EXPORTED
        )
    }
}
