package com.droidmonitor.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val DroidMonitorColors = darkColorScheme(
    primary = Color(0xFF38BDF8),
    secondary = Color(0xFF0EA5E9),
    tertiary = Color(0xFFEC5E00), // laranja do logo NuDuck, usado em destaques pontuais
    background = Color(0xFF0F172A),
    surface = Color(0xFF0B0B0B), // preto do topo/menus, igual aos mockups
    onPrimary = Color.Black,
    onBackground = Color.White,
    onSurface = Color.White,
)

@Composable
fun DroidMonitorTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DroidMonitorColors,
        content = content,
    )
}
