package com.wecom.agent.ui

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.method.ScrollingMovementMethod
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.wecom.agent.service.AgentForegroundService
import com.wecom.agent.service.MessageNotificationListener
import com.wecom.agent.service.WeComAccessibilityService
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Single-screen control panel:
 *   - Backend connection settings (ws url / robot_id / token)
 *   - Permission status row (accessibility, notification listener, battery)
 *   - Dry-run toggle (don't actually drive the WeCom UI yet — just ack)
 *   - Calibration buttons (dump UI tree / test send)
 *   - Live log
 */
class MainActivity : Activity() {
    private val logTag = "AgentMain"
    private lateinit var startBtn: Button
    private lateinit var statusTv: TextView
    private lateinit var logTv: TextView
    private lateinit var permTv: TextView
    private lateinit var a11yBtn: Button
    private lateinit var notifBtn: Button
    private lateinit var batteryBtn: Button
    private lateinit var dryCb: CheckBox
    private lateinit var keepAwakeCb: CheckBox
    private lateinit var a11yIngestCb: CheckBox
    private lateinit var dumpBtn: Button
    private lateinit var testContactEt: EditText
    private lateinit var testTextEt: EditText
    private lateinit var testSendBtn: Button

    @Volatile private var connectionState: String = "idle"
    @Volatile private var agentState: String = "未启动"

    private val stateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != AgentForegroundService.ACTION_STATE_CHANGED) return
            val state = intent.getStringExtra(AgentForegroundService.EXTRA_STATE).orEmpty()
            connectionState = state
            agentState = when (state) {
                "connecting" -> "正在连接后端..."
                "connected" -> "已连接后端"
                "disconnected" -> "连接已断开"
                else -> agentState
            }
            statusTv.text = "Agent ${agentState}"
            updateStartButton(state)
            appendLog("状态更新：$state")
        }
    }

    private val logReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != AgentForegroundService.ACTION_LOG) return
            val message = intent.getStringExtra(AgentForegroundService.EXTRA_MESSAGE).orEmpty()
            if (message.isNotBlank()) appendLog(message)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("agent", Context.MODE_PRIVATE)
        Log.i(logTag, "MainActivity onCreate")

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 64, 48, 48)
        }
        val scroll = ScrollView(this).apply { addView(root) }

        val buildTv = TextView(this).apply {
            text = "WeCom Agent · 真机版 · ${nowText()}"
            setPadding(0, 0, 0, 24)
        }

        val urlEt = EditText(this).apply {
            hint = "ws://10.0.2.2:8000"
            setText(prefs.getString("base_url", "ws://10.0.2.2:8000"))
        }
        val ridEt = EditText(this).apply {
            hint = "robot_id"
            setText(prefs.getString("robot_id", ""))
        }
        val tokenEt = EditText(this).apply {
            hint = "token"
            setText(prefs.getString("token", ""))
        }
        dryCb = CheckBox(this).apply {
            text = "Dry-run（不真正驱动企微 UI，只 ack）"
            isChecked = prefs.getBoolean("dry_run", true)
        }
        keepAwakeCb = CheckBox(this).apply {
            text = "屏幕常亮 / 防熄屏（建议保持开启）"
            isChecked = prefs.getBoolean("keep_screen_on", true)
            setOnCheckedChangeListener { _, checked ->
                prefs.edit().putBoolean("keep_screen_on", checked).apply()
                applyKeepScreenOn(checked)
                appendLog(if (checked) "已开启屏幕常亮" else "已关闭屏幕常亮")
            }
        }
        a11yIngestCb = CheckBox(this).apply {
            text = "无障碍采集聊天消息（实验性，默认关闭）"
            isChecked = prefs.getBoolean("a11y_ingest", false)
            setOnCheckedChangeListener { _, checked ->
                prefs.edit().putBoolean("a11y_ingest", checked).apply()
                val intent = Intent(this@MainActivity, AgentForegroundService::class.java).apply {
                    action = AgentForegroundService.ACTION_SET_A11Y_INGEST
                    putExtra(AgentForegroundService.EXTRA_A11Y_INGEST, checked)
                }
                startService(intent)
                appendLog("无障碍消息采集 = $checked")
            }
        }
        // apply once at startup
        applyKeepScreenOn(prefs.getBoolean("keep_screen_on", true))
        startBtn = Button(this).apply { text = "启动 Agent" }
        statusTv = TextView(this).apply {
            text = "未启动"
            setPadding(0, 24, 0, 0)
        }

        permTv = TextView(this).apply {
            text = "权限状态：检查中..."
            setPadding(0, 24, 0, 12)
        }
        val permRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        a11yBtn = Button(this).apply { text = "打开无障碍" }
        notifBtn = Button(this).apply { text = "打开通知监听" }
        batteryBtn = Button(this).apply { text = "电池白名单" }
        permRow.addView(a11yBtn)
        permRow.addView(notifBtn)
        permRow.addView(batteryBtn)

        val calibTitle = TextView(this).apply {
            text = "—— 真机校准 ——"
            setPadding(0, 32, 0, 8)
        }
        dumpBtn = Button(this).apply { text = "采集当前 UI 树（写 logcat + 上传后端）" }
        testContactEt = EditText(this).apply {
            hint = "测试联系人昵称（用于打开聊天）"
            setText(prefs.getString("test_contact", ""))
        }
        testTextEt = EditText(this).apply {
            hint = "测试文本"
            setText(prefs.getString("test_text", "你好，这是一条测试"))
        }
        testSendBtn = Button(this).apply { text = "本地发送测试" }

        val logHeader = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, 24, 0, 4)
        }
        val logTitleTv = TextView(this).apply {
            text = "日志"
            layoutParams = LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f,
            )
            gravity = android.view.Gravity.CENTER_VERTICAL
        }
        val clearLogBtn = Button(this).apply {
            text = "清空日志"
            setOnClickListener {
                if (::logTv.isInitialized) {
                    logTv.text = "日志:\n"
                    appendLog("日志已清空")
                }
            }
        }
        logHeader.addView(logTitleTv)
        logHeader.addView(clearLogBtn)
        logTv = TextView(this).apply {
            text = "日志:\n"
            movementMethod = ScrollingMovementMethod()
            // cap at ~2000 lines so the activity doesn't OOM on long-running sessions
            setHorizontallyScrolling(false)
        }

        startBtn.setOnClickListener { onStartClicked(prefs, urlEt, ridEt, tokenEt) }
        a11yBtn.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        notifBtn.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        batteryBtn.setOnClickListener {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
        dumpBtn.setOnClickListener {
            val intent = Intent(this, AgentForegroundService::class.java).apply {
                action = AgentForegroundService.ACTION_DUMP_UI
            }
            startService(intent)
            appendLog("已请求采集 UI 树（请确保企微已在前台）")
        }
        testSendBtn.setOnClickListener {
            val c = testContactEt.text.toString().trim()
            val t = testTextEt.text.toString().trim()
            if (c.isBlank() || t.isBlank()) {
                Toast.makeText(this, "联系人和文本都不能为空", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            prefs.edit().putString("test_contact", c).putString("test_text", t).apply()
            val intent = Intent(this, AgentForegroundService::class.java).apply {
                action = AgentForegroundService.ACTION_SEND_TEST
                putExtra(AgentForegroundService.EXTRA_TEST_CONTACT, c)
                putExtra(AgentForegroundService.EXTRA_TEST_TEXT, t)
            }
            startService(intent)
            appendLog("已请求本地发送测试 → $c")
        }
        dryCb.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean("dry_run", checked).apply()
            val intent = Intent(this, AgentForegroundService::class.java).apply {
                action = AgentForegroundService.ACTION_SET_DRY_RUN
                putExtra(AgentForegroundService.EXTRA_DRY_RUN, checked)
            }
            startService(intent)
            appendLog("dry_run = $checked （已请求即时生效）")
        }

        listOf(
            buildTv, urlEt, ridEt, tokenEt, dryCb, keepAwakeCb, a11yIngestCb, startBtn, statusTv,
            permTv, permRow,
            calibTitle, dumpBtn, testContactEt, testTextEt, testSendBtn,
            logHeader, logTv,
        ).forEach { root.addView(it) }
        setContentView(scroll)

        registerReceiver(stateReceiver, IntentFilter(AgentForegroundService.ACTION_STATE_CHANGED))
        registerReceiver(logReceiver, IntentFilter(AgentForegroundService.ACTION_LOG))
        appendLog("页面已加载")
    }

    override fun onResume() {
        super.onResume()
        refreshPermissionStatus()
        // Re-apply in case some system event cleared the flag (e.g. config change)
        val prefs = getSharedPreferences("agent", Context.MODE_PRIVATE)
        applyKeepScreenOn(prefs.getBoolean("keep_screen_on", true))
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(stateReceiver) }
        runCatching { unregisterReceiver(logReceiver) }
        super.onDestroy()
    }

    private fun onStartClicked(
        prefs: android.content.SharedPreferences,
        urlEt: EditText, ridEt: EditText, tokenEt: EditText,
    ) {
        if (connectionState == "connected") {
            appendLog("点击断开 Agent")
            val intent = Intent(this, AgentForegroundService::class.java).apply {
                action = AgentForegroundService.ACTION_STOP
            }
            startForegroundService(intent)
            updateStartButton("disconnected")
            return
        }

        appendLog("点击启动 Agent")
        statusTv.text = "正在检查配置..."
        val base = urlEt.text.toString().trim()
        val rid = ridEt.text.toString().trim()
        val token = tokenEt.text.toString().trim()
        if (base.isBlank() || rid.isBlank() || token.isBlank()) {
            statusTv.text = "启动失败：请先填完 ws 地址、robot_id 和 token"
            Toast.makeText(this, "请先填完字段", Toast.LENGTH_SHORT).show()
            return
        }

        prefs.edit()
            .putString("base_url", base)
            .putString("robot_id", rid)
            .putString("token", token)
            .apply()

        try {
            val intent = Intent(this, AgentForegroundService::class.java).apply {
                putExtra(AgentForegroundService.EXTRA_BASE_URL, base)
                putExtra(AgentForegroundService.EXTRA_ROBOT_ID, rid)
                putExtra(AgentForegroundService.EXTRA_TOKEN, token)
                putExtra(AgentForegroundService.EXTRA_DRY_RUN, dryCb.isChecked)
                putExtra(AgentForegroundService.EXTRA_A11Y_INGEST, a11yIngestCb.isChecked)
            }
            startForegroundService(intent)
            agentState = "正在连接后端..."
            statusTv.text = "Agent 已启动，$agentState"
            updateStartButton("connecting")
            Toast.makeText(this, "Agent 已启动", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Log.e(logTag, "start service failed", e)
            statusTv.text = "启动失败: ${e.message}"
            Toast.makeText(this, "启动失败: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    // ----------------------------------------------------- permissions
    private fun refreshPermissionStatus() {
        val a11y = isAccessibilityEnabled()
        val notif = isNotificationListenerEnabled()
        val battery = isIgnoringBatteryOptimizations()
        val parts = listOf(
            "无障碍：${if (a11y) "✅" else "❌"}",
            "通知监听：${if (notif) "✅" else "❌"}",
            "电池白名单：${if (battery) "✅" else "❌"}",
        )
        permTv.text = parts.joinToString("  ")
        a11yBtn.visibility = if (a11y) View.GONE else View.VISIBLE
        notifBtn.visibility = if (notif) View.GONE else View.VISIBLE
        batteryBtn.visibility = if (battery) View.GONE else View.VISIBLE
    }

    private fun isAccessibilityEnabled(): Boolean {
        val cn = ComponentName(this, WeComAccessibilityService::class.java).flattenToString()
        val enabled = Settings.Secure.getString(
            contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        ).orEmpty()
        return enabled.split(':').any { it.equals(cn, ignoreCase = true) }
    }

    private fun isNotificationListenerEnabled(): Boolean {
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners").orEmpty()
        val cn = ComponentName(this, MessageNotificationListener::class.java).flattenToString()
        return flat.split(':').any { it.equals(cn, ignoreCase = true) }
    }

    private fun isIgnoringBatteryOptimizations(): Boolean {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.isIgnoringBatteryOptimizations(packageName)
    }

    // -------------------------------------------------------- helpers
    private val maxLogLines = 500

    private fun appendLog(message: String) {
        val line = "${nowText()}  $message"
        Log.i(logTag, line)
        if (!::logTv.isInitialized) return
        logTv.append("$line\n")
        // bounded buffer — drop the oldest 25% when we exceed the cap
        val text = logTv.text.toString()
        val lineCount = text.count { it == '\n' }
        if (lineCount > maxLogLines) {
            val toDrop = lineCount - (maxLogLines * 3 / 4)
            var idx = 0
            repeat(toDrop) {
                val nl = text.indexOf('\n', idx)
                if (nl < 0) return
                idx = nl + 1
            }
            logTv.text = "日志:（已截断,只保留最近 ${maxLogLines * 3 / 4} 行）\n" + text.substring(idx)
        }
    }

    private fun nowText(): String =
        SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())

    /** Toggle the window flag that keeps the screen on while this Activity
     *  is visible. The foreground service holds a separate PARTIAL_WAKE_LOCK
     *  that keeps the CPU awake when the screen is off. */
    private fun applyKeepScreenOn(on: Boolean) {
        if (on) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    private fun updateStartButton(state: String) {
        when (state) {
            "connecting" -> {
                startBtn.text = "连接中..."
                startBtn.isEnabled = false
            }
            "connected" -> {
                startBtn.text = "断开 Agent"
                startBtn.isEnabled = true
            }
            "disconnected" -> {
                startBtn.text = "重新连接 Agent"
                startBtn.isEnabled = true
            }
            else -> {
                startBtn.text = "启动 Agent"
                startBtn.isEnabled = true
            }
        }
    }
}
