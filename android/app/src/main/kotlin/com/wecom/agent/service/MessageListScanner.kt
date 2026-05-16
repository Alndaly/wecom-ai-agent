package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Periodically scans the WeCom 消息 tab for unread conversations that the
 * notification listener might have missed. Three tiers — caller drives them
 * at different frequencies:
 *
 *   tier 1 — `scanVisible()`           : just whatever is on screen now
 *   tier 2 — `scanPagesDown(n)`        : swipe up `n` times, harvest each
 *   tier 3 — `scanToBottom(maxSwipes)` : keep swiping until movement stops
 *
 * Every tier restores the list back to its original scroll position when
 * done, so the user's view isn't permanently scrolled.
 *
 * All scans require WeCom to be foreground. If WeCom is foreground but another
 * page/tab is active, the scanner first navigates back to 消息, then harvests.
 */
class MessageListScanner(
    private val log: (String) -> Unit,
    private val shouldYield: () -> Boolean = { false },
) {
    private val tag = "MsgListScanner"

    suspend fun scanVisible(): ScanReport {
        val svc = WeComAccessibilityService.instance ?: return ScanReport.skipped("无障碍服务未运行")
        if (!svc.isWeComForeground()) return ScanReport.skipped("WeCom 不在前台")
        ensureMessagesTab(svc).takeIf { !it.ok }?.let { return it }
        svc.forceHarvestHomeList()
        return ScanReport.ok("已扫描可见区域")
    }

    suspend fun scanPagesDown(pages: Int): ScanReport {
        return scanWithSwipes(maxSwipes = pages.coerceIn(1, 8), stopOnStagnant = false)
    }

    suspend fun scanToBottom(maxSwipes: Int = 30): ScanReport {
        return scanWithSwipes(maxSwipes = maxSwipes.coerceIn(1, 60), stopOnStagnant = true)
    }

    private suspend fun scanWithSwipes(maxSwipes: Int, stopOnStagnant: Boolean): ScanReport {
        val svc = WeComAccessibilityService.instance ?: return ScanReport.skipped("无障碍服务未运行")
        if (!svc.isWeComForeground()) return ScanReport.skipped("WeCom 不在前台")
        ensureMessagesTab(svc).takeIf { !it.ok }?.let { return it }
        val bounds = svc.getMessagesListBounds() ?: return ScanReport.skipped("未找到会话列表")

        // First harvest the top of the list.
        svc.forceHarvestHomeList()

        // Compute swipe path inside the list bounds. Going from a low Y to a
        // high Y in screen coordinates is *up* on screen, which scrolls the
        // list *down* (more recent → older conversations get revealed).
        val cx = (bounds.left + bounds.right) / 2f
        val yLow = bounds.top + bounds.height() * 0.18f
        val yHigh = bounds.bottom - bounds.height() * 0.08f

        var swipes = 0
        var stagnantStreak = 0
        var lastFingerprint = listFingerprint(svc)
        var preempted = false
        while (swipes < maxSwipes) {
            if (shouldYield()) {
                preempted = true
                log("检测到高优任务，提前让出（已下滑 $swipes 次）")
                break
            }
            val ok = swipe(svc, cx, yHigh, cx, yLow, durationMs = 280)
            if (!ok) {
                log("swipe up 失败，提前结束")
                break
            }
            swipes++
            // Let the list settle (animation + content load).
            delay(600)
            svc.forceHarvestHomeList()
            if (stopOnStagnant) {
                val now = listFingerprint(svc)
                if (now == lastFingerprint) {
                    stagnantStreak++
                    if (stagnantStreak >= 2) break  // looks like bottom reached
                } else {
                    stagnantStreak = 0
                    lastFingerprint = now
                }
            }
        }

        // Restore — scroll back up the same number of times. Direction swaps:
        // low → high on screen pulls the list back to top. We DO restore even
        // when preempted, otherwise the operator's view stays scrolled.
        repeat(swipes) {
            swipe(svc, cx, yLow, cx, yHigh, durationMs = 240)
            delay(240)
        }
        return ScanReport.ok(
            if (preempted) "已下滑 $swipes 次并因高优任务提前恢复"
            else "已下滑 $swipes 次并恢复"
        )
    }

    private suspend fun ensureMessagesTab(svc: WeComAccessibilityService): ScanReport {
        if (svc.isOnMessagesTab()) return ScanReport.ok("已在「消息」Tab")

        if (svc.currentPage == WeComAccessibilityService.Page.CHAT) {
            val backOk = svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            log(if (backOk) "从聊天页返回消息列表" else "从聊天页返回失败")
            delay(700)
            if (svc.isOnMessagesTab()) return ScanReport.ok("已返回「消息」Tab")
        }

        val tapped = tapMessagesTab(svc)
        if (!tapped) return ScanReport.skipped("未找到可点击的「消息」Tab")
        delay(800)
        return if (svc.isOnMessagesTab()) {
            ScanReport.ok("已切到「消息」Tab")
        } else {
            ScanReport.skipped("已尝试切到「消息」Tab，但未识别到会话列表")
        }
    }

    private suspend fun tapMessagesTab(svc: AccessibilityService): Boolean {
        val root = svc.rootInActiveWindow ?: return false
        val screenHeight = svc.resources.displayMetrics.heightPixels
        val bottomStart = (screenHeight * 0.72f).toInt()
        val candidates = mutableListOf<AccessibilityNodeInfo>()

        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val text = n.text?.toString()?.trim().orEmpty()
            if (text == "消息") {
                val r = Rect()
                n.getBoundsInScreen(r)
                if (r.centerY() >= bottomStart) candidates.add(n)
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)

        val node = candidates.firstOrNull() ?: return false
        var target: AccessibilityNodeInfo? = node
        while (target != null && !target.isClickable) target = target.parent
        if (target?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true) return true

        val r = Rect()
        node.getBoundsInScreen(r)
        if (r.isEmpty) return false
        return tap(svc, r.centerX().toFloat(), r.centerY().toFloat())
    }

    /** Cheap state-hash of the current list view — used to detect when we've
     *  reached the bottom (subsequent swipes don't change anything). */
    private fun listFingerprint(svc: WeComAccessibilityService): String {
        val r = svc.getMessagesListBounds() ?: return ""
        val root = svc.rootInActiveWindow ?: return ""
        // gather first 6 visible TextView texts inside the list bounds
        val texts = mutableListOf<String>()
        fun walk(n: android.view.accessibility.AccessibilityNodeInfo?) {
            n ?: return
            if (texts.size >= 6) return
            val cls = n.className?.toString().orEmpty()
            if (cls.contains("TextView", true) && !n.text.isNullOrBlank()) {
                val nr = Rect()
                n.getBoundsInScreen(nr)
                if (Rect.intersects(nr, r)) {
                    texts.add(n.text.toString())
                }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)
        return texts.joinToString("|")
    }

    private suspend fun swipe(
        svc: AccessibilityService,
        x1: Float, y1: Float, x2: Float, y2: Float,
        durationMs: Long,
    ): Boolean {
        val path = Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return suspendCancellableCoroutine { cont ->
            val ok = svc.dispatchGesture(
                gesture,
                object : AccessibilityService.GestureResultCallback() {
                    override fun onCompleted(g: GestureDescription?) {
                        if (cont.isActive) cont.resume(true)
                    }
                    override fun onCancelled(g: GestureDescription?) {
                        if (cont.isActive) cont.resume(false)
                    }
                },
                null,
            )
            if (!ok) cont.resume(false)
        }
    }

    private suspend fun tap(svc: AccessibilityService, x: Float, y: Float): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0, 80)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return suspendCancellableCoroutine { cont ->
            val ok = svc.dispatchGesture(
                gesture,
                object : AccessibilityService.GestureResultCallback() {
                    override fun onCompleted(g: GestureDescription?) {
                        if (cont.isActive) cont.resume(true)
                    }
                    override fun onCancelled(g: GestureDescription?) {
                        if (cont.isActive) cont.resume(false)
                    }
                },
                null,
            )
            if (!ok) cont.resume(false)
        }
    }
}

data class ScanReport(val ok: Boolean, val message: String) {
    companion object {
        fun ok(msg: String) = ScanReport(true, msg)
        fun skipped(msg: String) = ScanReport(false, msg)
    }
}
