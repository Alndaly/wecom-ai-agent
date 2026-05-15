package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.os.Build
import android.util.Base64
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.wecom.agent.model.ScreenFramePayload
import kotlinx.coroutines.suspendCancellableCoroutine
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume

/**
 * Singleton accessibility service for WeCom. Two jobs:
 *
 *  1. Track current page so [WeComAutomator] / the foreground service know
 *     whether we're on HOME / CHAT / SEARCH / ...
 *  2. When the user is on a ChatActivity, watch the message list and forward
 *     newly-appeared inbound bubbles to a registered callback. This is the
 *     "while-you-watch" channel; the always-on channel is
 *     [MessageNotificationListener].
 *
 * Heuristics are version-dependent — see WeComAutomator for the dump helper.
 */
class WeComAccessibilityService : AccessibilityService() {
    companion object {
        @Volatile var instance: WeComAccessibilityService? = null
            private set

        /** Set by AgentForegroundService; called for every fresh visible message bubble. */
        @Volatile var onChatMessage: ((sender: String, content: String) -> Unit)? = null
    }

    private val tag = "WeComA11y"

    enum class Page { HOME, CHAT, SEARCH, CONTACT, MOMENTS, UNKNOWN }

    @Volatile var currentPage: Page = Page.UNKNOWN
        private set

    @Volatile var currentChatTitle: String? = null
        private set

    /** Dedupe per-session: we never re-fire the same (sender, content) within a short window. */
    private val recent = ArrayDeque<Triple<String, String, Long>>()
    private val recentTtlMs = 10 * 60_000L
    private var baselineChatTitle: String? = null
    private var baselineReady = false

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(tag, "service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> updatePage(event)
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> {
                if (currentPage == Page.CHAT) maybeHarvestChat()
            }
            else -> Unit
        }
    }

    override fun onInterrupt() = Unit

    /** Print the active window's tree into [out]; used by calibration. */
    fun dumpToString(out: StringBuilder) {
        val root = rootInActiveWindow ?: run {
            out.append("rootInActiveWindow is null (is WeCom in foreground?)")
            return
        }
        out.append("=== UI dump pkg=").append(root.packageName).append(" page=").append(currentPage).append(" ===\n")
        walk(root, 0, out)
    }

    suspend fun captureScreenJpegBase64(quality: Int = 55): ScreenFramePayload {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return ScreenFramePayload(error = "实时屏幕需要 Android 11 / API 30 及以上")
        }
        return suspendCancellableCoroutine { cont ->
            takeScreenshot(
                android.view.Display.DEFAULT_DISPLAY,
                mainExecutor,
                object : TakeScreenshotCallback {
                    override fun onSuccess(screenshot: ScreenshotResult) {
                        val bitmap = Bitmap.wrapHardwareBuffer(
                            screenshot.hardwareBuffer,
                            screenshot.colorSpace,
                        )
                        screenshot.hardwareBuffer.close()
                        if (bitmap == null) {
                            cont.resume(ScreenFramePayload(error = "截图失败：bitmap 为空"))
                            return
                        }
                        val software = bitmap.copy(Bitmap.Config.ARGB_8888, false)
                        bitmap.recycle()
                        val out = ByteArrayOutputStream()
                        software.compress(Bitmap.CompressFormat.JPEG, quality.coerceIn(20, 90), out)
                        val width = software.width
                        val height = software.height
                        software.recycle()
                        cont.resume(
                            ScreenFramePayload(
                                image = Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP),
                                mime = "image/jpeg",
                                width = width,
                                height = height,
                            ),
                        )
                    }

                    override fun onFailure(errorCode: Int) {
                        cont.resume(ScreenFramePayload(error = "截图失败：$errorCode"))
                    }
                },
            )
        }
    }

    private fun walk(n: AccessibilityNodeInfo?, depth: Int, sb: StringBuilder) {
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
        }
        sb.append("[$cls]")
        if (id.isNotEmpty()) sb.append(" id=$id")
        if (txt.isNotEmpty()) sb.append(" txt=\"$txt\"")
        if (desc.isNotEmpty()) sb.append(" desc=\"$desc\"")
        if (flags.isNotEmpty()) sb.append(" $flags")
        sb.append('\n')
        for (i in 0 until n.childCount) walk(n.getChild(i), depth + 1, sb)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    // ----------------------------------------------------------------- page
    private fun updatePage(event: AccessibilityEvent) {
        val cls = event.className?.toString().orEmpty()
        currentPage = when {
            cls.contains("LauncherUI", true) ||
                cls.contains("WwMainActivity", true) -> Page.HOME
            cls.contains("ChatActivity", true) ||
                cls.contains("MessageList", true) ||
                cls.contains("MessageInfo", true) -> Page.CHAT
            cls.contains("Search", true) -> Page.SEARCH
            cls.contains("Contact", true) -> Page.CONTACT
            cls.contains("Moments", true) || cls.contains("SNS", true) -> Page.MOMENTS
            else -> Page.UNKNOWN
        }
        Log.d(tag, "page=$currentPage cls=$cls")
        if (currentPage == Page.CHAT) {
            val title = inferChatTitle()
            if (title != currentChatTitle) {
                baselineChatTitle = null
                baselineReady = false
            }
            currentChatTitle = title
        } else {
            baselineChatTitle = null
            baselineReady = false
        }
    }

    private fun inferChatTitle(): String? {
        val root = rootInActiveWindow ?: return null
        // Conventionally the chat title is in a TextView near the top, often a sibling of a back arrow.
        val candidates = mutableListOf<AccessibilityNodeInfo>()
        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val cls = n.className?.toString() ?: ""
            if (cls.contains("TextView", ignoreCase = true)) candidates.add(n)
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)
        // pick the top-most TextView with non-empty text and y < 250px (rough header zone)
        return candidates
            .filter { it.text?.isNotBlank() == true }
            .minByOrNull {
                val r = android.graphics.Rect()
                it.getBoundsInScreen(r)
                r.top
            }
            ?.text?.toString()
    }

    // -------------------------------------------------------- harvest chat
    /** Walk the chat list and emit any unseen inbound bubbles. */
    private fun maybeHarvestChat() {
        val cb = onChatMessage ?: return
        val root = rootInActiveWindow ?: return
        val title = currentChatTitle ?: return

        // Heuristic for an inbound bubble: a TextView whose text is non-empty,
        // sits in the left-half of the screen, and whose nearest scrollable
        // ancestor is the message list. WeCom puts outbound on the right.
        val screenWidth = resources.displayMetrics.widthPixels
        val now = System.currentTimeMillis()
        gc(now)

        val candidates = mutableListOf<String>()
        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val cls = n.className?.toString() ?: ""
            if (cls.contains("TextView", true) && !n.text.isNullOrBlank()) {
                val r = android.graphics.Rect()
                n.getBoundsInScreen(r)
                val content = n.text.toString().trim()
                if (isInboundBubbleCandidate(n, r, screenWidth, content)) {
                    candidates.add(content)
                }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)

        if (!baselineReady || baselineChatTitle != title) {
            for (content in candidates) {
                recent.addLast(Triple(title, content, now))
            }
            baselineChatTitle = title
            baselineReady = true
            Log.d(tag, "baseline chat=$title candidates=${candidates.size}")
            return
        }

        for (content in candidates) {
            if (alreadySeen(title, content)) continue
            recent.addLast(Triple(title, content, now))
            cb(title, content)
            Log.d(tag, "harvest chat=$title content=${content.take(60)}")
        }
    }

    private fun isInboundBubbleCandidate(
        node: AccessibilityNodeInfo,
        bounds: android.graphics.Rect,
        screenWidth: Int,
        content: String,
    ): Boolean {
        // header zone excluded
        if (bounds.top < 200) return false
        // input bar (bottom) excluded
        if (bounds.bottom > resources.displayMetrics.heightPixels - 120) return false
        // outbound bubble (right half) excluded
        if (bounds.left > screenWidth / 2) return false
        if (content.isEmpty() || content.length > 2000) return false
        if (content == currentChatTitle) return false
        if (!hasScrollableAncestor(node)) return false
        return true
    }

    private fun hasScrollableAncestor(node: AccessibilityNodeInfo): Boolean {
        var p = node.parent
        var depth = 0
        while (p != null && depth < 8) {
            if (p.isScrollable) return true
            p = p.parent
            depth += 1
        }
        return false
    }

    private fun alreadySeen(sender: String, content: String): Boolean {
        for ((s, c) in recent) {
            if (s == sender && c == content) return true
        }
        return false
    }

    private fun gc(now: Long) {
        while (recent.isNotEmpty() && now - recent.first().third > recentTtlMs) {
            recent.removeFirst()
        }
    }
}
