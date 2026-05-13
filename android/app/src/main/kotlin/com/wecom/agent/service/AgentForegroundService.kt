package com.wecom.agent.service

import android.app.*
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.wecom.agent.R
import com.wecom.agent.model.HeartbeatPayload
import com.wecom.agent.model.TaskAckPayload
import com.wecom.agent.model.TaskDispatchPayload
import com.wecom.agent.net.BackendClient
import kotlinx.coroutines.*
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Foreground service that owns the WebSocket lifecycle and the task executor.
 * Started by MainActivity after the user configures backend URL / robot_id / token.
 */
class AgentForegroundService : Service() {
    companion object {
        const val CHANNEL_ID = "wecom_agent"
        const val NOTIFICATION_ID = 1001
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_ROBOT_ID = "robot_id"
        const val EXTRA_TOKEN = "token"
    }

    private val tag = "AgentSvc"
    private val json = Json { ignoreUnknownKeys = true }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var client: BackendClient? = null
    private val executor = TaskExecutor()
    private var heartbeatJob: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ensureChannel()
        startForeground(NOTIFICATION_ID, buildNotification("running"))

        val base = intent?.getStringExtra(EXTRA_BASE_URL) ?: return START_NOT_STICKY
        val rid = intent.getStringExtra(EXTRA_ROBOT_ID) ?: return START_NOT_STICKY
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: return START_NOT_STICKY

        client?.stop()
        client = BackendClient(base, rid, token) { event, payload ->
            handleEvent(event, payload)
        }.also { it.start() }

        heartbeatJob?.cancel()
        heartbeatJob = scope.launch {
            // initial hello
            client?.sendEvent("device.hello", json.encodeToJsonElement(HeartbeatPayload.serializer(), HeartbeatPayload(current_page = "HOME")))
            while (isActive) {
                delay(30_000L)
                client?.sendEvent(
                    "device.heartbeat",
                    json.encodeToJsonElement(HeartbeatPayload.serializer(), HeartbeatPayload(current_page = "HOME")),
                )
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        heartbeatJob?.cancel()
        client?.stop()
        scope.cancel()
        super.onDestroy()
    }

    private fun handleEvent(event: String, payload: JsonElement?) {
        Log.i(tag, "<- $event")
        when (event) {
            "task.dispatch" -> {
                val p = json.decodeFromJsonElement(TaskDispatchPayload.serializer(), payload ?: return)
                scope.launch {
                    val result = executor.run(p)
                    val ackEvent = if (result.success) "task.completed" else "task.failed"
                    val ackPayload = json.encodeToJsonElement(
                        TaskAckPayload.serializer(),
                        TaskAckPayload(task_id = p.task_id, error = result.error),
                    )
                    client?.sendEvent(ackEvent, ackPayload)
                }
            }
            "device.command" -> {
                // MVP1: ignore (screenshot, restart, clear_cache will be handled later)
            }
        }
    }

    private fun ensureChannel() {
        val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "WeCom Agent", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    private fun buildNotification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("WeCom Agent")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .build()
}
