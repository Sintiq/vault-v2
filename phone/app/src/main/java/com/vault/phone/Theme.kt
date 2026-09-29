package com.vault.phone

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

// The same warm paper and terracotta as the vault window on the PC, so the
// phone does not feel like a different product.
//
// Every container role is named on purpose: Material3 falls back to its own
// purple baseline for any role left unset, and cards, the navigation bar and
// tonal buttons all read from roles a short palette never mentions.

private val Accent = Color(0xFFD97757)
private val AccentSoftLight = Color(0xFFF6ECE7)
private val AccentSoftDark = Color(0xFF3A2B25)

private val Light = lightColorScheme(
    primary = Accent,
    onPrimary = Color.White,
    primaryContainer = AccentSoftLight,
    onPrimaryContainer = Color(0xFF5A2A18),
    secondary = Accent,
    onSecondary = Color.White,
    secondaryContainer = AccentSoftLight,
    onSecondaryContainer = Color(0xFF5A2A18),
    tertiary = Accent,
    onTertiary = Color.White,
    tertiaryContainer = AccentSoftLight,
    onTertiaryContainer = Color(0xFF5A2A18),
    background = Color(0xFFFAF9F5),
    onBackground = Color(0xFF2C2A26),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF2C2A26),
    surfaceVariant = Color(0xFFF1EEE6),
    onSurfaceVariant = Color(0xFF7D766A),
    surfaceTint = Color(0xFFFFFFFF),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFFFFFFF),
    surfaceContainer = Color(0xFFFFFFFF),
    surfaceContainerHigh = Color(0xFFF5F2EB),
    surfaceContainerHighest = Color(0xFFF1EEE6),
    inverseSurface = Color(0xFF262624),
    inverseOnSurface = Color(0xFFE9E6DF),
    outline = Color(0xFFD9D3C8),
    outlineVariant = Color(0xFFE7E3DA),
    error = Color(0xFFC0553F),
    onError = Color.White,
)

private val Dark = darkColorScheme(
    primary = Accent,
    onPrimary = Color.White,
    primaryContainer = AccentSoftDark,
    onPrimaryContainer = Color(0xFFF6ECE7),
    secondary = Accent,
    onSecondary = Color.White,
    secondaryContainer = AccentSoftDark,
    onSecondaryContainer = Color(0xFFF6ECE7),
    tertiary = Accent,
    onTertiary = Color.White,
    tertiaryContainer = AccentSoftDark,
    onTertiaryContainer = Color(0xFFF6ECE7),
    background = Color(0xFF1F1E1C),
    onBackground = Color(0xFFECE9E2),
    surface = Color(0xFF2A2927),
    onSurface = Color(0xFFECE9E2),
    surfaceVariant = Color(0xFF32302D),
    onSurfaceVariant = Color(0xFF9B948A),
    surfaceTint = Color(0xFF2A2927),
    surfaceContainerLowest = Color(0xFF171614),
    surfaceContainerLow = Color(0xFF242321),
    surfaceContainer = Color(0xFF2A2927),
    surfaceContainerHigh = Color(0xFF32302D),
    surfaceContainerHighest = Color(0xFF3B3934),
    inverseSurface = Color(0xFFECE9E2),
    inverseOnSurface = Color(0xFF262624),
    outline = Color(0xFF4A4741),
    outlineVariant = Color(0xFF3B3934),
    error = Color(0xFFE08270),
    onError = Color(0xFF2C2A26),
)

@Composable
fun VaultTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    val scheme = if (dark) Dark else Light
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = !dark
        }
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
