package com.vault.phone

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * The page Health Connect opens when the owner asks why Vault wants to read
 * his health data. Android 13 will not show the permission prompt at all if
 * an app has no such page, so it is small but required.
 */
class HealthRationaleActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            VaultTheme {
                Surface(color = MaterialTheme.colorScheme.background) {
                    Column(Modifier.padding(24.dp)) {
                        Text("Why Vault reads Health Connect", style = MaterialTheme.typography.titleLarge)
                        Text(
                            "Vault shows today's steps, heart rate, last night's sleep and blood oxygen " +
                                "from your watch on its Watch tab. It only reads — it never writes to " +
                                "Health Connect — and it does not store these numbers or send them " +
                                "anywhere, not even to your PC. You can remove the permission at any " +
                                "time in Health Connect.",
                            modifier = Modifier.padding(top = 16.dp),
                        )
                    }
                }
            }
        }
    }
}
