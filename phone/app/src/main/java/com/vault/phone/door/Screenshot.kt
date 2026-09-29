package com.vault.phone.door

import android.app.Activity
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.app.NotificationCompat
import java.io.ByteArrayOutputStream

/** Whole-screen, owner-only capture. The PNG exists only for its matching HTTP request. */
object Screenshot {
    const val EXTRA_REQUEST_ID = "request_id"
    private const val CHANNEL = "door-screenshot"
    private const val NOTIFICATION_ID = 4713
    private val requests = ScreenRequests()

    fun ensureChannel(context: Context) {
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(
            android.app.NotificationChannel(
                CHANNEL, "Picture of the screen", NotificationManager.IMPORTANCE_HIGH
            ).apply { description = "Shown when the agent asks for a picture of this screen" }
        )
    }

    /** Called on a server thread. No monitor is held while waiting for the owner. */
    fun ask(context: Context): ScreenRequests.Result {
        val request = requests.begin()
        if (!request.accepted) return requests.await(request)
        if (!Approval.canAsk(context)) {
            requests.deny(request.id)
            return requests.await(request).copy(error = Approval.CANNOT_ASK)
        }
        val manager = context.getSystemService(NotificationManager::class.java)
        return try {
            manager.notify(request.id, NOTIFICATION_ID, notification(context, request.id))
            requests.await(request)
        } catch (_: Exception) {
            requests.deny(request.id)
            requests.await(request).copy(error = "screen capture could not start")
        } finally {
            requests.deny(request.id)
            manager.cancel(request.id, NOTIFICATION_ID)
        }
    }

    fun consent(id: String) = requests.consent(id)
    fun awaitingCapture(id: String) = requests.awaitingCapture(id)
    fun capture(id: String, release: () -> Unit) = requests.capture(id, release)
    fun deliver(id: String, png: ByteArray?) = requests.deliver(id, png)
    fun deny(id: String) = requests.deny(id)
    fun cancelAll() = requests.cancelAll()

    private fun consentIntent(context: Context, id: String): PendingIntent =
        PendingIntent.getActivity(
            context, 0,
            Intent(context, ScreenshotActivity::class.java)
                // Extras alone do not distinguish PendingIntents. This identity is never reused.
                .setAction("com.vault.phone.door.SCREENSHOT.$id")
                .putExtra(EXTRA_REQUEST_ID, id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE,
        )

    private fun notification(context: Context, id: String) =
        NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentTitle("Take a picture of this screen?")
            .setContentText("The agent is asking. Look at what is on the screen first.")
            .setStyle(NotificationCompat.BigTextStyle().bigText(
                "The agent is asking for a picture of this screen. Android can only " +
                    "capture the whole screen on this phone, so whatever is showing will " +
                    "be in it. Look before you allow, and Android will ask again."
            ))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_SECRET)
            .setAutoCancel(true)
            .setContentIntent(consentIntent(context, id))
            .addAction(0, "Take one", consentIntent(context, id))
            .build()
}

/** Only this request's notification may launch Android's own capture consent. */
class ScreenshotActivity : ComponentActivity() {
    private var requestId: String? = null

    private val consent = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val id = requestId
        if (id != null && Screenshot.awaitingCapture(id)) {
            if (result.resultCode == Activity.RESULT_OK && result.data != null) {
                try {
                    startForegroundService(
                        Intent(this, ScreenshotService::class.java)
                            .putExtra(Screenshot.EXTRA_REQUEST_ID, id)
                            .putExtra(ScreenshotService.EXTRA_CODE, result.resultCode)
                            .putExtra(ScreenshotService.EXTRA_DATA, result.data)
                    )
                } catch (_: Exception) {
                    Screenshot.deny(id)
                }
            } else {
                Screenshot.deny(id)
            }
        }
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val id = savedInstanceState?.getString(Screenshot.EXTRA_REQUEST_ID)
            ?: intent.getStringExtra(Screenshot.EXTRA_REQUEST_ID)
        requestId = id
        if (id == null || if (savedInstanceState == null) !Screenshot.consent(id)
            else !Screenshot.awaitingCapture(id)) {
            finish()
            return
        }
        if (savedInstanceState == null) {
            try {
                consent.launch(getSystemService(MediaProjectionManager::class.java).createScreenCaptureIntent())
            } catch (_: Exception) {
                Screenshot.deny(id)
                finish()
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString(Screenshot.EXTRA_REQUEST_ID, requestId)
        super.onSaveInstanceState(outState)
    }
}

/** Each callback owns one capture; stale cleanup cannot release a newer projection. */
class ScreenshotService : android.app.Service() {
    companion object {
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        private const val ONGOING_ID = 4714
    }

    private class Capture(val id: String, val startId: Int) {
        var projection: MediaProjection? = null
        var reader: ImageReader? = null
        var display: VirtualDisplay? = null
        var timeout: Runnable? = null
        var finished = false
    }

    private val handler = Handler(Looper.getMainLooper())
    private var current: Capture? = null

    override fun onBind(intent: Intent?) = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val id = intent?.getStringExtra(Screenshot.EXTRA_REQUEST_ID)
        if (id == null || !Screenshot.awaitingCapture(id)) {
            if (current == null) stopSelf(startId)
            return START_NOT_STICKY
        }
        val session = Capture(id, startId)
        if (!Screenshot.capture(id) { handler.post { cleanup(session) } }) {
            if (current == null) stopSelf(startId)
            return START_NOT_STICKY
        }
        current?.let { finish(it, null) }
        current = session
        try {
            DoorNotifications.ensureChannels(this)
            startForegroundCompat()
            val code = intent.getIntExtra(EXTRA_CODE, Activity.RESULT_CANCELED)
            val data: Intent? = if (Build.VERSION.SDK_INT >= 33)
                intent.getParcelableExtra(EXTRA_DATA, Intent::class.java)
            else @Suppress("DEPRECATION") intent.getParcelableExtra(EXTRA_DATA)
            if (code != Activity.RESULT_OK || data == null) finish(session, null)
            else captureFrame(session, code, data)
        } catch (_: Exception) {
            finish(session, null)
        }
        return START_NOT_STICKY
    }

    private fun startForegroundCompat() {
        val note = NotificationCompat.Builder(this, DoorNotifications.CHANNEL_DOOR)
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentTitle("Taking one picture of the screen")
            .setVisibility(NotificationCompat.VISIBILITY_SECRET)
            .build()
        startForeground(
            ONGOING_ID, note,
            android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION,
        )
    }

    private fun captureFrame(session: Capture, code: Int, data: Intent) {
        val metrics = DisplayMetrics().also {
            @Suppress("DEPRECATION")
            getSystemService(android.view.WindowManager::class.java).defaultDisplay.getRealMetrics(it)
        }
        val media = getSystemService(MediaProjectionManager::class.java).getMediaProjection(code, data)
        session.projection = media
        if (media == null) {
            finish(session, null)
            return
        }
        media.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { finish(session, null) }
        }, handler)
        val reader = ImageReader.newInstance(
            metrics.widthPixels, metrics.heightPixels, PixelFormat.RGBA_8888, 2
        )
        session.reader = reader
        reader.setOnImageAvailableListener({ source ->
            if (session.finished || current !== session) return@setOnImageAvailableListener
            val image = runCatching { source.acquireLatestImage() }.getOrNull()
                ?: return@setOnImageAvailableListener
            val png = try {
                toPng(image, metrics.widthPixels, metrics.heightPixels)
            } catch (_: Exception) {
                null
            } finally {
                image.close()
            }
            finish(session, png)
        }, handler)
        session.display = media.createVirtualDisplay(
            "vault-door-${session.id}", metrics.widthPixels, metrics.heightPixels, metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.surface, null, handler,
        )
        session.timeout = Runnable { finish(session, null) }.also { handler.postDelayed(it, 8_000) }
    }

    private fun toPng(image: android.media.Image, width: Int, height: Int): ByteArray {
        val plane = image.planes[0]
        val padding = plane.rowStride - plane.pixelStride * width
        val bitmap = Bitmap.createBitmap(
            width + padding / plane.pixelStride, height, Bitmap.Config.ARGB_8888
        )
        var cropped: Bitmap? = null
        return try {
            bitmap.copyPixelsFromBuffer(plane.buffer)
            val visible = Bitmap.createBitmap(bitmap, 0, 0, width, height)
            cropped = visible
            ByteArrayOutputStream().use {
                visible.compress(Bitmap.CompressFormat.PNG, 100, it)
                it.toByteArray()
            }
        } finally {
            if (cropped !== bitmap) cropped?.recycle()
            bitmap.recycle()
        }
    }

    private fun finish(session: Capture, png: ByteArray?) {
        if (session.finished) return
        if (current === session) Screenshot.deliver(session.id, png)
        cleanup(session)
    }

    private fun cleanup(session: Capture) {
        if (session.finished) return
        session.finished = true
        session.timeout?.let { handler.removeCallbacks(it) }
        runCatching { session.reader?.setOnImageAvailableListener(null, null) }
        runCatching { session.display?.release() }
        runCatching { session.reader?.close() }
        runCatching { session.projection?.stop() }
        session.display = null
        session.reader = null
        session.projection = null
        if (current === session) {
            current = null
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf(session.startId)
        }
    }

    override fun onDestroy() {
        current?.let {
            Screenshot.deny(it.id)
            cleanup(it)
        }
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }
}
