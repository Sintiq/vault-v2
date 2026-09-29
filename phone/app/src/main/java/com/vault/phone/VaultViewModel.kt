package com.vault.phone

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

val PANES = listOf("staging", "documents", "personal")
val PANE_TITLES = mapOf("staging" to "Staging", "documents" to "Documents", "personal" to "Personal")

private const val TEXT_LIMIT = 1024 * 1024

data class VaultState(
    val connected: Boolean = false,
    val address: String = "",
    val listings: Map<String, Listing?> = emptyMap(),
    val rels: Map<String, String> = PANES.associateWith { "" },
    val tasks: List<Task> = emptyList(),
    val health: HealthTimeline = HealthTimeline(),
    val chat: ChatState = ChatState(),
    val monitor: Monitor = Monitor(),
    val busy: Boolean = false,
    val message: String? = null,
    /** While pairing: the six digits to compare with the PC, or null. */
    val pairingCode: String? = null,
    val pairing: Boolean = false,
    val link: Link = Link.RED,
    val pinSet: Boolean = false,
    val locked: Boolean = true,
    val pinWorking: Boolean = false,
    val pinMessage: String? = null,
    val pinWarning: String? = null,
    /** Set by a wipe: the app is back to its first screen. */
    val wiped: Boolean = false,
    /** Copies kept on this phone, inside Vault. */
    val offline: List<OfflineCopy> = emptyList(),
)

/**
 * Holds what the phone knows about the vault.
 *
 * Every call is a round trip; nothing is written locally. If the PC is asleep
 * the screens say so rather than showing a stale copy of the vault, because a
 * stale answer about your own documents is worse than no answer.
 */
class VaultViewModel(app: Application) : AndroidViewModel(app) {

    private val prefs = app.getSharedPreferences("vault", Context.MODE_PRIVATE)
    private val pin = PinStore(app)
    private val offlineStore = OfflineStore(app)
    private val client = VaultClient(prefs.getString("base", "").orEmpty(),
                                     SecretBox.read(prefs, "key").orEmpty())

    private val _state = MutableStateFlow(VaultState(address = prefs.getString("base", "").orEmpty(),
                                                     pinSet = pin.isSet, locked = true))
    val state: StateFlow<VaultState> = _state.asStateFlow()

    val isConfigured: Boolean
        get() = !prefs.getString("base", "").isNullOrBlank() && prefs.contains("key")

    fun dismissMessage() = _state.update { it.copy(message = null) }

    private fun say(text: String) = _state.update { it.copy(message = text) }

    /** The agent's jobs: same as [work], but say up front that it may take minutes. */
    private fun <T> slowWork(block: suspend () -> T, then: (T) -> Unit) {
        say("the PC is reading — this can take a couple of minutes")
        work(block, then)
    }

    private fun <T> work(block: suspend () -> T, then: (T) -> Unit) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true) }
            try {
                val value = withContext(Dispatchers.IO) { block() }
                _state.update { it.copy(busy = false, connected = true) }
                then(value)
            } catch (e: VaultClient.NotConnected) {
                _state.update { it.copy(busy = false, connected = false, message = e.message) }
            } catch (e: VaultClient.VaultException) {
                // The vault answered — it refused, or is busy. Still connected; say why.
                _state.update { it.copy(busy = false, message = e.message) }
            } catch (e: java.io.IOException) {
                // The answer began and then broke off: not reached, said plainly, never a crash.
                _state.update { it.copy(busy = false, connected = false, message = "the connection broke off — try again") }
            }
        }
    }

    // -- the door ------------------------------------------------------------

    /** Accepts either a bare address or the whole `http://host:port/?key=…` link. */
    fun connect(input: String, keyInput: String, onDone: (Boolean) -> Unit) {
        var base = input.trim()
        var key = keyInput.trim()
        val marker = base.indexOf("?key=")
        if (marker >= 0) {
            key = base.substring(marker + 5).substringBefore('&')
            base = base.substring(0, marker)
        }
        if (!base.startsWith("http")) base = "http://$base"
        base = base.trimEnd('/')
        if (!VaultAddress.plain(base)) {
            _state.update { it.copy(connected = false, message = "that is not a vault address") }
            onDone(false)
            return
        }
        client.configure(base, key)
        viewModelScope.launch {
            val ok = withContext(Dispatchers.IO) { runCatching { client.tasks() }.isSuccess }
            if (ok) {
                SecretBox.write(prefs.edit().putString("base", base), "key", key).remove("vault_id").apply()
                vaultId = null; resetLink()
                _state.update { it.copy(connected = true, address = base, message = null) }
                refreshAll()
            } else {
                _state.update { it.copy(connected = false, message = "no answer — check Tailscale, the address and the key") }
            }
            onDone(ok)
        }
    }

    fun forget() {
        pinGeneration++
        exportUntil = 0L
        resetLink()
        prefs.edit().clear().apply()
        _state.value = VaultState(pinSet = pin.isSet, locked = false)
    }

    // -- the PIN (contract §3) -------------------------------------------------------

    private var exportUntil: Long = 0L
    /**
     * The one export whose system picker or share sheet is in front. It survives the activity
     * being recreated, and it may finish — but it never keeps the rest of Vault unlocked.
     */
    var pendingExport: PreparedExport? = null

    /** Bumped by lock, forget, wipe and a cancelled PIN dialog: an older PIN answer grants nothing. */
    @Volatile private var pinGeneration = 0
    private var pinCheckRunning = false

    fun setPin(first: String, second: String) {
        if (pinCheckRunning) return
        if (!PinPolicy.valid(first)) { _state.update { it.copy(pinMessage = "6 to 12 digits") }; return }
        if (first != second) { _state.update { it.copy(pinMessage = "the two PINs differ") }; return }
        val generation = pinGeneration
        pinCheckRunning = true
        viewModelScope.launch {
            _state.update { it.copy(pinWorking = true, pinMessage = null) }
            val saved = runCatching { withContext(Dispatchers.Default) { pin.set(first) } }.isSuccess
            pinCheckRunning = false
            if (!saved) {
                // Storage refused the write (full, or damaged): say so; nothing is opened.
                _state.update { it.copy(pinWorking = false,
                    pinMessage = "The PIN could not be saved on this phone — try again.") }
                return@launch
            }
            _state.update { it.copy(pinWorking = false, pinSet = true) }
            // Set while Vault stayed open: open it. Otherwise the PIN is set but Vault stays locked.
            if (generation == pinGeneration) {
                _state.update { it.copy(locked = false) }
                refreshAll()
            }
        }
    }

    /** The last warning before the wipe comes from what is stored, so a restart cannot hide it. */
    fun refreshPinWarning() {
        val warn = if (pin.failures == PinPolicy.WIPE_AT - 1) "One more wrong PIN erases Vault on this phone." else null
        _state.update { it.copy(pinWarning = warn) }
    }

    fun cancelPinDialog() {
        pinGeneration++
    }

    fun lock() {
        pinGeneration++
        exportUntil = 0L
        resetLink()
        _state.update { it.copy(locked = true, pinMessage = null) }
        refreshPinWarning()
    }

    fun unlock(entered: String) = checkPin(entered) {
        _state.update { it.copy(locked = false, pinWarning = null, pinMessage = null) }
        refreshAll()
    }

    fun exportWindowOpen(): Boolean = android.os.SystemClock.elapsedRealtime() < exportUntil

    /** The same PIN again before files leave Vault; opens a five-minute window. */
    fun confirmExportPin(entered: String, onOk: () -> Unit) = checkPin(entered) {
        exportUntil = android.os.SystemClock.elapsedRealtime() + PinPolicy.EXPORT_WINDOW_MS
        _state.update { it.copy(pinWarning = null, pinMessage = null) }
        onOk()
    }

    private fun checkPin(entered: String, onOk: () -> Unit) {
        if (pinCheckRunning) return
        pinCheckRunning = true
        val generation = pinGeneration
        viewModelScope.launch {
            _state.update { it.copy(pinWorking = true) }
            val result = runCatching { withContext(Dispatchers.Default) { pin.check(entered) } }
                .getOrElse { PinStore.Check.Wait(0) }
            pinCheckRunning = false
            _state.update { it.copy(pinWorking = false) }
            when (result) {
                // A right PIN answered after Vault was locked or the dialog closed grants nothing.
                PinStore.Check.Ok -> if (generation == pinGeneration) onOk()
                is PinStore.Check.Wait -> _state.update {
                    it.copy(pinMessage = if (result.ms > 0) "Too many wrong PINs. Try again in ${PinPolicy.waitWords(result.ms)}."
                                         else "The PIN could not be checked — try again.")
                }
                is PinStore.Check.Wrong -> _state.update {
                    it.copy(pinMessage = "Wrong PIN (${result.failures} in a row)." +
                                (pin.waitMs().takeIf { w -> w > 0 }?.let { w -> " Next try in ${PinPolicy.waitWords(w)}." } ?: ""),
                            pinWarning = if (result.lastChance) "One more wrong PIN erases Vault on this phone." else null)
                }
                PinStore.Check.Wipe -> wipe()
            }
        }
    }

    /** Erase Vault on this phone (contract §3). Also resumes an interrupted wipe. */
    fun wipe() {
        pinGeneration++
        exportUntil = 0L
        _state.update { it.copy(locked = true, pinWorking = true, pinMessage = "Erasing Vault on this phone…") }
        viewModelScope.launch {
            val app = getApplication<Application>()
            val hadKey = isConfigured
            val done = withContext(Dispatchers.IO) {
                runCatching { Wipe.run(app, if (hadKey) ({ client.selfWipe() }) else null) }.getOrDefault(false)
            }
            if (!done) {
                // Stays locked; the mark is kept and the next start tries again.
                _state.update { it.copy(locked = true, pinWorking = false,
                    pinMessage = "Vault could not finish erasing itself on this phone. It stays locked " +
                                 "and tries again the next time it opens.") }
                return@launch
            }
            client.configure("", "")
            _state.value = VaultState(pinSet = false, locked = false, wiped = true,
                                      message = "Vault on this phone was erased after 9 wrong PINs. " +
                                                "Pair again from the PC (Phone… → Add phone…).")
        }
    }

    init {
        // A ninth wrong PIN is final even if the process died before the wipe began.
        if (Wipe.pending(app) || pin.failures >= PinPolicy.WIPE_AT) wipe() else refreshPinWarning()
    }

    // -- pairing by the PC's QR ------------------------------------------------

    private var pairingJob: kotlinx.coroutines.Job? = null

    /**
     * Claim the ticket, show the digits, wait for the owner's Confirm at the PC, keep the key.
     * An expired or rejected attempt changes nothing about an existing connection.
     */
    fun startPairing(link: PairLink) {
        pairingJob?.cancel()
        pairingJob = viewModelScope.launch {
            _state.update { it.copy(pairing = true, pairingCode = null, message = null) }
            val name = android.os.Build.MODEL ?: "Phone"
            val claim = try {
                withContext(Dispatchers.IO) { client.pairClaim(link.base, link.ticket, name) }
            } catch (e: VaultClient.VaultException) {
                _state.update { it.copy(pairing = false, message = e.message ?: "the PC did not answer") }
                return@launch
            }
            _state.update { it.copy(pairingCode = claim.code.take(3) + " " + claim.code.drop(3)) }
            repeat(150) {
                kotlinx.coroutines.delay(2_000)
                val st = runCatching { withContext(Dispatchers.IO) { client.pairStatus(link.base, claim.claim_id) } }
                    .getOrNull() ?: return@repeat
                when (st.state) {
                    "confirmed" -> {
                        // A new pairing may be a different vault: forget which one the dot expected.
                        SecretBox.write(prefs.edit().putString("base", link.base), "key", st.token)
                            .remove("vault_id").apply()
                        vaultId = null
                        pinGeneration++
                        exportUntil = 0L
                        resetLink()
                        client.configure(link.base, st.token)
                        _state.update { it.copy(pairing = false, pairingCode = null, connected = true,
                                                address = link.base, message = "paired with the PC") }
                        checkLink()
                        refreshAll()
                        return@launch
                    }
                    "rejected", "expired" -> {
                        _state.update { it.copy(pairing = false, pairingCode = null,
                            message = if (st.state == "rejected") "the PC rejected this pairing"
                                      else "the pairing expired — make a new QR at the PC") }
                        return@launch
                    }
                }
            }
            _state.update { it.copy(pairing = false, pairingCode = null, message = "the pairing expired — make a new QR at the PC") }
        }
    }

    fun cancelPairing() {
        pairingJob?.cancel()
        _state.update { it.copy(pairing = false, pairingCode = null) }
    }

    // -- the dot -----------------------------------------------------------------

    private var lastOkAt: Long? = null
    private var vaultId: String? = prefs.getString("vault_id", null)
    private var apiVersion: Int? = null

    /** Ask the PC who we are; green only after an authorised answer from the same vault. */
    private var linkGeneration = 0
    private var linkInFlight = false
    private var vaultMatches = true

    /** Forget what the dot knew: a new connection, a lock, a return from the background. */
    fun resetLink() {
        linkGeneration++
        lastOkAt = null; apiVersion = null; vaultMatches = true
        _state.update { it.copy(link = Link.RED) }
    }

    /** Recompute the dot from what is known now; green ages out on its own after 30 s. */
    fun refreshDot() {
        val link = LinkState.of(lastOkAt, android.os.SystemClock.elapsedRealtime(), apiVersion, vaultMatches)
        if (link != _state.value.link) _state.update { it.copy(link = link) }
    }

    /** Ask the PC who we are; one question at a time, and a late answer to an old question is dropped. */
    fun checkLink() {
        if (!isConfigured || linkInFlight) return
        linkInFlight = true
        val generation = linkGeneration
        viewModelScope.launch {
            val session = runCatching { withContext(Dispatchers.IO) { client.session() } }.getOrNull()
            linkInFlight = false
            if (generation != linkGeneration) return@launch
            val now = android.os.SystemClock.elapsedRealtime()
            if (session == null) {
                apiVersion = null          // a version is only a fact of the latest answer
                lastOkAt = null            // a 401 or a timeout is not «online», not even for 30 s
            } else {
                apiVersion = session.api_version
                if (vaultId == null) {
                    vaultId = session.vault_id
                    prefs.edit().putString("vault_id", session.vault_id).apply()
                }
                vaultMatches = session.vault_id == vaultId
                if (vaultMatches) lastOkAt = now
            }
            refreshDot()
        }
    }

    // -- files to the phone (contract §4): Staging only, PC online, the owner presses -----

    fun loadOffline() = _state.update { it.copy(offline = offlineStore.list()) }

    fun canTakeOut(): Boolean = _state.value.link == Link.GREEN

    /** «Available offline»: the exact bytes, kept inside Vault under the PIN. */
    fun makeOffline(entry: Entry) {
        if (!canTakeOut()) { say("the PC must be online (green) to copy a file to this phone"); return }
        work({
            val g = client.grant(entry.rel, "offline")
            val bytes = client.grantBytes(g.grant_id)
            offlineStore.save(g.sha256, g.name, entry.rel, bytes)
        }) { say("kept on this phone: ${it.name}"); loadOffline() }
    }

    fun removeOffline(copy: OfflineCopy) {
        viewModelScope.launch {
            val ok = withContext(Dispatchers.IO) { runCatching { offlineStore.remove(copy) }.isSuccess }
            say(if (ok) "removed from this phone: ${copy.name} — the PC's file is untouched"
                else "could not remove ${copy.name} from this phone — it is still here")
            loadOffline()
        }
    }

    fun offlineFile(copy: OfflineCopy): java.io.File = offlineStore.file(copy)

    /** Open a kept copy: read and draw it off the main thread; text is shown up to 1 MB. */
    fun openOffline(copy: OfflineCopy, onText: (FileText) -> Unit, onRendered: (Rendered) -> Unit) {
        val file = offlineStore.file(copy)
        val lower = copy.name.lowercase()
        viewModelScope.launch {
            val shown: Any = withContext(Dispatchers.IO) {
                runCatching {
                    when {
                        lower.endsWith(".pdf") -> renderLocalPdf(file)
                        lower.endsWith(".txt") || lower.endsWith(".md") || lower.endsWith(".csv") -> {
                            val raw = file.inputStream().use { ins ->
                                val buf = ByteArray(TEXT_LIMIT + 1)
                                var n = 0
                                while (n < buf.size) {
                                    val r = ins.read(buf, n, buf.size - n)
                                    if (r < 0) break
                                    n += r
                                }
                                buf.copyOf(n)
                            }
                            FileText(copy.name, copy.rel, raw.take(TEXT_LIMIT).toByteArray().decodeToString(),
                                     raw.size > TEXT_LIMIT)
                        }
                        else -> renderImage(file.readBytes())
                    }
                }.getOrElse { Rendered.Failed("this copy could not be opened: ${it.message}") }
            }
            if (shown is FileText) onText(shown) else onRendered(shown as Rendered)
        }
    }

    /**
     * «Save a copy to Files…» or «Share…»: a fresh grant (the PC re-checks Staging and the
     * hash), the verified bytes into files/export, then the system picker or share sheet.
     */
    fun prepareExport(rel: String, sha256: String?, action: String, onReady: (PreparedExport) -> Unit) {
        if (!canTakeOut()) { say("the PC must be online (green) to take a file out"); return }
        if (!exportWindowOpen()) { say("enter the PIN to take files out"); return }
        val generation = pinGeneration
        work({
            val g = client.grant(rel, action, sha256)
            val bytes = client.grantBytes(g.grant_id)
            check(Sha.hex(bytes) == g.sha256) { "the copy does not match the PC's file" }
            // Locked, forgotten or erased meanwhile: write nothing, open nothing.
            if (generation != pinGeneration || Wipe.pending(getApplication())) null
            else PreparedExport(g.grant_id, ExportFiles.write(getApplication(), g.grant_id, g.name, bytes), g.name, action)
        }) { prepared ->
            if (prepared == null || generation != pinGeneration || !exportWindowOpen()) {
                prepared?.file?.parentFile?.deleteRecursively()
                if (prepared != null) reportOutcome(prepared, "cancelled")
                say("Vault was locked while the copy was prepared — nothing left Vault")
                return@work
            }
            pendingExport = prepared
            onReady(prepared)
        }
    }

    /** The picker or sheet came back: finish the one operation it belongs to, off the main thread. */
    fun finishSave(prepared: PreparedExport, uri: android.net.Uri?) {
        pendingExport = null
        if (uri == null) { prepared.file.parentFile?.deleteRecursively(); reportOutcome(prepared, "cancelled"); return }
        viewModelScope.launch {
            val ok = withContext(Dispatchers.IO) {
                runCatching {
                    getApplication<Application>().contentResolver.openOutputStream(uri)?.use { out ->
                        prepared.file.inputStream().use { it.copyTo(out) }
                    } ?: error("no stream")
                }.isSuccess.also { prepared.file.parentFile?.deleteRecursively() }
            }
            reportOutcome(prepared, if (ok) "saved" else "failed")
        }
    }

    /** The share sheet came back. Only a callback for this very operation names an app. */
    fun finishShare(prepared: PreparedExport) {
        pendingExport = null
        viewModelScope.launch {
            kotlinx.coroutines.delay(1_500)       // the chooser's callback may land a moment later
            val chosen = ExportOutcome.take(prepared.grantId)
            if (chosen != null) reportOutcome(prepared, "handed_to", chosen)
            else reportOutcome(prepared, "unknown")
        }
    }

    /** What the phone saw; best effort, and never «sent». */
    fun reportOutcome(prepared: PreparedExport, outcome: String, target: String? = null) {
        viewModelScope.launch {
            runCatching { withContext(Dispatchers.IO) { client.outcome(prepared.grantId, outcome, target) } }
            say(when (outcome) {
                "saved" -> "saved a copy: ${prepared.name}"
                "handed_to" -> "handed to ${target ?: "an app"} — Vault cannot see whether it was sent"
                "cancelled" -> "nothing left Vault"
                "unknown" -> "Vault cannot tell whether an app took the file"
                else -> "the copy could not be written"
            })
        }
    }

    // -- updates (contract §7): signed by Vault's key, installed on the owner's confirmation ----

    fun checkForUpdate(onAvailable: (ReleaseManifest) -> Unit) {
        work({
            if (BuildConfig.RELEASE_KEY.isBlank()) ReleaseCheck.NotConfigured
            else ReleaseRules.decide(BuildConfig.RELEASE_KEY, client.releaseManifest(), client.releaseSignature(),
                                     BuildConfig.VERSION_CODE, LinkState.API_VERSION)
        }) { r ->
            when (r) {
                ReleaseCheck.NotConfigured -> say("this build does not check for updates")
                ReleaseCheck.UpToDate -> say("Vault ${BuildConfig.VERSION_NAME} is the newest the PC offers")
                is ReleaseCheck.Refused -> say("update refused: ${r.why}")
                is ReleaseCheck.Available -> onAvailable(r.manifest)
            }
        }
    }

    /** Download, check the bytes against the signed manifest, hand them to Android's installer. */
    fun installUpdate(m: ReleaseManifest) {
        val app = getApplication<Application>()
        if (!app.packageManager.canRequestPackageInstalls()) {
            // Once per phone: Android wants the owner to let Vault install apps. Open that page;
            // the owner allows it, comes back, and presses Update again.
            say("allow «Install unknown apps» for Vault, then check for the update again")
            app.startActivity(android.content.Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                android.net.Uri.parse("package:" + app.packageName)).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        }
        work({
            UpdateJob.run {
                val parent = java.io.File(app.filesDir, "update")
                // Only one update runs at a time, so whatever is here was left by one that died.
                parent.listFiles()?.forEach { it.deleteRecursively() }
                var apk: java.io.File? = null
                try {
                    apk = client.downloadRelease(m, parent)
                    try {
                        Updater.install(app, apk, m)
                    } catch (e: VaultClient.VaultException) {
                        throw e
                    } catch (e: Exception) {
                        throw VaultClient.VaultException("Android's installer did not take the update: " +
                                                         (e.message ?: e.javaClass.simpleName))
                    }
                } finally {
                    apk?.parentFile?.deleteRecursively()
                }
            }
        }) { say("Android asks you to confirm the update") }
    }

    // -- reads ---------------------------------------------------------------

    fun refreshAll() {
        PANES.forEach { loadPane(it) }
        loadTasks(); loadHealth(); loadChat(); loadMonitor()
    }

    fun loadPane(pane: String, rel: String = _state.value.rels[pane].orEmpty()) =
        work({ client.files(pane, rel) }) { listing ->
            _state.update {
                it.copy(listings = it.listings + (pane to listing),
                        rels = it.rels + (pane to listing.rel))
            }
        }

    fun enter(pane: String, entry: Entry) = loadPane(pane, entry.rel)

    fun leave(pane: String) {
        val rel = _state.value.rels[pane].orEmpty()
        loadPane(pane, rel.substringBeforeLast('/', ""))
    }

    fun loadTasks() = work({ client.tasks() }) { list -> _state.update { it.copy(tasks = list.tasks) } }
    fun loadHealth() = work({ client.health() }) { t -> _state.update { it.copy(health = t) } }
    fun loadChat() = work({ client.chatState() }) { c -> _state.update { it.copy(chat = c) } }
    fun loadMonitor() = work({ client.monitor() }) { m -> _state.update { it.copy(monitor = m) } }

    fun readText(pane: String, entry: Entry, onText: (FileText) -> Unit) =
        work({ client.text(pane, entry.rel) }, onText)

    /** Fetch and render a scan or a photograph. Decoding happens off the main thread. */
    fun view(pane: String, entry: Entry, onRendered: (Rendered) -> Unit) =
        work({ if (entry.view == "PDF") pdfPages(pane, entry) else renderImage(client.blob(pane, entry.rel)) },
             onRendered)

    /** Pages drawn by the PC, one request each; a busy PC gets a moment, not a flood. */
    private suspend fun pdfPages(pane: String, entry: Entry): Rendered {
        val info = client.pdfInfo(pane, entry.rel)
        if (info.pages <= 0) return Rendered.Failed("this PDF has no pages")
        val pages = (0 until minOf(info.pages, PDF_MAX_PAGES)).map { index ->
            var attempt = 0
            while (true) {
                try {
                    val png = client.pdfPage(pane, entry.rel, index, PDF_PAGE_WIDTH_PX)
                    return@map decodePage(png)
                        ?: return Rendered.Failed("page ${index + 1} could not be decoded on the phone")
                } catch (e: VaultClient.VaultBusy) {
                    if (++attempt >= 5) throw e
                    kotlinx.coroutines.delay(400L * attempt)
                }
            }
            @Suppress("UNREACHABLE_CODE") error("unreachable")
        }
        return Rendered.Pages(pages, total = info.pages, warning = info.warning)
    }

    // -- writes --------------------------------------------------------------

    fun transfer(pane: String, entry: Entry, to: String, move: Boolean) =
        work({ client.transfer(pane, entry.rel, to, move) }) {
            say((if (move) "moved to " else "copied to ") + PANE_TITLES[to])
            loadPane(pane); loadPane(to)
        }

    fun trash(pane: String, entry: Entry) = work({ client.trash(pane, entry.rel) }) {
        say("${entry.name} → Trash, restorable on the PC")
        loadPane(pane)
    }

    // -- the owner's own choices ---------------------------------------------

    fun loadShelves(onResult: (Shelves) -> Unit) = work({ client.shelves() }, onResult)

    fun setShelf(pane: String, entry: Entry, shelf: String) =
        work({ client.setShelf(pane, entry.rel, shelf) }) {
            say("${entry.name} → $shelf"); loadPane(pane)
        }

    fun setTaskDue(task: Task, due: String?) = work({ client.setTaskDue(task.id, due) }) {
        say(if (due == null) "date cleared" else "due $due — set by you"); loadTasks()
    }

    fun setHealthDate(entry: HealthEntry, date: String?) = work({ client.setHealthDate(entry.id, date) }) {
        say(if (date == null) "date cleared" else "dated $date — set by you"); loadHealth()
    }

    fun setTaskDone(task: Task, done: Boolean) =
        work({ client.setTaskDone(task.id, done) }) { loadTasks() }

    fun pairDoor(address: String, doorKey: String) =
        work({ client.pairDoor(address, doorKey) }) { say("the PC has the door key") }

    fun upload(name: String, bytes: ByteArray) = work({ client.upload(name, bytes) }) {
        say("into Staging: $name")
        loadPane("staging")
    }

    // -- the agent's jobs ----------------------------------------------------
    // A proposal writes nothing; only "add ticked" goes to the vault's stores.

    fun sortPropose(onResult: (SortProposals) -> Unit) = slowWork({ client.sortPropose() }, onResult)
    // The count comes from the PC's answer, not from what was ticked: a stale
    // list is refused whole (409), and a write that stopped midway says so.
    fun sortConfirm(items: List<SortAccept>) = work({ client.sortConfirm(items) }) {
        say(it.summary("confirmed", "card(s)")); loadPane("staging")
    }
    fun tasksPropose(onResult: (TaskProposals) -> Unit) = slowWork({ client.tasksPropose() }, onResult)
    fun tasksAdd(ids: List<String>) = work({ client.tasksAdd(ids) }) { say(it.summary("added", "task(s)")); loadTasks() }
    fun healthPropose(onResult: (HealthProposals) -> Unit) = slowWork({ client.healthPropose() }, onResult)
    fun healthAdd(ids: List<String>) = work({ client.healthAdd(ids) }) { say(it.summary("added", "entr(ies)")); loadHealth() }

    /** Something the screen wants said in the snackbar. */
    fun tell(text: String) = say(text)

    /**
     * Yesterday and today from the watch into the Health timeline, on the owner's press.
     * Each day is one write on the PC; if the second day fails after the first landed,
     * the message says exactly what landed instead of hiding it behind the error.
     */
    fun watchAdd(days: List<WatchDay>) {
        if (days.isEmpty()) { say("nothing from the watch to add yet"); return }
        work({
            var added = 0
            val done = ArrayList<String>()      // the PC confirmed these
            val notStarted = ArrayList<String>() // never asked
            var unclear: String? = null          // asked, no confirmation: may or may not have landed
            var why = ""
            for (d in days) {
                if (unclear != null) { notStarted += d.day; continue }
                try {
                    added += client.addWatchDay(d).added
                    done += d.day
                } catch (e: VaultClient.NotConnected) {
                    if (done.isEmpty()) throw e   // nothing landed: the usual "not connected" path
                    unclear = d.day; why = e.message ?: "no answer"
                } catch (e: VaultClient.VaultException) {
                    unclear = d.day; why = e.message ?: "refused"
                }
            }
            val day = unclear
            if (day == null) "added $added line(s) to Health for ${done.joinToString()}"
            else buildString {
                if (done.isNotEmpty()) append("added $added line(s) for ${done.joinToString()}; ")
                append("$day: the PC said \"$why\" — it may or may not have landed, check the Health list")
                if (notStarted.isNotEmpty()) append("; not sent: ${notStarted.joinToString()}")
            }
        }) { say(it); loadHealth() }
    }
    fun ask(phrase: String, onResult: (AskAnswer) -> Unit) = slowWork({ client.ask(phrase) }, onResult)

    fun send(text: String) {
        val pending = _state.value.chat
        _state.update {
            it.copy(chat = pending.copy(history = pending.history + Message("user", text)))
        }
        viewModelScope.launch {
            _state.update { it.copy(busy = true) }
            val reply = withContext(Dispatchers.IO) { runCatching { client.say(text) } }
            _state.update { s ->
                val answer = reply.getOrElse { "Failed: ${it.message}" }
                s.copy(busy = false, chat = s.chat.copy(history = s.chat.history + Message("assistant", answer)))
            }
        }
    }
}
