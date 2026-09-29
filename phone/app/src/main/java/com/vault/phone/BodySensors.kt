package com.vault.phone

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import androidx.core.content.ContextCompat

/**
 * What the phone itself knows about the body carrying it.
 *
 * A watch is not here yet, and Health Connect — which is how a watch would
 * feed this on Android 13 — is a separate app that is not installed. Rather
 * than a tab that says "soon", this reads the step counter the phone already
 * has, so the tab shows something true today and has somewhere for a watch to
 * plug in later.
 *
 * The step counter counts since the phone last booted. That is a strange
 * number to show as "steps today", so it is labelled for what it is.
 */
class BodySensors(private val context: Context) {

    data class Reading(
        val available: Boolean,
        val stepsSinceBoot: Long?,
        val note: String,
    )

    private val manager: SensorManager? =
        context.getSystemService(Context.SENSOR_SERVICE) as? SensorManager

    private fun granted(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACTIVITY_RECOGNITION) ==
            PackageManager.PERMISSION_GRANTED

    val hasStepCounter: Boolean
        get() = manager?.getDefaultSensor(Sensor.TYPE_STEP_COUNTER) != null

    val needsPermission: Boolean
        get() = hasStepCounter && !granted()

    /**
     * One reading, then stop listening. The step counter only reports when it
     * changes, so a phone lying still may take a moment — or never answer, if
     * it has not moved since boot.
     */
    fun readSteps(onResult: (Reading) -> Unit) {
        val sensor = manager?.getDefaultSensor(Sensor.TYPE_STEP_COUNTER)
        if (sensor == null) {
            onResult(Reading(false, null, "this phone has no step counter"))
            return
        }
        if (!granted()) {
            onResult(Reading(false, null, "physical activity permission not granted"))
            return
        }
        var delivered = false
        val listener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                if (delivered) return
                delivered = true
                manager.unregisterListener(this)
                onResult(Reading(true, event.values.firstOrNull()?.toLong() ?: 0L,
                                 "counted by the phone since it last started"))
            }

            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
        }
        manager.registerListener(listener, sensor, SensorManager.SENSOR_DELAY_UI)

        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            if (!delivered) {
                delivered = true
                manager.unregisterListener(listener)
                onResult(Reading(true, null,
                                 "the counter has not reported yet — it speaks when the phone moves"))
            }
        }, 4_000)
    }

    companion object {
        /** Health Connect is how a watch will feed this; on Android 13 it is a separate app. */
        const val HEALTH_CONNECT_PACKAGE = "com.google.android.apps.healthdata"

        fun healthConnectInstalled(context: Context): Boolean =
            runCatching {
                context.packageManager.getPackageInfo(HEALTH_CONNECT_PACKAGE, 0)
                true
            }.getOrDefault(false)
    }
}
