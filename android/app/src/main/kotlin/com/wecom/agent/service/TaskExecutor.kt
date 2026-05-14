package com.wecom.agent.service

import android.content.Context
import android.util.Log
import com.wecom.agent.model.TaskDispatchPayload
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class TaskResult(val success: Boolean, val error: String? = null)

/**
 * Serial executor — only one task runs at a time per device, since we are
 * driving a single phone UI.
 *
 * `dryRun=true` (default until the user toggles the calibration switch) keeps
 * the old "just delay and ack" behaviour so backend wiring can be tested
 * before the phone UI matchers are dialled in.
 */
class TaskExecutor(
    private val ctx: Context,
    private val logSink: (String) -> Unit,
    private val onTaskLog: (suspend (Long?, String, String) -> Unit)? = null,
) {
    private val tag = "TaskExecutor"
    private val mutex = Mutex()
    private val automator = WeComAutomator(ctx, { msg -> logSink(msg) })

    @Volatile var dryRun: Boolean = false

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
        val contactExternalId = payload["conversation_external_id"]?.jsonPrimitive?.content
        val text = payload["text"]?.jsonPrimitive?.content
        if (contactExternalId.isNullOrBlank() || text.isNullOrBlank()) {
            return TaskResult(false, "missing fields")
        }

        logSink("执行 send_text，dry_run=$dryRun")
        if (dryRun) {
            logSink("[dry-run] send_text → $contactExternalId : $text")
            Log.i(tag, "[dry-run] send_text → $contactExternalId : $text")
            kotlinx.coroutines.delay(300)
            return TaskResult(true)
        }

        logSink("发送消息 → $contactExternalId")
        val err = automator.sendText(contactName = contactExternalId, text = text)
        if (err == null) return TaskResult(true)
        onTaskLog?.invoke(task.task_id, "error", err)
        return TaskResult(false, err).also {
            Log.w(tag, "send_text failed: $err")
        }
    }
}
