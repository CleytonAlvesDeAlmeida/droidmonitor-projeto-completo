package com.droidmonitor

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.droidmonitor.discovery.MdnsDiscovery
import com.droidmonitor.discovery.PcInfo
import com.droidmonitor.webrtc.SignalingClient
import com.droidmonitor.webrtc.WebRtcClient
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.webrtc.VideoTrack

/** Telas possíveis do app. */
sealed class Screen {
    data object Discovery : Screen()
    data class PinEntry(val pc: PcInfo) : Screen()
    data object Connecting : Screen()
    data object Connected : Screen()
    data class ConnectionError(val message: String) : Screen()
}

data class UiState(
    val discoveredPcs: List<PcInfo> = emptyList(),
    val screen: Screen = Screen.Discovery,
    val quality: String = "media",
    val pinError: String? = null,
)

class MainViewModel(application: Application) : AndroidViewModel(application) {

    private val _uiState = MutableStateFlow(UiState())
    val uiState: StateFlow<UiState> = _uiState

    private val _remoteVideoTrack = MutableStateFlow<VideoTrack?>(null)
    val remoteVideoTrack: StateFlow<VideoTrack?> = _remoteVideoTrack

    private val mdnsDiscovery = MdnsDiscovery(application)
    private var signalingClient: SignalingClient? = null
    private var webRtcClient: WebRtcClient? = null

    init {
        mdnsDiscovery.onPcFound = { pc ->
            _uiState.update { state ->
                if (state.discoveredPcs.any { it.host == pc.host && it.port == pc.port }) state
                else state.copy(discoveredPcs = state.discoveredPcs + pc)
            }
        }
        mdnsDiscovery.onPcRemoved = { name ->
            _uiState.update { state ->
                state.copy(discoveredPcs = state.discoveredPcs.filterNot { it.name == name })
            }
        }
        mdnsDiscovery.start()
    }

    fun selectPc(pc: PcInfo) {
        _uiState.update { it.copy(screen = Screen.PinEntry(pc), pinError = null) }
    }

    fun cancelPinEntry() {
        _uiState.update { it.copy(screen = Screen.Discovery, pinError = null) }
    }

    fun submitPin(pc: PcInfo, pin: String) {
        _uiState.update { it.copy(screen = Screen.Connecting) }

        val signaling = SignalingClient(pc.host, pc.port)
        signalingClient = signaling

        val rtc = WebRtcClient(getApplication(), signaling)
        webRtcClient = rtc

        rtc.listener = object : WebRtcClient.Listener {
            override fun onRemoteVideoTrack(track: VideoTrack) {
                _remoteVideoTrack.value = track
                _uiState.update { it.copy(screen = Screen.Connected) }
            }

            override fun onControlChannelReady() {
                // Canal pronto; eventos de toque já podem ser enviados.
            }

            override fun onConnectionFailed(reason: String) {
                _uiState.update { it.copy(screen = Screen.ConnectionError(reason)) }
            }
        }

        signaling.listener = object : SignalingClient.Listener {
            override fun onPinAccepted() {
                rtc.startConnection(_uiState.value.quality)
            }

            override fun onPinRejected(blocked: Boolean) {
                if (blocked) {
                    _uiState.update {
                        it.copy(
                            screen = Screen.ConnectionError("Muitas tentativas de PIN erradas. Tente novamente em instantes.")
                        )
                    }
                } else {
                    _uiState.update {
                        it.copy(screen = Screen.PinEntry(pc), pinError = "PIN incorreto. Tente novamente.")
                    }
                }
            }

            override fun onAnswerReceived(sdp: String, sdpType: String) {
                rtc.applyRemoteAnswer(sdp, sdpType)
            }

            override fun onSignalingError(message: String) {
                _uiState.update { it.copy(screen = Screen.ConnectionError(message)) }
            }

            override fun onSignalingClosed() {
                if (_uiState.value.screen is Screen.Connected) {
                    _uiState.update { it.copy(screen = Screen.ConnectionError("Conexão encerrada pelo PC.")) }
                }
            }
        }

        viewModelScope.launch {
            signaling.connect()
            signaling.sendPin(pin)
        }
    }

    fun changeQuality(quality: String) {
        _uiState.update { it.copy(quality = quality) }
        signalingClient?.sendQualityChange(quality)
    }

    fun sendControlEvent(json: org.json.JSONObject) {
        webRtcClient?.sendControlEvent(json)
    }

    /** Exposto para a UI poder inicializar o SurfaceViewRenderer com o mesmo EglBase do cliente WebRTC. */
    val activeWebRtcClient: WebRtcClient?
        get() = webRtcClient

    fun disconnect() {
        webRtcClient?.close()
        signalingClient?.close()
        webRtcClient = null
        signalingClient = null
        _remoteVideoTrack.value = null
        _uiState.update { it.copy(screen = Screen.Discovery, pinError = null) }
    }

    override fun onCleared() {
        super.onCleared()
        mdnsDiscovery.stop()
        webRtcClient?.close()
        signalingClient?.close()
    }
}
