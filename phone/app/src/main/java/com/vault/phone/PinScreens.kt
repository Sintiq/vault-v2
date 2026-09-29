package com.vault.phone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp

@Composable
private fun PinField(value: String, label: String, onChange: (String) -> Unit) {
    OutlinedTextField(
        value = value,
        onValueChange = { v -> onChange(v.filter { it in '0'..'9' }.take(PinPolicy.MAX)) },
        label = { Text(label) },
        singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
        modifier = Modifier.fillMaxWidth(),
    )
}

/** First launch after pairing: choose the PIN that opens Vault on this phone. */
@Composable
fun SetPinScreen(working: Boolean, message: String?, onSet: (String, String) -> Unit) {
    var first by remember { mutableStateOf("") }
    var second by remember { mutableStateOf("") }
    Column(
        Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp, Alignment.CenterVertically),
    ) {
        Text("Choose a PIN for Vault", style = MaterialTheme.typography.headlineSmall)
        Text(
            "6 to 12 digits, different from the phone's own unlock code. Vault asks for it when it " +
                "opens and before a file leaves it. Nine wrong PINs in a row erase Vault on this " +
                "phone — the documents on the PC are not touched, and you pair again from the PC.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        PinField(first, "PIN") { first = it }
        PinField(second, "PIN again") { second = it }
        Button(
            onClick = { onSet(first, second) },
            enabled = !working && PinPolicy.valid(first) && second.isNotEmpty(),
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (working) "Saving…" else "Set PIN") }
        if (message != null) Text(message, color = MaterialTheme.colorScheme.error,
                                  style = MaterialTheme.typography.bodySmall)
    }
}

/** Vault is locked: the PIN opens it. */
@Composable
fun UnlockScreen(working: Boolean, message: String?, warning: String?, onUnlock: (String) -> Unit) {
    var pin by remember { mutableStateOf("") }
    Column(
        Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp, Alignment.CenterVertically),
    ) {
        Text("Vault is locked", style = MaterialTheme.typography.headlineSmall)
        PinField(pin, "PIN") { pin = it }
        Button(
            onClick = { onUnlock(pin); pin = "" },
            enabled = !working && pin.length >= PinPolicy.MIN,
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (working) "Checking…" else "Open") }
        if (warning != null) Text(warning, color = MaterialTheme.colorScheme.error,
                                  style = MaterialTheme.typography.bodyMedium)
        if (message != null) Text(message, color = MaterialTheme.colorScheme.onSurfaceVariant,
                                  style = MaterialTheme.typography.bodySmall)
    }
}

/** Before a file leaves Vault: the same PIN again, good for five minutes in this session. */
@Composable
fun ExportPinDialog(working: Boolean, message: String?, warning: String?,
                    onConfirm: (String) -> Unit, onCancel: () -> Unit) {
    var pin by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onCancel,
        title = { Text("PIN to take files out") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Good for five minutes while Vault stays open.",
                     style = MaterialTheme.typography.bodySmall)
                PinField(pin, "PIN") { pin = it }
                if (warning != null) Text(warning, color = MaterialTheme.colorScheme.error,
                                          style = MaterialTheme.typography.bodySmall)
                if (message != null) Text(message, style = MaterialTheme.typography.bodySmall)
            }
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(pin); pin = "" },
                       enabled = !working && pin.length >= PinPolicy.MIN) { Text("OK") }
        },
        dismissButton = { TextButton(onClick = onCancel) { Text("Cancel") } },
    )
}
