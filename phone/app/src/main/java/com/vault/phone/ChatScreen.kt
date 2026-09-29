package com.vault.phone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * The same conversation as the Agent pane on the PC — send from the sofa,
 * read the answer at the desk. The agent still sees only the Staging listing;
 * that limit is enforced on the PC, not here.
 */
@Composable
fun ChatScreen(model: VaultViewModel, state: VaultState) {
    var draft by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    LaunchedEffect(Unit) { model.loadChat() }
    LaunchedEffect(state.chat.history.size) {
        if (state.chat.history.isNotEmpty()) listState.animateScrollToItem(state.chat.history.lastIndex)
    }

    Column(Modifier.fillMaxSize()) {
        Note(
            (state.chat.backend.ifBlank { "not connected" }) + " · the agent sees only Staging"
        )
        LazyColumn(
            state = listState,
            modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            if (state.chat.history.isEmpty()) {
                item {
                    Box(Modifier.fillMaxWidth().padding(40.dp), contentAlignment = Alignment.Center) {
                        Text("Same conversation as the vault window on the PC.",
                             style = MaterialTheme.typography.bodySmall,
                             color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
            items(state.chat.history) { message -> Bubble(message) }
            if (state.busy) {
                item { Bubble(Message("assistant", "…")) }
            }
        }
        Row(
            Modifier.fillMaxWidth().padding(10.dp),
            verticalAlignment = Alignment.Bottom,
        ) {
            OutlinedTextField(
                value = draft, onValueChange = { draft = it },
                placeholder = { Text("Ask about the files in Staging…") },
                modifier = Modifier.weight(1f),
                maxLines = 4,
            )
            IconButton(
                onClick = { model.send(draft.trim()); draft = "" },
                enabled = draft.isNotBlank() && !state.busy,
            ) {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send",
                     tint = MaterialTheme.colorScheme.primary)
            }
        }
    }
}

@Composable
private fun Bubble(message: Message) {
    val mine = message.role == "user"
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start,
    ) {
        Surface(
            color = if (mine) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surface,
            contentColor = if (mine) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface,
            shape = RoundedCornerShape(
                topStart = 16.dp, topEnd = 16.dp,
                bottomStart = if (mine) 16.dp else 4.dp,
                bottomEnd = if (mine) 4.dp else 16.dp,
            ),
            modifier = Modifier.widthIn(max = 300.dp),
        ) {
            Text(message.content, modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                 style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
fun Note(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 10.dp),
    )
}
