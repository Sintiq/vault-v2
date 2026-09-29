package com.vault.phone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/** Waiting for the owner's Confirm at the PC, with the digits both screens must show. */
@Composable
private fun PairingPanel(code: String?, onCancel: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Pairing with the PC", style = MaterialTheme.typography.headlineSmall)
        if (code == null) {
            Text("Asking the PC…", style = MaterialTheme.typography.bodyMedium)
        } else {
            Text(code, fontSize = 40.sp, style = MaterialTheme.typography.displaySmall)
            Text("Check that the PC window shows the same six digits, then press Confirm there.",
                 style = MaterialTheme.typography.bodyMedium,
                 color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        TextButton(onClick = onCancel) { Text("Cancel") }
    }
}

/**
 * The connect screen. Shown until the phone has an address and a key that the
 * vault actually answers to — a wrong key never gets past this point.
 */
@Composable
fun ConnectScreen(model: VaultViewModel, message: String?, onConnected: () -> Unit) {
    var address by remember { mutableStateOf("") }
    var key by remember { mutableStateOf("") }
    var trying by remember { mutableStateOf(false) }

    val state by model.state.collectAsStateWithLifecycle()
    if (state.pairing) {
        PairingPanel(state.pairingCode) { model.cancelPairing() }
        return
    }

    Column(
        modifier = Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp, Alignment.CenterVertically),
    ) {
        Text("Connect to your vault", style = MaterialTheme.typography.headlineSmall)
        Text(
            "On the PC press Phone… → Add phone…, then scan the QR with this phone's camera. " +
                "Or paste the pairing link from that window into the address field.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedTextField(
            value = address, onValueChange = { address = it },
            label = { Text("Address") },
            placeholder = { Text("https://your-pc.example.ts.net") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = key, onValueChange = { key = it },
            label = { Text("Key (old shared key only)") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Button(
            onClick = {
                val pair = PairLink.parse(address)
                if (pair != null) {
                    model.startPairing(pair)
                } else {
                    trying = true
                    model.connect(address, key) { ok ->
                        trying = false
                        if (ok) onConnected()
                    }
                }
            },
            enabled = !trying && address.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (trying) "Connecting…" else "Connect") }

        if (message != null) {
            Text(message, color = MaterialTheme.colorScheme.error,
                 style = MaterialTheme.typography.bodySmall)
        }
        TextButton(onClick = { model.forget() }) { Text("Clear saved connection") }
        Text(
            "The vault answers only inside your Tailscale network. Turn Tailscale on before connecting.",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
