package com.vault.phone.door

import android.content.Context
import java.io.File
import java.security.MessageDigest
import java.security.SecureRandom
import org.json.JSONObject

/**
 * The phone's own receipt log.
 *
 * Hash-chained like the vault's: each line carries the hash of the one
 * before, so a deleted or edited middle record breaks verification. The PC
 * keeps its own log of what the agent asked for; two independent logs that
 * can be compared are worth more than one that can be quietly rewritten.
 *
 * The passport is explicit about one thing: a location call is recorded as
 * having happened, never with the coordinates. Nothing here ever stores a
 * latitude.
 *
 * Rotation keeps immutable, chronologically numbered segments: .1 is oldest,
 * then .2, and so on; no retention deletion. The byte threshold is checked by
 * trim() and before the next append, so the current file can exceed it by one
 * record. Reading, lastHash and full verification are O(total history).
 * Without an independently retained tip, loss of the newest suffix (or the
 * complete history) cannot be proved by an ordinary hash chain alone.
 */
class DoorReceipts(private val file: File, private val maxBytes: Long = 1024 * 1024) {

    constructor(context: Context) : this(File(context.filesDir, "door-receipts.jsonl"))

    private val lock = locks.computeIfAbsent(file.canonicalPath) { Any() }

    init { require(maxBytes > 0) { "receipt segment size must be positive" } }

    companion object {
        val GENESIS = "0".repeat(64)
        private val locks = java.util.concurrent.ConcurrentHashMap<String, Any>()

        fun sha256(text: String): String =
            MessageDigest.getInstance("SHA-256").digest(text.toByteArray())
                .joinToString("") { "%02x".format(it) }
    }

    fun append(tool: String, tier: String, outcome: String, detail: String = "") {
        synchronized(lock) {
            trim()
            val body = JSONObject()
                .put("ts", System.currentTimeMillis())
                .put("tool", tool)
                .put("tier", tier)
                .put("outcome", outcome)
                .put("detail", detail)
                .put("prev", lastHash())
            val line = JSONObject(body.toString()).put("hash", sha256(body.toString()))
            file.appendText(line.toString() + "\n")
        }
    }

    private fun lastHash(): String =
        readLines().lastOrNull()?.optString("hash").takeUnless { it.isNullOrBlank() } ?: GENESIS

    private fun segments(): List<Pair<Long, File>> {
        val name = Regex.escape(file.name.removeSuffix(".jsonl"))
        val pattern = Regex("^$name\\.(.*)\\.jsonl$")
        val directory = requireNotNull(file.absoluteFile.parentFile)
        val children = directory.listFiles()
        check(children != null || !directory.exists()) { "unable to read receipt directory" }
        val segments = children.orEmpty().mapNotNull { candidate ->
            val match = pattern.matchEntire(candidate.name) ?: return@mapNotNull null
            val text = match.groupValues[1]
            val index = text.toLongOrNull()
            check(index != null && index > 0 && index.toString() == text) { "invalid receipt segment index" }
            check(candidate.isFile) { "unreadable receipt segment" }
            index to candidate
        }.sortedBy { it.first }
        segments.forEachIndexed { index, segment ->
            check(segment.first == index.toLong() + 1) { "missing receipt segment" }
        }
        return segments
    }

    fun readLines(): List<JSONObject> = synchronized(lock) {
        val archives = try {
            segments()
        } catch (_: Exception) {
            return@synchronized listOf(brokenRow("Receipt segment layout is unreadable; diagnostic only, timestamp unavailable"))
        }
        val files = archives.map { it.second } + listOfNotNull(file.takeIf { it.exists() })
        files.flatMap { segment ->
            try {
                segment.readLines().map { line ->
                    try {
                        val parser = org.json.JSONTokener(line)
                        val record = parser.nextValue()
                        if (record !is JSONObject || parser.nextClean() != '\u0000') {
                            throw org.json.JSONException("receipt line must contain exactly one object")
                        }
                        record
                    } catch (_: org.json.JSONException) {
                        brokenRow("Unreadable receipt line; diagnostic only, timestamp unavailable")
                    }
                }
            } catch (_: Exception) {
                listOf(brokenRow("Unreadable receipt segment; diagnostic only, timestamp unavailable"))
            }
        }
    }

    private fun brokenRow(detail: String): JSONObject = JSONObject()
        .put("receipt_error", true)
        .put("tool", "receipt log integrity error")
        .put("tier", "diagnostic")
        .put("outcome", "BROKEN")
        .put("detail", detail)

    fun tail(n: Int = 100): List<JSONObject> = readLines().takeLast(n).reversed()

    /** Number of intact records, or -1 at the first break. */
    fun verify(): Int {
        var prev = GENESIS
        var count = 0
        for (record in readLines()) {
            if (record.optBoolean("receipt_error")) return -1
            val stored = record.optString("hash")
            val body = JSONObject(record.toString()).apply { remove("hash") }
            if (record.optString("prev") != prev) return -1
            if (sha256(body.toString()) != stored) return -1
            prev = stored
            count++
        }
        return count
    }

    fun trim() {
        synchronized(lock) {
            if (!file.exists() || file.length() <= maxBytes) return
            // Leave a damaged layout in place, visibly broken, for manual review.
            val archives = try { segments() } catch (_: Exception) { return }
            val index = (archives.lastOrNull()?.first ?: 0L) + 1
            val archive = File(file.absoluteFile.parentFile,
                "${file.name.removeSuffix(".jsonl")}.$index.jsonl")
            check(!archive.exists() && file.renameTo(archive)) { "unable to rotate receipt log" }
        }
    }
}

/** The key the PC must present. Generated here, never leaves except to the owner's eyes. */
class DoorKey(context: Context) {

    private val prefs = context.getSharedPreferences("door", Context.MODE_PRIVATE)

    val value: String
        get() = com.vault.phone.SecretBox.read(prefs, "key") ?: rotate()

    fun rotate(): String = rotate { com.vault.phone.SecretBox.write(prefs.edit(), "key", it).apply() }

    companion object {
        /** The same revocation precedes Android persistence and JVM storage. */
        fun rotate(persist: (String) -> Unit): String {
            val bytes = ByteArray(18).also { SecureRandom().nextBytes(it) }
            val key = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
            try {
                InputSession.revoke { persist(key) }
            } finally {
                Screenshot.cancelAll()
            }
            return key
        }
    }

    /** The door starts closed. Nothing answers until the owner opens it. */
    var isOpen: Boolean
        get() = prefs.getBoolean("open", false)
        set(value) = prefs.edit().putBoolean("open", value).apply()
}
