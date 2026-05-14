package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

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

    /** Dedupe per-session: we never re-fire the same (sender, content) within 30s. */
    private val recent = ArrayDeque<Triple<String, String, Long>>()
    private val recentTtlMs = 30_000L

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(tag, "service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> updatePage(event)
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> {
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
            cls.contains("LauncherUI", true) -> Page.HOME
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
            currentChatTitle = inferChatTitle()
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

        val texts = mutableListOf<AccessibilityNodeInfo>()
        fun walk(n: AccessibilityNodeInfo?) {
            n ?: return
            val cls = n.className?.toString() ?: ""
            if (cls.contains("TextView", true) && !n.text.isNullOrBlank()) texts.add(n)
            for (i in 0 until n.childCount) walk(n.getChild(i))
        }
        walk(root)

        for (t in texts) {
            val r = android.graphics.Rect()
            t.getBoundsInScreen(r)
            // header zone excluded
            if (r.top < 200) continue
            // input bar (bottom) excluded
            if (r.bottom > resources.displayMetrics.heightPixels - 120) continue
            // outbound bubble (right half) excluded
            if (r.left > screenWidth / 2) continue
            val content = t.text.toString().trim()
            if (content.isEmpty() || content.length > 2000) continue
            if (alreadySeen(title, content, now)) continue
            recent.addLast(Triple(title, content, now))
            cb(title, content)
        }
    }

    private fun alreadySeen(sender: String, content: String, now: Long): Boolean {
        for ((s, c, _t) in recent) {
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
