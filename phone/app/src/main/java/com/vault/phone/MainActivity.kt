package com.vault.phone

import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.filled.CheckBox
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Watch
import com.vault.phone.door.DoorScreen
import com.vault.phone.door.restoreDoorIfOpen
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/**
 * The window into the vault.
 *
 * Five tabs, in the order the day uses them: the files first, the agent next,
 * then what has to be done, then what the papers say about health, and last a
 * tab that is honestly empty until a watch exists to fill it.
 */
class MainActivity : ComponentActivity() {

    private val model: VaultViewModel by viewModels()

    private val pickFile = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        uri?.let { upload(it) }
    }

    // The operation lives in the view model, so a recreated activity still finishes it.
    private val saveTo = registerForActivityResult(ActivityResultContracts.CreateDocument("*/*")) { uri ->
        model.pendingExport?.let { model.finishSave(it, uri) }
    }

    private val shareVia = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        model.pendingExport?.let { model.finishShare(it) }
    }

    /** After the owner confirmed and the PIN window is open: prepare, then hand to the system. */
    fun startTakeOut(rel: String, sha256: String?, action: String) {
        model.prepareExport(rel, sha256, action) { prepared ->
            val launched = runCatching {
                if (action == "save") saveTo.launch(prepared.name)
                else shareVia.launch(ShareSheet.intent(this, prepared))
            }
            if (launched.isFailure) {
                model.pendingExport = null
                prepared.file.parentFile?.deleteRecursively()
                model.reportOutcome(prepared, "failed")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // The app switcher shows no picture of Vault's screens (Android 13+).
        if (android.os.Build.VERSION.SDK_INT >= 33) setRecentsScreenshotEnabled(false)
        setContent {
            VaultTheme {
                Surface(color = MaterialTheme.colorScheme.background) {
                    val state by model.state.collectAsStateWithLifecycle()
                    var connected by remember { mutableStateOf(model.isConfigured) }
                    var atDoor by remember { mutableStateOf(false) }
                    var takeOut by remember { mutableStateOf<TakeOut?>(null) }
                    var askPin by remember { mutableStateOf(false) }
                    takeOut?.let { t ->
                        if (askPin) {
                            ExportPinDialog(state.pinWorking, state.pinMessage, state.pinWarning,
                                onConfirm = { pin -> model.confirmExportPin(pin) {
                                    askPin = false; takeOut = null; startTakeOut(t.rel, t.sha, t.action) } },
                                onCancel = { model.cancelPinDialog(); askPin = false; takeOut = null })
                        } else {
                            androidx.compose.material3.AlertDialog(
                                onDismissRequest = { takeOut = null },
                                title = { Text(if (t.action == "save") "Save a copy to Files" else "Share") },
                                text = { Text("«${t.name}» — this copy leaves Vault. Its PIN and revoking this " +
                                              "phone no longer protect it.") },
                                confirmButton = { androidx.compose.material3.TextButton(onClick = {
                                    if (model.exportWindowOpen()) { takeOut = null; startTakeOut(t.rel, t.sha, t.action) }
                                    else { model.refreshPinWarning(); askPin = true }
                                }) { Text("Continue") } },
                                dismissButton = { androidx.compose.material3.TextButton(onClick = { takeOut = null }) {
                                    Text("Cancel") } },
                            )
                        }
                    }
                    LaunchedEffect(state.connected, state.pairing) {
                        if (state.connected && !state.pairing) connected = true
                    }
                    LaunchedEffect(state.wiped) { if (state.wiped) connected = false }
                    when {
                        !connected || state.pairing -> ConnectScreen(model, state.message) { connected = true }
                        !state.pinSet -> SetPinScreen(state.pinWorking, state.pinMessage) { a, b -> model.setPin(a, b) }
                        state.locked -> UnlockScreen(state.pinWorking, state.pinMessage, state.pinWarning) { model.unlock(it) }
                        atDoor -> DoorPage(
                            onBack = { atDoor = false },
                            onPairWithPc = { address, doorKey -> model.pairDoor(address, doorKey) },
                        )
                        else -> VaultScaffold(model, state, { pickFile.launch("*/*") },
                                              onTakeOut = { rel, name, sha, action -> takeOut = TakeOut(rel, name, sha, action) },
                                              startTab = if (intent?.getStringExtra("tab") == "tasks") 2 else 0,
                                              onOpenDoor = { atDoor = true },
                                              // The vault moved (a new address, a new key): back to the
                                              // connect screen without clearing the app. The door keeps
                                              // its own pairing; only the vault address and key go.
                                              onChangeConnection = { model.forget(); connected = false })
                    }
                }
            }
        }
        PairLink.fromUri(intent?.data)?.let { model.startPairing(it) }
        // Reads start after the PIN opens Vault (unlock → refreshAll), not before.
        ExportFiles.sweep(this)
        // A reboot should not silently re-open the door, but it should not
        // silently close one the owner deliberately left open either.
        restoreDoorIfOpen(this)
    }

    /** Leaving Vault, or the screen going off, locks it; the export window ends too. */
    override fun onStop() {
        super.onStop()
        model.lock()
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        PairLink.fromUri(intent.data)?.let { model.startPairing(it) }
    }

    /** Read the picked file here, where the content resolver lives. */
    private fun upload(uri: Uri) {
        val name = contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (index >= 0 && cursor.moveToFirst()) cursor.getString(index) else null
        } ?: uri.lastPathSegment ?: "upload.bin"
        val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: return
        model.upload(name, bytes)
    }
}

private data class Tab(val label: String, val icon: ImageVector)

private val TABS = listOf(
    Tab("Files", Icons.Filled.Folder),
    Tab("Chat", Icons.AutoMirrored.Filled.Chat),
    Tab("Tasks", Icons.Filled.CheckBox),
    Tab("Health", Icons.Filled.Favorite),
    Tab("Watch", Icons.Filled.Watch),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DoorPage(onBack: () -> Unit, onPairWithPc: (String, String) -> Unit) {
    // Without this the system back button leaves the app altogether, which is
    // not what "back" means when you are one screen deep.
    BackHandler(onBack = onBack)
    Scaffold(
        topBar = {
            CenterAlignedTopAppBar(
                title = { Text("Door") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
            )
        },
    ) { padding ->
        Surface(Modifier.padding(padding), color = MaterialTheme.colorScheme.background) {
            DoorScreen(onPairWithPc)
        }
    }
}

/** "Vault" with a green, red or amber dot and the word for it. */
@Composable
private fun LinkTitle(link: Link) {
    val color = when (link) {
        Link.GREEN -> androidx.compose.ui.graphics.Color(0xFF2E9E55)
        Link.RED -> androidx.compose.ui.graphics.Color(0xFFC0553F)
        Link.AMBER -> androidx.compose.ui.graphics.Color(0xFFD99A2B)
    }
    androidx.compose.foundation.layout.Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
        Text("Vault")
        androidx.compose.foundation.layout.Spacer(Modifier.padding(start = 8.dp))
        androidx.compose.foundation.Canvas(Modifier.padding(top = 2.dp).then(
            Modifier.size(10.dp))) { drawCircle(color) }
        Text("  " + LinkState.words(link), style = MaterialTheme.typography.labelMedium, color = color)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun VaultScaffold(
    model: VaultViewModel,
    state: VaultState,
    onPickFile: () -> Unit,
    startTab: Int = 0,
    onTakeOut: (String, String, String?, String) -> Unit = { _, _, _, _ -> },
    onOpenDoor: () -> Unit,
    onChangeConnection: () -> Unit,
) {
    var tab by remember { mutableStateOf(startTab) }
    val snackbar = remember { SnackbarHostState() }

    // The dot: ask the PC every 15 s while this screen is shown, and let green age out
    // by itself even when an answer never comes back.
    LaunchedEffect(Unit) {
        var tick = 0
        while (true) {
            if (tick % 3 == 0) model.checkLink()
            model.refreshDot()
            tick++
            kotlinx.coroutines.delay(5_000)
        }
    }

    LaunchedEffect(state.message) {
        state.message?.let {
            snackbar.showSnackbar(it)
            model.dismissMessage()
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            CenterAlignedTopAppBar(
                title = { LinkTitle(state.link) },
                actions = {
                    IconButton(onClick = onOpenDoor) {
                        Icon(Icons.Filled.Lock, contentDescription = "Agent door")
                    }
                    IconButton(onClick = { model.refreshAll() }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh")
                    }
                    var menu by remember { mutableStateOf(false) }
                    var offer by remember { mutableStateOf<ReleaseManifest?>(null) }
                    IconButton(onClick = { menu = true }) {
                        Icon(Icons.Filled.Settings, contentDescription = "Settings")
                    }
                    androidx.compose.material3.DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        androidx.compose.material3.DropdownMenuItem(
                            text = { Text("Check for update (now ${BuildConfig.VERSION_NAME})") },
                            onClick = { menu = false; model.checkForUpdate { offer = it } })
                        androidx.compose.material3.DropdownMenuItem(
                            text = { Text("Change connection") },
                            onClick = { menu = false; onChangeConnection() })
                    }
                    offer?.let { m ->
                        androidx.compose.material3.AlertDialog(
                            onDismissRequest = { offer = null },
                            title = { Text("Update to Vault ${m.version}") },
                            text = { Text("Signed by Vault's release key and checked. Android will ask you to " +
                                          "confirm — the first time also to allow Vault to install apps. " +
                                          "Your pairing, PIN and files on this phone stay.") },
                            confirmButton = { androidx.compose.material3.TextButton(onClick = {
                                offer = null; model.installUpdate(m) }) { Text("Update") } },
                            dismissButton = { androidx.compose.material3.TextButton(onClick = { offer = null }) {
                                Text("Later") } },
                        )
                    }
                },
            )
        },
        bottomBar = {
            // Dark bar, as on the web page and the tabs in the vault window.
            val bar = MaterialTheme.colorScheme.inverseSurface
            val onBar = MaterialTheme.colorScheme.inverseOnSurface
            NavigationBar(containerColor = bar) {
                TABS.forEachIndexed { index, item ->
                    NavigationBarItem(
                        selected = tab == index,
                        onClick = { tab = index },
                        icon = { Icon(item.icon, contentDescription = item.label) },
                        label = { Text(item.label, maxLines = 1, softWrap = false) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = onBar,
                            selectedTextColor = onBar,
                            indicatorColor = MaterialTheme.colorScheme.primary,
                            unselectedIconColor = onBar.copy(alpha = 0.55f),
                            unselectedTextColor = onBar.copy(alpha = 0.55f),
                        ),
                    )
                }
            }
        },
    ) { padding ->
        Surface(Modifier.padding(padding), color = MaterialTheme.colorScheme.background) {
            when (tab) {
                0 -> FilesScreen(model, state, onPickFile, onTakeOut)
                1 -> ChatScreen(model, state)
                2 -> TasksScreen(model, state)
                3 -> HealthScreen(model, state)
                else -> WatchScreen(model, state)
            }
        }
    }
}


/** A take-out the owner asked for: waiting for the confirm screen and, if needed, the PIN. */
data class TakeOut(val rel: String, val name: String, val sha: String?, val action: String)
