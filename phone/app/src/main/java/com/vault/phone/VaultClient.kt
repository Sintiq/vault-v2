package com.vault.phone

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * The vault, as seen from the phone.
 *
 * The address is a Tailscale one, so this only ever talks to the owner's own
 * machine. Nothing is cached to disk here: what the phone shows is what the
 * PC said a moment ago, and the PC keeps the receipts.
 */
class VaultClient(private var base: String, private var key: String) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    // The agent's jobs read whole documents with a local model that partly runs
    // on the CPU: one long health record took 126 s on the owner's PC. Waiting
    // less would call a slow answer "no connection".
    private val agentHttp = http.newBuilder().readTimeout(AGENT_READ_TIMEOUT_S, TimeUnit.SECONDS).build()

    private val wipeHttp = http.newBuilder().callTimeout(3, TimeUnit.SECONDS).build()

    // The dot must not stay green on a hung network: the session question gives up fast.
    private val sessionHttp = http.newBuilder().callTimeout(8, TimeUnit.SECONDS).build()

    private val json = Json { ignoreUnknownKeys = true }

    fun configure(base: String, key: String) {
        this.base = base.trimEnd('/')
        this.key = key
    }

    /** Something the vault said, or the way to it. The subclass tells the screens which. */
    open class VaultException(message: String) : Exception(message)
    /** No answer, or a refused key: the phone is not connected to its vault. */
    class NotConnected(message: String) : VaultException(message)
    /** The proposals on screen are no longer the ones on the PC. Refresh, then choose again. */
    class ProposalsChanged(message: String) : VaultException(message)
    /** The PC is mid-write (a backup, another request). Try again in a moment. */
    class VaultBusy(message: String) : VaultException(message)

    companion object {
        const val AGENT_READ_TIMEOUT_S = 300L
        const val STORE_FAILED = "the update could not be stored on this phone"

        /** Which exception an HTTP status deserves. Pure, so it is tested. */
        fun failure(code: Int, detail: String?): VaultException = when (code) {
            401 -> NotConnected("the vault refused this key")
            409 -> ProposalsChanged(detail ?: "proposals changed — refresh the list")
            503 -> VaultBusy(detail ?: "the vault is busy — try again")
            else -> VaultException(detail ?: "vault error $code")
        }
    }

    private fun url(path: String, params: Map<String, String> = emptyMap()) =
        (base.trimEnd('/') + path).toHttpUrl().newBuilder()
            .apply { params.forEach { (k, v) -> addQueryParameter(k, v) } }
            .build()

    private fun call(request: Request): String {
        val client = when {
            request.url.encodedPath.startsWith("/api/agent/") -> agentHttp
            request.url.encodedPath == "/api/session" -> sessionHttp
            else -> http
        }
        val response = try {
            client.newCall(request).execute()
        } catch (e: Exception) {
            throw NotConnected(e.message ?: "cannot reach the vault")
        }
        response.use {
            val body = readOrBroke { it.body?.string().orEmpty() }
            if (it.code == 401) throw NotConnected("the vault refused this key")
            if (!it.isSuccessful) {
                val detail = runCatching { json.decodeFromString<ApiError>(body).error }.getOrNull()
                throw failure(it.code, detail)
            }
            return body
        }
    }

    /** An answer that began and then broke off is «not reached», like no answer at all. */
    private inline fun <T> readOrBroke(read: () -> T): T = try {
        read()
    } catch (e: java.io.IOException) {
        throw NotConnected("the answer broke off — try again")
    }

    private fun get(path: String, params: Map<String, String> = emptyMap()): String =
        call(Request.Builder().url(url(path, params)).header("Authorization", "Bearer $key").build())

    private fun post(path: String, body: String): String = call(
        Request.Builder().url(url(path))
            .header("Authorization", "Bearer $key")
            .post(body.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .build()
    )

    // -- reads ---------------------------------------------------------------

    /** Claim the ticket from the QR; the answer carries the six digits both screens show. */
    fun pairClaim(base: String, ticket: String, name: String): PairClaim =
        json.decodeFromString(unauthPost(base, "/api/pair/claim",
            json.encodeToString(PairClaimRequest.serializer(), PairClaimRequest(ticket, name, "app"))))

    fun pairStatus(base: String, claimId: String): PairStatus =
        json.decodeFromString(unauthPost(base, "/api/pair/status",
            json.encodeToString(PairStatusRequest.serializer(), PairStatusRequest(claimId))))

    private fun unauthPost(base: String, path: String, body: String): String = call(
        Request.Builder().url((base.trimEnd('/') + path).toHttpUrl())
            .post(body.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .build()
    )

    // Updates travel on their own clients: no redirects, one deadline for the whole call.
    private val releaseHttp = http.newBuilder().followRedirects(false).followSslRedirects(false)
        .callTimeout(20, TimeUnit.SECONDS).build()
    private val apkHttp = http.newBuilder().followRedirects(false).followSslRedirects(false)
        .callTimeout(10, TimeUnit.MINUTES).build()

    /**
     * The address updates may come from, parsed once and used for the whole call: the vault's
     * HTTPS address, or plain HTTP to 127.0.0.1 exactly, which only a test on this device uses.
     * Parsed, not compared as text, so a user-info prefix such as `127.0.0.1:80@` is not loopback.
     */
    private fun releaseOrigin(): HttpUrl {
        val u = base.toHttpUrlOrNull()
        val ok = u != null && u.username.isEmpty() && u.password.isEmpty() && u.query == null &&
            u.fragment == null && (u.scheme == "https" || (u.scheme == "http" && u.host == "127.0.0.1"))
        if (!ok) throw VaultException("updates come only over the vault's HTTPS address")
        return u!!
    }

    private fun releaseUrl(origin: HttpUrl, path: String): HttpUrl {
        val u = (origin.toString().trimEnd('/') + path).toHttpUrl()
        if (u.scheme != origin.scheme || u.host != origin.host || u.port != origin.port || u.username.isNotEmpty())
            throw VaultException("updates come only over the vault's HTTPS address")
        return u
    }

    /** The PC's offer of an update: the signed manifest and its signature, each read within its limit. */
    fun releaseManifest(): ByteArray = bounded("/app/vault-release.json", ReleaseRules.MANIFEST_MAX)
    fun releaseSignature(): String = bounded("/app/vault-release.json.sig", ReleaseRules.SIGNATURE_MAX).decodeToString()

    private fun bounded(path: String, limit: Int): ByteArray {
        val origin = releaseOrigin()
        val response = try {
            releaseHttp.newCall(Request.Builder().url(releaseUrl(origin, path)).build()).execute()
        } catch (e: Exception) {
            throw NotConnected(e.message ?: "cannot reach the vault")
        }
        response.use {
            if (it.code == 404) throw VaultException("the PC offers no update")
            if (!it.isSuccessful) throw VaultException("vault error ${it.code}")
            val body = it.body ?: throw VaultException("empty answer")
            val out = java.io.ByteArrayOutputStream()
            try {
                body.byteStream().use { ins ->
                    val buf = ByteArray(8192)
                    while (true) {
                        // Never more than one byte past the limit is read.
                        val r = ins.read(buf, 0, minOf(buf.size, limit + 1 - out.size()))
                        if (r < 0) break
                        out.write(buf, 0, r)
                        if (out.size() > limit) throw VaultException("the release description is too large")
                    }
                }
            } catch (e: java.io.IOException) {
                throw NotConnected("the answer broke off — try again")
            }
            return out.toByteArray()
        }
    }

    /**
     * The APK, streamed into a folder of its own under [parent] with the hash taken on the
     * way; returns the file. Each call has its own folder, so two calls never share a byte.
     * Kept only if it is exactly the size and hash the signed manifest names; otherwise the
     * folder is removed. The caller removes the folder of a returned file when done with it.
     */
    fun downloadRelease(m: ReleaseManifest, parent: java.io.File): java.io.File {
        val origin = releaseOrigin()
        val attempt = java.io.File(parent, "attempt-" + java.util.UUID.randomUUID())
        if (!attempt.mkdirs()) throw VaultException(STORE_FAILED)
        val part = java.io.File(attempt, "vault.apk.part")
        val dest = java.io.File(attempt, "vault.apk")
        var kept = false
        try {
            val response = try {
                apkHttp.newCall(Request.Builder().url(releaseUrl(origin, "/app/vault.apk")).build()).execute()
            } catch (e: Exception) {
                throw NotConnected(e.message ?: "cannot reach the vault")
            }
            response.use {
                if (!it.isSuccessful) throw VaultException("the PC did not hand over the update (${it.code})")
                val body = it.body ?: throw VaultException("empty answer")
                val digest = java.security.MessageDigest.getInstance("SHA-256")
                var total = 0L
                try {
                    body.byteStream().use { ins ->
                        part.outputStream().use { out ->
                            val buf = ByteArray(64 * 1024)
                            while (true) {
                                val r = try {
                                    ins.read(buf)
                                } catch (e: java.io.IOException) {
                                    throw NotConnected("the update download broke off — try again")
                                }
                                if (r < 0) break
                                total += r
                                if (total > m.size) throw VaultException("the update is larger than its description")
                                digest.update(buf, 0, r)
                                out.write(buf, 0, r)
                            }
                            out.fd.sync()
                        }
                    }
                } catch (e: java.io.IOException) {
                    throw VaultException(STORE_FAILED)      // the phone's storage, not the network
                }
                val sha = digest.digest().joinToString("") { b -> "%02x".format(b) }
                if (total != m.size || sha != m.sha256) throw VaultException("the downloaded file is not the signed release")
            }
            if (!part.renameTo(dest)) throw VaultException(STORE_FAILED)
            kept = true
            return dest
        } finally {
            part.delete()
            if (!kept) attempt.deleteRecursively()
        }
    }

    /** Who this phone is to the vault. The green dot rests on this answer, not on ping. */
    fun session(): Session = json.decodeFromString(get("/api/session"))

    /** After too many wrong PINs: tell the PC to shut this phone's key out too. At most 3 s. */
    fun selfWipe() {
        val request = Request.Builder().url(url("/api/device/self-wipe"))
            .header("Authorization", "Bearer $key")
            .post("{}".toRequestBody("application/json; charset=utf-8".toMediaType())).build()
        wipeHttp.newCall(request).execute().close()
    }

    fun ping(): Boolean = runCatching {
        json.decodeFromString<Ping>(call(Request.Builder().url(url("/api/ping")).build())).ok
    }.getOrDefault(false)

    fun files(pane: String, rel: String): Listing =
        json.decodeFromString(get("/api/files", mapOf("pane" to pane, "rel" to rel)))

    fun text(pane: String, rel: String): FileText =
        json.decodeFromString(get("/api/file", mapOf("pane" to pane, "rel" to rel)))

    /** Ask the PC for one Staging file for this phone; it checks device, place and hash now. */
    fun grant(rel: String, action: String, sha256: String? = null): PhoneGrant =
        json.decodeFromString(post("/api/phone/grant",
            json.encodeToString(GrantRequest.serializer(), GrantRequest(rel, action, sha256))))

    fun grantBytes(grantId: String): ByteArray = bytes("/api/phone/blob", mapOf("grant" to grantId))

    /** What the phone saw happen to a copy: saved, handed_to <app>, cancelled, failed. */
    fun outcome(grantId: String, outcome: String, target: String? = null) {
        post("/api/phone/outcome", json.encodeToString(OutcomeRequest.serializer(),
                                                        OutcomeRequest(grantId, outcome, target)))
    }

    /** The file itself — a scan or a photograph — for the viewer to show. */
    fun blob(pane: String, rel: String): ByteArray = bytes("/api/blob", mapOf("pane" to pane, "rel" to rel))

    /**
     * A PDF is drawn on the PC, form fields and all, and arrives as pictures:
     * Android's own PDF renderer shows a filled form as an empty template.
     */
    fun pdfInfo(pane: String, rel: String): PdfInfo =
        json.decodeFromString(get("/api/pdf/info", mapOf("pane" to pane, "rel" to rel)))

    fun pdfPage(pane: String, rel: String, page: Int, width: Int): ByteArray =
        bytes("/api/pdf/page", mapOf("pane" to pane, "rel" to rel,
                                      "page" to page.toString(), "width" to width.toString()))

    private fun bytes(path: String, params: Map<String, String>): ByteArray {
        val request = Request.Builder()
            .url(url(path, params))
            .header("Authorization", "Bearer $key")
            .build()
        val response = try {
            http.newCall(request).execute()
        } catch (e: Exception) {
            throw NotConnected(e.message ?: "cannot reach the vault")
        }
        response.use {
            if (it.code == 401) throw NotConnected("the vault refused this key")
            val body = readOrBroke { it.body?.bytes() ?: ByteArray(0) }
            if (!it.isSuccessful) {
                val detail = runCatching {
                    json.decodeFromString<ApiError>(body.decodeToString()).error
                }.getOrNull()
                throw failure(it.code, detail)
            }
            return body
        }
    }

    fun tasks(): TaskList = json.decodeFromString(get("/api/tasks"))

    fun health(): HealthTimeline = json.decodeFromString(get("/api/health"))

    fun chatState(): ChatState = json.decodeFromString(get("/api/chat"))

    fun monitor(): Monitor = json.decodeFromString(get("/api/monitor"))

    // -- writes (each one becomes a receipt on the PC) ------------------------

    fun transfer(pane: String, rel: String, to: String, move: Boolean) {
        post("/api/transfer", json.encodeToString(TransferRequest.serializer(),
            TransferRequest(pane, rel, to, move)))
    }

    fun trash(pane: String, rel: String) {
        post("/api/trash", json.encodeToString(PathRequest.serializer(), PathRequest(pane, rel)))
    }

    // -- the owner's own choices: a shelf, a date ----------------------------

    fun shelves(): Shelves = json.decodeFromString(get("/api/shelves"))

    fun setShelf(pane: String, rel: String, shelf: String) {
        post("/api/card/shelf", json.encodeToString(ShelfRequest.serializer(), ShelfRequest(pane, rel, shelf)))
    }

    /** `null` clears the date: "no date" is an answer too. */
    fun setTaskDue(id: String, due: String?) {
        post("/api/task/due", json.encodeToString(DueRequest.serializer(), DueRequest(id, due)))
    }

    fun setHealthDate(id: String, date: String?) {
        post("/api/health/date", json.encodeToString(DateRequest.serializer(), DateRequest(id, date)))
    }

    /** One day from the watch into the Health timeline. Only ever on the owner's press. */
    fun addWatchDay(day: WatchDay): WatchAdded =
        json.decodeFromString(post("/api/health/watch", json.encodeToString(WatchDay.serializer(), day)))

    fun setTaskDone(id: String, done: Boolean) {
        post("/api/task", json.encodeToString(TaskRequest.serializer(), TaskRequest(id, done)))
    }

    /** Hand the door's address and key to the PC over the channel it already trusts. */
    fun pairDoor(address: String, doorKey: String) {
        post("/api/door/pair", json.encodeToString(PairRequest.serializer(),
            PairRequest(address, doorKey)))
    }

    // -- the agent's jobs, from the phone ------------------------------------

    fun sortPropose(): SortProposals = json.decodeFromString(post("/api/agent/sort", "{}"))

    // The `id` in each proposal is a one-use handle bound to what was shown; the
    // PC answers 409 if the list changed underneath, and says what it wrote if
    // a write stopped midway. Both are read here, not guessed from the status.
    fun sortConfirm(items: List<SortAccept>): AcceptResult = json.decodeFromString(
        post("/api/agent/sort/confirm", json.encodeToString(SortConfirmRequest.serializer(), SortConfirmRequest(items))))

    fun tasksPropose(): TaskProposals = json.decodeFromString(post("/api/agent/tasks", "{}"))
    fun tasksAdd(ids: List<String>): AcceptResult =
        json.decodeFromString(post("/api/agent/tasks/add", json.encodeToString(IdsRequest.serializer(), IdsRequest(ids))))

    fun healthPropose(): HealthProposals = json.decodeFromString(post("/api/agent/health", "{}"))
    fun healthAdd(ids: List<String>): AcceptResult =
        json.decodeFromString(post("/api/agent/health/add", json.encodeToString(IdsRequest.serializer(), IdsRequest(ids))))

    fun ask(phrase: String): AskAnswer =
        json.decodeFromString(post("/api/agent/ask", json.encodeToString(AskRequest.serializer(), AskRequest(phrase))))

    fun say(text: String): String =
        json.decodeFromString<ChatReply>(
            post("/api/chat", json.encodeToString(ChatRequest.serializer(), ChatRequest(text)))
        ).reply

    fun upload(name: String, bytes: ByteArray) {
        val part = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("file", name, bytes.toRequestBody("application/octet-stream".toMediaType()))
            .build()
        call(Request.Builder().url(url("/api/upload"))
            .header("Authorization", "Bearer $key").post(part).build())
    }
}

// -- what the vault answers ---------------------------------------------------

@Serializable data class ApiError(val error: String = "")
@Serializable data class Ping(val ok: Boolean = false, val name: String = "")
@Serializable data class Session(val vault_id: String = "", val api_version: Int = 0,
                                 val device_id: String = "", val device_name: String = "",
                                 val legacy: Boolean = false)
@Serializable data class PhoneGrant(val grant_id: String = "", val name: String = "", val size: Long = 0,
                                    val sha256: String = "", val action: String = "")
@Serializable private data class GrantRequest(val rel: String, val action: String, val sha256: String?)
@Serializable private data class OutcomeRequest(val grant_id: String, val outcome: String, val target: String?)
@Serializable data class PairClaim(val claim_id: String = "", val code: String = "")
@Serializable data class PairStatus(val state: String = "", val device_id: String = "", val token: String = "")
@Serializable private data class PairClaimRequest(val ticket: String, val name: String, val kind: String)
@Serializable private data class PairStatusRequest(val claim_id: String)

@Serializable
data class Entry(
    val name: String,
    val rel: String,
    val dir: Boolean,
    val size: Long = 0,
    val modified: Long = 0,
    val kind: String = "",
    /** How a viewer can show it: TEXT, IMAGE, PDF, DIR or NONE. */
    val view: String = "NONE",
    val shelf: String? = null,
    val confirmed: Boolean? = null,
    val topics: List<String> = emptyList(),
    val issuer: String? = null,
    val year: Int? = null,
    val recipients: List<String> = emptyList(),
) {
    /** The shelf keeps its "?" until the owner confirms the card — as on the PC. */
    val shelfLabel: String?
        get() = shelf?.let { if (confirmed == true) it else "$it ?" }
}

@Serializable data class Listing(val pane: String, val rel: String, val items: List<Entry>)
@Serializable data class FileText(val name: String, val rel: String, val text: String,
                                  val truncated: Boolean = false)

@Serializable
data class Task(
    val id: String, val title: String, val due: String? = null,
    val quote: String = "", val doc: String = "", val done: Boolean = false,
    val origin: String = "",
    /** Where the date came from: "quote" (read from the document), "owner" (set by you), "none". */
    val due_source: String = "none",
    val flags: List<String> = emptyList(),
    /** Counted by the PC from `due` and today, never by a model. */
    val days_left: Int? = null,
    val overdue: Boolean = false,
)

@Serializable data class TaskList(val tasks: List<Task>)

@Serializable
data class HealthEntry(
    val id: String, val date: String? = null, val kind: String = "",
    val label: String = "", val quote: String = "", val doc: String = "",
    val due_source: String = "none",
    val flags: List<String> = emptyList(),
)

@Serializable data class HealthYear(val year: String, val entries: List<HealthEntry>)
@Serializable data class HealthTimeline(val summary: String = "", val years: List<HealthYear> = emptyList())

@Serializable data class Message(val role: String, val content: String)
@Serializable data class ChatState(val backend: String = "", val ready: Boolean = false,
                                   val history: List<Message> = emptyList())
@Serializable data class ChatReply(val reply: String = "")
@Serializable data class Monitor(val available: Boolean = false, val note: String = "")

// -- what the phone asks for --------------------------------------------------

@Serializable private data class TransferRequest(val pane: String, val rel: String,
                                                 val to: String, val move: Boolean)
@Serializable private data class PathRequest(val pane: String, val rel: String)
@Serializable private data class TaskRequest(val id: String, val done: Boolean)
@Serializable private data class ChatRequest(val text: String)
@Serializable private data class PairRequest(val address: String, val key: String)
@Serializable private data class IdsRequest(val ids: List<String>)
@Serializable private data class ShelfRequest(val pane: String, val rel: String, val shelf: String)
// No defaults on purpose: the PC requires the key even when the value is null.
@Serializable private data class DueRequest(val id: String, val due: String?)
@Serializable private data class DateRequest(val id: String, val date: String?)

@Serializable data class PdfInfo(val pages: Int = 0, val warning: String = "")

/** What the Watch tab showed for one day, in the vault's words. Absent metrics are absent, not zero. */
@Serializable data class WatchReadings(
    val steps: Long? = null,
    val heart_min: Long? = null, val heart_avg: Long? = null, val heart_max: Long? = null,
    val sleep_minutes: Long? = null, val sleep_end: String? = null,
    val oxygen: Double? = null,
    // What Health Connect named as the writer of each metric — a device model and an
    // app — or null, in which case the PC says "source not verified" on that line.
    val steps_source: String? = null, val heart_source: String? = null,
    val sleep_source: String? = null, val oxygen_source: String? = null,
) {
    /** Nothing the timeline would print as a line. */
    val isEmpty: Boolean
        get() = steps == null && (heart_min == null || heart_max == null) && sleep_minutes == null && oxygen == null
}

@Serializable data class WatchDay(val day: String, val readings: WatchReadings)
@Serializable data class WatchAdded(val added: Int = 0, val labels: List<String> = emptyList())

@Serializable data class Shelves(val shelves: List<String> = emptyList(), val standard: List<String> = emptyList())
@Serializable private data class AskRequest(val phrase: String)
@Serializable private data class SortConfirmRequest(val items: List<SortAccept>)

// -- what the agent proposes -------------------------------------------------

@Serializable
data class SortProposal(
    val id: String, val name: String, val size: Long = 0, val shelf: String,
    val topics: List<String> = emptyList(), val issuer: String? = null, val year: Int? = null,
    val recipients: List<String> = emptyList(), val origin: String = "",
    val reason: String = "", val flags: List<String> = emptyList(),
    val differs: Boolean = false, val baseline_shelf: String = "",
)
@Serializable data class SortProposals(val proposals: List<SortProposal> = emptyList(), val notes: List<String> = emptyList())
/** What goes back: the id, and a shelf only if the owner changed it. */
@Serializable data class SortAccept(val id: String, val shelf: String? = null)

/** Present only when a write stopped midway: what landed, what is uncertain, what was never tried. */
@Serializable data class Outcome(val completed: List<String> = emptyList(),
                                 val failed_unknown: List<String> = emptyList(),
                                 val not_attempted: List<String> = emptyList())

/** The PC's answer to a selection. `confirmed` is Sort's word, `added` is Tasks' and Health's. */
@Serializable data class AcceptResult(val confirmed: Int = 0, val added: Int = 0,
                                      val errors: List<String> = emptyList(), val outcome: Outcome? = null) {
    /** One honest line for the snackbar; never "done" when the PC said "unknown". */
    fun summary(verb: String, noun: String): String {
        val o = outcome
        if (o != null) return "written ${o.completed.size} $noun, uncertain ${o.failed_unknown.size}, " +
            "not attempted ${o.not_attempted.size} — refresh the list before choosing again"
        if (errors.isNotEmpty()) return errors.joinToString(" · ")
        return "$verb ${confirmed + added} $noun"
    }
}

@Serializable
data class TaskProposal(
    val id: String, val doc_sha256: String = "", val doc_name: String = "", val title: String,
    val due: String? = null, val quote: String = "", val origin: String = "", val done: Boolean = false,
)
@Serializable data class TaskProposals(val proposals: List<TaskProposal> = emptyList(), val notes: List<String> = emptyList())

@Serializable
data class HealthProposal(
    val id: String, val doc_sha256: String = "", val doc_name: String = "", val date: String? = null,
    val kind: String = "", val label: String, val quote: String = "", val origin: String = "",
)
@Serializable data class HealthProposals(val proposals: List<HealthProposal> = emptyList(), val notes: List<String> = emptyList())

@Serializable data class AskMatch(val name: String, val shelf: String? = null,
                                  val added_by_agent: Boolean = false, val omitted_by_agent: Boolean = false,
                                  /** Where it lies: Ask now searches all three panes. */
                                  val pane: String = "staging", val rel: String = "") {
    /** "Personal / IMMIGRATION / form.pdf" — the path the owner would click through. */
    val where: String
        get() = (listOf(PANE_TITLES[pane] ?: pane) + rel.split('/').filter { it.isNotBlank() }.dropLast(1))
            .joinToString(" / ")
}
@Serializable data class AskAnswer(val phrase: String = "", val matches: List<AskMatch> = emptyList(),
                                   val recipient: String? = null, val notes: List<String> = emptyList(),
                                   val export: String = "")
