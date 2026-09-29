package com.vault.phone

import androidx.compose.foundation.clickable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.SubdirectoryArrowLeft
import androidx.compose.material3.Card
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Button
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

/**
 * Three panes cannot stand side by side on a phone, so they are pages you
 * swipe between. Moving a file is an action on the row rather than a drag:
 * a drag across pages is a gesture nobody wins.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FilesScreen(model: VaultViewModel, state: VaultState, onPickFile: () -> Unit,
                onTakeOut: (rel: String, name: String, sha256: String?, action: String) -> Unit = { _, _, _, _ -> }) {
    val pager = rememberPagerState(pageCount = { PANES.size })
    val pane = PANES[pager.currentPage]
    var sheetFor by remember { mutableStateOf<Entry?>(null) }
    var reading by remember { mutableStateOf<FileText?>(null) }
    // The shelf picker: which file, and the list the PC answered with.
    var shelfFor by remember { mutableStateOf<Pair<Entry, Shelves>?>(null) }
    var viewing by remember { mutableStateOf<Pair<String, Rendered>?>(null) }
    val sortSheet = remember { ProposalState<SortProposals>() }
    var asking by remember { mutableStateOf(false) }
    var askPhrase by remember { mutableStateOf("") }
    var askAnswer by remember { mutableStateOf<AskAnswer?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(pane) { model.loadPane(pane) }
    LaunchedEffect(Unit) { model.loadOffline() }
    var onPhone by remember { mutableStateOf(false) }

    Box(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize()) {
            // The selector has to move the pager, not just fetch the pane:
            // loading Documents while the page still shows Staging looks
            // exactly like a button that does nothing.
            PaneSelector(pager.currentPage) { index ->
                scope.launch { pager.animateScrollToPage(index) }
            }
            if (pane == "staging") {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    OutlinedButton(onClick = { model.sortPropose { sortSheet.show(it) } },
                                   enabled = !state.busy, modifier = Modifier.weight(1f)) {
                        Text("Sort…", maxLines = 1)
                    }
                    OutlinedButton(onClick = { asking = true },
                                   enabled = !state.busy, modifier = Modifier.weight(1f)) {
                        Text("Ask…", maxLines = 1)
                    }
                }
            }
            if (state.offline.isNotEmpty()) {
                TextButton(onClick = { onPhone = true }, modifier = Modifier.padding(start = 8.dp)) {
                    Text("On this phone (${state.offline.size})")
                }
            }
            val rel = state.rels[pane].orEmpty()
            Text(
                "/" + PANE_TITLES[pane] + if (rel.isEmpty()) "" else "/$rel",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(start = 20.dp, bottom = 6.dp),
            )
            HorizontalPager(state = pager, modifier = Modifier.fillMaxSize()) { page ->
                val p = PANES[page]
                PaneList(
                    listing = state.listings[p],
                    atRoot = state.rels[p].orEmpty().isEmpty(),
                    onUp = { model.leave(p) },
                    onOpen = { entry -> if (entry.dir) model.enter(p, entry) else sheetFor = entry },
                    onMore = { entry -> sheetFor = entry },
                )
            }
        }
        FloatingActionButton(
            onClick = onPickFile,
            containerColor = MaterialTheme.colorScheme.primary,
            contentColor = MaterialTheme.colorScheme.onPrimary,
            modifier = Modifier.align(Alignment.BottomEnd).padding(20.dp),
        ) { Icon(Icons.Filled.Add, contentDescription = "Upload into Staging") }
    }

    sheetFor?.let { entry ->
        ModalBottomSheet(onDismissRequest = { sheetFor = null }) {
            EntryActions(
                entry = entry, pane = pane,
                onOpen = {
                    sheetFor = null
                    if (entry.view == "TEXT") model.readText(pane, entry) { reading = it }
                    else model.view(pane, entry) { viewing = entry.name to it }
                },
                onSetShelf = {
                    sheetFor = null
                    model.loadShelves { shelfFor = entry to it }
                },
                onTransfer = { to, move -> sheetFor = null; model.transfer(pane, entry, to, move) },
                onTrash = { sheetFor = null; model.trash(pane, entry) },
                online = state.link == Link.GREEN,
                onOffline = { sheetFor = null; model.makeOffline(entry) },
                onSave = { sheetFor = null; onTakeOut(entry.rel, entry.name, null, "save") },
                onShare = { sheetFor = null; onTakeOut(entry.rel, entry.name, null, "share") },
            )
        }
    }

    if (onPhone) {
        ModalBottomSheet(onDismissRequest = { onPhone = false }) {
            OnThisPhone(
                copies = state.offline, online = state.link == Link.GREEN,
                onOpen = { copy ->
                    onPhone = false
                    model.openOffline(copy, onText = { reading = it }, onRendered = { viewing = copy.name to it })
                },
                onSave = { copy -> onPhone = false; onTakeOut(copy.rel, copy.name, copy.sha256, "save") },
                onShare = { copy -> onPhone = false; onTakeOut(copy.rel, copy.name, copy.sha256, "share") },
                onRemove = { copy -> model.removeOffline(copy) },
            )
        }
    }

    reading?.let { doc ->
        ModalBottomSheet(onDismissRequest = { reading = null }) {
            // The whole document, scrolled — not the first screenful.
            Column(Modifier.padding(horizontal = 20.dp).verticalScroll(rememberScrollState())) {
                Text(doc.name, style = MaterialTheme.typography.titleMedium)
                if (doc.truncated) {
                    Text("very large file — only the first 2 MB is shown",
                         style = MaterialTheme.typography.labelMedium,
                         color = MaterialTheme.colorScheme.error,
                         modifier = Modifier.padding(top = 4.dp))
                }
                Text(doc.text, style = MaterialTheme.typography.bodySmall,
                     modifier = Modifier.padding(top = 12.dp, bottom = 32.dp))
            }
        }
    }

    shelfFor?.let { (entry, list) ->
        ModalBottomSheet(onDismissRequest = { shelfFor = null }) {
            Column(Modifier.padding(bottom = 24.dp).verticalScroll(rememberScrollState())) {
                Text("Shelf for ${entry.name}", style = MaterialTheme.typography.titleSmall,
                     maxLines = 1, overflow = TextOverflow.Ellipsis,
                     modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
                list.shelves.forEach { shelf ->
                    val current = shelf == entry.shelf
                    SheetAction(
                        shelf + (if (current) "  ✓" else "") + (if (shelf !in list.standard) "  · yours" else ""),
                    ) {
                        shelfFor = null
                        if (!current || entry.confirmed != true) model.setShelf(pane, entry, shelf)
                    }
                }
            }
        }
    }

    if (sortSheet.open) {
        val p = sortSheet.payload ?: SortProposals()
        ProposalSheet(
            title = "Cards for what is in Staging",
            rows = p.proposals.map {
                val sub = it.shelf +
                    (if (it.differs) "  (keywords said " + it.baseline_shelf + ")" else "") +
                    (if (it.topics.isNotEmpty()) " · " + it.topics.joinToString(", ") else "")
                ProposalRow(it.id, it.name, sub, it.reason, it.origin == "AGENT")
            },
            notes = p.notes,
            acceptLabel = "Confirm ticked",
            onAccept = { ids -> sortSheet.close(); model.sortConfirm(ids.map { SortAccept(it) }) },
            onDismiss = { sortSheet.close() },
        )
    }

    if (asking) {
        ModalBottomSheet(onDismissRequest = { asking = false; askAnswer = null }) {
            Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                Text("Ask which documents match", style = MaterialTheme.typography.titleMedium)
                Text("Search only — exporting a pack stays at the desk.",
                     style = MaterialTheme.typography.labelMedium,
                     color = MaterialTheme.colorScheme.onSurfaceVariant)
                OutlinedTextField(
                    value = askPhrase, onValueChange = { askPhrase = it },
                    placeholder = { Text("e.g. everything for the doctor") },
                    singleLine = true, modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                )
                Button(
                    onClick = { model.ask(askPhrase) { askAnswer = it } },
                    enabled = askPhrase.isNotBlank() && !state.busy,
                    modifier = Modifier.padding(top = 8.dp),
                ) { Text(if (state.busy) "asking…" else "Ask") }
                val a = askAnswer
                Column(Modifier.padding(top = 12.dp, bottom = 24.dp)) {
                    if (a != null) {
                        if (a.matches.isEmpty()) Text("no document matched",
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        a.matches.forEach { m ->
                            Text("• " + m.name + (if (m.shelf != null) "  · " + m.shelf else "") +
                                (if (m.added_by_agent) "  [added by agent]" else "") +
                                (if (m.omitted_by_agent) "  [omitted by agent]" else ""))
                            // Ask searches the whole archive now: say where each one lies.
                            Text("   " + m.where, style = MaterialTheme.typography.labelMedium,
                                 color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        if (a.matches.any { it.pane != "staging" }) Text(
                            "to send these, copy them to Staging at the desk (Ask → Copy to Staging)",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 6.dp))
                        if (a.recipient != null) Text("recipient: " + a.recipient,
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.padding(top = 6.dp))
                        if (a.notes.isNotEmpty()) Text(a.notes.joinToString(" · "),
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 4.dp))
                    }
                }
            }
        }
    }

    // A scan wants the whole screen, not a sheet peeking from the bottom.
    viewing?.let { (name, rendered) ->
        ModalBottomSheet(
            onDismissRequest = { viewing = null },
            sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        ) {
            DocumentViewer(name, rendered)
        }
    }
}

@Composable
private fun PaneSelector(current: Int, onPick: (Int) -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        PANES.forEachIndexed { index, pane ->
            // "Documents" is the longest label and must not be clipped to
            // "Docume" on a narrow phone: no button padding, no wrapping.
            val label: @Composable () -> Unit = {
                Text(
                    PANE_TITLES[pane].orEmpty(),
                    style = MaterialTheme.typography.labelLarge,
                    maxLines = 1, softWrap = false,
                    color = if (index == current) MaterialTheme.colorScheme.onSecondaryContainer
                            else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            val shape = MaterialTheme.shapes.medium
            if (index == current) {
                FilledTonalButton(
                    onClick = { onPick(index) }, modifier = Modifier.weight(1f),
                    shape = shape, contentPadding = PaddingValues(horizontal = 2.dp, vertical = 8.dp),
                ) { label() }
            } else {
                OutlinedButton(
                    onClick = { onPick(index) }, modifier = Modifier.weight(1f),
                    shape = shape, contentPadding = PaddingValues(horizontal = 2.dp, vertical = 8.dp),
                ) { label() }
            }
        }
    }
}

@Composable
private fun PaneList(
    listing: Listing?,
    atRoot: Boolean,
    onUp: () -> Unit,
    onOpen: (Entry) -> Unit,
    onMore: (Entry) -> Unit,
) {
    if (listing == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("…", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        return
    }
    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 12.dp),
        verticalArrangement = Arrangement.spacedBy(7.dp),
    ) {
        if (!atRoot) {
            item {
                Card(Modifier.fillMaxWidth().clickable(onClick = onUp), colors = paperCard()) {
                    Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Filled.SubdirectoryArrowLeft, contentDescription = "Up",
                             modifier = Modifier.size(20.dp))
                        Text("..", modifier = Modifier.padding(start = 12.dp))
                    }
                }
            }
        }
        if (listing.items.isEmpty()) {
            item {
                Box(Modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
                    Text("empty", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
        items(listing.items, key = { it.rel }) { entry ->
            EntryRow(entry, onClick = { onOpen(entry) }, onMore = { onMore(entry) })
        }
    }
}

@Composable
private fun EntryRow(entry: Entry, onClick: () -> Unit, onMore: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(onClick = onClick), colors = paperCard()) {
        Row(Modifier.padding(start = 14.dp, top = 10.dp, bottom = 10.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Icon(
                if (entry.dir) Icons.Filled.Folder else Icons.Filled.Description,
                contentDescription = null, modifier = Modifier.size(22.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Column(Modifier.weight(1f).padding(horizontal = 12.dp)) {
                Text(entry.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Row {
                    entry.shelfLabel?.let {
                        Text("$it · ", style = MaterialTheme.typography.labelMedium,
                             color = MaterialTheme.colorScheme.primary,
                             fontWeight = FontWeight.SemiBold)
                    }
                    Text(
                        if (entry.dir) modified(entry) else "${humanSize(entry.size)} · ${modified(entry)}",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            IconButton(onClick = onMore) {
                Icon(Icons.Filled.MoreVert, contentDescription = "Actions",
                     tint = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun EntryActions(
    entry: Entry,
    pane: String,
    onOpen: () -> Unit,
    onSetShelf: () -> Unit,
    onTransfer: (String, Boolean) -> Unit,
    onTrash: () -> Unit,
    online: Boolean = false,
    onOffline: () -> Unit = {},
    onSave: () -> Unit = {},
    onShare: () -> Unit = {},
) {
    Column(Modifier.fillMaxWidth().padding(bottom = 24.dp)) {
        Text(entry.name, style = MaterialTheme.typography.titleSmall,
             maxLines = 1, overflow = TextOverflow.Ellipsis,
             modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        if (!entry.dir && entry.view != "NONE") {
            SheetAction(
                when (entry.view) {
                    "PDF" -> "Open the scan"
                    "IMAGE" -> "Open the picture"
                    else -> "Open"
                }
            ) { onOpen() }
        }
        if (!entry.dir) {
            SheetAction("Set shelf…" + (entry.shelfLabel?.let { "  (now $it)" } ?: "")) { onSetShelf() }
        }
        if (!entry.dir && pane == "staging") {
            if (online) {
                SheetAction("Available offline") { onOffline() }
                SheetAction("Save a copy to Files…") { onSave() }
                SheetAction("Share…") { onShare() }
            } else {
                Text("Copies to this phone need the PC online (green).",
                     style = MaterialTheme.typography.labelMedium,
                     color = MaterialTheme.colorScheme.onSurfaceVariant,
                     modifier = Modifier.padding(horizontal = 34.dp, vertical = 6.dp))
            }
        }
        PANES.filter { it != pane }.forEach { target ->
            SheetAction("Copy to ${PANE_TITLES[target]}") { onTransfer(target, false) }
            SheetAction("Move to ${PANE_TITLES[target]}") { onTransfer(target, true) }
        }
        SheetAction("Move to Trash", danger = true) { onTrash() }
    }
}

/** Files kept on this phone, inside Vault. Readable without the PC; taking out needs it. */
@Composable
private fun OnThisPhone(
    copies: List<OfflineCopy>, online: Boolean,
    onOpen: (OfflineCopy) -> Unit, onSave: (OfflineCopy) -> Unit,
    onShare: (OfflineCopy) -> Unit, onRemove: (OfflineCopy) -> Unit,
) {
    Column(Modifier.fillMaxWidth().padding(bottom = 24.dp)) {
        Text("On this phone", style = MaterialTheme.typography.titleSmall,
             modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        Text("Kept inside Vault, under its PIN, erased with it. Opening needs no PC; " +
                 "taking a copy out needs the PC online.",
             style = MaterialTheme.typography.labelMedium,
             color = MaterialTheme.colorScheme.onSurfaceVariant,
             modifier = Modifier.padding(horizontal = 24.dp))
        copies.forEach { copy ->
            Text(copy.name + "  ·  " + humanSize(copy.size), style = MaterialTheme.typography.bodyMedium,
                 modifier = Modifier.padding(start = 24.dp, top = 12.dp))
            Row(Modifier.padding(horizontal = 12.dp)) {
                TextButton(onClick = { onOpen(copy) }) { Text("Open") }
                TextButton(onClick = { onSave(copy) }, enabled = online) { Text("Save…") }
                TextButton(onClick = { onShare(copy) }, enabled = online) { Text("Share…") }
                TextButton(onClick = { onRemove(copy) }) {
                    Text("Remove", color = MaterialTheme.colorScheme.error)
                }
            }
        }
    }
}

@Composable
private fun SheetAction(label: String, danger: Boolean = false, onClick: () -> Unit) {
    TextButton(onClick = onClick, modifier = Modifier.fillMaxWidth()) {
        Text(
            label,
            modifier = Modifier.fillMaxWidth().padding(start = 10.dp),
            color = if (danger) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
        )
    }
}

/** A card the colour of paper, as in the vault window — not Material's tinted default. */
@Composable
fun paperCard() = CardDefaults.cardColors(
    containerColor = MaterialTheme.colorScheme.surface,
    contentColor = MaterialTheme.colorScheme.onSurface,
)

fun humanSize(bytes: Long): String = when {
    bytes < 1024 -> "$bytes B"
    bytes < 1024 * 1024 -> "${bytes / 1024} KB"
    else -> "${bytes / (1024 * 1024)} MB"
}

// The same stamp the vault window shows, so a file reads the same on both screens.
private val STAMP = java.text.SimpleDateFormat("yy-MM-dd HH:mm", java.util.Locale.US)

private fun modified(entry: Entry): String = STAMP.format(java.util.Date(entry.modified * 1000))
