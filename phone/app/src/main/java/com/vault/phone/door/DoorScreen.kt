package com.vault.phone.door

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ClipboardManager
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONObject

private val STAMP = SimpleDateFormat("yy-MM-dd HH:mm:ss", Locale.US)

/** Integrity diagnostics have no event time; do not display the epoch as one. */
fun receiptTime(row: JSONObject): String {
    val timestamp = row.optLong("ts", -1L)
    if (row.optBoolean("receipt_error") || timestamp < 0) return "time unavailable"
    return synchronized(STAMP) { STAMP.format(Date(timestamp)) }
}

/**
 * The door, as the owner sees it: one switch, the address and key to pair
 * with, what is and is not on offer, and the phone's own log of every knock.
 */
@Composable
fun DoorScreen(onPairWithPc: (address: String, key: String) -> Unit = { _, _ -> }) {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val key = remember { DoorKey(context) }
    val receipts = remember { DoorReceipts(context) }

    var open by remember { mutableStateOf(key.isOpen) }
    var refresh by remember { mutableIntStateOf(0) }
    val rows = remember(refresh, open) { receipts.tail(80) }
    val address = remember(refresh, open) { Capabilities.tailscaleAddress() }

    val asker = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { refresh++ }

    fun has(permission: String) =
        androidx.core.content.ContextCompat.checkSelfPermission(context, permission) ==
            PackageManager.PERMISSION_GRANTED

    LazyColumn(
        Modifier.fillMaxSize().padding(horizontal = 14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Card(Modifier.fillMaxWidth(), colors = paper()) {
                Column(Modifier.padding(16.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("Agent door", style = MaterialTheme.typography.titleMedium)
                            Text(
                                if (open) "answering on ${address ?: "—"}:${DoorService.PORT}"
                                else "closed — nothing answers",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Switch(
                            checked = open,
                            onCheckedChange = { wanted ->
                                key.isOpen = wanted
                                open = wanted
                                if (wanted) DoorService.start(context) else DoorService.stop(context)
                                refresh++
                            },
                        )
                    }
                    Text(
                        "Reachable only from your own Tailscale devices, and only with the key " +
                            "below. Closing the switch closes the socket.",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 10.dp),
                    )
                    if (open && !Approval.canAsk(context)) {
                        Text(
                            "Notifications for this app are off, so the phone has no way to " +
                                "ask you. Anything reserved for your tap is refused until you " +
                                "turn them on.",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier.padding(top = 8.dp),
                        )
                    }
                    if (open && address == null) {
                        Text(
                            "Tailscale is not up, so the door has nothing safe to listen on.",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier.padding(top = 8.dp),
                        )
                    }
                }
            }
        }

        item {
            Card(Modifier.fillMaxWidth(), colors = paper()) {
                Column(Modifier.padding(16.dp)) {
                    Text("Key for the PC", style = MaterialTheme.typography.titleSmall)
                    Text(
                        "Send it straight to the vault over the connection this app already " +
                            "has. Nothing needs to be read off the screen or typed anywhere — " +
                            "that is the step where a key ends up somewhere it should not be.",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 4.dp),
                    )
                    OutlinedButton(
                        onClick = { onPairWithPc("http://$address:${DoorService.PORT}", key.value) },
                        enabled = address != null,
                        modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    ) { Text("Send the key to the PC", maxLines = 1) }

                    var revealed by remember { mutableStateOf(false) }
                    if (revealed) {
                        Text(
                            key.value,
                            fontFamily = FontFamily.Monospace,
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(vertical = 8.dp),
                        )
                    } else {
                        TextButton(
                            onClick = { revealed = true },
                            modifier = Modifier.padding(top = 4.dp),
                        ) { Text("Show the key") }
                    }
                    // Three buttons do not fit across a phone; the third one
                    // came out squeezed into a vertical pill. Two, then one.
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        OutlinedButton(
                            onClick = { copy(clipboard, key.value) },
                            modifier = Modifier.weight(1f),
                        ) { Text("Copy key", maxLines = 1, softWrap = false) }
                        OutlinedButton(
                            onClick = { copy(clipboard, "http://${address ?: "?"}:${DoorService.PORT}") },
                            modifier = Modifier.weight(1f),
                        ) { Text("Copy address", maxLines = 1, softWrap = false) }
                    }
                    OutlinedButton(
                        onClick = { key.rotate(); refresh++ },
                        modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    ) { Text("Replace the key", maxLines = 1) }
                    Text(
                        "Replacing it locks out the PC until you paste the new one there.",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                }
            }
        }

        // Only shown while it is true: an open window must never be something
        // the owner has to go looking for.
        if (InputSession.isOpen) {
            item {
                var left by remember { mutableIntStateOf(InputSession.secondsLeft.toInt()) }
                LaunchedEffect(Unit) {
                    while (InputSession.isOpen) {
                        left = InputSession.secondsLeft.toInt()
                        kotlinx.coroutines.delay(1000)
                    }
                    left = 0
                    refresh++
                }
                Card(
                    Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.primaryContainer,
                        contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
                    ),
                ) {
                    Column(Modifier.padding(16.dp)) {
                        Text("The agent is using the screen",
                             style = MaterialTheme.typography.titleSmall)
                        Text(
                            "${left / 60}m ${left % 60}s left · " +
                                "${InputSession.actionsThisWindow} taps so far · " +
                                "it cannot see what is on the screen",
                            style = MaterialTheme.typography.labelMedium,
                            modifier = Modifier.padding(top = 4.dp),
                        )
                        OutlinedButton(
                            onClick = { InputSession.close(); refresh++ },
                            modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
                        ) { Text("Stop now") }
                    }
                }
            }
        }

        item {
            Card(Modifier.fillMaxWidth(), colors = paper()) {
                Column(Modifier.padding(16.dp)) {
                    Text("What the agent may ask for", style = MaterialTheme.typography.titleSmall)
                    Tier("Answered straight away", "battery, whether you are on wifi and which network")
                    Tier("Only after you tap Allow here", "this phone's location — and the log never records where you were")
                    Tier("Does something, always logged",
                         "ring the phone; open an app — " +
                             if (Capabilities(context).canOpenDirectly()) "outright, and it is logged."
                             else "by leaving a notification, until you allow the vault to display " +
                                 "over other apps in Settings; then outright.")
                    Tier("Only inside a window you open",
                         "tapping and swiping on the screen, for five minutes at a time. " +
                             if (InputService.current() == null)
                                 "Off: switch it on in Settings, Accessibility, Vault."
                             else "On — and it still asks before each window.")
                    Tier("Not offered at all", "messages, chats and mail, banking screens, contacts, anything that turns security off")

                    val needsLocation = !has(Manifest.permission.ACCESS_FINE_LOCATION)
                    val needsNotify = android.os.Build.VERSION.SDK_INT >= 33 &&
                        !has(Manifest.permission.POST_NOTIFICATIONS)
                    if (needsLocation || needsNotify) {
                        Text(
                            buildString {
                                append("Android needs two permissions for this to work: ")
                                append("notifications, so the phone can ask you; and location, ")
                                append("which it also requires merely to name the wifi network. ")
                                append("Granting it does not take a fix — the door still asks first.")
                            },
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 10.dp),
                        )
                        OutlinedButton(
                            onClick = {
                                val wanted = buildList {
                                    if (needsLocation) add(Manifest.permission.ACCESS_FINE_LOCATION)
                                    if (needsNotify) add(Manifest.permission.POST_NOTIFICATIONS)
                                }
                                asker.launch(wanted.toTypedArray())
                            },
                            modifier = Modifier.padding(top = 6.dp),
                        ) { Text("Grant them") }
                    }
                }
            }
        }

        item {
            Row(
                Modifier.fillMaxWidth().padding(top = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "Every knock, in order" +
                        (if (receipts.verify() >= 0) " · chain intact" else " · CHAIN BROKEN"),
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                OutlinedButton(onClick = { refresh++ }) { Text("Refresh") }
            }
        }

        if (rows.isEmpty()) {
            item {
                Text(
                    "nothing has knocked yet",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(20.dp),
                )
            }
        }
        items(rows) { row -> ReceiptRow(row) }
        item { Text("", Modifier.padding(bottom = 20.dp)) }
    }
}

@Composable
private fun Tier(title: String, what: String) {
    Column(Modifier.padding(top = 10.dp)) {
        Text(title, style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
        Text(what, style = MaterialTheme.typography.labelMedium,
             color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ReceiptRow(row: JSONObject) {
    Card(Modifier.fillMaxWidth(), colors = paper()) {
        Column(Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
            Row {
                Text(row.optString("tool"), fontWeight = FontWeight.SemiBold)
                Text(
                    " · " + row.optString("outcome"),
                    color = when (row.optString("outcome")) {
                        "refused", "failed", "not started", "BROKEN" -> MaterialTheme.colorScheme.error
                        else -> MaterialTheme.colorScheme.primary
                    },
                )
            }
            val detail = row.optString("detail")
            Text(
                receiptTime(row) + " · " + row.optString("tier") +
                    (if (detail.isBlank()) "" else " · $detail"),
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun paper() = CardDefaults.cardColors(
    containerColor = MaterialTheme.colorScheme.surface,
    contentColor = MaterialTheme.colorScheme.onSurface,
)

private fun copy(clipboard: ClipboardManager, text: String) =
    clipboard.setText(AnnotatedString(text))

/** Start the door again after a reboot, but only if the owner had left it open. */
fun restoreDoorIfOpen(context: Context) {
    if (DoorKey(context).isOpen) DoorService.start(context)
}
