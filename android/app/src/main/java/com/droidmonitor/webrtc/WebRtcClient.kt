package com.droidmonitor.webrtc

import android.content.Context
import android.util.Log
import org.json.JSONObject
import org.webrtc.DataChannel
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.VideoTrack
import java.nio.charset.Charset

private const val TAG = "WebRtcClient"

/**
 * Encapsula a PeerConnectionFactory/PeerConnection do lado Android.
 * O PC é sempre quem oferece vídeo (recvonly aqui); o Android envia
 * eventos de toque por um DataChannel "control".
 *
 * DTLS/SRTP são obrigatórios por padrão na pilha WebRTC — não há como
 * negociar uma conexão de mídia sem criptografia.
 */
class WebRtcClient(
    context: Context,
    private val signalingClient: SignalingClient,
) {
    interface Listener {
        fun onRemoteVideoTrack(track: VideoTrack)
        fun onControlChannelReady()
        fun onConnectionFailed(reason: String)
    }

    var listener: Listener? = null

    val eglBase: EglBase = EglBase.create()

    private val factory: PeerConnectionFactory
    private var peerConnection: PeerConnection? = null
    private var controlChannel: DataChannel? = null

    init {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(context)
                .createInitializationOptions()
        )

        val encoderFactory = DefaultVideoEncoderFactory(eglBase.eglBaseContext, true, true)
        val decoderFactory = DefaultVideoDecoderFactory(eglBase.eglBaseContext)

        factory = PeerConnectionFactory.builder()
            .setVideoEncoderFactory(encoderFactory)
            .setVideoDecoderFactory(decoderFactory)
            .createPeerConnectionFactory()
    }

    /** Cria a PeerConnection, gera a offer (recvonly de vídeo) e envia via sinalização. */
    fun startConnection(quality: String) {
        val rtcConfig = PeerConnection.RTCConfiguration(emptyList<PeerConnection.IceServer>()).apply {
            // Sem servidores STUN/TURN: conexão é sempre direta na rede local.
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
        }

        peerConnection = factory.createPeerConnection(rtcConfig, object : PeerConnection.Observer {
            override fun onIceCandidate(candidate: IceCandidate?) {
                // aiortc inclui os candidatos diretamente no SDP (sem trickle),
                // então não é necessário retransmiti-los aqui.
            }

            override fun onAddStream(stream: MediaStream?) {
                val track = stream?.videoTracks?.firstOrNull()
                if (track != null) {
                    Log.i(TAG, "Track de vídeo remota recebida")
                    listener?.onRemoteVideoTrack(track)
                }
            }

            override fun onDataChannel(channel: DataChannel?) {}
            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState?) {
                Log.i(TAG, "Estado ICE: $state")
                if (state == PeerConnection.IceConnectionState.FAILED) {
                    listener?.onConnectionFailed("Conexão WebRTC falhou (ICE)")
                }
            }

            override fun onSignalingChange(state: PeerConnection.SignalingState?) {}
            override fun onIceConnectionReceivingChange(receiving: Boolean) {}
            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState?) {}
            override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) {}
            override fun onAddTrack(receiver: org.webrtc.RtpReceiver?, streams: Array<out MediaStream>?) {}
            override fun onRemoveStream(stream: MediaStream?) {}
            override fun onRenegotiationNeeded() {}
        })

        // Canal de dados usado para enviar toques/teclas ao PC.
        val init = DataChannel.Init().apply { ordered = true }
        controlChannel = peerConnection?.createDataChannel("control", init)
        controlChannel?.registerObserver(object : DataChannel.Observer {
            override fun onStateChange() {
                if (controlChannel?.state() == DataChannel.State.OPEN) {
                    listener?.onControlChannelReady()
                }
            }
            override fun onMessage(buffer: DataChannel.Buffer?) {}
            override fun onBufferedAmountChange(amount: Long) {}
        })

        val offerConstraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "false"))
        }

        peerConnection?.createOffer(object : SdpObserver by NoopSdpObserver {
            override fun onCreateSuccess(desc: SessionDescription) {
                peerConnection?.setLocalDescription(NoopSdpObserver, desc)
                signalingClient.sendOffer(
                    sdp = desc.description,
                    sdpType = "offer",
                    quality = quality,
                )
            }

            override fun onCreateFailure(error: String?) {
                listener?.onConnectionFailed("Falha ao criar offer: $error")
            }
        }, offerConstraints)
    }

    /** Aplica a answer recebida do PC via sinalização. */
    fun applyRemoteAnswer(sdp: String, sdpType: String) {
        val description = SessionDescription(SessionDescription.Type.fromCanonicalForm(sdpType), sdp)
        peerConnection?.setRemoteDescription(NoopSdpObserver, description)
    }

    /** Envia um evento de toque/tecla ao PC (ignorado pelo servidor se "Permitir controle" estiver desligado). */
    fun sendControlEvent(json: JSONObject) {
        val channel = controlChannel ?: return
        if (channel.state() != DataChannel.State.OPEN) return
        val bytes = json.toString().toByteArray(Charset.forName("UTF-8"))
        channel.send(DataChannel.Buffer(java.nio.ByteBuffer.wrap(bytes), false))
    }

    fun close() {
        controlChannel?.close()
        controlChannel = null
        peerConnection?.close()
        peerConnection = null
        eglBase.release()
    }
}

/** SdpObserver que ignora os callbacks que não usamos, para evitar boilerplate repetido. */
private object NoopSdpObserver : SdpObserver {
    override fun onCreateSuccess(p0: SessionDescription?) {}
    override fun onSetSuccess() {}
    override fun onCreateFailure(p0: String?) {}
    override fun onSetFailure(p0: String?) {}
}
