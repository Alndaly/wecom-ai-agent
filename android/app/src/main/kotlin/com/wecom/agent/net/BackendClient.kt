package com.wecom.agent.net

import android.util.Log
import com.wecom.agent.model.WireEvent
import kotlinx.coroutines.*
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import okhttp3.*
import okio.ByteString
import java.util.concurrent.TimeUnit

/**
 * Single WebSocket connection to the backend.
 * Auto-reconnects with exponential backoff.
 *
 * MVP1: state managed in-memory, single Activity owns the lifecycle.
 */
class BackendClient(
    private val baseWsUrl: String,    // e.g. "ws://10.0.2.2:8000"
    private val robotId: String,
    private val token: String,
    private val onEvent: (event: String, payload: JsonElement?) -> Unit,
    private val onState: (state: String) -> Unit = {},
) {
    private val tag = "BackendClient"
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val httpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()
    private var ws: WebSocket? = null
    private var backoffMs = 1_000L
    @Volatile private var stopped = false

    fun start() {
        scope.launch { connectLoop() }
    }

    fun stop() {
        stopped = true
        ws?.close(1000, "client stop")
        scope.cancel()
    }

    fun sendEvent(event: String, payload: JsonElement? = null): Boolean {
        val w = ws ?: return false
        val msg = json.encodeToString(WireEvent.serializer(), WireEvent(event, payload))
        return w.send(msg)
    }

    private suspend fun connectLoop() {
        while (!stopped) {
            try {
                val url = "$baseWsUrl/ws/android?robot_id=$robotId&token=$token"
                val req = Request.Builder().url(url).build()
                val open = CompletableDeferred<Unit>()
                val closed = CompletableDeferred<Unit>()

                ws = httpClient.newWebSocket(req, object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        Log.i(tag, "ws open")
                        backoffMs = 1_000L
                        onState("connected")
                        open.complete(Unit)
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        try {
                            val evt = json.decodeFromString(WireEvent.serializer(), text)
                            try {
                                onEvent(evt.event, evt.payload)
                            } catch (e: Exception) {
                                Log.e(tag, "event handler failed event=${evt.event}", e)
                            }
                        } catch (e: Exception) {
                            Log.w(tag, "decode failed: $text", e)
                        }
                    }

                    override fun onMessage(webSocket: WebSocket, bytes: ByteString) { /* unused */ }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        Log.w(tag, "ws failure response_code=${response?.code} response_msg=${response?.message}", t)
                        onState("disconnected")
                        if (!closed.isCompleted) closed.complete(Unit)
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        Log.i(tag, "ws closed $code $reason")
                        onState("disconnected")
                        if (!closed.isCompleted) closed.complete(Unit)
                    }
                })

                closed.await()
            } catch (e: Exception) {
                Log.w(tag, "connect error", e)
            }
            if (stopped) return
            delay(backoffMs)
            backoffMs = (backoffMs * 2).coerceAtMost(30_000L)
        }
    }
}
