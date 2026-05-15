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

    @Volatile var dryRun: Boolean = false

    suspend fun run(task: TaskDispatchPayload): TaskResult = mutex.withLock {
        return when (task.type) {
            "send_text" -> {
                // send_text is now executed end-to-end by the backend ReAct
                // agent via `device.command` primitives — the old heuristic
                // executor path has been removed. If a stale dispatch reaches
                // us we surface a clear error rather than silently no-op.
                val msg = "send_text 由后端 ReAct agent 直接执行，本路径已弃用"
                Log.w(tag, msg)
                logSink(msg)
                TaskResult(false, msg)
            }
            else -> {
                Log.w(tag, "unknown task type=${task.type}")
                TaskResult(false, "unknown task type: ${task.type}")
            }
        }
    }
}
