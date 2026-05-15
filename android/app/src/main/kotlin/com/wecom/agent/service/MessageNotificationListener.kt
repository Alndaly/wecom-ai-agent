package com.wecom.agent.service

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import android.os.Bundle

/**
 * Primary inbound channel — fires for every incoming WeCom notification,
 * even when the user is not in the chat.
 *
 * WeCom puts the sender into `android.title` and the body into `android.text`.
 * For group chats the title is typically "<senderNick> · <groupName>" or
 * "<groupName>" with the body prefixed by "<senderNick>: ...". We don't try
 * to be too smart here — we hand the raw nickname + content up to a
 * callback and the backend resolves the contact.
 *
 * Dedupe is keyed by `(packageName, postTime, title, text)` — Android often
 * re-posts the same notification when the count changes.
 */
class MessageNotificationListener : NotificationListenerService() {
    companion object {
        @Volatile var instance: MessageNotificationListener? = null
            private set

        /** Set by AgentForegroundService. */
        @Volatile var onMessage: ((sender: String, content: String, postTime: Long) -> Unit)? = null
    }

    private val tag = "MsgNotifListener"
    private val wecomPkg = "com.tencent.wework"

    // Cap at 200 — small bounded LRU.
    private val seen = LinkedHashSet<String>()
    private val seenCap = 200

    /**
     * WeCom aggregates pending unread messages into a single notification
     * whose body is prefixed with "[N条]" (e.g. "[3条]你好"). It's
     * notification-layer noise, not part of the actual message content —
     * strip it before we send anywhere.
     */
    private val aggregationPrefix = Regex("""^\s*\[\s*\d+\s*条\s*]\s*""")

    override fun onListenerConnected() {
        super.onListenerConnected()
        instance = this
        Log.i(tag, "listener connected")
    }

    override fun onListenerDisconnected() {
        super.onListenerDisconnected()
        if (instance === this) instance = null
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        sbn ?: return
        if (sbn.packageName != wecomPkg) return
        val n: Notification = sbn.notification ?: return
        val extras = n.extras
        val title = firstText(extras, Notification.EXTRA_TITLE, Notification.EXTRA_BIG_TEXT, Notification.EXTRA_SUB_TEXT)
        val rawText = firstText(
            extras,
            Notification.EXTRA_TEXT,
            Notification.EXTRA_BIG_TEXT,
            Notification.EXTRA_TEXT_LINES,
        )
        if (title.isEmpty() || rawText.isEmpty()) {
            Log.d(tag, "skip notif: missing title/text pkg=${sbn.packageName} extras=${extras.keySet()}")
            return
        }

        // ignore the persistent "您有 N 条未读消息" summary
        if (rawText.contains("未读消息") || rawText.contains("条新消息")) return

        // strip WeCom's "[N条]" aggregation prefix that appears on the 2nd+
        // pending message in the same chat
        val cleanText = aggregationPrefix.replace(rawText, "").trim()
        val cleanTitle = aggregationPrefix.replace(title, "").trim()
        if (cleanText.isEmpty()) return

        val (sender, content) = parseSenderAndContent(cleanTitle, cleanText)
        if (content.isEmpty()) return

        val key = "${sbn.postTime}|$sender|$content"
        if (!rememberKey(key)) return

        Log.d(tag, "inbound: $sender :: ${content.take(60)}")
        onMessage?.invoke(sender, content, sbn.postTime)
    }

    private fun firstText(extras: Bundle, vararg keys: String): String {
        for (key in keys) {
            val v = extras.getCharSequence(key)?.toString()?.trim().orEmpty()
            if (v.isNotEmpty()) return v
        }
        return ""
    }

    /**
     * Heuristic split:
     *  - "张三: 在吗?"            → ("张三", "在吗?")
     *  - title="张三", text="在吗?" → ("张三", "在吗?")
     *  - title="销售群", text="张三: 在吗?" → ("销售群#张三", "在吗?")
     */
    private fun parseSenderAndContent(title: String, text: String): Pair<String, String> {
        val colonRx = Regex("^([^:：\\s][^:：]{0,40})[:：]\\s?(.*)\$", RegexOption.DOT_MATCHES_ALL)
        val m = colonRx.matchEntire(text)
        return if (m != null) {
            val inner = m.groupValues[1].trim()
            val body = m.groupValues[2].trim()
            val sender = if (title.isNotEmpty() && title != inner) "$title#$inner" else inner
            sender to body
        } else {
            title to text
        }
    }

    @Synchronized
    private fun rememberKey(key: String): Boolean {
        if (key in seen) return false
        seen.add(key)
        if (seen.size > seenCap) {
            val it = seen.iterator()
            it.next(); it.remove()
        }
        return true
    }
}
