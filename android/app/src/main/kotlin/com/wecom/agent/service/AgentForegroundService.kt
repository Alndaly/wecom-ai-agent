package com.wecom.agent.service

import android.app.*
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import com.wecom.agent.R
import com.wecom.agent.model.Contact
import com.wecom.agent.model.DeviceCommandAckPayload
import com.wecom.agent.model.HeartbeatPayload
import com.wecom.agent.model.MessageReceivedPayload
import com.wecom.agent.model.ScreenFramePayload
import com.wecom.agent.model.TaskAckPayload
import com.wecom.agent.model.TaskDispatchPayload
import com.wecom.agent.net.BackendClient
import kotlinx.coroutines.*
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.text.SimpleDateFormat
import java.time.Instant
import java.util.Date
import java.util.Locale
import java.util.UUID

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
        const val EXTRA_DRY_RUN = "dry_run"
        const val EXTRA_A11Y_INGEST = "a11y_ingest"
        const val ACTION_STOP = "com.wecom.agent.ACTION_STOP"
        const val ACTION_STATE_CHANGED = "com.wecom.agent.ACTION_STATE_CHANGED"
        const val ACTION_LOG = "com.wecom.agent.ACTION_LOG"
        const val ACTION_DUMP_UI = "com.wecom.agent.ACTION_DUMP_UI"
        const val ACTION_SEND_TEST = "com.wecom.agent.ACTION_SEND_TEST"
        const val ACTION_SET_DRY_RUN = "com.wecom.agent.ACTION_SET_DRY_RUN"
        const val ACTION_SET_A11Y_INGEST = "com.wecom.agent.ACTION_SET_A11Y_INGEST"
        const val EXTRA_STATE = "state"
        const val EXTRA_MESSAGE = "message"
        const val EXTRA_TEST_CONTACT = "test_contact"
        const val EXTRA_TEST_TEXT = "test_text"
    }

    private val tag = "AgentSvc"
    private val json = Json { ignoreUnknownKeys = true }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var client: BackendClient? = null
    private var executor: TaskExecutor? = null
    private var heartbeatJob: Job? = null
    private var screenStreamJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var connected = false
    @Volatile private var a11yInboundEnabled = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                broadcastLog("收到断开请求，正在停止服务")
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_DUMP_UI -> {
                broadcastLog("请求采集 UI 树")
                scope.launch { dumpAndUpload("manual") }
                return START_NOT_STICKY
            }
            ACTION_SEND_TEST -> {
                val c = intent.getStringExtra(EXTRA_TEST_CONTACT).orEmpty()
                val t = intent.getStringExtra(EXTRA_TEST_TEXT).orEmpty()
                if (c.isBlank() || t.isBlank()) {
                    broadcastLog("发送测试失败：联系人或文本为空")
                } else {
                    scope.launch { runSendTest(c, t) }
                }
                return START_NOT_STICKY
            }
            ACTION_SET_DRY_RUN -> {
                val dryRun = intent.getBooleanExtra(EXTRA_DRY_RUN, false)
                executor?.dryRun = dryRun
                broadcastLog("dry_run 已即时更新为 $dryRun")
                return if (executor == null) START_NOT_STICKY else START_STICKY
            }
            ACTION_SET_A11Y_INGEST -> {
                a11yInboundEnabled = intent.getBooleanExtra(EXTRA_A11Y_INGEST, false)
                broadcastLog("无障碍消息采集已即时更新为 $a11yInboundEnabled")
                return START_STICKY
            }
        }

        ensureChannel()
        startForeground(NOTIFICATION_ID, buildNotification("starting"))
        acquireWakeLockIfNeeded()

        val base = intent?.getStringExtra(EXTRA_BASE_URL) ?: return START_NOT_STICKY
        val rid = intent.getStringExtra(EXTRA_ROBOT_ID) ?: return START_NOT_STICKY
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: return START_NOT_STICKY
        val dryRun = intent.getBooleanExtra(EXTRA_DRY_RUN, false)
        a11yInboundEnabled = intent.getBooleanExtra(EXTRA_A11Y_INGEST, false)
        Log.i(tag, "service starting base=$base robot_id=$rid token_len=${token.length} dryRun=$dryRun a11yInbound=$a11yInboundEnabled")
        updateNotification("connecting $rid")
        broadcastState("connecting")
        broadcastLog("服务启动，正在连接 $base${if (dryRun) " (dry-run)" else ""}")

        // executor + UI listeners — kept alive for the service lifetime
        executor = TaskExecutor(
            applicationContext,
            { msg -> broadcastLog(msg) },
            onTaskLog = { taskId, level, message ->
                scope.launch {
                    appendTaskLog(taskId, level, message)
                }
            },
        ).also {
            it.dryRun = dryRun
        }

        // wire inbound channels → ws.message.received
        MessageNotificationListener.onMessage = { sender, content, postTime ->
            forwardInboundMessage(sender, content, postTime, viaNotification = true)
        }
        WeComAccessibilityService.onChatMessage = { sender, content ->
            if (a11yInboundEnabled) {
                forwardInboundMessage(sender, content, System.currentTimeMillis(), viaNotification = false)
            }
        }

        client?.stop()
        connected = false
        client = BackendClient(
            baseWsUrl = base,
            robotId = rid,
            token = token,
            onEvent = { event, payload -> handleEvent(event, payload) },
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
            while (!connected && isActive) delay(200L)
            if (!isActive) return@launch
            client?.sendEvent(
                "device.hello",
                json.encodeToJsonElement(HeartbeatPayload.serializer(), heartbeatPayload(includeDeviceInfo = true)),
            )
            broadcastLog("已发送 device.hello")
            while (isActive) {
                delay(30_000L)
                client?.sendEvent(
                    "device.heartbeat",
                    json.encodeToJsonElement(HeartbeatPayload.serializer(), heartbeatPayload(includeDeviceInfo = true)),
                )
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        heartbeatJob?.cancel()
        screenStreamJob?.cancel()
        client?.stop()
        MessageNotificationListener.onMessage = null
        WeComAccessibilityService.onChatMessage = null
        releaseWakeLock()
        connected = false
        broadcastState("disconnected")
        broadcastLog("服务已停止")
        scope.cancel()
        super.onDestroy()
    }

    /** Keeps the CPU awake even when the screen is off so the agent keeps
     *  reconnecting and consuming WS events. Does NOT keep the screen on
     *  by itself — that's the Activity's job via FLAG_KEEP_SCREEN_ON.
     *
     *  Held for the lifetime of the foreground service. Released in
     *  onDestroy. Idempotent — multiple onStartCommand calls don't stack. */
    private fun acquireWakeLockIfNeeded() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "wecom-agent:fg")
        lock.setReferenceCounted(false)
        try {
            lock.acquire()  // no timeout — released in onDestroy
            wakeLock = lock
            broadcastLog("已获取 CPU wake-lock（锁屏后仍保持运行）")
        } catch (e: Exception) {
            Log.w(tag, "wake lock acquire failed", e)
            broadcastLog("获取 wake-lock 失败: ${e.message}")
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.takeIf { it.isHeld }?.release()
        } catch (e: Exception) {
            Log.w(tag, "wake lock release failed", e)
        }
        wakeLock = null
    }

    private fun currentPage(): String =
        WeComAccessibilityService.instance?.currentPage?.name ?: "UNKNOWN"

    private fun heartbeatPayload(includeDeviceInfo: Boolean = false): HeartbeatPayload {
        val metrics = resources.displayMetrics
        val versionName = runCatching {
            packageManager.getPackageInfo(packageName, 0).versionName
        }.getOrNull()
        val deviceName = runCatching {
            Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)
        }.getOrNull()
        return HeartbeatPayload(
            current_page = currentPage(),
            device_type = if (includeDeviceInfo) "android" else null,
            device_name = if (includeDeviceInfo) deviceName ?: Build.MODEL else null,
            manufacturer = if (includeDeviceInfo) Build.MANUFACTURER else null,
            model = if (includeDeviceInfo) Build.MODEL else null,
            android_version = if (includeDeviceInfo) Build.VERSION.RELEASE else null,
            sdk_int = if (includeDeviceInfo) Build.VERSION.SDK_INT else null,
            app_version = if (includeDeviceInfo) versionName else null,
            screen_width = if (includeDeviceInfo) metrics.widthPixels else null,
            screen_height = if (includeDeviceInfo) metrics.heightPixels else null,
        )
    }

    private suspend fun dumpAndUpload(reason: String, requestId: String? = null) {
        val svc = WeComAccessibilityService.instance
        if (svc == null) {
            broadcastLog("无障碍服务未启用，无法采集 UI 树")
            return
        }
        val tree = StringBuilder().also { svc.dumpToString(it) }.toString()
        broadcastLog("UI 树共 ${tree.length} 字符，已写入 logcat 并尝试上传后端")
        val payload = json.encodeToJsonElement(
            com.wecom.agent.model.UiDumpPayload.serializer(),
            com.wecom.agent.model.UiDumpPayload(
                reason = reason,
                request_id = requestId,
                current_page = currentPage(),
                tree = tree,
            ),
        )
        val ok = client?.sendEvent("device.ui_dump", payload) == true
        broadcastLog(if (ok) "UI 树已上传" else "UI 树未上传（未连接后端）")
    }

    private suspend fun runSendTest(contact: String, text: String) {
        val ex = executor ?: run {
            broadcastLog("executor 未初始化")
            return
        }
        broadcastLog("开始本地测试发送: $contact :: $text")
        val payload = kotlinx.serialization.json.JsonObject(mapOf(
            "conversation_external_id" to kotlinx.serialization.json.JsonPrimitive(contact),
            "text" to kotlinx.serialization.json.JsonPrimitive(text),
        ))
        val task = TaskDispatchPayload(task_id = -1L, type = "send_text", payload = payload)
        val r = ex.run(task)
        broadcastLog(if (r.success) "本地测试发送成功" else "本地测试失败: ${r.error}")
    }

    private suspend fun appendTaskLog(taskId: Long?, level: String, message: String) {
        if (taskId == null) {
            broadcastLog("任务日志未发送：未连接后端")
            return
        }
        val payload = kotlinx.serialization.json.JsonObject(
            mapOf(
                "task_id" to kotlinx.serialization.json.JsonPrimitive(taskId),
                "level" to kotlinx.serialization.json.JsonPrimitive(level),
                "message" to kotlinx.serialization.json.JsonPrimitive(message),
            )
        )
        client?.sendEvent("task.log", payload)
    }

    private fun handleEvent(event: String, payload: JsonElement?) {
        Log.i(tag, "<- $event")
        broadcastLog("收到后端事件：$event")
        when (event) {
            "task.dispatch" -> {
                val p = json.decodeFromJsonElement(TaskDispatchPayload.serializer(), payload ?: return)
                broadcastLog("收到任务 #${p.task_id}：${p.type}")
                scope.launch {
                    val ex = executor ?: return@launch
                    val result = ex.run(p)
                    val ackEvent = if (result.success) "task.completed" else "task.failed"
                    val ackPayload = json.encodeToJsonElement(
                        TaskAckPayload.serializer(),
                        TaskAckPayload(task_id = p.task_id, error = result.error),
                    )
                    client?.sendEvent(ackEvent, ackPayload)
                    broadcastLog(
                        if (result.success) "任务 #${p.task_id} 执行完成"
                        else "任务 #${p.task_id} 执行失败：${result.error}"
                    )
                }
            }
            "device.command" -> {
                val obj = payload?.jsonObject ?: return
                val command = obj["command"]?.jsonPrimitive?.contentOrNull
                when (command) {
                    "dump_ui" -> {
                        val requestId = obj["request_id"]?.jsonPrimitive?.contentOrNull
                        val reason = obj["reason"]?.jsonPrimitive?.contentOrNull ?: "remote"
                        scope.launch { dumpAndUpload(reason, requestId) }
                    }
                    "screen_start" -> {
                        val intervalMs = obj["interval_ms"]?.jsonPrimitive?.contentOrNull
                            ?.toLongOrNull()
                            ?.coerceIn(500L, 5_000L)
                            ?: 1_000L
                        startScreenStream(intervalMs)
                    }
                    "screen_stop" -> stopScreenStream()
                    else -> {
                        sendCommandAck(command ?: "unknown", false, "未知设备命令")
                        broadcastLog("未知设备命令：$command")
                    }
                }
            }
        }
    }

    private fun startScreenStream(intervalMs: Long) {
        screenStreamJob?.cancel()
        // Authoritative check: read the system setting rather than rely solely
        // on the service singleton, which can be momentarily null right after
        // the user enables accessibility.
        if (!isAccessibilityServiceEnabled()) {
            sendCommandAck("screen_start", false, "无障碍服务未启用，请在系统设置中开启后重试")
            broadcastLog("拒绝开启实时屏幕：无障碍服务未启用")
            return
        }
        sendCommandAck("screen_start", true, "实时屏幕已开启")
        screenStreamJob = scope.launch {
            broadcastLog("实时屏幕已开启，间隔 ${intervalMs}ms")
            while (isActive) {
                val ok = sendScreenFrame()
                if (!ok) {
                    broadcastLog("实时屏幕已停止：无障碍服务不可用")
                    screenStreamJob = null
                    break
                }
                delay(intervalMs)
            }
        }
    }

    private fun isAccessibilityServiceEnabled(): Boolean {
        val cn = ComponentName(this, WeComAccessibilityService::class.java).flattenToString()
        val enabled = Settings.Secure.getString(
            contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        ).orEmpty()
        return enabled.split(':').any { it.equals(cn, ignoreCase = true) }
    }

    private fun stopScreenStream() {
        screenStreamJob?.cancel()
        screenStreamJob = null
        sendCommandAck("screen_stop", true, "实时屏幕已关闭")
        broadcastLog("实时屏幕已关闭")
    }

    private fun sendCommandAck(command: String, ok: Boolean, message: String? = null) {
        val payload = json.encodeToJsonElement(
            DeviceCommandAckPayload.serializer(),
            DeviceCommandAckPayload(command = command, ok = ok, message = message),
        )
        client?.sendEvent("device.command_ack", payload)
    }

    /**
     * Capture one frame and forward to backend.
     *
     * @return false if the accessibility service is unavailable — caller should
     *  stop the stream loop instead of polling and flooding identical errors.
     */
    private suspend fun sendScreenFrame(): Boolean {
        val svc = WeComAccessibilityService.instance
        if (svc == null) {
            // Either accessibility was just toggled off, or the service hasn't
            // finished onServiceConnected yet. Bail; the caller stops the loop.
            val payload = json.encodeToJsonElement(
                ScreenFramePayload.serializer(),
                ScreenFramePayload(error = "无障碍服务未启用，无法截图"),
            )
            client?.sendEvent("device.screen_frame", payload)
            return false
        }
        val frame = svc.captureScreenJpegBase64(quality = 55)
        val payload = json.encodeToJsonElement(ScreenFramePayload.serializer(), frame)
        val ok = client?.sendEvent("device.screen_frame", payload) == true
        if (!ok) broadcastLog("屏幕帧上传失败（未连接后端）")
        return true
    }

    private fun forwardInboundMessage(
        sender: String,
        content: String,
        postTimeMs: Long,
        viaNotification: Boolean,
    ) {
        val payload = MessageReceivedPayload(
            contact = Contact(external_id = sender, nickname = sender),
            external_msg_id = "rt_${postTimeMs}_${UUID.randomUUID().toString().take(8)}",
            type = "text",
            content = content,
            sent_at = Instant.ofEpochMilli(postTimeMs).toString(),
        )
        val element = json.encodeToJsonElement(MessageReceivedPayload.serializer(), payload)
        val sent = client?.sendEvent("message.received", element) == true
        val src = if (viaNotification) "notif" else "a11y"
        broadcastLog(
            if (sent) "上报新消息[$src] $sender :: ${content.take(40)}"
            else "上报失败(未连接)[$src] $sender :: ${content.take(40)}"
        )
        Log.i(tag, "inbound[$src] sent=$sent sender=$sender content=${content.take(60)}")
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

    private fun nowText(): String = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
}
