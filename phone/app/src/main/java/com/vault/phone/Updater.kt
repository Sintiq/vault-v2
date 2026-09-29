package com.vault.phone

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.widget.Toast
import java.io.File

/**
 * Hands a verified APK to Android's own installer. Android shows its confirmation and
 * installs over the current app only if the signature is the same; nothing is silent.
 * A session that could not be committed is abandoned, never left behind.
 */
object Updater {
    fun install(context: Context, apk: File, m: ReleaseManifest) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(context.packageName)
        val id = installer.createSession(params)
        var committed = false
        try {
            installer.openSession(id).use { session ->
                session.openWrite("vault.apk", 0, m.size).use { out ->
                    apk.inputStream().use { ReleaseRules.copyVerified(it, out, m) }
                    session.fsync(out)
                }
                val status = PendingIntent.getBroadcast(
                    context, id, Intent(context, InstallReceiver::class.java),
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE,
                )
                session.commit(status.intentSender)
                committed = true
            }
        } finally {
            if (!committed) runCatching { installer.abandonSession(id) }
        }
    }
}

/**
 * Android's answer. When it needs the owner's confirmation, show its dialog; when it
 * refused or the owner cancelled, say so — never report success before Android does.
 */
class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm: Intent? = if (Build.VERSION.SDK_INT >= 33)
                    intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_INTENT)
                confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let(context::startActivity)
            }
            PackageInstaller.STATUS_SUCCESS -> Unit      // the new version starts by itself
            else -> {
                val why = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE).orEmpty()
                val text = when (status) {
                    PackageInstaller.STATUS_FAILURE_ABORTED -> "Update cancelled."
                    PackageInstaller.STATUS_FAILURE_STORAGE -> "Update not installed: not enough space."
                    PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE ->
                        "Update not installed: it does not match this app (signature or device)."
                    else -> "Update not installed" + if (why.isNotBlank()) ": $why" else "."
                }
                Toast.makeText(context, text, Toast.LENGTH_LONG).show()
            }
        }
    }
}
