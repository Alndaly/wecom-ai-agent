package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Bitmap
import android.graphics.Path
import android.os.Build
import android.util.Base64
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import com.wecom.agent.model.ScreenFramePayload
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
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
        @Volatile var onChatMessage: ((sender: String, type: String, content: String, fromSelf: Boolean, messageKey: String, bounds: List<Int>, relatedMediaBounds: List<Int>, observationSource: String, unreadCount: Int?) -> Unit)? = null
    }

    private val tag = "WeComA11y"

    enum class Page { HOME, CHAT, SEARCH, CONTACT, MOMENTS, UNKNOWN }

    @Volatile var currentPage: Page = Page.UNKNOWN
        private set

    @Volatile var currentChatTitle: String? = null
        private set

    /** Dedupe per-session: we never re-fire the same visible bubble within a short window. */
    private data class RecentBubble(
        val chat: String,
        val type: String,
        val content: String,
        val fromSelf: Boolean,
        val at: Long,
        val boundsKey: String = "",
    )
    private data class ChatBubble(
        val type: String,
        val content: String,
        val fromSelf: Boolean,
        val bounds: android.graphics.Rect,
        val relatedMediaBounds: android.graphics.Rect? = null,
    )
    data class ChatHarvestResult(
        val requestedMessages: Int,
        val emittedMessages: Int,
        val observedBubbles: Int,
        val scrollPages: Int,
        val quietWindowMs: Long,
        val maxDurationMs: Long,
        val stoppedReason: String,
    )
    private data class BubbleRole(val fromSelf: Boolean, val anchor: android.graphics.Rect)
    private val recentLock = Any()
    private val recent = ArrayDeque<RecentBubble>()
    private val recentTtlMs = 10 * 60_000L
    private var baselineChatTitle: String? = null
    private var baselineReady = false
    /** Snapshot of the message-list rows seen the first time we land on it; we
     *  only emit rows whose preview *changes* compared to this baseline so we
     *  don't re-fire all history on startup. */
    private val conversationListBaseline = HashMap<String, String>()
    private val emittedUnreadConversationListPreviews = HashSet<String>()
    private var conversationListBaselineReady = false
    private var lastChatHarvestAt = 0L
    private data class CachedScreenshot(val atMs: Long, val quality: Int, val frame: ScreenFramePayload)
    private val screenshotMutex = Mutex()
    private var lastScreenshotAtMs = 0L
    private var cachedScreenshot: CachedScreenshot? = null
    private val screenshotMinIntervalMs = 1_000L
    private val screenshotCacheTtlMs = 900L

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
            maybeHarvestConversationList()
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
        if (found) refreshChatTitle(root)
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

    fun isInputPanelVisible(): Boolean {
        return windows.any { it.type == AccessibilityWindowInfo.TYPE_INPUT_METHOD }
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
        val txt = n.text?.toString() ?: ""
        val desc = n.contentDescription?.toString() ?: ""
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

    suspend fun captureScreenJpegBase64(quality: Int = 55, allowCached: Boolean = true): ScreenFramePayload {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return ScreenFramePayload(error = "实时屏幕需要 Android 11 / API 30 及以上")
        }
        val normalizedQuality = quality.coerceIn(20, 90)
        return screenshotMutex.withLock {
            val now = System.currentTimeMillis()
            if (allowCached) {
                cachedScreenshot
                    ?.takeIf { it.quality == normalizedQuality && it.frame.error == null && now - it.atMs <= screenshotCacheTtlMs }
                    ?.let { return@withLock it.frame }
            }

            val waitMs = screenshotMinIntervalMs - (now - lastScreenshotAtMs)
            if (waitMs > 0) {
                if (allowCached) {
                    cachedScreenshot
                        ?.takeIf { it.quality == normalizedQuality && it.frame.error == null }
                        ?.let { return@withLock it.frame }
                }
                delay(waitMs)
            }

            val frame = captureScreenOnceJpegBase64(normalizedQuality)
            val finishedAt = System.currentTimeMillis()
            lastScreenshotAtMs = finishedAt
            if (frame.error == null) {
                cachedScreenshot = CachedScreenshot(finishedAt, normalizedQuality, frame)
            } else if (allowCached) {
                cachedScreenshot
                    ?.takeIf { it.quality == normalizedQuality && it.frame.error == null && finishedAt - it.atMs <= 5_000L }
                    ?.let { return@withLock it.frame }
            }
            frame
        }
    }

    private suspend fun captureScreenOnceJpegBase64(quality: Int): ScreenFramePayload {
        // takeScreenshot is callback-based and on rare occasions the callback
        // never fires (display gone, surface flinger stall). Don't let that
        // wedge the caller — bound the wait and report the timeout explicitly.
        return withTimeoutOrNull(8_000L) {
            suspendCancellableCoroutine { cont ->
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
                        software.compress(Bitmap.CompressFormat.JPEG, quality, out)
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
                        cont.resume(ScreenFramePayload(error = screenshotErrorMessage(errorCode)))
                    }
                },
            )
            }
        } ?: ScreenFramePayload(error = "截图失败：回调超时(8s)").also {
            Log.w(tag, "screenshot callback timeout after 8s — takeScreenshot never called back")
        }
    }

    private fun screenshotErrorMessage(errorCode: Int): String {
        return when (errorCode) {
            1 -> "截图失败：系统内部错误(1)"
            2 -> "截图失败：无无障碍截图权限(2)"
            3 -> "截图失败：请求过于频繁，请稍后重试(3)"
            4 -> "截图失败：无效显示器(4)"
            5 -> "截图失败：无效窗口(5)"
            6 -> "截图失败：安全窗口禁止截图(6)"
            else -> "截图失败：未知错误($errorCode)"
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
            refreshChatTitle()
        } else {
            baselineChatTitle = null
            baselineReady = false
            currentChatTitle = null
        }
        if (currentPage != Page.HOME) {
            // re-prime the home-list baseline on next entry to avoid replaying
            // stale previews after a navigation away.
            conversationListBaseline.clear()
            emittedUnreadConversationListPreviews.clear()
            conversationListBaselineReady = false
        }
    }

    private fun refreshChatTitle(root: AccessibilityNodeInfo? = rootInActiveWindow): String? {
        val title = inferChatTitle(root)
        if (!title.isNullOrBlank() && title != currentChatTitle) {
            Log.i(tag, "chat title changed old=$currentChatTitle new=$title")
            baselineChatTitle = null
            baselineReady = false
            currentChatTitle = title
        }
        return currentChatTitle
    }

    private data class ChatTitleCandidate(
        val text: String,
        val id: String,
        val bounds: android.graphics.Rect,
    )

    private fun inferChatTitle(root: AccessibilityNodeInfo? = rootInActiveWindow): String? {
        root ?: return null
        val candidates = mutableListOf<ChatTitleCandidate>()
        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val cls = n.className?.toString() ?: ""
            if (cls.contains("TextView", ignoreCase = true)) {
                val text = n.text?.toString().orEmpty().trim()
                if (text.isNotEmpty() && looksLikeChatTitleText(text)) {
                    val r = android.graphics.Rect()
                    n.getBoundsInScreen(r)
                    if (isInChatHeaderTitleZone(r)) {
                        candidates.add(
                            ChatTitleCandidate(
                                text = text,
                                id = n.viewIdResourceName?.substringAfterLast('/').orEmpty(),
                                bounds = r,
                            )
                        )
                    }
                }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)
        return candidates.maxByOrNull { chatTitleScore(it) }?.text
    }

    private fun isInChatHeaderTitleZone(bounds: android.graphics.Rect): Boolean {
        if (bounds.isEmpty) return false
        val screenWidth = resources.displayMetrics.widthPixels
        val screenHeight = resources.displayMetrics.heightPixels
        val headerBottom = (screenHeight * 0.13f).toInt()
        if (bounds.top < 0 || bounds.centerY() > headerBottom) return false
        // Avoid left back button text/icons and right-side action labels. The
        // title normally sits in the middle band; keep this proportional so it
        // works across devices.
        if (bounds.centerX() < screenWidth * 0.18f) return false
        if (bounds.centerX() > screenWidth * 0.82f) return false
        return true
    }

    private fun looksLikeChatTitleText(text: String): Boolean {
        if (text.isBlank()) return false
        if (text.startsWith("@") || text.startsWith("＠")) return false
        if (looksLikeTimestamp(text)) return false
        if (looksLikeUnreadBadge(text)) return false
        if (looksLikeBottomTabLabel(text)) return false
        if (looksLikeNonMessageText(text)) return false
        if (text in setOf("消息", "邮件", "文档", "工作台", "通讯录", "设置")) return false
        return true
    }

    private fun chatTitleScore(c: ChatTitleCandidate): Int {
        val screenWidth = resources.displayMetrics.widthPixels
        val centerDelta = kotlin.math.abs(c.bounds.centerX() - screenWidth / 2)
        val centerScore = (screenWidth / 2 - centerDelta).coerceAtLeast(0) / 10
        val idScore = if (c.id == "ncq") 120 else 0
        val widthScore = c.bounds.width().coerceAtMost(screenWidth / 2) / 20
        return idScore + centerScore + widthScore - c.bounds.top / 8
    }

    // -------------------------------------------------------- harvest chat
    /** Walk the chat list and emit any unseen inbound bubbles. */
    fun forceHarvestCurrentChat() {
        val root = rootInActiveWindow ?: return
        if (root.packageName?.toString() != "com.tencent.wework") return
        if (looksLikeChatPage(root)) primeCurrentChatBaseline(reason = "force")
    }

    suspend fun forceEmitRecentCustomerBubbles(
        maxMessages: Int,
        quietWindowMs: Long,
        maxDurationMs: Long,
    ): ChatHarvestResult {
        val requestedMessages = maxMessages.coerceIn(1, 30)
        val quietWindow = quietWindowMs.coerceIn(500L, 5_000L)
        val maxDuration = maxDurationMs.coerceIn(2_000L, 20_000L)
        val emptyResult = ChatHarvestResult(
            requestedMessages = requestedMessages,
            emittedMessages = 0,
            observedBubbles = 0,
            scrollPages = 0,
            quietWindowMs = quietWindow,
            maxDurationMs = maxDuration,
            stoppedReason = "chat_not_ready",
        )
        val cb = onChatMessage ?: return emptyResult
        val root = rootInActiveWindow ?: return emptyResult
        if (root.packageName?.toString() != "com.tencent.wework") return emptyResult
        if (!looksLikeChatPage(root)) return emptyResult

        val title = refreshChatTitle(root) ?: "当前聊天"
        val now = System.currentTimeMillis()
        val deadlineAt = now + maxDuration
        gc(now)

        val olderPages = mutableListOf<List<ChatBubble>>()
        val quietPages = mutableListOf<List<ChatBubble>>()
        var swipes = 0
        val maxSwipes = when {
            requestedMessages <= 5 -> 1
            requestedMessages <= 10 -> 2
            else -> 4
        }
        // noMorePages becomes true when there's nothing useful left to scroll
        // for. Two ways to hit it:
        //   1. swipeChatTowardOlderMessages() returned false (gesture dropped
        //      or list isn't scrollable) — see swipe() for the drop logging.
        //   2. swipe succeeded but the new page exposed zero new bubbles, i.e.
        //      we're already at the top of the chat history.
        // Once true, the quiet loop is allowed to terminate on the quiet
        // window instead of burning the full deadline.
        var noMorePages = false

        while (true) {
            olderPages.add(collectCurrentCustomerBubbles())
            val observedBeforeSwipe = orderedUniqueBubbles(olderPages, quietPages).size
            if (observedBeforeSwipe >= requestedMessages) break
            if (swipes >= maxSwipes) {
                noMorePages = true
                break
            }
            if (System.currentTimeMillis() >= deadlineAt) break
            val swiped = swipeChatTowardOlderMessages()
            if (!swiped) {
                noMorePages = true
                break
            }
            swipes++
            delay(450)
            // Confirm the swipe actually exposed new content. If a swipe +
            // re-collect added zero new unique bubbles, we're at the top of
            // the chat (or the scroller is wedged) — stop burning swipes.
            olderPages.add(collectCurrentCustomerBubbles())
            val observedAfterSwipe = orderedUniqueBubbles(olderPages, quietPages).size
            if (observedAfterSwipe == observedBeforeSwipe) {
                Log.i(tag, "harvest reached top contact=$title swipes=$swipes observed=$observedAfterSwipe")
                noMorePages = true
                break
            }
        }

        repeat(swipes) {
            swipeChatTowardNewerMessages()
            delay(180)
        }

        var lastNewBubbleAt = System.currentTimeMillis()
        var lastObservedCount = orderedUniqueBubbles(olderPages, quietPages).size
        var stoppedReason = "quiet_window_reached"
        while (true) {
            quietPages.add(collectCurrentCustomerBubbles())
            val observed = orderedUniqueBubbles(olderPages, quietPages).size
            if (observed > lastObservedCount) {
                lastObservedCount = observed
                lastNewBubbleAt = System.currentTimeMillis()
            }

            val nowLoop = System.currentTimeMillis()
            val enoughMessagesOrNoMorePages =
                observed >= requestedMessages || swipes >= maxSwipes || noMorePages
            if (observed > 0 && enoughMessagesOrNoMorePages && nowLoop - lastNewBubbleAt >= quietWindow) {
                stoppedReason = "quiet_window_reached"
                break
            }
            if (observed >= requestedMessages && observed >= 30) {
                stoppedReason = "max_messages_reached"
                break
            }
            if (nowLoop >= deadlineAt) {
                stoppedReason = "max_duration_reached"
                break
            }
            delay(250)
        }

        val observedBubbles = orderedUniqueBubbles(olderPages, quietPages)
        val bubbles = observedBubbles
            .takeLast(requestedMessages)

        var emitted = 0
        for (bubble in bubbles) {
            remember(RecentBubble(title, bubble.type, bubble.content, bubble.fromSelf, now, bubble.boundsKey()))
            cb(
                title,
                bubble.type,
                bubble.content,
                bubble.fromSelf,
                bubble.boundsKey(),
                bubble.boundsList(),
                bubble.relatedMediaBoundsList(),
                "chat_message_bubble",
                null,
            )
            emitted++
        }
        baselineChatTitle = title
        baselineReady = true
        lastChatHarvestAt = System.currentTimeMillis()
        Log.i(tag, "forced chat harvest contact=$title requested_messages=$requestedMessages emitted=$emitted observed=${observedBubbles.size} scroll_pages=$swipes stopped_reason=$stoppedReason quiet_window_ms=$quietWindow max_duration_ms=$maxDuration")
        return ChatHarvestResult(
            requestedMessages = requestedMessages,
            emittedMessages = emitted,
            observedBubbles = observedBubbles.size,
            scrollPages = swipes,
            quietWindowMs = quietWindow,
            maxDurationMs = maxDuration,
            stoppedReason = stoppedReason,
        )
    }

    private fun collectCurrentCustomerBubbles(): List<ChatBubble> {
        val root = rootInActiveWindow ?: return emptyList()
        val screenWidth = resources.displayMetrics.widthPixels
        return collectChatBubbles(root, screenWidth)
            .filter { !it.fromSelf }
            .sortedBy { it.bounds.top }
    }

    private fun orderedUniqueBubbles(
        olderPages: List<List<ChatBubble>>,
        quietPages: List<List<ChatBubble>>,
    ): List<ChatBubble> {
        val out = LinkedHashMap<String, ChatBubble>()
        for (page in olderPages.asReversed()) {
            for (bubble in page) out.putIfAbsent(harvestBubbleKey(bubble), bubble)
        }
        for (page in quietPages) {
            for (bubble in page) out.putIfAbsent(harvestBubbleKey(bubble), bubble)
        }
        return out.values.toList()
    }

    private fun harvestBubbleKey(bubble: ChatBubble): String {
        return "${bubble.type}:${bubble.content}:${bubble.boundsKey()}"
    }

    private suspend fun swipeChatTowardOlderMessages(): Boolean =
        scrollChatList(forward = false, durationMs = 260)

    private suspend fun swipeChatTowardNewerMessages(): Boolean =
        scrollChatList(forward = true, durationMs = 220)

    /**
     * Scrolls the chat ListView via the a11y ACTION_SCROLL_* action when
     * possible — that path goes through the view's own scroll handler and
     * doesn't suffer from the dispatchGesture callback-never-fires problem
     * we kept hitting on WeCom. Falls back to a coordinate swipe (with the
     * timeout-bounded wrapper) if the node refuses the action.
     *
     * forward=false → toward older messages (scroll up the chat history).
     * forward=true  → toward newer messages (scroll back down to latest).
     */
    private suspend fun scrollChatList(forward: Boolean, durationMs: Long): Boolean {
        val root = rootInActiveWindow ?: return false
        val node = findChatMessageList(root)
        if (node != null && node.isScrollable) {
            val action = if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
            else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
            val ok = node.performAction(action)
            if (ok) return true
            Log.w(tag, "chat scroll action rejected forward=$forward — falling back to gesture")
        } else {
            Log.w(
                tag,
                "chat scroll node missing or not scrollable (node=${node != null}) — falling back to gesture",
            )
        }
        val bounds = currentChatMessageListBounds() ?: return false
        val x = bounds.centerX().toFloat()
        return if (forward) {
            val fromY = bounds.bottom - bounds.height() * 0.12f
            val toY = bounds.top + bounds.height() * 0.28f
            swipe(x, fromY, x, toY, durationMs = durationMs)
        } else {
            val fromY = bounds.top + bounds.height() * 0.28f
            val toY = bounds.bottom - bounds.height() * 0.12f
            swipe(x, fromY, x, toY, durationMs = durationMs)
        }
    }

    private fun currentChatMessageListBounds(): android.graphics.Rect? {
        val root = rootInActiveWindow ?: return null
        val node = findChatMessageList(root) ?: return null
        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        return bounds.takeUnless { it.isEmpty }
    }

    private fun primeCurrentChatBaseline(reason: String) {
        val root = rootInActiveWindow ?: return
        val title = refreshChatTitle(root) ?: "当前聊天"
        val screenWidth = resources.displayMetrics.widthPixels
        val now = System.currentTimeMillis()
        gc(now)

        val candidates = collectChatBubbles(root, screenWidth)
        for (bubble in candidates) {
            remember(RecentBubble(title, bubble.type, bubble.content, bubble.fromSelf, now, bubble.boundsKey()))
        }
        baselineChatTitle = title
        baselineReady = true
        lastChatHarvestAt = now
        Log.d(tag, "baseline refreshed chat=$title candidates=${candidates.size} reason=$reason")
    }

    private fun maybeHarvestChat(reason: String) {
        val cb = onChatMessage ?: return
        val root = rootInActiveWindow ?: return
        val title = refreshChatTitle(root) ?: "当前聊天"

        // Heuristic for message bubbles: collect visible TextView bubbles in
        // the message area, then classify left/right alignment. Customer
        // messages trigger AI downstream; self messages are persisted only.
        val screenWidth = resources.displayMetrics.widthPixels
        val now = System.currentTimeMillis()
        if (now - lastChatHarvestAt < 350) return
        lastChatHarvestAt = now
        gc(now)

        val candidates = collectChatBubbles(root, screenWidth)

        if (!baselineReady || baselineChatTitle != title) {
            for (bubble in candidates) {
                remember(RecentBubble(title, bubble.type, bubble.content, bubble.fromSelf, now, bubble.boundsKey()))
            }
            baselineChatTitle = title
            baselineReady = true
            Log.d(tag, "baseline chat=$title candidates=${candidates.size} reason=$reason")
            return
        }

        var emitted = 0
        for (bubble in candidates) {
            if (!rememberIfUnseen(title, bubble, now)) continue
            cb(
                title,
                bubble.type,
                bubble.content,
                bubble.fromSelf,
                bubble.boundsKey(),
                bubble.boundsList(),
                bubble.relatedMediaBoundsList(),
                "chat_message_bubble",
                null,
            )
            emitted++
            Log.d(tag, "harvest chat=$title self=${bubble.fromSelf} type=${bubble.type} reason=$reason content=${bubble.content}")
        }
        if (emitted == 0) {
            Log.d(tag, "chat scan no new title=$title candidates=${candidates.size} reason=$reason")
        }
    }

    private fun collectChatBubbles(root: AccessibilityNodeInfo, screenWidth: Int): List<ChatBubble> {
        val candidates = mutableListOf<ChatBubble>()
        val messageList = findChatMessageList(root) ?: root
        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val cls = n.className?.toString() ?: ""
            if (cls.contains("TextView", true) && !n.text.isNullOrBlank()) {
                val content = n.text.toString().trim()
                classifyMessageBubble(n, screenWidth, content)?.let { candidates.add(it) }
            } else {
                classifyMediaBubble(n, screenWidth)?.let { candidates.add(it) }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(messageList)
        return attachAdjacentInboundImages(
            candidates.distinctBy { "${it.type}:${it.fromSelf}:${it.boundsKey()}" }
                .sortedBy { it.bounds.top }
        )
    }

    private fun attachAdjacentInboundImages(candidates: List<ChatBubble>): List<ChatBubble> {
        return candidates.mapIndexed { index, bubble ->
            if (bubble.type != "text" || bubble.fromSelf) return@mapIndexed bubble
            val image = candidates
                .take(index)
                .asReversed()
                .firstOrNull {
                    it.type == "image" &&
                        !it.fromSelf &&
                        it.bounds.bottom <= bubble.bounds.top + 80 &&
                        bubble.bounds.top - it.bounds.bottom < 900
                }
            if (image == null) bubble else bubble.copy(relatedMediaBounds = image.bounds)
        }
    }

    private fun classifyMessageBubble(
        node: AccessibilityNodeInfo,
        screenWidth: Int,
        content: String,
    ): ChatBubble? {
        if (content.isEmpty() || content.length > 2000) return null
        if (content == currentChatTitle) return null
        if (looksLikeNonMessageText(content)) return null
        val role = messageBubbleRole(node) ?: return null

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
            ?: role.anchor
        return ChatBubble(type = "text", content = content, fromSelf = role.fromSelf, bounds = bubbleRect)
    }

    private fun classifyMediaBubble(
        node: AccessibilityNodeInfo,
        screenWidth: Int,
    ): ChatBubble? {
        val cls = node.className?.toString().orEmpty()
        val id = node.viewIdResourceName?.substringAfterLast('/').orEmpty()
        val text = node.text?.toString()?.trim().orEmpty()
        val desc = node.contentDescription?.toString()?.trim().orEmpty()
        if (text.isNotEmpty()) return null

        val kind = inferMediaMessageType(cls, id, desc) ?: return null
        val role = messageBubbleRole(node) ?: return null
        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        if (!isInMessageArea(bounds)) return null
        if (bounds.width() < 40 || bounds.height() < 40) return null
        if (bounds.width() > (screenWidth * 0.88f).toInt()) return null

        val label = when (kind) {
            "video" -> "[视频]"
            else -> "[图片]"
        }
        return ChatBubble(type = kind, content = label, fromSelf = role.fromSelf, bounds = role.anchor)
    }

    private fun inferMediaMessageType(cls: String, id: String, desc: String): String? {
        val haystack = "$cls $id $desc"
        if (haystack.contains("video", true) || desc.contains("视频")) return "video"
        if (
            haystack.contains("image", true) ||
            haystack.contains("photo", true) ||
            haystack.contains("thumb", true) ||
            cls.contains("ImageView", true) ||
            desc.contains("图片") ||
            desc.contains("照片")
        ) return "image"
        return null
    }

    private fun ChatBubble.boundsKey(): String {
        return "${bounds.left / 8}:${bounds.top / 8}:${bounds.right / 8}:${bounds.bottom / 8}"
    }

    private fun ChatBubble.boundsList(): List<Int> {
        return listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)
    }

    private fun ChatBubble.relatedMediaBoundsList(): List<Int> {
        val r = relatedMediaBounds ?: return emptyList()
        return listOf(r.left, r.top, r.right, r.bottom)
    }

    private fun messageBubbleRole(node: AccessibilityNodeInfo): BubbleRole? {
        val textViewId = node.viewIdResourceName?.substringAfterLast('/').orEmpty()
        var p = node.parent
        var depth = 0
        var row: android.graphics.Rect? = null
        while (p != null && depth < 8) {
            val id = p.viewIdResourceName?.substringAfterLast('/').orEmpty()
            if (id == "hrr" || id == "hsj") {
                val r = android.graphics.Rect()
                p.getBoundsInScreen(r)
                return BubbleRole(fromSelf = id == "hrr", anchor = r)
            }
            if (id == "cmg") {
                row = android.graphics.Rect()
                p.getBoundsInScreen(row)
            }
            p = p.parent
            depth += 1
        }
        if (textViewId == "i9j" && row != null) {
            val bounds = android.graphics.Rect()
            node.getBoundsInScreen(bounds)
            val screenWidth = resources.displayMetrics.widthPixels
            val center = bounds.centerX()
            val fromSelf =
                bounds.left > screenWidth * 0.38f ||
                center > screenWidth * 0.58f ||
                bounds.right > screenWidth * 0.82f
            val looksInbound =
                bounds.right < screenWidth * 0.78f ||
                center < screenWidth * 0.50f ||
                bounds.left < screenWidth * 0.42f
            if (fromSelf || looksInbound) {
                return BubbleRole(fromSelf = fromSelf, anchor = row)
            }
        }
        return null
    }

    private fun previewMessageType(preview: String): String {
        return when (preview.trim()) {
            "[图片]", "[图片消息]", "[图片表情]", "[Image]" -> "image"
            "[视频]", "[Video]" -> "video"
            else -> "text"
        }
    }

    private fun looksLikeNonMessageText(content: String): Boolean {
        val s = content.trim()
        if (s.isEmpty()) return true
        if (Regex("""^(上午|下午)?\s*\d{1,2}:\d{2}$""").matches(s)) return true
        if (Regex("""^(昨天|今天|刚刚|星期[一二三四五六日天]|周[一二三四五六日天])$""").matches(s)) return true
        if (Regex("""^\d{4}/\d{1,2}/\d{1,2}$""").matches(s)) return true
        if (s in setOf("企业名片", "发起收款", "客户转账", "快捷回复", "推荐客服", "商品图册", "直播", "客户详情", "添加")) return true
        return false
    }

    private fun findChatMessageList(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        fun walk(n: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
            n ?: return null
            val cls = n.className?.toString().orEmpty()
            val id = n.viewIdResourceName?.substringAfterLast('/').orEmpty()
            if ((cls.contains("ListView", true) || n.isScrollable) && id == "iju") return n
            for (i in 0 until n.childCount) {
                val found = walk(n.getChild(i))
                if (found != null) return found
            }
            return null
        }
        return walk(root)
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
        synchronized(recentLock) {
            for (item in recent) {
                if (
                    item.chat == sender &&
                    item.type == bubble.type &&
                    item.content == bubble.content &&
                    item.fromSelf == bubble.fromSelf
                ) return true
            }
        }
        return false
    }

    private fun alreadySeen(sender: String, content: String): Boolean {
        synchronized(recentLock) {
            for (item in recent) {
                if (item.chat == sender && item.content == content && !item.fromSelf) return true
            }
        }
        return false
    }

    private fun gc(now: Long) {
        synchronized(recentLock) {
            while (recent.isNotEmpty() && now - recent.first().at > recentTtlMs) {
                recent.removeFirst()
            }
        }
    }

    private fun remember(item: RecentBubble) {
        synchronized(recentLock) {
            recent.addLast(item)
        }
    }

    private fun rememberIfUnseen(sender: String, bubble: ChatBubble, now: Long): Boolean {
        synchronized(recentLock) {
            for (item in recent) {
                if (
                    item.chat == sender &&
                    item.type == bubble.type &&
                    item.content == bubble.content &&
                    item.fromSelf == bubble.fromSelf
                ) return false
            }
            recent.addLast(RecentBubble(sender, bubble.type, bubble.content, bubble.fromSelf, now, bubble.boundsKey()))
            return true
        }
    }

    private suspend fun swipe(
        x1: Float,
        y1: Float,
        x2: Float,
        y2: Float,
        durationMs: Long,
    ): Boolean {
        val path = Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        // dispatchGesture's callback may never fire if the gesture pipeline drops
        // the stroke (system busy, conflicting gesture, etc). Time it out instead
        // of hanging the entire ReAct command, and log loudly when it happens —
        // a silent gesture drop is the kind of thing we must never lose.
        val budget = durationMs + 1_500L
        val raw = withTimeoutOrNull(budget) {
            suspendCancellableCoroutine { cont ->
                val dispatched = dispatchGesture(
                    gesture,
                    object : GestureResultCallback() {
                        override fun onCompleted(gestureDescription: GestureDescription?) {
                            if (cont.isActive) cont.resume(true)
                        }

                        override fun onCancelled(gestureDescription: GestureDescription?) {
                            if (cont.isActive) cont.resume(false)
                        }
                    },
                    null,
                )
                if (!dispatched && cont.isActive) cont.resume(false)
            }
        }
        val outcome = when (raw) {
            true -> "completed"
            false -> "cancelled_or_rejected"
            null -> "callback_timeout"
        }
        val accepted = raw == true
        if (!accepted) {
            Log.w(
                tag,
                "swipe drop x=($x1,$y1)->($x2,$y2) duration=${durationMs}ms budget=${budget}ms outcome=$outcome",
            )
        }
        return accepted
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
    fun forceHarvestConversationList() {
        maybeHarvestConversationList()
    }

    /** Returns the bounds (in screen px) of the conversation list, so the
     *  caller can compute a swipe gesture inside it. */
    fun getMessagesListBounds(): android.graphics.Rect? {
        val root = rootInActiveWindow ?: return null
        val list = findConversationListScrollable(root) ?: return null
        val r = android.graphics.Rect()
        list.getBoundsInScreen(r)
        return r
    }

    /**
     * Scroll the conversation list via the a11y ACTION_SCROLL_* action. Same
     * reasoning as scrollChatList: this is far more reliable than injecting
     * a swipe gesture, which we've seen get silently dropped by the gesture
     * pipeline. Returns false if the node isn't found or refuses the action,
     * so the caller can fall back to a coordinate swipe.
     *
     * forward=true → scroll toward older conversations (down the list).
     */
    fun scrollMessagesList(forward: Boolean): Boolean {
        val root = rootInActiveWindow ?: return false
        val list = findConversationListScrollable(root) ?: return false
        if (!list.isScrollable) return false
        val action = if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
        else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        val ok = list.performAction(action)
        if (!ok) Log.w(tag, "conversation list scroll rejected forward=$forward")
        return ok
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
    private fun maybeHarvestConversationList() {
        val cb = onChatMessage ?: return
        val root = rootInActiveWindow ?: return
        if (!isMessagesListVisible(root)) return

        val now = System.currentTimeMillis()
        gc(now)

        val rows = collectListRows(root)
        if (rows.isEmpty()) {
            Log.i(tag, "conversation list harvest skipped: no conversation rows")
            return
        }

        if (!conversationListBaselineReady) {
            var emittedUnread = 0
            for (row in rows) {
                conversationListBaseline[row.name] = row.preview
                if (!row.hasUnread) continue
                if (row.preview.isEmpty()) continue
                if (isOutboundPreview(row.preview)) continue
                val key = "conversation_list:${row.name}:${row.preview}:unread=${row.unreadCount}"
                if (!emittedUnreadConversationListPreviews.add(key)) continue
                val previewType = previewMessageType(row.preview)
                remember(RecentBubble(row.name, previewType, row.preview, fromSelf = false, at = now, boundsKey = key))
                cb(row.name, previewType, row.preview, false, key, emptyList(), emptyList(), "conversation_list_preview", row.unreadCount)
                emittedUnread++
            }
            conversationListBaselineReady = true
            Log.i(tag, "conversation list baseline primed rows=${rows.size} unread_rows=${rows.count { it.hasUnread }} emitted_unread_rows=$emittedUnread")
            return
        }

        var emitted = 0
        for (row in rows) {
            if (row.preview.isEmpty()) continue
            if (isOutboundPreview(row.preview)) continue
            val previousPreviewText = conversationListBaseline[row.name]
            val changed = previousPreviewText != row.preview
            if (!changed && !row.hasUnread) continue
            conversationListBaseline[row.name] = row.preview
            val key = "conversation_list:${row.name}:${row.preview}:unread=${row.unreadCount}"
            if (!emittedUnreadConversationListPreviews.add(key)) continue
            val previewType = previewMessageType(row.preview)
            remember(RecentBubble(row.name, previewType, row.preview, fromSelf = false, at = now, boundsKey = key))
            cb(row.name, previewType, row.preview, false, key, emptyList(), emptyList(), "conversation_list_preview", row.unreadCount)
            emitted++
            Log.d(
                tag,
                "conversation list harvest contact=${row.name} has_unread=${row.hasUnread} preview_changed=$changed preview_text=${row.preview}",
            )
        }
        Log.i(tag, "conversation list harvest checked rows=${rows.size} unread_rows=${rows.count { it.hasUnread }} emitted=$emitted")
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
                val headerCutoff = (resources.displayMetrics.heightPixels * 0.12f).toInt()
                if (r.centerY() < headerCutoff) {
                    found = true
                    return
                }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)
        return found
    }

    private data class ConversationListRow(
        val name: String,
        val preview: String,
        val hasUnread: Boolean,
        val unreadCount: Int,
    )

    private data class ConversationListText(
        val y: Int,
        val x: Int,
        val id: String,
        val text: String,
    )

    private fun collectListRows(root: AccessibilityNodeInfo): List<ConversationListRow> {
        val list = findConversationListScrollable(root) ?: run {
            Log.i(tag, "conversation list harvest skipped: no scrollable conversation list")
            return emptyList()
        }
        val out = mutableListOf<ConversationListRow>()
        val screenHeight = resources.displayMetrics.heightPixels
        val headerCutoff = (screenHeight * 0.09f).toInt()
        val footerCutoff = (screenHeight * 0.90f).toInt()

        for (i in 0 until list.childCount) {
            val row = list.getChild(i) ?: continue
            val rowBounds = android.graphics.Rect()
            row.getBoundsInScreen(rowBounds)
            if (rowBounds.bottom < headerCutoff || rowBounds.top > footerCutoff) continue

            val texts = mutableListOf<ConversationListText>()
            fun walk(n: AccessibilityNodeInfo?) {
                n ?: return
                val cls = n.className?.toString().orEmpty()
                if (cls.contains("TextView", true)) {
                    val t = n.text?.toString().orEmpty().trim()
                    if (t.isNotEmpty()) {
                        val r = android.graphics.Rect()
                        n.getBoundsInScreen(r)
                        texts.add(
                            ConversationListText(
                                y = r.centerY(),
                                x = r.centerX(),
                                id = n.viewIdResourceName?.substringAfterLast('/').orEmpty(),
                                text = t,
                            )
                        )
                    }
                }
                for (j in 0 until n.childCount) walk(n.getChild(j))
            }
            walk(row)
            if (texts.size < 2) continue

            texts.sortWith(compareBy<ConversationListText> { it.y }.thenBy { it.x })
            val idBased = parseConversationListRowByKnownFields(texts)
            val fallback = idBased ?: parseConversationListRowByTextShape(texts)
            val parsed = fallback ?: continue
            val (name, preview, hasUnread, unreadCount) = parsed
            if (preview.isBlank()) continue
            out.add(ConversationListRow(name = name, preview = preview, hasUnread = hasUnread, unreadCount = unreadCount))
            Log.d(tag, "conversation list row parsed contact=$name has_unread=$hasUnread unread_count=$unreadCount preview_text=$preview")
        }
        Log.i(tag, "conversation list rows parsed=${out.size} list_children=${list.childCount}")
        return out
    }

    private fun parseConversationListRowByKnownFields(texts: List<ConversationListText>): ConversationListRow? {
        val name = texts
            .firstOrNull { it.id == "hrr" && !looksLikeBottomTabLabel(it.text) && it.text != "消息" }
            ?.text
            ?: return null
        val preview = texts
            .firstOrNull { it.id == "mdj" && it.text.isNotBlank() }
            ?.text
            ?: return null
        val unreadCount = texts
            .filter { it.id == "ko_" }
            .mapNotNull { unreadBadgeCount(it.text) }
            .maxOrNull() ?: 0
        return ConversationListRow(name = name, preview = preview, hasUnread = unreadCount > 0, unreadCount = unreadCount)
    }

    private fun parseConversationListRowByTextShape(texts: List<ConversationListText>): ConversationListRow? {
        val name = texts
            .map { it.text }
            .firstOrNull {
                !looksLikeUnreadBadge(it) &&
                    !looksLikeTimestamp(it) &&
                    !looksLikeBottomTabLabel(it)
            }
            ?: return null
        val rest = texts.map { it.text }
            .filter { it != name }
            .filterNot { looksLikeTimestamp(it) }
            .filterNot { looksLikeUnreadBadge(it) }
            .filterNot { looksLikeContactTag(it) }
            .filterNot { looksLikeBottomTabLabel(it) }
        if (rest.isEmpty()) return null
        val preview = rest.maxByOrNull { it.length }?.trim() ?: return null
        val unreadCount = texts.mapNotNull { unreadBadgeCount(it.text) }.maxOrNull() ?: 0
        return ConversationListRow(name = name, preview = preview, hasUnread = unreadCount > 0, unreadCount = unreadCount)
    }

    private fun findConversationListScrollable(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val candidates = mutableListOf<Pair<Int, AccessibilityNodeInfo>>()
        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val cls = n.className?.toString().orEmpty()
            if (n.isScrollable || cls.contains("RecyclerView", true)) {
                val score = conversationListScore(n)
                if (score > 0) {
                    candidates.add(score to n)
                }
            }
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)
        return candidates.maxByOrNull { it.first }?.second
    }

    private fun conversationListScore(node: AccessibilityNodeInfo): Int {
        val r = android.graphics.Rect()
        node.getBoundsInScreen(r)
        if (r.height() <= 0 || r.width() <= 0) return 0
        val screenWidth = resources.displayMetrics.widthPixels
        val screenHeight = resources.displayMetrics.heightPixels
        if (r.width() < screenWidth * 0.45f) return 0
        if (r.bottom < screenHeight * 0.20f) return 0

        var rowishChildren = 0
        var knownFieldCount = 0
        var unreadBadgeCount = 0
        for (idx in 0 until node.childCount) {
            val child = node.getChild(idx) ?: continue
            val cr = android.graphics.Rect()
            child.getBoundsInScreen(cr)
            if (cr.height() > screenHeight * 0.035f && cr.width() > screenWidth * 0.45f) {
                rowishChildren++
            }
            collectTextIds(child) { id, text ->
                if (id in setOf("hrr", "mdj", "ko_", "g80")) knownFieldCount++
                if (id == "ko_" && looksLikeUnreadBadge(text)) unreadBadgeCount++
            }
        }
        if (rowishChildren == 0 && knownFieldCount == 0) return 0
        return rowishChildren * 10 +
            knownFieldCount * 8 +
            unreadBadgeCount * 20 +
            r.height() / 100 +
            r.width() / 200
    }

    private fun collectTextIds(
        node: AccessibilityNodeInfo,
        visit: (id: String, text: String) -> Unit,
    ) {
        val cls = node.className?.toString().orEmpty()
        if (cls.contains("TextView", true)) {
            val text = node.text?.toString().orEmpty().trim()
            val id = node.viewIdResourceName?.substringAfterLast('/').orEmpty()
            if (text.isNotEmpty() || id.isNotEmpty()) visit(id, text)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectTextIds(child, visit)
        }
    }

    private fun looksLikeTimestamp(s: String): Boolean {
        if (s.length > 8) return false
        // 16:43, 昨天, 星期三, 03/05, 2024/03/05
        if (s == "昨天" || s.startsWith("星期")) return true
        return s.matches(Regex("""^\d{1,2}[:/-]\d{1,2}([:/-]\d{1,2})?$"""))
    }

    private fun looksLikeUnreadBadge(s: String): Boolean {
        return unreadBadgeCount(s) != null
    }

    private fun unreadBadgeCount(s: String): Int? {
        val trimmed = s.trim()
        if (!trimmed.matches(Regex("""^\d{1,3}\+?$"""))) return null
        val value = trimmed.removeSuffix("+").toIntOrNull() ?: return null
        return if (trimmed.endsWith("+")) value + 1 else value
    }

    private fun looksLikeContactTag(s: String): Boolean {
        return s.startsWith("@") || s.startsWith("＠") || s in setOf("外部", "微信")
    }

    private fun looksLikeBottomTabLabel(s: String): Boolean {
        return s in setOf("消息", "邮件", "文档", "工作台", "通讯录", "设置")
    }

    private fun isOutboundPreview(p: String): Boolean {
        if (p.startsWith("我:") || p.startsWith("我：")) return true
        if (p.startsWith("[草稿]") || p.startsWith("[Draft]")) return true
        return false
    }
}
