package com.vault.phone

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import java.io.File
import java.security.MessageDigest

/** One file kept on this phone, inside Vault, readable without the PC under the PIN. */
@Serializable
data class OfflineCopy(val sha256: String, val name: String, val rel: String, val size: Long, val savedAt: Long)

/**
 * «Available offline»: exact bytes in app-private storage (not cache, which Android may
 * empty), listed under «On this phone». Written to a part file and renamed only after
 * the hash matches, so a broken download never shows up as ready. The wipe erases it all.
 */
class OfflineStore(context: Context) {
    private val dir = File(context.filesDir, "offline").apply { mkdirs() }
    private val index = File(dir, "index.json")
    private val json = Json { ignoreUnknownKeys = true }

    fun list(): List<OfflineCopy> = runCatching {
        json.decodeFromString(ListSerializer(OfflineCopy.serializer()), index.readText())
    }.getOrDefault(emptyList()).filter { File(dir, it.sha256).isFile }

    fun file(copy: OfflineCopy): File = File(dir, copy.sha256)

    fun has(sha256: String): Boolean = list().any { it.sha256 == sha256 }

    /** Keep [bytes] only if they are exactly [sha256]; otherwise nothing is kept. */
    fun save(sha256: String, name: String, rel: String, bytes: ByteArray): OfflineCopy = synchronized(LOCK) {
        require(Sha.hex(bytes) == sha256) { "the copy does not match the PC's file" }
        val part = File(dir, "$sha256.part")
        part.writeBytes(bytes)
        val final = File(dir, sha256)
        if (!part.renameTo(final)) {
            part.delete()
            error("could not keep the copy")
        }
        val copy = OfflineCopy(sha256, name, rel, bytes.size.toLong(), System.currentTimeMillis())
        write(list().filter { it.sha256 != sha256 } + copy)
        copy
    }

    /** Throws if the file or the index could not be changed; the copy is then still listed. */
    fun remove(copy: OfflineCopy) = synchronized(LOCK) {
        val f = File(dir, copy.sha256)
        if (f.exists() && !f.delete()) error("the file could not be deleted")
        write(list().filter { it.sha256 != copy.sha256 })
    }

    private fun write(copies: List<OfflineCopy>) {
        val tmp = File(dir, "index.json.tmp")
        tmp.writeText(json.encodeToString(ListSerializer(OfflineCopy.serializer()), copies))
        if (!tmp.renameTo(index)) {
            tmp.delete()
            error("the list of kept copies could not be written")
        }
    }

    private companion object {
        val LOCK = Any()
    }
}

object Sha {
    fun hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
}

/**
 * A copy about to leave Vault through «Save a copy to Files…» or «Share…». Lives in
 * app-private `files/export/` so the FileProvider can hand one read-only URI to the app
 * the owner picks. It is not deleted the moment the other app opens — a mail app may
 * read it later — but files older than 30 minutes are cleared on the next start.
 */
object ExportFiles {
    private const val KEEP_MS = 30 * 60 * 1000L

    fun dir(context: Context): File = File(context.filesDir, "export").apply { mkdirs() }

    fun write(context: Context, grantId: String, name: String, bytes: ByteArray): File {
        val safe = name.replace(Regex("[\\\\/:*?\"<>|]"), "_").take(120).ifBlank { "document" }
        val folder = File(dir(context), grantId).apply { mkdirs() }
        return File(folder, safe).apply { writeBytes(bytes) }
    }

    fun sweep(context: Context, now: Long = System.currentTimeMillis()) {
        dir(context).listFiles()?.forEach { folder ->
            if (now - folder.lastModified() > KEEP_MS) folder.deleteRecursively()
        }
    }
}
