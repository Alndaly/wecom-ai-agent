package com.wecom.agent.ui

import android.app.Activity
import android.content.Context
import android.content.BroadcastReceiver
import android.content.Intent
import android.content.IntentFilter
import android.os.Bundle
import android.util.Log
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.wecom.agent.service.AgentForegroundService
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Minimal config screen: backend URL / robot_id / token, then start the foreground service.
 *
 * MVP1: built programmatically to avoid a layout XML for the scaffold.
 */
class MainActivity : Activity() {
    private val logTag = "AgentMain"
    private lateinit var startBtn: Button
    private lateinit var statusTv: TextView
    private lateinit var logTv: TextView
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
        val scroll = ScrollView(this).apply {
            addView(root)
        }

        val buildTv = TextView(this).apply {
            text = "WeCom Agent debug-ui-v2 - ${nowText()}"
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
        startBtn = Button(this).apply {
            text = "启动 Agent"
        }
        statusTv = TextView(this).apply {
            text = "未启动"
            setPadding(0, 24, 0, 0)
        }
        logTv = TextView(this).apply {
            text = "日志:\n"
            setPadding(0, 24, 0, 0)
        }

        startBtn.apply {
            setOnClickListener {
                if (connectionState == "connected") {
                    appendLog("点击断开 Agent")
                    val intent = Intent(this@MainActivity, AgentForegroundService::class.java).apply {
                        action = AgentForegroundService.ACTION_STOP
                    }
                    startForegroundService(intent)
                    updateStartButton("disconnected")
                    return@setOnClickListener
                }

                appendLog("点击启动 Agent")
                Log.i(logTag, "start button clicked")
                statusTv.text = "正在检查配置..."
                val base = urlEt.text.toString().trim()
                val rid = ridEt.text.toString().trim()
                val token = tokenEt.text.toString().trim()
                appendLog("base=$base robot_id=${rid.ifBlank { "<empty>" }} token_len=${token.length}")
                if (base.isBlank() || rid.isBlank() || token.isBlank()) {
                    statusTv.text = "启动失败：请先填完 ws 地址、robot_id 和 token"
                    appendLog("启动失败：字段未填完")
                    Toast.makeText(this@MainActivity, "请先填完 ws 地址、robot_id 和 token", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }

                prefs.edit()
                    .putString("base_url", base)
                    .putString("robot_id", rid)
                    .putString("token", token)
                    .apply()

                try {
                    val intent = Intent(this@MainActivity, AgentForegroundService::class.java).apply {
                        putExtra(AgentForegroundService.EXTRA_BASE_URL, base)
                        putExtra(AgentForegroundService.EXTRA_ROBOT_ID, rid)
                        putExtra(AgentForegroundService.EXTRA_TOKEN, token)
                    }
                    startForegroundService(intent)
                    agentState = "正在连接后端..."
                    statusTv.text = "Agent 已启动，$agentState"
                    updateStartButton("connecting")
                    appendLog("已调用 startForegroundService")
                    Toast.makeText(this@MainActivity, "Agent 已启动", Toast.LENGTH_SHORT).show()
                } catch (e: Exception) {
                    Log.e(logTag, "start service failed", e)
                    statusTv.text = "启动失败: ${e.message}"
                    appendLog("启动异常：${e.javaClass.simpleName}: ${e.message}")
                    Toast.makeText(this@MainActivity, "启动失败: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }

        root.addView(buildTv)
        root.addView(urlEt)
        root.addView(ridEt)
        root.addView(tokenEt)
        root.addView(startBtn)
        root.addView(statusTv)
        root.addView(logTv)
        setContentView(scroll)
        registerReceiver(stateReceiver, IntentFilter(AgentForegroundService.ACTION_STATE_CHANGED))
        registerReceiver(logReceiver, IntentFilter(AgentForegroundService.ACTION_LOG))
        appendLog("页面已加载")
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(stateReceiver) }
        runCatching { unregisterReceiver(logReceiver) }
        super.onDestroy()
    }

    private fun appendLog(message: String) {
        val line = "${nowText()}  $message"
        Log.i(logTag, line)
        if (::logTv.isInitialized) {
            logTv.append("$line\n")
        }
    }

    private fun nowText(): String =
        SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())

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
