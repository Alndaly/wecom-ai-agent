package com.wecom.agent.service

import android.util.Log
import com.wecom.agent.model.TaskDispatchPayload
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class TaskResult(val success: Boolean, val error: String? = null)

/**
 * Serial executor — only one task runs at a time per device, since we are
 * driving a single phone UI.
 *
 * MVP1 stub: actual UI automation (open WeCom → find chat → send text)
 * will be implemented in [WeComAccessibilityService] and invoked from here.
 */
class TaskExecutor {
    private val tag = "TaskExecutor"
    private val mutex = Mutex()

    suspend fun run(task: TaskDispatchPayload): TaskResult = mutex.withLock {
        return when (task.type) {
            "send_text" -> runSendText(task)
            else -> {
                Log.w(tag, "unknown task type=${task.type}")
                TaskResult(false, "unknown task type: ${task.type}")
            }
        }
    }

    private suspend fun runSendText(task: TaskDispatchPayload): TaskResult {
        val payload = task.payload.jsonObject
        val convExtId = payload["conversation_external_id"]?.jsonPrimitive?.content
        val text = payload["text"]?.jsonPrimitive?.content
        if (convExtId.isNullOrBlank() || text.isNullOrBlank()) {
            return TaskResult(false, "missing fields")
        }
        Log.i(tag, "send_text → $convExtId : $text")
        // TODO(MVP1b): call WeComAccessibilityService.sendText(convExtId, text)
        delay(400)
        return TaskResult(true)
    }
}
