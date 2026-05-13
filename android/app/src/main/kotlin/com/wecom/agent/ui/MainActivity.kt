package com.wecom.agent.ui

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import com.wecom.agent.service.AgentForegroundService

/**
 * Minimal config screen: backend URL / robot_id / token, then start the foreground service.
 *
 * MVP1: built programmatically to avoid a layout XML for the scaffold.
 */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("agent", Context.MODE_PRIVATE)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 64, 48, 48)
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
        val startBtn = Button(this).apply {
            text = "启动 Agent"
            setOnClickListener {
                val base = urlEt.text.toString().trim()
                val rid = ridEt.text.toString().trim()
                val token = tokenEt.text.toString().trim()
                prefs.edit().putString("base_url", base).putString("robot_id", rid).putString("token", token).apply()
                val intent = Intent(this@MainActivity, AgentForegroundService::class.java).apply {
                    putExtra(AgentForegroundService.EXTRA_BASE_URL, base)
                    putExtra(AgentForegroundService.EXTRA_ROBOT_ID, rid)
                    putExtra(AgentForegroundService.EXTRA_TOKEN, token)
                }
                startForegroundService(intent)
            }
        }

        root.addView(urlEt)
        root.addView(ridEt)
        root.addView(tokenEt)
        root.addView(startBtn)
        setContentView(root)
    }
}
