package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Generic device-level primitives the backend ReAct agent calls through
 * `device.command`. There is **no** WeCom-specific heuristic in here anymore
 * — the LLM observes the UI tree and decides which primitive to invoke.
 *
 * The only WeCom-aware method left is [openWeCom], used as a pre-flight by
 * the backend to bring the app to foreground before reasoning starts.
 */
class WeComAutomator(
    private val ctx: Context,
    private val log: (String) -> Unit,
) {
    private val tag = "WeComAuto"
    private val wecomPkg = "com.tencent.wework"

    /** UI operations have to happen on the main looper; we just poll. */
    private suspend fun a11y(): AccessibilityService? {
        val svc = withTimeoutOrNull(3_000) {
            while (WeComAccessibilityService.instance == null) delay(100)
            WeComAccessibilityService.instance
        }
        if (svc == null) log("AccessibilityService 未运行（请到「设置 → 无障碍」中打开）")
        return svc
    }

    /** Bring WeCom to the foreground. Returns null on success or an error msg. */
    suspend fun openWeCom(): Pair<Boolean, String> {
        return try {
            val intent = ctx.packageManager.getLaunchIntentForPackage(wecomPkg)
                ?: Intent().apply {
                    component = ComponentName(wecomPkg, "com.tencent.wework.launch.LauncherActivity")
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                }
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
            ctx.startActivity(intent)
            // Give the launcher a moment to settle so the next primitive (usually
            // a UI dump) sees the WeCom tree rather than the previous app.
            delay(700)
            Pair(true, "已打开 WeCom")
        } catch (e: Exception) {
            Pair(false, "openWeCom: ${e.message}")
        }
    }

    // ====================================================================
    //  Primitive ops for the backend ReAct agent. Generic — no WeCom-specific
    //  heuristics; the agent decides what to do based on UI tree + screenshot.
    // ====================================================================

    suspend fun reactTapText(text: String): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val root = svc.rootInActiveWindow ?: return false to "无活动窗口"
        val node = root.findFirst { matchesText(it, text) }
            ?: return false to "未找到包含「$text」的节点"
        var n: AccessibilityNodeInfo? = node
        while (n != null && !n.isClickable) n = n.parent
        val target = n ?: node
        val ok = target.tap()
        return ok to if (ok) "已点击「$text」" else "节点不可点击"
    }

    suspend fun reactTapXY(x: Int, y: Int): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = gestureTap(svc, x.toFloat(), y.toFloat())
        return ok to if (ok) "已在 ($x, $y) 点击" else "手势失败"
    }

    suspend fun reactSwipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long = 300): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = gestureSwipe(svc, x1.toFloat(), y1.toFloat(), x2.toFloat(), y2.toFloat(), durationMs)
        return ok to if (ok) "已滑动 ($x1,$y1)→($x2,$y2)" else "手势失败"
    }

    suspend fun reactInputText(text: String): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val root = svc.rootInActiveWindow ?: return false to "无活动窗口"
        // Prefer the currently focused editable; fall back to any editable.
        val edit = root.findFirst { it.isEditable && it.isFocused }
            ?: root.findFirst { it.isEditable }
            ?: return false to "未找到可编辑输入框"
        return edit.replaceText(text).let { it to if (it) "已输入文本" else "ACTION_SET_TEXT 失败" }
    }

    suspend fun reactBack(): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        return ok to if (ok) "已返回" else "返回手势失败"
    }

    suspend fun reactHome(): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
        return ok to if (ok) "已回主屏" else "Home 手势失败"
    }

    private fun gestureTap(svc: AccessibilityService, x: Float, y: Float): Boolean {
        val path = android.graphics.Path().apply { moveTo(x, y) }
        val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, 80)
        val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
        return svc.dispatchGesture(gesture, null, null)
    }

    private fun gestureSwipe(
        svc: AccessibilityService,
        x1: Float, y1: Float, x2: Float, y2: Float,
        durationMs: Long,
    ): Boolean {
        val path = android.graphics.Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
        return svc.dispatchGesture(gesture, null, null)
    }

    // -------------------------------------------------------- dump helper
    fun dumpTree(svc: AccessibilityService, reason: String) {
        val root = svc.rootInActiveWindow ?: run {
            log("dump[$reason]: rootInActiveWindow is null")
            return
        }
        val sb = StringBuilder()
        sb.append("=== UI dump (").append(reason).append(") pkg=").append(root.packageName).append(" ===\n")
        printNode(root, 0, sb)
        Log.i(tag, sb.toString())
        log("已写入 UI 树到 logcat（tag=$tag, reason=$reason）。adb logcat -s $tag 查看。")
    }

    private fun printNode(n: AccessibilityNodeInfo?, depth: Int, sb: StringBuilder) {
        n ?: return
        sb.append("  ".repeat(depth))
        val cls = n.className?.toString()?.substringAfterLast('.') ?: "?"
        val txt = (n.text?.toString() ?: "").take(40)
        val desc = (n.contentDescription?.toString() ?: "").take(40)
        val id = n.viewIdResourceName?.substringAfterLast('/') ?: ""
        val flags = buildString {
            if (n.isClickable) append("C")
            if (n.isFocusable) append("F")
            if (n.isEditable) append("E")
            if (n.isScrollable) append("S")
            if (n.isCheckable) append("K")
        }
        sb.append("[$cls]")
        if (id.isNotEmpty()) sb.append(" id=$id")
        if (txt.isNotEmpty()) sb.append(" txt=\"$txt\"")
        if (desc.isNotEmpty()) sb.append(" desc=\"$desc\"")
        if (flags.isNotEmpty()) sb.append(" $flags")
        sb.append('\n')
        for (i in 0 until n.childCount) printNode(n.getChild(i), depth + 1, sb)
    }
}

// ---- AccessibilityNodeInfo helpers ----
private fun AccessibilityNodeInfo.findFirst(pred: (AccessibilityNodeInfo) -> Boolean): AccessibilityNodeInfo? {
    if (pred(this)) return this
    for (i in 0 until childCount) {
        val c = getChild(i) ?: continue
        c.findFirst(pred)?.let { return it }
    }
    return null
}

private fun matchesText(n: AccessibilityNodeInfo, s: String): Boolean {
    val a = n.text?.toString().orEmpty()
    val b = n.contentDescription?.toString().orEmpty()
    return a.contains(s) || b.contains(s)
}

private fun AccessibilityNodeInfo.tap(): Boolean {
    return performAction(AccessibilityNodeInfo.ACTION_CLICK)
}

/** Named distinctly to avoid colliding with the deprecated AccessibilityNodeInfo.setText
 *  (which returns Unit and would otherwise win overload resolution). */
private fun AccessibilityNodeInfo.replaceText(text: String): Boolean {
    val bundle = Bundle().apply {
        putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
    }
    return performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)
}
