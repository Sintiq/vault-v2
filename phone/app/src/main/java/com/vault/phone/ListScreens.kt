package com.vault.phone

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.TextButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.launch

/** Tasks, health and the watch tab — three read-mostly lists, one file. */

@Composable
fun TasksScreen(model: VaultViewModel, state: VaultState) {
    LaunchedEffect(Unit) { model.loadTasks() }
    val open = state.tasks.count { !it.done }
    val proposals = remember { ProposalState<TaskProposals>() }
    var dating by remember { mutableStateOf<Task?>(null) }
    dating?.let { task ->
        DateChooser("Due date for “${task.title}”", task.due,
                    onPick = { dating = null; model.setTaskDue(task, it) },
                    onDismiss = { dating = null })
    }
    Column(Modifier.fillMaxSize()) {
        Note("$open open · ${state.tasks.size - open} done · every task quotes the document that says it")
        AgentButton("Find tasks in Staging", state.busy) {
            model.tasksPropose { proposals.show(it) }
        }
        if (proposals.open) {
            val p = proposals.payload ?: TaskProposals()
            ProposalSheet(
                title = "Tasks the documents mention",
                rows = p.proposals.map {
                    ProposalRow(it.id, it.title, if (it.due != null) "due " + it.due else "",
                                it.doc_name + " — " + it.quote, it.origin == "AGENT")
                },
                notes = p.notes,
                acceptLabel = "Add ticked",
                onAccept = { ids -> proposals.close(); model.tasksAdd(ids) },
                onDismiss = { proposals.close() },
            )
        }
        if (state.tasks.isEmpty()) {
            Empty("no tasks yet — press “Find tasks” to read what is in Staging")
            return@Column
        }
        LazyColumn(
            Modifier.fillMaxSize().padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            items(state.tasks, key = { it.id }) { task ->
                Card(Modifier.fillMaxWidth().clickable { model.setTaskDone(task, !task.done) },
                     colors = paperCard()) {
                    Row(Modifier.padding(end = 14.dp), verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(checked = task.done,
                                 onCheckedChange = { model.setTaskDone(task, it) })
                        Column(Modifier.weight(1f).padding(vertical = 10.dp)) {
                            Row {
                                Text(
                                    task.title,
                                    textDecoration = if (task.done) TextDecoration.LineThrough else null,
                                    color = if (task.done) MaterialTheme.colorScheme.onSurfaceVariant
                                            else MaterialTheme.colorScheme.onSurface,
                                )
                                task.due?.let {
                                    val left = DateText.remaining(task.days_left)
                                    Text(" · due $it" + (left?.let { l -> " ($l)" } ?: ""),
                                         color = if (task.overdue && !task.done) MaterialTheme.colorScheme.error
                                                 else MaterialTheme.colorScheme.primary,
                                         fontWeight = FontWeight.SemiBold)
                                }
                            }
                            DateNote(task.due_source, task.flags)
                            Quote("${task.doc} — “${task.quote}”")
                        }
                        TextButton(onClick = { dating = task }) {
                            Text(if (task.due == null) "Set date" else "Date")
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun HealthScreen(model: VaultViewModel, state: VaultState) {
    LaunchedEffect(Unit) { model.loadHealth() }
    val proposals = remember { ProposalState<HealthProposals>() }
    var dating by remember { mutableStateOf<HealthEntry?>(null) }
    dating?.let { entry ->
        DateChooser("Date for “${entry.label}”", entry.date,
                    onPick = { dating = null; model.setHealthDate(entry, it) },
                    onDismiss = { dating = null })
    }
    Column(Modifier.fillMaxSize()) {
        Note(
            state.health.summary.ifBlank { "nothing recorded yet" } +
                " · a reading of your own documents, not a medical record and not advice"
        )
        AgentButton("Read the documents in Staging", state.busy) {
            model.healthPropose { proposals.show(it) }
        }
        if (proposals.open) {
            val p = proposals.payload ?: HealthProposals()
            ProposalSheet(
                title = "What the documents state",
                rows = p.proposals.map {
                    ProposalRow(it.id, it.label, (it.date ?: "undated") + " · " + it.kind,
                                it.doc_name + " — " + it.quote, it.origin == "AGENT")
                },
                notes = p.notes,
                acceptLabel = "Add ticked",
                onAccept = { ids -> proposals.close(); model.healthAdd(ids) },
                onDismiss = { proposals.close() },
            )
        }
        if (state.health.years.isEmpty()) {
            Empty("nothing recorded yet — press “Read the documents” to read what is in Staging")
            return@Column
        }
        LazyColumn(
            Modifier.fillMaxSize().padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            state.health.years.forEach { year ->
                item(key = "year-${year.year}") {
                    Text(
                        year.year, style = MaterialTheme.typography.labelLarge,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(start = 6.dp, top = 12.dp, bottom = 2.dp),
                    )
                }
                items(year.entries, key = { it.id }) { entry ->
                    Card(Modifier.fillMaxWidth(), colors = paperCard()) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f).padding(14.dp)) {
                                Text(entry.label)
                                Text(
                                    "${entry.date ?: "undated"} · ${entry.kind}",
                                    style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                                DateNote(entry.due_source, entry.flags)
                                Quote("${entry.doc} — “${entry.quote}”")
                            }
                            TextButton(onClick = { dating = entry }) {
                                Text(if (entry.date == null) "Set date" else "Date")
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * The tab a watch will fill. Until then it shows what the phone itself
 * counts, so it says something true rather than "soon".
 */
@Composable
fun WatchScreen(model: VaultViewModel, state: VaultState) {
    val context = LocalContext.current
    val sensors = remember { BodySensors(context) }
    var reading by remember { mutableStateOf<BodySensors.Reading?>(null) }
    var asked by remember { mutableIntStateOf(0) }

    val permission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { asked++ }

    LaunchedEffect(asked) {
        model.loadMonitor()
        if (!sensors.needsPermission) sensors.readSteps { reading = it }
    }

    Column(Modifier.fillMaxSize()) {
        Note("the watch through Health Connect, and what this phone counts · read here, kept only when you add it to Health")
        LazyColumn(
            Modifier.fillMaxSize().padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            item { WatchCard(model) }
            item {
                Card(Modifier.fillMaxWidth(), colors = paperCard()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("Steps counted by this phone", style = MaterialTheme.typography.titleSmall)
                        when {
                            sensors.needsPermission -> {
                                Text(
                                    "Android needs the physical activity permission before the " +
                                        "phone will say how many steps it has counted.",
                                    style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                                OutlinedButton(
                                    onClick = {
                                        permission.launch(android.Manifest.permission.ACTIVITY_RECOGNITION)
                                    },
                                    modifier = Modifier.padding(top = 8.dp),
                                ) { Text("Allow it") }
                            }

                            reading?.stepsSinceBoot != null -> {
                                Text(
                                    "${reading!!.stepsSinceBoot}",
                                    style = MaterialTheme.typography.headlineMedium,
                                    modifier = Modifier.padding(vertical = 4.dp),
                                )
                                Text(reading!!.note, style = MaterialTheme.typography.labelMedium,
                                     color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }

                            else -> Text(
                                reading?.note ?: "reading…",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }
            }

        }
    }
}

/** One quiet button under the note: the agent works, the owner decides after. */
@Composable
fun AgentButton(label: String, busy: Boolean, onClick: () -> Unit) {
    OutlinedButton(
        onClick = onClick, enabled = !busy,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 2.dp),
    ) { Text(if (busy) "working…" else label, maxLines = 1) }
}

@Composable
private fun Quote(text: String) {
    Text(
        text, style = MaterialTheme.typography.labelMedium,
        fontStyle = FontStyle.Italic,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 3.dp),
    )
}

@Composable
private fun Empty(text: String) {
    Box(Modifier.fillMaxSize().padding(40.dp), contentAlignment = Alignment.TopCenter) {
        Text(text, style = MaterialTheme.typography.bodySmall,
             color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}


/** Where a date came from, and whether it needs the owner — in words. */
@Composable
private fun DateNote(dueSource: String, flags: List<String>) {
    val note = DateText.note(dueSource, flags) ?: return
    Text(
        note,
        style = MaterialTheme.typography.labelMedium,
        fontStyle = FontStyle.Italic,
        color = if (DateText.needsOwner(dueSource, flags)) MaterialTheme.colorScheme.error
                else MaterialTheme.colorScheme.onSurfaceVariant,
    )
}


/**
 * Today from the watch: Samsung Health writes it into Health Connect, this
 * reads it back. Asks Health Connect's own permission screen the first time.
 */
@Composable
private fun WatchCard(model: VaultViewModel) {
    val context = LocalContext.current
    val health = remember { HealthData(context) }
    var state by remember { mutableStateOf<HealthData.State?>(null) }
    var refresh by remember { mutableIntStateOf(0) }
    val ask = rememberLauncherForActivityResult(HealthData.permissionContract()) { refresh++ }
    val scope = rememberCoroutineScope()
    LaunchedEffect(refresh) { state = health.read() }

    // Yesterday whole and today so far, one press: the PC keeps one line per metric per day,
    // so pressing again tomorrow completes today rather than repeating it.
    fun addToHealth() = scope.launch {
        val zone = ZoneId.systemDefault()
        val today = LocalDate.now(zone)
        val days = ArrayList<WatchDay>()
        for (day in listOf(today.minusDays(1), today)) {
            when (val read = health.day(day, zone)) {
                // A day that cannot be read is not an empty day: send nothing, say why.
                is HealthData.Day.Unavailable -> { model.tell("Health Connect, $day: ${read.why} — nothing sent"); return@launch }
                is HealthData.Day.Ready -> if (!read.readings.isEmpty) days += WatchDay(day.toString(), read.readings)
            }
        }
        model.watchAdd(days)
    }

    Card(Modifier.fillMaxWidth(), colors = paperCard()) {
        Column(Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("From the watch", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
                TextButton(onClick = { refresh++ }) { Text("Refresh") }
            }
            when (val s = state) {
                null -> Hint("reading Health Connect…")
                HealthData.State.NotInstalled -> Hint("Health Connect is not installed on this phone.")
                HealthData.State.NeedsUpdate -> Hint("Health Connect needs an update in Google Play.")
                HealthData.State.NeedsPermission -> {
                    Hint("Vault needs permission to read steps, heart rate, sleep and blood oxygen. " +
                         "It only reads, and keeps nothing.")
                    OutlinedButton(onClick = { ask.launch(HealthData.PERMISSIONS) },
                                   modifier = Modifier.padding(top = 8.dp)) { Text("Allow reading") }
                }
                is HealthData.State.Failed -> Hint("Health Connect did not answer: ${s.why}")
                is HealthData.State.Ready -> {
                    val t = s.today
                    Metric("Steps today", t.steps?.toString())
                    Metric("Heart rate", t.lastHeart?.let { "$it bpm" },
                           listOfNotNull(HealthText.ago(t.lastHeartAt), HealthText.heart(t.heartMin, t.heartAvg, t.heartMax))
                               .joinToString(" · ").ifBlank { null })
                    Metric("Sleep last night", HealthText.duration(t.sleep),
                           t.sleepEnded?.let { "woke " + HealthText.ago(it) })
                    Metric("Blood oxygen", HealthText.oxygen(t.oxygen), HealthText.ago(t.oxygenAt))
                    if (listOf(t.steps, t.lastHeart, t.sleep, t.oxygen).all { it == null }) {
                        Hint("Nothing from the watch yet. Samsung Health copies readings into Health " +
                             "Connect every so often — wear the watch, open Samsung Health once, then Refresh.")
                    } else {
                        OutlinedButton(onClick = { addToHealth() }, modifier = Modifier.padding(top = 12.dp)) {
                            Text("Add to Health")
                        }
                        Hint("Writes yesterday and today into the Health timeline on the PC, one line per " +
                             "reading, with the watch named as the source. Nothing is sent until you press.")
                    }
                }
            }
        }
    }
}

@Composable
private fun Metric(label: String, value: String?, sub: String? = null) {
    Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.Bottom) {
        Text(label, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
        Text(value ?: "—", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
    }
    if (sub != null) Text(sub, style = MaterialTheme.typography.labelMedium,
                          color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun Hint(text: String) {
    Text(text, style = MaterialTheme.typography.labelMedium,
         color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 4.dp))
}
