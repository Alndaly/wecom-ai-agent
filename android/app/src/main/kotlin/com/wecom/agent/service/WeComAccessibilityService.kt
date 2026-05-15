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
    /** Snapshot of the message-list rows seen the first time we land on it; we
     *  only emit rows whose preview *changes* compared to this baseline so we
     *  don't re-fire all history on startup. */
    private val homeListBaseline = HashMap<String, String>()
    private var homeBaselineReady = false

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
                // Dispatch by *current root content* rather than the cached
                // `currentPage`. WeCom switches tabs inside a single activity,
                // so WINDOW_STATE_CHANGED doesn't fire and the cache stays stale
                // (often UNKNOWN), which used to suppress harvest entirely.
                val root = rootInActiveWindow ?: return
                if (root.packageName?.toString() != "com.tencent.wework") return
                if (isMessagesListVisible(root)) {
                    maybeHarvestHomeList()
                } else if (looksLikeChatPage(root)) {
                    maybeHarvestChat()
                }
            }
            else -> Unit
        }
    }

    private fun looksLikeChatPage(root: AccessibilityNodeInfo): Boolean {
        // Cheap signal: a focusable+editable below the mid-line is the chat
        // input. The top is also a TextView (chat title) but we don't gate
        // on it — we just need to know we're inside *some* conversation.
        val screenH = resources.displayMetrics.heightPixels
        var found = false
        fun walk(n: AccessibilityNodeInfo?) {
            if (n == null || found) return
            if (n.isEditable && n.isFocusable) {
                val r = android.graphics.Rect()
                n.getBoundsInScreen(r)
                if (r.centerY() > screenH * 0.55) {
                    found = true
                    return
                }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)
        if (found && currentChatTitle.isNullOrEmpty()) {
            // Make sure maybeHarvestChat has a title to dedupe against.
            currentChatTitle = inferChatTitle()
        }
        return found
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
        if (currentPage != Page.HOME) {
            // re-prime the home-list baseline on next entry to avoid replaying
            // stale previews after a navigation away.
            homeListBaseline.clear()
            homeBaselineReady = false
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

    // ---- public hooks for the periodic message-list scanner -----------------
    fun isWeComForeground(): Boolean {
        val pkg = rootInActiveWindow?.packageName?.toString().orEmpty()
        return pkg == "com.tencent.wework"
    }

    fun isOnMessagesTab(): Boolean {
        val root = rootInActiveWindow ?: return false
        return isMessagesListVisible(root)
    }

    /** Force a harvest of whatever is currently visible. Same logic as the
     *  WINDOW_CONTENT_CHANGED handler but driven externally. */
    fun forceHarvestHomeList() {
        maybeHarvestHomeList()
    }

    /** Returns the bounds (in screen px) of the conversation list, so the
     *  caller can compute a swipe gesture inside it. */
    fun getMessagesListBounds(): android.graphics.Rect? {
        val root = rootInActiveWindow ?: return null
        val list = findFirstScrollable(root) ?: return null
        val r = android.graphics.Rect()
        list.getBoundsInScreen(r)
        return r
    }

    // ---------------------------------------------------- harvest 消息 tab
    /**
     * Walk the conversation list under the 消息 tab and emit any row whose
     * preview text changed since the last observation. Covers the case where
     * WeCom is foreground (no system notification fires) but the user is not
     * yet inside the specific chat.
     *
     * Heuristics:
     *  - We must be on the 消息 tab itself — i.e. there is a TextView "消息"
     *    near the top header.
     *  - A row is any clickable view-group inside the scrollable list that
     *    contains ≥ 2 non-empty TextViews. The top one is the contact name,
     *    the rest joined form the preview.
     *  - Outbound previews (prefixed with "我:" / "我：" / "[草稿]") are skipped.
     */
    private fun maybeHarvestHomeList() {
        val cb = onChatMessage ?: return
        val root = rootInActiveWindow ?: return
        if (!isMessagesListVisible(root)) return

        val now = System.currentTimeMillis()
        gc(now)

        val rows = collectListRows(root)
        if (rows.isEmpty()) return

        if (!homeBaselineReady) {
            for ((name, preview) in rows) homeListBaseline[name] = preview
            homeBaselineReady = true
            Log.d(tag, "home baseline primed rows=${rows.size}")
            return
        }

        for ((name, preview) in rows) {
            if (preview.isEmpty()) continue
            if (isOutboundPreview(preview)) continue
            val prev = homeListBaseline[name]
            if (prev == preview) continue
            homeListBaseline[name] = preview
            if (alreadySeen(name, preview)) continue
            recent.addLast(Triple(name, preview, now))
            cb(name, preview)
            Log.d(tag, "home harvest name=$name preview=${preview.take(60)}")
        }
    }

    private fun isMessagesListVisible(root: AccessibilityNodeInfo): Boolean {
        // The 消息 tab shows a "消息" title at the top of the screen.
        var found = false
        fun walk(n: AccessibilityNodeInfo?) {
            if (n == null || found) return
            val t = n.text?.toString().orEmpty()
            if (t == "消息") {
                val r = android.graphics.Rect()
                n.getBoundsInScreen(r)
                if (r.centerY() < 180) {
                    found = true
                    return
                }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)
        return found
    }

    private fun collectListRows(root: AccessibilityNodeInfo): List<Pair<String, String>> {
        // Find the scrollable list (RecyclerView/ListView). It might be nested
        // a few levels deep — we just look for the first scrollable.
        val list = findFirstScrollable(root) ?: return emptyList()
        val out = mutableListOf<Pair<String, String>>()
        val headerCutoff = 200  // skip the top title bar
        val footerCutoff =
            resources.displayMetrics.heightPixels - 160  // skip the bottom tabs

        for (i in 0 until list.childCount) {
            val row = list.getChild(i) ?: continue
            val rowBounds = android.graphics.Rect()
            row.getBoundsInScreen(rowBounds)
            if (rowBounds.bottom < headerCutoff || rowBounds.top > footerCutoff) continue

            val texts = mutableListOf<Pair<Int, String>>() // (y, text)
            fun walk(n: AccessibilityNodeInfo?) {
                n ?: return
                val cls = n.className?.toString().orEmpty()
                if (cls.contains("TextView", true)) {
                    val t = n.text?.toString().orEmpty().trim()
                    if (t.isNotEmpty()) {
                        val r = android.graphics.Rect()
                        n.getBoundsInScreen(r)
                        texts.add(r.centerY() to t)
                    }
                }
                for (j in 0 until n.childCount) walk(n.getChild(j))
            }
            walk(row)
            if (texts.size < 2) continue

            texts.sortBy { it.first }
            val name = texts.first().second
            // The right-most/bottom-most text is usually a timestamp like
            // "昨天" / "16:43" — strip if it matches a time/date pattern.
            val rest = texts.drop(1).map { it.second }.filterNot { looksLikeTimestamp(it) }
            if (rest.isEmpty()) continue
            // The preview is typically the longest remaining text; numeric
            // unread badges ("1" / "9+" / "99") are short and noisy.
            val preview = rest.maxByOrNull { it.length }!!.trim()
            if (preview.length < 2) continue
            out.add(name to preview)
        }
        return out
    }

    private fun findFirstScrollable(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (n.isScrollable) return n
        for (i in 0 until n.childCount) {
            val c = n.getChild(i) ?: continue
            findFirstScrollable(c)?.let { return it }
        }
        return null
    }

    private fun looksLikeTimestamp(s: String): Boolean {
        if (s.length > 8) return false
        // 16:43, 昨天, 星期三, 03/05, 2024/03/05
        if (s == "昨天" || s.startsWith("星期")) return true
        return s.matches(Regex("""^\d{1,2}[:/-]\d{1,2}([:/-]\d{1,2})?$"""))
    }

    private fun isOutboundPreview(p: String): Boolean {
        if (p.startsWith("我:") || p.startsWith("我：")) return true
        if (p.startsWith("[草稿]") || p.startsWith("[Draft]")) return true
        return false
    }
}
