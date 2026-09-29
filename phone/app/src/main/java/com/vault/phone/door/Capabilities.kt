package com.vault.phone.door

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import androidx.core.content.ContextCompat
import java.net.Inet4Address
import java.net.NetworkInterface
import org.json.JSONObject

/**
 * What the phone is willing to answer.
 *
 * Only the passport's tiers exist here. Nothing in the "never" list has an
 * implementation at all — not a disabled one, not a guarded one: message
 * bodies, banking screens and contacts simply have no code path out.
 */
class Capabilities(private val context: Context) {

    private var ringer: MediaPlayer? = null
    private var volumeBefore: Int? = null

    private fun granted(permission: String) =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    // -- read freely ---------------------------------------------------------

    fun battery(): JSONObject {
        val status = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = status?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = status?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val plugged = (status?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
        return JSONObject()
            .put("percent", if (level < 0) -1 else level * 100 / scale)
            .put("charging", plugged)
    }

    /**
     * Whether the phone is on wifi and, when Android allows it, which network.
     *
     * Reading the SSID needs the location permission on Android 10 and later,
     * even though no fix is taken. Without it we say so rather than guessing.
     */
    fun network(): JSONObject {
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val out = JSONObject().put("wifi_enabled", wifi.isWifiEnabled)
        if (!granted(Manifest.permission.ACCESS_FINE_LOCATION)) {
            return out.put("ssid", JSONObject.NULL)
                .put("note", "the network name needs the location permission, which is not granted")
        }
        @Suppress("DEPRECATION")
        val info = wifi.connectionInfo
        @Suppress("DEPRECATION")
        val ssid = normalizeSsid(info?.ssid, wifi.isWifiEnabled)
        return out
            .put("ssid", ssid ?: JSONObject.NULL)
            .put("connected", ssid != null)
            .put("signal_dbm", @Suppress("DEPRECATION") (info?.rssi ?: 0))
    }

    // -- only on the owner's direct question ---------------------------------

    /**
     * The owner's rule, written into the passport on 2026-09-17: coordinates
     * only when he asks in plain words, never to enrich another answer. The
     * phone cannot know what was said on the PC, so it asks him here, and the
     * receipt records that location was requested — never where he was.
     */
    fun location(): JSONObject {
        if (!granted(Manifest.permission.ACCESS_FINE_LOCATION)) {
            return JSONObject().put("error", "the location permission is not granted on the phone")
        }
        val allowed = Approval.ask(
            context,
            "Where is this phone?",
            "The agent is asking for this phone's location. Allow once?",
        )
        if (!allowed) return JSONObject().put("error", "refused on the phone")

        val manager = context.getSystemService(LocationManager::class.java)
        val fix: Location? = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .asSequence()
            .mapNotNull { runCatching { manager.getLastKnownLocation(it) }.getOrNull() }
            .maxByOrNull { it.time }
        return if (fix == null) JSONObject().put("error", "no recent fix on the phone")
        else JSONObject()
            .put("latitude", fix.latitude)
            .put("longitude", fix.longitude)
            .put("accuracy_m", fix.accuracy)
            .put("age_seconds", (System.currentTimeMillis() - fix.time) / 1000)
    }

    // -- act (logged, reversible) --------------------------------------------

    fun ring(): JSONObject {
        stopRinging()
        val audio = context.getSystemService(AudioManager::class.java)
        // Finding a phone is worth being loud for; keeping it loud afterwards
        // is not. Remember what it was and put it back when the ringing stops.
        volumeBefore = audio.getStreamVolume(AudioManager.STREAM_ALARM)
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        ringer = MediaPlayer().apply {
            setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build()
            )
            setDataSource(context, uri)
            isLooping = true
            prepare()
            start()
        }
        audio.setStreamVolume(
            AudioManager.STREAM_ALARM,
            audio.getStreamMaxVolume(AudioManager.STREAM_ALARM),
            0,
        )
        return JSONObject().put("ringing", true)
    }

    fun stopRinging(): JSONObject {
        ringer?.runCatching { stop(); release() }
        ringer = null
        volumeBefore?.let {
            context.getSystemService(AudioManager::class.java)
                .setStreamVolume(AudioManager.STREAM_ALARM, it, 0)
            volumeBefore = null
        }
        return JSONObject().put("ringing", false)
    }

    /**
     * Open an app — outright, if the owner has allowed it; by asking, if not.
     *
     * Android stops a background service from starting someone else's app,
     * with one exemption the owner can grant: "display over other apps". With
     * it, this launches directly, which is what the passport's act tier meant
     * by "launch an app — logged, reversible". Without it, the best the phone
     * can do is a notification whose button is the app's own launcher, and it
     * says so rather than pretending.
     */
    fun launch(packageName: String): JSONObject {
        val intent = context.packageManager.getLaunchIntentForPackage(packageName)
            ?: return JSONObject().put("error", "no such app on this phone: $packageName")
        val label = runCatching {
            context.packageManager.getApplicationLabel(
                context.packageManager.getApplicationInfo(packageName, 0)
            ).toString()
        }.getOrDefault(packageName)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

        if (canOpenDirectly()) {
            val started = runCatching { context.startActivity(intent); true }.getOrDefault(false)
            if (started) return JSONObject().put("opened", true).put("app", label)
            // fall through: the exemption is there but the launch still failed
        }

        val tap = PendingIntent.getActivity(
            context, packageName.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        DoorNotifications.askToOpen(context, label, tap)
        return JSONObject().put("asked", true).put("app", label)
            .put("note", "a notification is waiting on the phone; the app opens when it is tapped. " +
                "To open apps outright, allow the vault to display over other apps in Settings.")
    }

    /** The one permission that lets an app open another from the background. */
    fun canOpenDirectly(): Boolean = android.provider.Settings.canDrawOverlays(context)

    companion object {
        /**
         * Is this one of Tailscale's addresses? The tailnet lives in the
         * carrier-grade NAT range 100.64.0.0/10, which is 100.64 through
         * 100.127 — not every address that merely begins with "100.".
         */
        fun isTailscaleAddress(address: String): Boolean {
            val parts = address.split(".")
            if (parts.size != 4 || parts.any { it.toIntOrNull() == null }) return false
            return parts[0].toInt() == 100 && parts[1].toInt() in 64..127
        }

        /**
         * This phone's address inside the tailnet, or null when Tailscale is
         * down. The door binds here and nowhere else: on a cafe wifi there is
         * simply no socket to find.
         */
        fun tailscaleAddress(): String? = runCatching {
            NetworkInterface.getNetworkInterfaces().toList()
                .asSequence()
                .flatMap { it.inetAddresses.toList().asSequence() }
                .filterIsInstance<Inet4Address>()
                .map { it.hostAddress.orEmpty() }
                .firstOrNull { isTailscaleAddress(it) }
        }.getOrNull()

        /**
         * Android hands back "<unknown ssid>" or "0x" when there is nothing to
         * report. Passing that on as a network name is a small lie that would
         * answer "is he home?" wrongly, so it becomes null.
         */
        fun normalizeSsid(raw: String?, wifiEnabled: Boolean): String? {
            if (!wifiEnabled) return null
            val name = raw?.trim()?.trim('"').orEmpty()
            return name.takeUnless {
                it.isBlank() || it.equals("<unknown ssid>", true) || it == "0x"
            }
        }
    }
}
