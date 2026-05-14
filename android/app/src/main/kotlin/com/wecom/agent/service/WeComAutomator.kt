package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.util.Log
import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Drives the WeCom client through the system AccessibilityService.
 *
 * Locators are heuristic by design — WeCom changes view ids between versions,
 * so we match on **class + content + position** rather than ids. When a step
 * fails the automator dumps the relevant subtree to Logcat so you can fix the
 * locator without re-running ingest.
 */
class WeComAutomator(
    private val ctx: Context,
    private val log: (String) -> Unit,
) {
    private val tag = "WeComAuto"
    private val wecomPkg = "com.tencent.wework"

    private enum class UiPlace { TARGET_CHAT, CHAT, HOME, SEARCH, OTHER }

    /** UI operations have to happen on the main looper; we just poll. */
    private suspend fun a11y(): AccessibilityService? {
        val svc = withTimeoutOrNull(3_000) {
            while (WeComAccessibilityService.instance == null) delay(100)
            WeComAccessibilityService.instance
        }
        if (svc == null) log("AccessibilityService 未运行（请到「设置 → 无障碍」中打开）")
        return svc
    }

    /**
     * Open WeCom and navigate to the chat with [contactName], then send [text].
     * Returns null on success, error message on failure.
     */
    suspend fun sendText(contactName: String, text: String): String? {
        val svc = a11y() ?: return "accessibility service not running"

        // 1. bring WeCom to foreground
        if (!openWeCom()) return "failed to open WeCom"
        if (!waitForPackage(svc, wecomPkg)) return "WeCom not in foreground after open"
        delay(700)

        // 2. Decide from the current UI tree. If we're already in the right
        // chat, do not navigate again; otherwise normalize to a page where
        // contact selection is safe.
        if (!ensureTargetChat(svc, contactName)) {
            dumpTree(svc, "target-chat-not-open")
            return "could not open chat '$contactName'"
        }
        delay(500)

        // 3. type into chat input + click send
        if (!typeIntoChatInput(svc, text)) {
            dumpTree(svc, "chat-input-not-found")
            return "could not type into chat input"
        }
        delay(300)
        if (!clickSendButton(svc)) {
            dumpTree(svc, "send-button-not-found")
            return "could not find send button"
        }
        log("sendText OK → $contactName")
        return null
    }

    private suspend fun ensureTargetChat(
        svc: AccessibilityService,
        contactName: String,
    ): Boolean {
        when (classifyUi(svc, contactName)) {
            UiPlace.TARGET_CHAT -> {
                log("当前已在目标聊天页 → $contactName")
                return true
            }
            UiPlace.CHAT, UiPlace.SEARCH, UiPlace.OTHER -> {
                if (!backToHomeOrTargetChat(svc, contactName)) return false
            }
            UiPlace.HOME -> Unit
        }

        if (classifyUi(svc, contactName) == UiPlace.TARGET_CHAT) return true

        if (clickVisibleConversation(svc, contactName)) {
            delay(700)
            if (classifyUi(svc, contactName) == UiPlace.TARGET_CHAT) return true
        }

        log("未能可靠确认目标聊天页，停止在搜索/更多按钮猜测链路上继续操作")
        return classifyUi(svc, contactName) == UiPlace.TARGET_CHAT
    }

    private suspend fun backToHomeOrTargetChat(
        svc: AccessibilityService,
        contactName: String,
    ): Boolean {
        repeat(5) {
            when (classifyUi(svc, contactName)) {
                UiPlace.TARGET_CHAT, UiPlace.HOME -> return true
                else -> {
                    log("当前页面非目标聊天/首页，执行返回校准")
                    svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                    delay(450)
                }
            }
        }
        return classifyUi(svc, contactName) in setOf(UiPlace.TARGET_CHAT, UiPlace.HOME)
    }

    private fun classifyUi(
        svc: AccessibilityService,
        contactName: String,
    ): UiPlace {
        val root = svc.rootInActiveWindow ?: return UiPlace.OTHER
        val page = (svc as? WeComAccessibilityService)?.currentPage
        val title = if (page == WeComAccessibilityService.Page.CHAT) {
            (svc as? WeComAccessibilityService)?.currentChatTitle.orEmpty()
        } else {
            ""
        }

        if (page == WeComAccessibilityService.Page.CHAT) {
            if (isTargetChat(root, title, contactName)) return UiPlace.TARGET_CHAT
            if (isChatLike(root)) return UiPlace.CHAT
        }
        if (page == WeComAccessibilityService.Page.SEARCH || isSearchLike(root)) return UiPlace.SEARCH
        if (page == WeComAccessibilityService.Page.HOME || isHomeLike(root)) return UiPlace.HOME
        if (isTargetChat(root, title, contactName)) return UiPlace.TARGET_CHAT
        if (isChatLike(root)) return UiPlace.CHAT
        return UiPlace.OTHER
    }

    private fun isTargetChat(
        root: AccessibilityNodeInfo,
        title: String,
        contactName: String,
    ): Boolean {
        val hasChatInput = root.findFirst {
            it.isEditable && it.isFocusable && it.boundsCenterY() > ctx.resources.displayMetrics.heightPixels * 0.55
        } != null
        if (!hasChatInput) return false
        if (title.contains(contactName)) return true
        return root.findFirst {
            matchesText(it, contactName) && it.boundsCenterY() in 60..220
        } != null
    }

    private fun isChatLike(root: AccessibilityNodeInfo): Boolean {
        return root.findFirst { it.isEditable && it.isFocusable } != null
    }

    private fun isSearchLike(root: AccessibilityNodeInfo): Boolean {
        val topEditable = root.findFirst {
            it.isEditable && it.boundsCenterY() < 260
        }
        return topEditable != null && root.findFirst {
            matchesText(it, "搜索") && it.boundsCenterY() < 260
        } != null
    }

    private fun isHomeLike(root: AccessibilityNodeInfo): Boolean {
        val topMessageTitle = root.findFirst {
            matchesText(it, "消息") && it.boundsCenterY() < 180
        }
        val bottomTabs = listOf("消息", "邮件", "文档", "工作台", "通讯录").count { label ->
            root.findFirst { matchesText(it, label) && it.boundsCenterY() > ctx.resources.displayMetrics.heightPixels * 0.75 } != null
        }
        return topMessageTitle != null || bottomTabs >= 2
    }

    // ---------------------------------------------------------------- nav
    private fun openWeCom(): Boolean {
        return try {
            val intent = ctx.packageManager.getLaunchIntentForPackage(wecomPkg)
                ?: Intent().apply {
                    component = ComponentName(wecomPkg, "com.tencent.wework.launch.LauncherActivity")
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                }
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
            ctx.startActivity(intent)
            true
        } catch (e: Exception) {
            log("openWeCom: ${e.message}")
            false
        }
    }

    private suspend fun waitForPackage(svc: AccessibilityService, pkg: String, timeoutMs: Long = 5_000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val root = svc.rootInActiveWindow
            if (root?.packageName?.toString() == pkg) return true
            delay(150)
        }
        return false
    }

    // -------------------------------------------------------------- search
    private suspend fun clickVisibleConversation(
        svc: AccessibilityService,
        contactName: String,
    ): Boolean {
        repeat(8) {
            val root = svc.rootInActiveWindow
            val match = root?.findFirst {
                matchesText(it, contactName) && it.boundsCenterY() > 180
            }
            if (match != null) {
                var n: AccessibilityNodeInfo? = match
                while (n != null && !n.isClickable) n = n.parent
                if (n != null && n.tap()) {
                    log("已点击可见会话 → $contactName")
                    return true
                }
                if (match.tap()) {
                    log("已点击可见会话文本 → $contactName")
                    return true
                }
            }
            delay(150)
        }
        return false
    }

    // -------------------------------------------------------------- chat
    private suspend fun typeIntoChatInput(svc: AccessibilityService, text: String): Boolean {
        val edit = findChatEditable(svc, timeoutMs = 3_000) ?: return false
        return edit.replaceText(text)
    }

    private fun clickSendButton(svc: AccessibilityService): Boolean {
        val root = svc.rootInActiveWindow ?: return false
        // primary: text == 发送 and clickable
        val byText = root.findFirst { it.isClickable && matchesText(it, "发送") }
        if (byText != null && byText.tap()) return true
        // secondary: a Button at the right of the input bar
        val anyButton = root.findAll {
            it.isClickable && (it.className?.contains("Button") == true)
        }.maxByOrNull { it.boundsCenterX() }
        return anyButton?.tap() == true
    }

    private suspend fun findChatEditable(svc: AccessibilityService, timeoutMs: Long): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val root = svc.rootInActiveWindow
            val edit = root?.findFirst {
                it.isEditable && it.isFocusable && it.boundsCenterY() > ctx.resources.displayMetrics.heightPixels * 0.55
            }
            if (edit != null) return edit
            delay(150)
        }
        return null
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

private fun AccessibilityNodeInfo.findAll(pred: (AccessibilityNodeInfo) -> Boolean): List<AccessibilityNodeInfo> {
    val out = mutableListOf<AccessibilityNodeInfo>()
    fun walk(n: AccessibilityNodeInfo) {
        if (pred(n)) out.add(n)
        for (i in 0 until n.childCount) n.getChild(i)?.let(::walk)
    }
    walk(this)
    return out
}

private fun matchesText(n: AccessibilityNodeInfo, s: String): Boolean {
    val a = n.text?.toString().orEmpty()
    val b = n.contentDescription?.toString().orEmpty()
    return a.contains(s) || b.contains(s)
}

private fun AccessibilityNodeInfo.boundsCenterX(): Int {
    val r = Rect()
    getBoundsInScreen(r)
    return r.centerX()
}

private fun AccessibilityNodeInfo.boundsCenterY(): Int {
    val r = Rect()
    getBoundsInScreen(r)
    return r.centerY()
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
