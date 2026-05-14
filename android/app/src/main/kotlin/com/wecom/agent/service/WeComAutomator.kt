package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.util.Log
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

        // 2. tap search icon on home → type contact → tap first hit
        if (!openSearch(svc)) return "could not open search"
        delay(500)
        if (!typeIntoSearch(svc, contactName)) return "could not type into search"
        delay(900) // let results render
        if (!clickFirstSearchResult(svc, contactName)) {
            dumpTree(svc, "search-no-result")
            return "no search result for '$contactName'"
        }
        delay(800)

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
    private suspend fun openSearch(svc: AccessibilityService): Boolean {
        val root = svc.rootInActiveWindow ?: return false
        // strategy 1: any node with content-description "搜索" / text "搜索"
        val byDesc = root.findFirst { matchesText(it, "搜索") }
        if (byDesc != null && byDesc.tap()) return true

        // strategy 2: ImageButton/ImageView at top-right
        val candidates = root.findAll {
            (it.className?.contains("ImageButton") == true ||
                    it.className?.contains("ImageView") == true) &&
                    it.isClickable
        }
        // pick the right-most top one
        val pick = candidates.maxByOrNull { it.boundsCenterX() - it.boundsCenterY() }
        return pick?.tap() == true
    }

    private suspend fun typeIntoSearch(svc: AccessibilityService, q: String): Boolean {
        // some versions auto-focus the search box; wait briefly for an editable field
        val edit = findEditable(svc, timeoutMs = 3_000) ?: return false
        return edit.replaceText(q)
    }

    private suspend fun clickFirstSearchResult(
        svc: AccessibilityService,
        contactName: String,
    ): Boolean {
        // wait for results to populate
        repeat(20) {
            val root = svc.rootInActiveWindow
            if (root != null) {
                val match = root.findFirst { matchesText(it, contactName) }
                if (match != null) {
                    // climb to the nearest clickable ancestor (a result row is usually a LinearLayout)
                    var n: AccessibilityNodeInfo? = match
                    while (n != null && !n.isClickable) n = n.parent
                    if (n != null && n.tap()) return true
                    if (match.tap()) return true
                }
            }
            delay(150)
        }
        return false
    }

    // -------------------------------------------------------------- chat
    private suspend fun typeIntoChatInput(svc: AccessibilityService, text: String): Boolean {
        val edit = findEditable(svc, timeoutMs = 3_000) ?: return false
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

    private suspend fun findEditable(svc: AccessibilityService, timeoutMs: Long): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val root = svc.rootInActiveWindow
            val edit = root?.findFirst {
                it.isEditable && it.isFocusable
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
    val r = android.graphics.Rect()
    getBoundsInScreen(r)
    return r.centerX()
}

private fun AccessibilityNodeInfo.boundsCenterY(): Int {
    val r = android.graphics.Rect()
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
