package com.vault.phone

import android.content.Context
import com.vault.phone.door.DoorService
import java.io.File

/**
 * After the 9th wrong PIN: erase Vault on this phone, and nothing else.
 *
 * The state "wiping" is written first, so an interrupted wipe resumes on the next
 * start. The PC is told first (best effort, a few seconds) so it can revoke this
 * phone's key; if it does not answer, the Devices window on the PC shows the phone as
 * not seen since and the owner revokes it there. Originals on the PC, files already
 * saved to Files or shared, Tailscale and other apps are not touched.
 */
object Wipe {
    private const val MARK = "wipe"

    fun pending(context: Context): Boolean =
        context.getSharedPreferences(MARK, Context.MODE_PRIVATE).getBoolean("wiping", false)

    /**
     * Blocking: call off the main thread. True only when everything Vault keeps on this
     * phone is gone; false leaves the mark in place so the next start tries again.
     * Nothing irreversible starts before the mark is written.
     */
    fun run(context: Context, tellPc: (() -> Unit)?): Boolean {
        val mark = context.getSharedPreferences(MARK, Context.MODE_PRIVATE)
        if (!mark.edit().putBoolean("wiping", true).commit()) return false
        // The call itself has a 3 s limit (VaultClient.selfWipe); a failure changes nothing here.
        if (tellPc != null) runCatching { tellPc() }
        runCatching { DoorService.stop(context) }
        var ok = true
        val prefsDir = File(context.applicationInfo.dataDir, "shared_prefs")
        for (name in listOf("vault", "pin", "door")) {
            context.getSharedPreferences(name, Context.MODE_PRIVATE).edit().clear().commit()
            context.deleteSharedPreferences(name)
            if (File(prefsDir, "$name.xml").exists()) ok = false
        }
        for (dir in listOf(File(context.filesDir, "offline"), File(context.filesDir, "export"))) {
            dir.deleteRecursively()
            if (dir.exists()) ok = false
        }
        val cache = context.cacheDir
        val before = cache.listFiles()
        if (before == null && cache.exists()) ok = false          // could not look: not «empty»
        before?.forEach { it.deleteRecursively() }
        val after = cache.listFiles()
        if ((after == null && cache.exists()) || !after.isNullOrEmpty()) ok = false
        if (!SecretBox.deleteKey()) ok = false
        if (ok) ok = mark.edit().clear().commit()
        return ok
    }
}
