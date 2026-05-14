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
        const val ACTION_STOP = "com.wecom.agent.ACTION_STOP"
        const val ACTION_STATE_CHANGED = "com.wecom.agent.ACTION_STATE_CHANGED"
        const val ACTION_LOG = "com.wecom.agent.ACTION_LOG"
        const val EXTRA_STATE = "state"
        const val EXTRA_MESSAGE = "message"
    }

    private val tag = "AgentSvc"
    private val json = Json { ignoreUnknownKeys = true }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var client: BackendClient? = null
    private val executor = TaskExecutor()
    private var heartbeatJob: Job? = null
    @Volatile private var connected = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            broadcastLog("收到断开请求，正在停止服务")
            stopSelf()
            return START_NOT_STICKY
        }

        ensureChannel()
        startForeground(NOTIFICATION_ID, buildNotification("starting"))

        val base = intent?.getStringExtra(EXTRA_BASE_URL) ?: return START_NOT_STICKY
        val rid = intent.getStringExtra(EXTRA_ROBOT_ID) ?: return START_NOT_STICKY
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: return START_NOT_STICKY
        Log.i(tag, "service starting base=$base robot_id=$rid token_len=${token.length}")
        updateNotification("connecting $rid")
        broadcastState("connecting")
        broadcastLog("服务启动，正在连接 $base")

        client?.stop()
        connected = false
        client = BackendClient(
            baseWsUrl = base,
            robotId = rid,
            token = token,
            onEvent = { event, payload ->
                handleEvent(event, payload)
            },
            onState = { state ->
                connected = state == "connected"
                updateNotification(if (connected) "connected $rid" else "connecting $rid")
                broadcastState(state)
                broadcastLog(if (connected) "WebSocket 已连接" else "WebSocket 已断开，等待重连")
                Log.i(tag, "ws state=$state")
            },
        ).also { it.start() }

        heartbeatJob?.cancel()
        heartbeatJob = scope.launch {
            while (!connected && isActive) {
                delay(200L)
            }
            if (!isActive) return@launch
            client?.sendEvent(
                "device.hello",
                json.encodeToJsonElement(HeartbeatPayload.serializer(), HeartbeatPayload(current_page = "HOME")),
            )
            broadcastLog("已发送 device.hello")
            while (isActive) {
                delay(30_000L)
                client?.sendEvent(
                    "device.heartbeat",
                    json.encodeToJsonElement(HeartbeatPayload.serializer(), HeartbeatPayload(current_page = "HOME")),
                )
                broadcastLog("已发送 device.heartbeat")
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        heartbeatJob?.cancel()
        client?.stop()
        connected = false
        broadcastState("disconnected")
        broadcastLog("服务已停止")
        scope.cancel()
        super.onDestroy()
    }

    private fun handleEvent(event: String, payload: JsonElement?) {
        Log.i(tag, "<- $event")
        broadcastLog("收到后端事件：$event")
        when (event) {
            "task.dispatch" -> {
                val p = json.decodeFromJsonElement(TaskDispatchPayload.serializer(), payload ?: return)
                broadcastLog("收到任务 #${p.task_id}：${p.type}")
                scope.launch {
                    val result = executor.run(p)
                    val ackEvent = if (result.success) "task.completed" else "task.failed"
                    val ackPayload = json.encodeToJsonElement(
                        TaskAckPayload.serializer(),
                        TaskAckPayload(task_id = p.task_id, error = result.error),
                    )
                    client?.sendEvent(ackEvent, ackPayload)
                    broadcastLog(
                        if (result.success) "任务 #${p.task_id} 执行完成，已回传 task.completed"
                        else "任务 #${p.task_id} 执行失败：${result.error}"
                    )
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

    private fun updateNotification(text: String) {
        val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        mgr.notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun broadcastState(state: String) {
        sendBroadcast(
            Intent(ACTION_STATE_CHANGED).apply {
                setPackage(packageName)
                putExtra(EXTRA_STATE, state)
            },
        )
    }

    private fun broadcastLog(message: String) {
        sendBroadcast(
            Intent(ACTION_LOG).apply {
                setPackage(packageName)
                putExtra(EXTRA_MESSAGE, message)
            },
        )
        Log.i(tag, message)
    }
}
