package com.vault.phone.door

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import androidx.core.app.NotificationCompat

/** The two notifications the door owns: the standing one, and "open this app?". */
object DoorNotifications {

    const val CHANNEL_DOOR = "door-status"
    private const val CHANNEL_ASK = "door-open-app"
    private const val CHANNEL_REMIND = "vault-reminders"
    const val ONGOING_ID = 4711
    private const val ASK_ID = 4712
    private const val REMIND_ID = 4715

    fun ensureChannels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_DOOR, "Agent door", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while the phone is answering the agent"
            }
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ASK, "Open an app", NotificationManager.IMPORTANCE_HIGH)
        )
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_REMIND, "Tasks due", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "A task in the vault is due tomorrow, today, or overdue"
            }
        )
        Approval.ensureChannel(context)
        Screenshot.ensureChannel(context)
    }

    /**
     * The door cannot be quiet. Android demands a visible notification for a
     * service like this, and that suits the design: a door that answers an
     * agent should be impossible to forget about.
     */
    fun ongoing(context: Context, address: String?): android.app.Notification =
        NotificationCompat.Builder(context, CHANNEL_DOOR)
            .setSmallIcon(android.R.drawable.ic_lock_idle_lock)
            .setContentTitle("Vault door is open")
            .setContentText(
                if (address == null) "waiting for Tailscale" else "answering on $address"
            )
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setVisibility(NotificationCompat.VISIBILITY_SECRET)
            .build()

    /** One notification for everything due; tapping it opens the Tasks tab. */
    fun reminder(context: Context, lines: List<String>) {
        val open = PendingIntent.getActivity(
            context, REMIND_ID,
            android.content.Intent(context, com.vault.phone.MainActivity::class.java)
                .putExtra("tab", "tasks")
                .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_REMIND)
            .setSmallIcon(android.R.drawable.ic_menu_agenda)
            .setContentTitle(if (lines.size == 1) "A task is due" else "${lines.size} tasks are due")
            .setContentText(lines.first())
            .setStyle(NotificationCompat.InboxStyle().also { s -> lines.forEach { s.addLine(it) } })
            .setContentIntent(open)
            .setAutoCancel(true)
            // The lock screen says something is due, not what.
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(
                NotificationCompat.Builder(context, CHANNEL_REMIND)
                    .setSmallIcon(android.R.drawable.ic_menu_agenda)
                    .setContentTitle("Something in the vault is due")
                    .build()
            )
            .build()
        context.getSystemService(NotificationManager::class.java).notify(REMIND_ID, notification)
    }

    fun askToOpen(context: Context, label: String, tap: PendingIntent) {
        val notification = NotificationCompat.Builder(context, CHANNEL_ASK)
            .setSmallIcon(android.R.drawable.ic_menu_send)
            .setContentTitle("Open $label?")
            .setContentText("The agent asked for it. Nothing opens until you tap.")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .addAction(0, "Open $label", tap)
            .build()
        context.getSystemService(NotificationManager::class.java).notify(ASK_ID, notification)
    }
}
