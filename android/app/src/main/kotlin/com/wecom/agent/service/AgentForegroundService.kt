package com.wecom.agent.service

import android.app.*
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.util.Log
import androidx.core.app.NotificationCompat
import com.wecom.agent.R
import com.wecom.agent.model.Contact
import com.wecom.agent.model.DeviceCommandAckPayload
import com.wecom.agent.model.HeartbeatPayload
import com.wecom.agent.model.MessageReceivedPayload
import com.wecom.agent.model.ScreenFramePayload
import com.wecom.agent.net.BackendClient
import kotlinx.coroutines.*
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.time.Instant
import java.util.UUID

/**
 * Foreground service that owns the WebSocket lifecycle and device primitives.
 * Started by MainActivity after the user configures backend URL / robot_id / token.
 */
class AgentForegroundService : Service() {
    companion object {
        const val CHANNEL_ID = "wecom_agent"
        const val NOTIFICATION_ID = 1001
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_ROBOT_ID = "robot_id"
        const val EXTRA_TOKEN = "token"
        const val EXTRA_A11Y_INGEST = "a11y_ingest"
        const val ACTION_STOP = "com.wecom.agent.ACTION_STOP"
        const val ACTION_STATE_CHANGED = "com.wecom.agent.ACTION_STATE_CHANGED"
        const val ACTION_LOG = "com.wecom.agent.ACTION_LOG"
        const val ACTION_DUMP_UI = "com.wecom.agent.ACTION_DUMP_UI"
        const val ACTION_SET_A11Y_INGEST = "com.wecom.agent.ACTION_SET_A11Y_INGEST"
        const val EXTRA_STATE = "state"
        const val EXTRA_MESSAGE = "message"
    }

    private val tag = "AgentSvc"
    private val json = Json { ignoreUnknownKeys = true }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var client: BackendClient? = null
    private var heartbeatJob: Job? = null
    private var screenStreamJob: Job? = null
    // periodic message-list scanner — three tiers at different cadences
    private var scanTier1Job: Job? = null
    private var scanTier2Job: Job? = null
    private var scanTier3Job: Job? = null
    private var chatScanJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var connected = false
    @Volatile private var a11yInboundEnabled = false
    private data class PendingInbound(
        val payload: MessageReceivedPayload,
        val src: String,
        val senderType: String,
    )
    private val pendingInbound = ArrayDeque<PendingInbound>()
    private val pendingInboundCap = 200
    private data class RecentAutomatedOutput(val content: String, val at: Long)
    private val recentAutomatedOutputs = ArrayDeque<RecentAutomatedOutput>()
    private val automatedOutputTtlMs = 10 * 60_000L

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
            ACTION_SET_A11Y_INGEST -> {
                a11yInboundEnabled = intent.getBooleanExtra(EXTRA_A11Y_INGEST, false)
                broadcastLog("无障碍消息采集已即时更新为 $a11yInboundEnabled")
                startMessageListScanners()
                return START_STICKY
            }
        }

        ensureChannel()
        startForeground(NOTIFICATION_ID, buildNotification("starting"))
        acquireWakeLockIfNeeded()

        val base = intent?.getStringExtra(EXTRA_BASE_URL) ?: return START_NOT_STICKY
        val rid = intent.getStringExtra(EXTRA_ROBOT_ID) ?: return START_NOT_STICKY
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: return START_NOT_STICKY
        a11yInboundEnabled = intent.getBooleanExtra(EXTRA_A11Y_INGEST, false)
        Log.i(tag, "service starting base=$base robot_id=$rid token_len=${token.length} a11yInbound=$a11yInboundEnabled")
        updateNotification("connecting $rid")
        broadcastState("connecting")
        broadcastLog("服务启动，正在连接 $base")
        requestNotificationListenerRebind()

        // wire inbound channels → ws.message.received
        MessageNotificationListener.registerCallback { sender, content, postTime ->
            forwardInboundMessage(sender, content, postTime, viaNotification = true)
        }
        WeComAccessibilityService.onChatMessage = { sender, content, fromSelf ->
            if (a11yInboundEnabled) {
                forwardChatMessage(sender, content, System.currentTimeMillis(), fromSelf = fromSelf)
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
                if (connected) {
                    flushPendingInbound()
                }
            },
        ).also { it.start() }

        startMessageListScanners()
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
        scanTier1Job?.cancel(); scanTier1Job = null
        scanTier2Job?.cancel(); scanTier2Job = null
        scanTier3Job?.cancel(); scanTier3Job = null
        chatScanJob?.cancel(); chatScanJob = null
        client?.stop()
        MessageNotificationListener.unregisterCallback()
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

    private fun requestNotificationListenerRebind() {
        try {
            val cn = ComponentName(this, MessageNotificationListener::class.java)
            NotificationListenerService.requestRebind(cn)
            broadcastLog("已请求系统重连通知监听服务")
        } catch (e: Exception) {
            Log.w(tag, "notification listener rebind request failed", e)
            broadcastLog("通知监听重连请求失败: ${e.message}")
        }
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
        val result = svc.dumpTreeWithNodes()
        broadcastLog("UI 树共 ${result.tree.length} 字符，${result.nodes.size} 个节点，已上报后端")
        val metrics = resources.displayMetrics
        val payload = json.encodeToJsonElement(
            com.wecom.agent.model.UiDumpPayload.serializer(),
            com.wecom.agent.model.UiDumpPayload(
                reason = reason,
                request_id = requestId,
                current_page = currentPage(),
                tree = result.tree,
                screen_width = metrics.widthPixels,
                screen_height = metrics.heightPixels,
                input_panel_visible = svc.isInputPanelVisible(),
                nodes = result.nodes.map { n ->
                    com.wecom.agent.model.UiNode(
                        id = n.id,
                        cls = n.cls,
                        view_id = n.viewId,
                        text = n.text,
                        desc = n.desc,
                        clickable = n.clickable,
                        focusable = n.focusable,
                        editable = n.editable,
                        scrollable = n.scrollable,
                        bounds = listOf(n.bounds.left, n.bounds.top, n.bounds.right, n.bounds.bottom),
                    )
                },
            ),
        )
        val ok = client?.sendEvent("device.ui_dump", payload) == true
        broadcastLog(if (ok) "UI 树已上传" else "UI 树未上传（未连接后端）")
    }

    private fun handleEvent(event: String, payload: JsonElement?) {
        Log.i(tag, "<- $event")
        broadcastLog("收到后端事件：$event")
        when (event) {
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
                    "screenshot_once",
                    "tap_text",
                    "tap_node",
                    "tap_xy",
                    "double_tap_node",
                    "double_tap_xy",
                    "long_press_node",
                    "long_press_xy",
                    "drag_xy",
                    "swipe",
                    "input_text",
                    "back",
                    "home",
                    "open_wecom" -> {
                        scope.launch { handleReactCommand(command, obj) }
                    }
                    else -> {
                        sendCommandAck(command ?: "unknown", false, "未知设备命令")
                        broadcastLog("未知设备命令：$command")
                    }
                }
            }
        }
    }

    // ----- periodic 消息 tab scanner ----------------------------------------
    //  Notifications miss messages when WeCom is foregrounded (no system
    //  notification fires). To catch those, we walk the conversation list at
    //  three cadences. All three skip cleanly when:
    //    - a11y ingest is off (user disabled the checkbox)
    //    - WeCom isn't in the foreground (don't hijack other apps)
    //    - user isn't on the 消息 tab (don't scroll their 通讯录/工作台)
    private fun startMessageListScanners() {
        if (scanTier1Job != null) {
            Log.d(tag, "message list scanners already running")
            return  // already running (sticky service)
        }
        Log.i(tag, "starting message list scanners a11yInbound=$a11yInboundEnabled")
        broadcastLog("消息列表巡检已启动（a11y=$a11yInboundEnabled）")
        val scanner = MessageListScanner { msg -> broadcastLog("scan: $msg") }

        scanTier1Job = scope.launch {
            // tier 1 — visible only, frequent. Cheap: no scrolling.
            delay(1_000L)  // prime the baseline before a fresh unread preview arrives
            while (isActive) {
                if (a11yInboundEnabled) {
                    val r = scanner.scanVisible()
                    Log.i(tag, "scan tier1 ok=${r.ok} ${r.message}")
                    if (!r.ok) broadcastLog("scan tier1: ${r.message}")
                }
                delay(5_000L)
            }
        }
        scanTier2Job = scope.launch {
            // tier 2 — 3-page scroll, medium frequency.
            delay(60_000L)
            while (isActive) {
                if (a11yInboundEnabled) {
                    val r = scanner.scanPagesDown(pages = 3)
                    Log.d(tag, "scan tier2 ok=${r.ok} ${r.message}")
                    if (r.ok) broadcastLog("scan tier2: ${r.message}")
                }
                delay(5 * 60_000L)
            }
        }
        scanTier3Job = scope.launch {
            // tier 3 — full scroll, rare. Picks up old conversations buried at
            // the bottom that haven't been re-promoted to the top.
            delay(10 * 60_000L)
            while (isActive) {
                if (a11yInboundEnabled) {
                    val r = scanner.scanToBottom(maxSwipes = 30)
                    Log.d(tag, "scan tier3 ok=${r.ok} ${r.message}")
                    if (r.ok) broadcastLog("scan tier3: ${r.message}")
                }
                delay(30 * 60_000L)
            }
        }
        chatScanJob = scope.launch {
            delay(3_000L)
            while (isActive) {
                if (a11yInboundEnabled) {
                    WeComAccessibilityService.instance?.forceHarvestCurrentChat()
                }
                delay(2_000L)
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

    // ---------------------------------------------------------------- ReAct
    //  Each primitive runs once and replies via `device.command_result`. The
    //  backend agent correlates by `request_id` and decides the next action.
    private suspend fun handleReactCommand(command: String, obj: kotlinx.serialization.json.JsonObject) {
        val requestId = obj["request_id"]?.jsonPrimitive?.contentOrNull
        if (requestId == null) {
            broadcastLog("ReAct 命令缺少 request_id：$command")
            sendCommandAck(command, false, "missing request_id")
            return
        }
        broadcastLog("ReAct ← $command (req=${requestId.take(8)})")
        val started = System.currentTimeMillis()
        var data: kotlinx.serialization.json.JsonElement? = null
        val automator = WeComAutomator(this) { msg -> broadcastLog("ReAct: $msg") }

        val result: Pair<Boolean, String> = try {
            when (command) {
                "screenshot_once" -> {
                    val svc = WeComAccessibilityService.instance
                    if (svc == null) {
                        Pair(false, "无障碍未启用")
                    } else {
                        val frame = svc.captureScreenJpegBase64(quality = 55, allowCached = false)
                        data = json.encodeToJsonElement(ScreenFramePayload.serializer(), frame)
                        Pair(frame.error == null, frame.error ?: "已截图")
                    }
                }
                "tap_text" -> {
                    val text = obj["text"]?.jsonPrimitive?.contentOrNull
                    if (text.isNullOrBlank()) Pair(false, "缺少 text 参数")
                    else automator.reactTapText(text)
                }
                "tap_node" -> {
                    val nodeId = obj["node_id"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val x = obj["x"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y = obj["y"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    if (nodeId == null) Pair(false, "缺少 node_id")
                    else automator.reactTapNode(nodeId, x, y)
                }
                "tap_xy" -> {
                    val x = obj["x"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y = obj["y"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    if (x == null || y == null) Pair(false, "缺少 x/y")
                    else automator.reactTapXY(x, y)
                }
                "double_tap_node" -> {
                    val nodeId = obj["node_id"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val x = obj["x"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y = obj["y"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    if (nodeId == null) Pair(false, "缺少 node_id")
                    else automator.reactDoubleTapNode(nodeId, x, y)
                }
                "double_tap_xy" -> {
                    val x = obj["x"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y = obj["y"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    if (x == null || y == null) Pair(false, "缺少 x/y")
                    else automator.reactDoubleTapXY(x, y)
                }
                "long_press_node" -> {
                    val nodeId = obj["node_id"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val x = obj["x"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y = obj["y"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val dur = obj["duration_ms"]?.jsonPrimitive?.contentOrNull?.toLongOrNull() ?: 650L
                    if (nodeId == null) Pair(false, "缺少 node_id")
                    else automator.reactLongPressNode(nodeId, x, y, dur)
                }
                "long_press_xy" -> {
                    val x = obj["x"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y = obj["y"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val dur = obj["duration_ms"]?.jsonPrimitive?.contentOrNull?.toLongOrNull() ?: 650L
                    if (x == null || y == null) Pair(false, "缺少 x/y")
                    else automator.reactLongPressXY(x, y, dur)
                }
                "drag_xy" -> {
                    val x1 = obj["x1"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y1 = obj["y1"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val x2 = obj["x2"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y2 = obj["y2"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val dur = obj["duration_ms"]?.jsonPrimitive?.contentOrNull?.toLongOrNull() ?: 450L
                    if (x1 == null || y1 == null || x2 == null || y2 == null)
                        Pair(false, "缺少 x1/y1/x2/y2")
                    else automator.reactDragXY(x1, y1, x2, y2, dur)
                }
                "swipe" -> {
                    val x1 = obj["x1"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y1 = obj["y1"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val x2 = obj["x2"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val y2 = obj["y2"]?.jsonPrimitive?.contentOrNull?.toIntOrNull()
                    val dur = obj["duration_ms"]?.jsonPrimitive?.contentOrNull?.toLongOrNull() ?: 300L
                    if (x1 == null || y1 == null || x2 == null || y2 == null)
                        Pair(false, "缺少 x1/y1/x2/y2")
                    else automator.reactSwipe(x1, y1, x2, y2, dur)
                }
                "input_text" -> {
                    val text = obj["text"]?.jsonPrimitive?.contentOrNull
                    val mode = obj["mode"]?.jsonPrimitive?.contentOrNull ?: "replace"
                    if (text == null) {
                        Pair(false, "缺少 text")
                    } else {
                        val r = automator.reactInputText(text, mode)
                        if (r.first) rememberAutomatedOutput(text)
                        r
                    }
                }
                "back" -> automator.reactBack()
                "home" -> automator.reactHome()
                "open_wecom" -> automator.openWeCom()
                else -> Pair(false, "未知命令 $command")
            }
        } catch (e: Exception) {
            Log.w(tag, "react command $command failed", e)
            Pair(false, e.message ?: e::class.java.simpleName)
        }
        val ok = result.first
        val msg = result.second

        val elapsed = System.currentTimeMillis() - started
        broadcastLog("ReAct → $command ok=$ok msg=$msg (${elapsed}ms)")
        val payload = json.encodeToJsonElement(
            com.wecom.agent.model.DeviceCommandResultPayload.serializer(),
            com.wecom.agent.model.DeviceCommandResultPayload(
                command = command,
                request_id = requestId,
                ok = ok,
                message = msg,
                data = data,
            ),
        )
        client?.sendEvent("device.command_result", payload)
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
        val frame = svc.captureScreenJpegBase64(quality = 55, allowCached = true)
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
        forwardMessage(sender, content, postTimeMs, senderType = "customer", src = if (viaNotification) "notif" else "a11y")
    }

    private fun forwardChatMessage(
        sender: String,
        content: String,
        postTimeMs: Long,
        fromSelf: Boolean,
    ) {
        if (fromSelf && isRecentAutomatedOutput(content)) {
            Log.i(tag, "skip automated self echo sender=$sender content=${content.take(60)}")
            broadcastLog("跳过自动发送回显[$sender] ${content.take(40)}")
            return
        }
        forwardMessage(sender, content, postTimeMs, senderType = if (fromSelf) "human" else "customer", src = if (fromSelf) "a11y-self" else "a11y")
    }

    @Synchronized
    private fun rememberAutomatedOutput(content: String) {
        val normalized = normalizeMessageContent(content)
        if (normalized.isBlank()) return
        val now = System.currentTimeMillis()
        gcAutomatedOutputs(now)
        recentAutomatedOutputs.addLast(RecentAutomatedOutput(normalized, now))
        while (recentAutomatedOutputs.size > 100) recentAutomatedOutputs.removeFirst()
        Log.d(tag, "remember automated output len=${normalized.length}")
    }

    @Synchronized
    private fun isRecentAutomatedOutput(content: String): Boolean {
        val normalized = normalizeMessageContent(content)
        if (normalized.isBlank()) return false
        val now = System.currentTimeMillis()
        gcAutomatedOutputs(now)
        return recentAutomatedOutputs.any { item ->
            normalized == item.content ||
                normalized.startsWith(item.content.take(80)) ||
                item.content.startsWith(normalized.take(80))
        }
    }

    private fun gcAutomatedOutputs(now: Long) {
        while (recentAutomatedOutputs.isNotEmpty() && now - recentAutomatedOutputs.first().at > automatedOutputTtlMs) {
            recentAutomatedOutputs.removeFirst()
        }
    }

    private fun normalizeMessageContent(content: String): String {
        return content.trim().replace(Regex("""\s+"""), " ")
    }

    private fun forwardMessage(
        sender: String,
        content: String,
        postTimeMs: Long,
        senderType: String,
        src: String,
    ) {
        val payload = MessageReceivedPayload(
            contact = Contact(external_id = sender, nickname = sender),
            external_msg_id = "rt_${postTimeMs}_${UUID.randomUUID().toString().take(8)}",
            type = "text",
            content = content,
            sender_type = senderType,
            sent_at = Instant.ofEpochMilli(postTimeMs).toString(),
        )
        val sent = sendInboundPayload(payload)
        if (!sent) {
            enqueuePendingInbound(PendingInbound(payload, src, senderType))
        }
        broadcastLog(
            if (sent) "上报消息[$src/$senderType] $sender :: ${content.take(40)}"
            else "上报暂存(未连接)[$src/$senderType] $sender :: ${content.take(40)}"
        )
        Log.i(tag, "message[$src/$senderType] sent=$sent queued=${!sent} sender=$sender content=${content.take(60)}")
    }

    private fun sendInboundPayload(payload: MessageReceivedPayload): Boolean {
        val element = json.encodeToJsonElement(MessageReceivedPayload.serializer(), payload)
        return client?.sendEvent("message.received", element) == true
    }

    @Synchronized
    private fun enqueuePendingInbound(item: PendingInbound) {
        pendingInbound.addLast(item)
        while (pendingInbound.size > pendingInboundCap) {
            val dropped = pendingInbound.removeFirst()
            Log.w(tag, "drop pending inbound src=${dropped.src} senderType=${dropped.senderType} content=${dropped.payload.content.take(60)}")
        }
    }

    @Synchronized
    private fun flushPendingInbound() {
        if (pendingInbound.isEmpty()) return
        var sent = 0
        while (pendingInbound.isNotEmpty()) {
            val item = pendingInbound.first()
            if (!sendInboundPayload(item.payload)) break
            pendingInbound.removeFirst()
            sent++
        }
        if (sent > 0) {
            broadcastLog("已补发暂存入站消息 $sent 条，剩余 ${pendingInbound.size} 条")
        }
        Log.i(tag, "flush pending inbound sent=$sent remaining=${pendingInbound.size}")
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
