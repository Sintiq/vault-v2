package com.vault.phone.door

import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log

/**
 * Keeps the door answering while the phone is in a pocket.
 *
 * Android kills background listeners, and rightly so; a foreground service
 * with a standing notification is the honest way to hold a socket open. The
 * notification is not a nuisance to be hidden — it is the only outward sign
 * that the phone is reachable, and it should stay visible.
 */
class DoorService : Service() {

    companion object {
        const val PORT = 8779
        private const val TAG = "VaultDoor"

        fun start(context: android.content.Context) {
            val intent = Intent(context, DoorService::class.java)
            androidx.core.content.ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: android.content.Context) {
            stop { context.stopService(Intent(context, DoorService::class.java)) }
        }

        /** Revoke synchronously; Android may destroy the service later. */
        fun stop(platformStop: () -> Unit) {
            InputSession.close()
            Screenshot.cancelAll()
            platformStop()
        }
    }

    private var server: DoorServer? = null
    private var receiver: Approval.Receiver? = null
    private val waiter = android.os.Handler(android.os.Looper.getMainLooper())
    private var attempt = 0

    override fun onCreate() {
        super.onCreate()
        DoorNotifications.ensureChannels(this)
        receiver = Approval.register(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground(Capabilities.tailscaleAddress())
        attempt = 0
        tryToOpen()
        return START_STICKY
    }

    /**
     * At boot this usually runs before Tailscale has an address. That is not a
     * failure, it is a few seconds early, so it waits and tries again — and
     * then gives up rather than spinning all day.
     */
    private fun tryToOpen() {
        if (server != null) return
        val key = DoorKey(this)
        if (!key.isOpen) return
        val receipts = DoorReceipts(this).also { it.trim() }
        val address = Capabilities.tailscaleAddress()

        if (address == null) {
            // Fail closed: with no tailnet address there is nothing safe to bind to.
            if (StartupPolicy.shouldKeepTrying(attempt)) {
                waiter.postDelayed({ attempt++; tryToOpen() }, StartupPolicy.delayMs(attempt))
                return
            }
            receipts.append("door", "-", "not started",
                            "Tailscale did not come up within ${StartupPolicy.totalWaitSeconds()}s")
            startInForeground(null)
            return
        }

        server = try {
            DoorServer(this, address, PORT, key, receipts).also {
                it.start(NanoTimeouts.SOCKET_READ_MS, false)
                receipts.append("door", "-", "opened", "$address:$PORT")
                startInForeground(address)
            }
        } catch (e: Exception) {
            Log.w(TAG, "door did not open", e)
            receipts.append("door", "-", "failed to open", e.message.orEmpty())
            null
        }
    }

    private fun startInForeground(address: String?) {
        val notification = DoorNotifications.ongoing(this, address?.let { "$it:$PORT" })
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                DoorNotifications.ONGOING_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            startForeground(DoorNotifications.ONGOING_ID, notification)
        }
    }

    override fun onDestroy() {
        InputSession.close()
        Screenshot.cancelAll()
        waiter.removeCallbacksAndMessages(null)
        server?.stop()
        server = null
        receiver?.let { runCatching { unregisterReceiver(it) } }
        receiver = null
        DoorReceipts(this).append("door", "-", "closed")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}

private object NanoTimeouts {
    /** A held-open socket should not pin a thread forever. */
    const val SOCKET_READ_MS = 120_000
}
