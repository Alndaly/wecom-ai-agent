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
 *     newly-appeared bubbles to a registered callback. This is the
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
        @Volatile var onChatMessage: ((sender: String, content: String, fromSelf: Boolean) -> Unit)? = null
    }

    private val tag = "WeComA11y"

    enum class Page { HOME, CHAT, SEARCH, CONTACT, MOMENTS, UNKNOWN }

    @Volatile var currentPage: Page = Page.UNKNOWN
        private set

    @Volatile var currentChatTitle: String? = null
        private set

    /** Dedupe per-session: we never re-fire the same visible bubble within a short window. */
    private data class RecentBubble(val chat: String, val content: String, val fromSelf: Boolean, val at: Long)
    private data class ChatBubble(val content: String, val fromSelf: Boolean, val bounds: android.graphics.Rect)
    private val recent = ArrayDeque<RecentBubble>()
    private val recentTtlMs = 10 * 60_000L
    private var baselineChatTitle: String? = null
    private var baselineReady = false
    /** Snapshot of the message-list rows seen the first time we land on it; we
     *  only emit rows whose preview *changes* compared to this baseline so we
     *  don't re-fire all history on startup. */
    private val homeListBaseline = HashMap<String, String>()
    private var homeBaselineReady = false
    private var lastChatHarvestAt = 0L

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(tag, "service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                updatePage(event)
                rootInActiveWindow?.let { root ->
                    if (root.packageName?.toString() == "com.tencent.wework" && looksLikeChatPage(root)) {
                        maybeHarvestChat(reason = "window_state")
                    }
                }
            }
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> {
                dispatchVisibleRootHarvest("content_changed")
            }
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> dispatchVisibleRootHarvest("view_event")
            else -> Unit
        }
    }

    private fun dispatchVisibleRootHarvest(reason: String) {
        // Dispatch by *current root content* rather than the cached
        // `currentPage`. WeCom switches tabs inside a single activity, so
        // WINDOW_STATE_CHANGED doesn't always fire and the cache can be stale.
        val root = rootInActiveWindow ?: return
        if (root.packageName?.toString() != "com.tencent.wework") return
        if (isMessagesListVisible(root)) {
            maybeHarvestHomeList()
        } else if (looksLikeChatPage(root)) {
            maybeHarvestChat(reason = reason)
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

    /** Result of [dumpTreeWithNodes]: a human-readable tree string with `[N]`
     *  prefixes, paired with a flat node list the backend uses to resolve
     *  `tap_node(N)` decisions back into concrete coordinates. */
    data class DumpResult(val tree: String, val nodes: List<DumpedNode>)

    data class DumpedNode(
        val id: Int,
        val cls: String,
        val viewId: String,
        val text: String,
        val desc: String,
        val clickable: Boolean,
        val focusable: Boolean,
        val editable: Boolean,
        val scrollable: Boolean,
        val bounds: android.graphics.Rect,
    )

    /** Walk the active window's tree, assign a sequential id to every node, and
     *  return both the indented text rendering and a flat list keyed by id. */
    fun dumpTreeWithNodes(): DumpResult {
        val root = rootInActiveWindow ?: return DumpResult(
            "rootInActiveWindow is null (is WeCom in foreground?)\n",
            emptyList(),
        )
        val sb = StringBuilder()
        sb.append("=== UI dump pkg=").append(root.packageName)
            .append(" page=").append(currentPage).append(" ===\n")
        val nodes = mutableListOf<DumpedNode>()
        walkNumbered(root, depth = 0, sb = sb, nodes = nodes)
        return DumpResult(sb.toString(), nodes)
    }

    /** Back-compat: legacy single-string dump for the existing UI button. */
    fun dumpToString(out: StringBuilder) {
        out.append(dumpTreeWithNodes().tree)
    }

    private fun walkNumbered(
        n: AccessibilityNodeInfo?,
        depth: Int,
        sb: StringBuilder,
        nodes: MutableList<DumpedNode>,
    ) {
        n ?: return
        val id = nodes.size + 1
        val cls = n.className?.toString()?.substringAfterLast('.') ?: "?"
        val viewId = n.viewIdResourceName?.substringAfterLast('/') ?: ""
        val txt = (n.text?.toString() ?: "").take(60)
        val desc = (n.contentDescription?.toString() ?: "").take(60)
        val bounds = android.graphics.Rect().also { n.getBoundsInScreen(it) }
        nodes.add(
            DumpedNode(
                id = id,
                cls = cls,
                viewId = viewId,
                text = txt,
                desc = desc,
                clickable = n.isClickable,
                focusable = n.isFocusable,
                editable = n.isEditable,
                scrollable = n.isScrollable,
                bounds = bounds,
            )
        )
        sb.append("  ".repeat(depth))
        sb.append("[").append(id).append("] ")
        sb.append("[").append(cls).append("]")
        if (viewId.isNotEmpty()) sb.append(" id=").append(viewId)
        if (txt.isNotEmpty()) sb.append(" txt=\"").append(txt).append("\"")
        if (desc.isNotEmpty()) sb.append(" desc=\"").append(desc).append("\"")
        val flags = buildString {
            if (n.isClickable) append("C")
            if (n.isFocusable) append("F")
            if (n.isEditable) append("E")
            if (n.isScrollable) append("S")
        }
        if (flags.isNotEmpty()) sb.append(" ").append(flags)
        sb.append('\n')
        for (i in 0 until n.childCount) walkNumbered(n.getChild(i), depth + 1, sb, nodes)
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
    fun forceHarvestCurrentChat() {
        val root = rootInActiveWindow ?: return
        if (root.packageName?.toString() != "com.tencent.wework") return
        if (looksLikeChatPage(root)) maybeHarvestChat(reason = "force")
    }

    private fun maybeHarvestChat(reason: String) {
        val cb = onChatMessage ?: return
        val root = rootInActiveWindow ?: return
        val title = currentChatTitle ?: inferChatTitle() ?: "当前聊天"
        currentChatTitle = title

        // Heuristic for message bubbles: collect visible TextView bubbles in
        // the message area, then classify left/right alignment. Customer
        // messages trigger AI downstream; self messages are persisted only.
        val screenWidth = resources.displayMetrics.widthPixels
        val now = System.currentTimeMillis()
        if (now - lastChatHarvestAt < 350) return
        lastChatHarvestAt = now
        gc(now)

        val candidates = mutableListOf<ChatBubble>()
        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val cls = n.className?.toString() ?: ""
            if (cls.contains("TextView", true) && !n.text.isNullOrBlank()) {
                val content = n.text.toString().trim()
                classifyMessageBubble(n, screenWidth, content)?.let { candidates.add(it) }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)

        if (!baselineReady || baselineChatTitle != title) {
            val shouldEmitLatest = reason != "force" && reason != "window_state" && candidates.isNotEmpty()
            val baselineOnly = if (shouldEmitLatest) candidates.dropLast(1) else candidates
            for (bubble in baselineOnly) {
                recent.addLast(RecentBubble(title, bubble.content, bubble.fromSelf, now))
            }
            baselineChatTitle = title
            baselineReady = true
            if (shouldEmitLatest) {
                val latest = candidates.last()
                if (!alreadySeen(title, latest)) {
                    recent.addLast(RecentBubble(title, latest.content, latest.fromSelf, now))
                    cb(title, latest.content, latest.fromSelf)
                    Log.d(tag, "harvest first chat=$title self=${latest.fromSelf} reason=$reason content=${latest.content.take(60)}")
                }
            }
            Log.d(tag, "baseline chat=$title candidates=${candidates.size} emitLatest=$shouldEmitLatest reason=$reason")
            return
        }

        var emitted = 0
        for (bubble in candidates) {
            if (alreadySeen(title, bubble)) continue
            recent.addLast(RecentBubble(title, bubble.content, bubble.fromSelf, now))
            cb(title, bubble.content, bubble.fromSelf)
            emitted++
            Log.d(tag, "harvest chat=$title self=${bubble.fromSelf} reason=$reason content=${bubble.content.take(60)}")
        }
        if (emitted == 0) {
            Log.d(tag, "chat scan no new title=$title candidates=${candidates.size} reason=$reason")
        }
    }

    private fun classifyMessageBubble(
        node: AccessibilityNodeInfo,
        screenWidth: Int,
        content: String,
    ): ChatBubble? {
        if (content.isEmpty() || content.length > 2000) return null
        if (content == currentChatTitle) return null

        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        if (!isInMessageArea(bounds)) return null

        val rects = mutableListOf<android.graphics.Rect>()
        rects.add(android.graphics.Rect(bounds))
        var p = node.parent
        var depth = 0
        while (p != null && depth < 5) {
            val r = android.graphics.Rect()
            p.getBoundsInScreen(r)
            if (isInMessageArea(r)) rects.add(r)
            p = p.parent
            depth += 1
        }

        val bubbleRect = rects
            .filter { it.width() in 24 until (screenWidth * 0.88f).toInt() }
            .maxByOrNull { it.width() }
            ?: bounds
        val center = bubbleRect.centerX()
        val fromSelf =
            bubbleRect.left > screenWidth * 0.35f ||
            center > screenWidth * 0.58f ||
            bubbleRect.right > screenWidth * 0.82f
        val looksInbound =
            bubbleRect.right < screenWidth * 0.78f ||
            center < screenWidth * 0.48f ||
            bounds.left < screenWidth * 0.42f
        if (!fromSelf && !looksInbound) {
            Log.d(tag, "skip ambiguous bubble bounds=$bubbleRect text=${content.take(40)}")
            return null
        }
        return ChatBubble(content = content, fromSelf = fromSelf, bounds = bubbleRect)
    }

    private fun isInMessageArea(bounds: android.graphics.Rect): Boolean {
        if (bounds.isEmpty) return false
        if (bounds.top < 200) return false
        if (bounds.bottom > resources.displayMetrics.heightPixels - 120) return false
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

    private fun alreadySeen(sender: String, bubble: ChatBubble): Boolean {
        for (item in recent) {
            if (item.chat == sender && item.content == bubble.content && item.fromSelf == bubble.fromSelf) return true
        }
        return false
    }

    private fun alreadySeen(sender: String, content: String): Boolean {
        for (item in recent) {
            if (item.chat == sender && item.content == content && !item.fromSelf) return true
        }
        return false
    }

    private fun gc(now: Long) {
        while (recent.isNotEmpty() && now - recent.first().at > recentTtlMs) {
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
            var emittedUnread = 0
            for (row in rows) {
                homeListBaseline[row.name] = row.preview
                if (!row.hasUnread) continue
                if (row.preview.isEmpty()) continue
                if (isOutboundPreview(row.preview)) continue
                if (alreadySeen(row.name, row.preview)) continue
                recent.addLast(RecentBubble(row.name, row.preview, fromSelf = false, at = now))
                cb(row.name, row.preview, false)
                emittedUnread++
            }
            homeBaselineReady = true
            Log.d(tag, "home baseline primed rows=${rows.size} emittedUnread=$emittedUnread")
            return
        }

        for (row in rows) {
            if (row.preview.isEmpty()) continue
            if (isOutboundPreview(row.preview)) continue
            val prev = homeListBaseline[row.name]
            val changed = prev != row.preview
            if (!changed && !row.hasUnread) continue
            homeListBaseline[row.name] = row.preview
            if (alreadySeen(row.name, row.preview)) continue
            recent.addLast(RecentBubble(row.name, row.preview, fromSelf = false, at = now))
            cb(row.name, row.preview, false)
            Log.d(
                tag,
                "home harvest name=${row.name} unread=${row.hasUnread} changed=$changed preview=${row.preview.take(60)}",
            )
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

    private data class HomeListRow(
        val name: String,
        val preview: String,
        val hasUnread: Boolean,
    )

    private fun collectListRows(root: AccessibilityNodeInfo): List<HomeListRow> {
        // Find the scrollable list (RecyclerView/ListView). It might be nested
        // a few levels deep — we just look for the first scrollable.
        val list = findFirstScrollable(root) ?: return emptyList()
        val out = mutableListOf<HomeListRow>()
        val headerCutoff = 200  // skip the top title bar
        val footerCutoff =
            resources.displayMetrics.heightPixels - 160  // skip the bottom tabs

        for (i in 0 until list.childCount) {
            val row = list.getChild(i) ?: continue
            val rowBounds = android.graphics.Rect()
            row.getBoundsInScreen(rowBounds)
            if (rowBounds.bottom < headerCutoff || rowBounds.top > footerCutoff) continue

            val texts = mutableListOf<Triple<Int, Int, String>>() // (y, x, text)
            fun walk(n: AccessibilityNodeInfo?) {
                n ?: return
                val cls = n.className?.toString().orEmpty()
                if (cls.contains("TextView", true)) {
                    val t = n.text?.toString().orEmpty().trim()
                    if (t.isNotEmpty()) {
                        val r = android.graphics.Rect()
                        n.getBoundsInScreen(r)
                        texts.add(Triple(r.centerY(), r.centerX(), t))
                    }
                }
                for (j in 0 until n.childCount) walk(n.getChild(j))
            }
            walk(row)
            if (texts.size < 2) continue

            texts.sortWith(compareBy<Triple<Int, Int, String>> { it.first }.thenBy { it.second })
            val name = texts
                .map { it.third }
                .firstOrNull { !looksLikeUnreadBadge(it) && !looksLikeTimestamp(it) }
                ?: continue
            // The right-most/bottom-most text is usually a timestamp like
            // "昨天" / "16:43" — strip if it matches a time/date pattern.
            val rest = texts.map { it.third }
                .filter { it != name }
                .filterNot { looksLikeTimestamp(it) }
            if (rest.isEmpty()) continue
            val hasUnread = texts.any { looksLikeUnreadBadge(it.third) }
            // The preview is typically the longest remaining text; numeric
            // unread badges ("1" / "9+" / "99") are short and noisy.
            val preview = rest
                .filterNot { looksLikeUnreadBadge(it) }
                .maxByOrNull { it.length }
                ?.trim()
                ?: continue
            if (preview.length < 2) continue
            out.add(HomeListRow(name = name, preview = preview, hasUnread = hasUnread))
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

    private fun looksLikeUnreadBadge(s: String): Boolean {
        return s.matches(Regex("""^\d{1,3}\+?$"""))
    }

    private fun isOutboundPreview(p: String): Boolean {
        if (p.startsWith("我:") || p.startsWith("我：")) return true
        if (p.startsWith("[草稿]") || p.startsWith("[Draft]")) return true
        return false
    }
}
