package com.droidmonitor

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.PointerEventType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.droidmonitor.discovery.PcInfo
import com.droidmonitor.ui.FloatingMenuHost
import com.droidmonitor.ui.QrScanScreen
import com.droidmonitor.ui.theme.DroidMonitorTheme
import com.droidmonitor.webrtc.ControlEvents
import org.webrtc.RendererCommon
import org.webrtc.SurfaceViewRenderer
import org.webrtc.VideoTrack

private val QUALITY_LABELS = linkedMapOf(
    "144p" to "144p",
    "240p" to "240p",
    "360p" to "360p",
    "480p" to "480p",
    "720p" to "720p",
    "1080p" to "1080p",
    "auto" to "Automático",
)

class MainActivity : ComponentActivity() {

    private val viewModel: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            DroidMonitorTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    DroidMonitorApp(viewModel)
                }
            }
        }
    }
}

@Composable
fun DroidMonitorApp(viewModel: MainViewModel) {
    val uiState by viewModel.uiState.collectAsState()

    when (val screen = uiState.screen) {
        is Screen.Discovery -> DiscoveryScreen(
            pcs = uiState.discoveredPcs,
            onPcSelected = viewModel::selectPc,
            onQrScanRequested = viewModel::openQrScan,
            onCableConnectRequested = viewModel::connectViaCable,
        )

        is Screen.QrScan -> QrScanScreen(
            onScanned = viewModel::onQrCodeScanned,
            onCancel = viewModel::cancelQrScan,
        )

        is Screen.PinEntry -> PinEntryScreen(
            pc = screen.pc,
            pinError = uiState.pinError,
            onCancel = viewModel::cancelPinEntry,
            onSubmit = { pin -> viewModel.submitPin(screen.pc, pin) },
        )

        is Screen.Connecting -> ConnectingScreen()

        is Screen.Connected -> ConnectedScreen(viewModel = viewModel, quality = uiState.quality)

        is Screen.ConnectionError -> ConnectionErrorScreen(
            message = screen.message,
            onDismiss = viewModel::disconnect,
        )
    }
}

@Composable
fun DiscoveryScreen(
    pcs: List<PcInfo>,
    onPcSelected: (PcInfo) -> Unit,
    onQrScanRequested: () -> Unit,
    onCableConnectRequested: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(20.dp),
    ) {
        // Cabeçalho escuro com o logo branco do NuDuck, igual ao mockup "inicio.svg".
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(20.dp))
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 20.dp, vertical = 18.dp),
        ) {
            Image(
                painter = painterResource(id = R.drawable.ic_nuduck_logo),
                contentDescription = null,
                modifier = Modifier.size(width = 28.dp, height = 32.dp),
            )
            Spacer(modifier = Modifier.size(14.dp))
            Text(
                text = "NuDuck",
                style = MaterialTheme.typography.headlineSmall,
                color = Color.White,
            )
        }

        Spacer(modifier = Modifier.height(16.dp))

        // Formas alternativas de conectar: ler o QR Code do PC ou usar cabo USB
        // (com "adb forward tcp:8765 tcp:8765" já rodando).
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = onQrScanRequested, modifier = Modifier.weight(1f)) {
                Text("QR Code")
            }
            OutlinedButton(onClick = onCableConnectRequested, modifier = Modifier.weight(1f)) {
                Text("Via cabo (USB)")
            }
        }

        Spacer(modifier = Modifier.height(20.dp))

        Text(
            text = "PCs encontrados:",
            style = MaterialTheme.typography.titleMedium,
            color = Color.White,
        )
        Spacer(modifier = Modifier.height(16.dp))

        if (pcs.isEmpty()) {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
                    Spacer(modifier = Modifier.height(16.dp))
                    Text(
                        text = "Procurando PCs na rede Wi-Fi",
                        color = Color(0xFF9CA3AF),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Text(
                        text = "(ou use adb forward para conectar via USB)",
                        color = Color(0xFF9CA3AF),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        } else {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(pcs) { pc ->
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.surface,
                        ),
                    ) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(16.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text("🖥", style = MaterialTheme.typography.headlineSmall)
                            Spacer(modifier = Modifier.size(12.dp))
                            Column(modifier = Modifier.weight(1f)) {
                                Text(pc.name, style = MaterialTheme.typography.titleMedium, color = Color.White)
                                Text(
                                    "${pc.host}:${pc.port}",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Color(0xFF9CA3AF),
                                )
                            }
                            Button(onClick = { onPcSelected(pc) }) {
                                Text("Conectar")
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun PinEntryScreen(pc: PcInfo, pinError: String?, onCancel: () -> Unit, onSubmit: (String) -> Unit) {
    var pin by remember { mutableStateOf("") }

    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            modifier = Modifier.padding(32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("Conectar a ${pc.name}", style = MaterialTheme.typography.titleLarge)
            Spacer(modifier = Modifier.height(8.dp))
            Text("Digite o PIN exibido na tela do PC:", style = MaterialTheme.typography.bodyMedium)
            Spacer(modifier = Modifier.height(16.dp))

            OutlinedTextField(
                value = pin,
                onValueChange = { if (it.length <= 6 && it.all(Char::isDigit)) pin = it },
                label = { Text("PIN de 6 dígitos") },
                isError = pinError != null,
                supportingText = pinError?.let { { Text(it) } },
            )

            Spacer(modifier = Modifier.height(24.dp))

            Row {
                TextButton(onClick = onCancel) { Text("Cancelar") }
                Spacer(modifier = Modifier.size(8.dp))
                Button(
                    onClick = { onSubmit(pin) },
                    enabled = pin.length == 6,
                ) { Text("Conectar") }
            }
        }
    }
}

@Composable
fun ConnectingScreen() {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            CircularProgressIndicator()
            Spacer(modifier = Modifier.height(12.dp))
            Text("Conectando...")
        }
    }
}

@Composable
fun ConnectionErrorScreen(message: String, onDismiss: () -> Unit) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            modifier = Modifier.padding(32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("Não foi possível conectar", style = MaterialTheme.typography.titleLarge)
            Spacer(modifier = Modifier.height(8.dp))
            Text(message, style = MaterialTheme.typography.bodyMedium)
            Spacer(modifier = Modifier.height(24.dp))
            Button(onClick = onDismiss) { Text("Voltar") }
        }
    }
}

@Composable
fun ConnectedScreen(viewModel: MainViewModel, quality: String) {
    val remoteTrack by viewModel.remoteVideoTrack.collectAsState()

    // Sem barra fixa: durante a transmissão a tela é só o vídeo remoto,
    // com o menu flutuante do NuDuck sobreposto (arrastável, abre/fecha por toque).
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        val track = remoteTrack
        if (track != null) {
            RemoteVideoView(
                videoTrack = track,
                eglBaseContext = viewModel.activeWebRtcClient?.eglBase?.eglBaseContext,
                onControlEvent = { json -> viewModel.sendControlEvent(json) },
            )
        } else {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = Color.White)
            }
        }

        FloatingMenuHost(
            quality = quality,
            qualityLabels = QUALITY_LABELS,
            onQualityChange = viewModel::changeQuality,
            onOpenSettings = {
                // TODO: tela de configurações ainda não existe no app;
                // este callback já está pronto para quando ela for criada.
            },
            onDisconnect = viewModel::disconnect,
        )
    }
}

/**
 * Renderiza a track de vídeo remota em um SurfaceViewRenderer e converte toques
 * do usuário em eventos de controle normalizados (0.0-1.0), enviados pelo DataChannel.
 */
@Composable
fun RemoteVideoView(
    videoTrack: VideoTrack,
    eglBaseContext: org.webrtc.EglBase.Context?,
    onControlEvent: (org.json.JSONObject) -> Unit,
) {
    var viewWidth by remember { mutableStateOf(1) }
    var viewHeight by remember { mutableStateOf(1) }

    AndroidView(
        modifier = Modifier
            .fillMaxSize()
            .pointerInput(Unit) {
                awaitPointerEventScope {
                    while (true) {
                        val event = awaitPointerEvent()
                        val position = event.changes.firstOrNull()?.position ?: continue
                        val nx = (position.x / viewWidth).coerceIn(0f, 1f)
                        val ny = (position.y / viewHeight).coerceIn(0f, 1f)

                        when (event.type) {
                            PointerEventType.Press -> onControlEvent(ControlEvents.down(nx, ny))
                            PointerEventType.Move -> onControlEvent(ControlEvents.move(nx, ny))
                            PointerEventType.Release -> onControlEvent(ControlEvents.up(nx, ny))
                            else -> {}
                        }
                    }
                }
            },
        factory = { ctx ->
            SurfaceViewRenderer(ctx).apply {
                eglBaseContext?.let { init(it, null) }
                setScalingType(RendererCommon.ScalingType.SCALE_ASPECT_FIT)
                setEnableHardwareScaler(true)
                videoTrack.addSink(this)
            }
        },
        update = { view ->
            viewWidth = view.width.takeIf { it > 0 } ?: 1
            viewHeight = view.height.takeIf { it > 0 } ?: 1
        },
    )

    DisposableEffect(videoTrack) {
        onDispose {
            // A limpeza do sink acontece implicitamente quando a PeerConnection fecha
            // (viewModel.disconnect / onCleared), evitando referenciar a view já destruída aqui.
        }
    }
}
