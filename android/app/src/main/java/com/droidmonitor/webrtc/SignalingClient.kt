package com.droidmonitor.webrtc

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

private const val TAG = "SignalingClient"

/**
 * Fala o protocolo de sinalização do server.py via WebSocket em ws://host:port/ws:
 *   -> {"type":"pin","pin":"123456"}
 *   <- {"type":"pin_ok"} | {"type":"pin_error","blocked":bool}
 *   -> {"type":"offer","sdp":...,"sdpType":"offer","quality":"media"}
 *   <- {"type":"answer","sdp":...,"sdpType":"answer"}
 *   -> {"type":"quality","value":"alta"}
 */
class SignalingClient(
    private val host: String,
    private val port: Int,
) {
    interface Listener {
        fun onPinAccepted()
        fun onPinRejected(blocked: Boolean)
        fun onAnswerReceived(sdp: String, sdpType: String)
        fun onSignalingError(message: String)
        fun onSignalingClosed()
    }

    private var webSocket: WebSocket? = null
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS) // WebSocket: sem timeout de leitura
        .build()

    var listener: Listener? = null

    fun connect() {
        val request = Request.Builder()
            .url("ws://$host:$port/ws")
            .build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "WebSocket conectado a $host:$port")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "Falha no WebSocket: ${t.message}", t)
                listener?.onSignalingError(t.message ?: "Erro de conexão")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "WebSocket fechado: $reason")
                listener?.onSignalingClosed()
            }
        })
    }

    private fun handleMessage(text: String) {
        val json = try {
            JSONObject(text)
        } catch (e: Exception) {
            Log.w(TAG, "Mensagem inválida do servidor: $text")
            return
        }

        when (json.optString("type")) {
            "pin_ok" -> listener?.onPinAccepted()
            "pin_error" -> listener?.onPinRejected(json.optBoolean("blocked", false))
            "answer" -> listener?.onAnswerReceived(
                sdp = json.getString("sdp"),
                sdpType = json.getString("sdpType"),
            )
            "error" -> listener?.onSignalingError(json.optString("message", "Erro desconhecido"))
        }
    }

    fun sendPin(pin: String) {
        send(JSONObject().apply {
            put("type", "pin")
            put("pin", pin)
        })
    }

    fun sendOffer(sdp: String, sdpType: String, quality: String) {
        send(JSONObject().apply {
            put("type", "offer")
            put("sdp", sdp)
            put("sdpType", sdpType)
            put("quality", quality)
        })
    }

    fun sendQualityChange(quality: String) {
        send(JSONObject().apply {
            put("type", "quality")
            put("value", quality)
        })
    }

    private fun send(payload: JSONObject) {
        webSocket?.send(payload.toString()) ?: Log.w(TAG, "Tentativa de envio sem conexão ativa")
    }

    fun close() {
        webSocket?.close(1000, "Cliente desconectou")
        webSocket = null
        client.dispatcher.executorService.shutdown()
    }
}
