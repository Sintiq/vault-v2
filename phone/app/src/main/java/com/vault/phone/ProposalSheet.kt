package com.vault.phone

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/**
 * What the agent proposes, for the owner to tick.
 *
 * The same shape serves cards, tasks and health entries: a headline, a line
 * under it, and the quote or reason the proposal rests on. Ticking is the
 * whole point — nothing here is written until "Add ticked".
 */
data class ProposalRow(
    val id: String,
    val title: String,
    val sub: String,
    val quote: String,
    val fromAgent: Boolean,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProposalSheet(
    title: String,
    rows: List<ProposalRow>,
    notes: List<String>,
    acceptLabel: String,
    onAccept: (List<String>) -> Unit,
    onDismiss: () -> Unit,
) {
    // The agent's own proposals start ticked; keyword fallbacks start clear,
    // the same convention as the dialogs on the PC.
    val ticked = remember(rows) { mutableStateListOf(*rows.map { it.fromAgent }.toTypedArray()) }
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            if (notes.isNotEmpty()) {
                Text(
                    notes.joinToString(" · "),
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
            if (rows.isEmpty()) {
                Text(
                    "nothing new to propose",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(vertical = 24.dp),
                )
            }
            LazyColumn(
                Modifier.fillMaxWidth().heightIn(max = 480.dp).padding(top = 8.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                itemsIndexed(rows, key = { _, r -> r.id }) { i, row ->
                    Card(
                        Modifier.fillMaxWidth().clickable { ticked[i] = !ticked[i] },
                        colors = paperCard(),
                    ) {
                        Row(Modifier.padding(end = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(checked = ticked[i], onCheckedChange = { ticked[i] = it })
                            Column(Modifier.weight(1f).padding(vertical = 8.dp)) {
                                Row {
                                    Text(row.title, fontWeight = FontWeight.SemiBold)
                                    Text(
                                        if (row.fromAgent) "  · agent" else "  · keywords",
                                        style = MaterialTheme.typography.labelMedium,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                                if (row.sub.isNotBlank()) {
                                    Text(row.sub, style = MaterialTheme.typography.labelMedium,
                                         color = MaterialTheme.colorScheme.primary)
                                }
                                if (row.quote.isNotBlank()) {
                                    Text(
                                        "“${row.quote}”",
                                        style = MaterialTheme.typography.labelMedium,
                                        fontStyle = FontStyle.Italic,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                            }
                        }
                    }
                }
            }
            Row(
                Modifier.fillMaxWidth().padding(top = 12.dp, bottom = 20.dp),
                horizontalArrangement = Arrangement.End,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                TextButton(onClick = onDismiss) { Text("Not now") }
                Button(
                    onClick = { onAccept(rows.indices.filter { ticked[it] }.map { rows[it].id }) },
                    enabled = rows.isNotEmpty() && ticked.any { it },
                    modifier = Modifier.padding(start = 8.dp),
                ) { Text(acceptLabel) }
            }
        }
    }
}

/** A tiny state holder so a screen can open the sheet from a button. */
class ProposalState<T> {
    var open by mutableStateOf(false)
    var payload by mutableStateOf<T?>(null)
    fun show(p: T) { payload = p; open = true }
    fun close() { open = false; payload = null }
}
