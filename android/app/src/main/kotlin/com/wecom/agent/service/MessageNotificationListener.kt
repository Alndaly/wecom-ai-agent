package com.wecom.agent.service

import android.app.Notification
import android.content.Intent
import android.os.Build
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
        @Volatile private var onMessage: ((sender: String, content: String, postTime: Long) -> Unit)? = null
        private val pending = ArrayDeque<Triple<String, String, Long>>()
        private const val pendingCap = 50

        @Synchronized
        fun registerCallback(cb: (sender: String, content: String, postTime: Long) -> Unit) {
            onMessage = cb
            while (pending.isNotEmpty()) {
                val (sender, content, postTime) = pending.removeFirst()
                cb(sender, content, postTime)
            }
        }

        @Synchronized
        fun unregisterCallback() {
            onMessage = null
        }

        @Synchronized
        private fun dispatch(sender: String, content: String, postTime: Long): Boolean {
            val cb = onMessage
            if (cb != null) {
                cb(sender, content, postTime)
                return true
            }
            pending.addLast(Triple(sender, content, postTime))
            while (pending.size > pendingCap) pending.removeFirst()
            Log.w("MsgNotifListener", "notification queued because foreground callback is not registered sender=$sender content=${content.take(40)}")
            return false
        }
    }

    private val tag = "MsgNotifListener"
    private val wecomPkg = "com.tencent.wework"

    // Cap at 200 — small bounded LRU.
    private val seen = LinkedHashSet<String>()
    private val seenCap = 200
    private var lastAgentStartAttemptAt = 0L

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
        activeNotifications?.forEach { onNotificationPosted(it) }
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
        val title = firstText(extras, Notification.EXTRA_TITLE, Notification.EXTRA_SUB_TEXT)
        val rawText = firstText(
            extras,
            Notification.EXTRA_TEXT,
            Notification.EXTRA_BIG_TEXT,
            Notification.EXTRA_TEXT_LINES,
        )
        if (title.isEmpty() || rawText.isEmpty()) {
            Log.i(tag, "skip notif: missing title/text title=${title.take(40)} text=${rawText.take(40)} extras=${extras.keySet()}")
            return
        }

        // ignore the persistent "您有 N 条未读消息" summary
        if (isSummaryNotification(cleanText(rawText), cleanText(title))) {
            Log.i(tag, "skip notif: summary title=${title.take(40)} text=${rawText.take(60)}")
            return
        }

        // strip WeCom's "[N条]" aggregation prefix that appears on the 2nd+
        // pending message in the same chat
        val cleanText = cleanText(rawText)
        val cleanTitle = cleanText(title)
        if (cleanText.isEmpty()) {
            Log.i(tag, "skip notif: empty after clean title=${title.take(40)} text=${rawText.take(60)}")
            return
        }

        val (sender, content) = parseSenderAndContent(cleanTitle, cleanText)
        if (content.isEmpty()) {
            Log.i(tag, "skip notif: empty parsed sender=${sender.take(40)} raw=${cleanText.take(60)}")
            return
        }

        val key = "${sbn.postTime}|$sender|$content"
        if (!rememberKey(key)) {
            Log.d(tag, "skip notif: duplicate sender=${sender.take(40)} content=${content.take(60)}")
            return
        }

        Log.i(tag, "inbound: $sender :: ${content.take(60)}")
        if (!dispatch(sender, content, sbn.postTime)) {
            requestAgentStartIfConfigured()
        }
    }

    private fun cleanText(text: String): String {
        return aggregationPrefix.replace(text, "").trim()
    }

    private fun isSummaryNotification(text: String, title: String): Boolean {
        if (text.contains("未读消息")) return true
        if (Regex("""^\s*\d+\s*条新消息\s*$""").matches(text)) return true
        if (Regex("""^\s*你收到\s*\d+\s*条新消息\s*$""").matches(text)) return true
        if (title == "企业微信" && Regex("""^\s*\d+\s*条消息\s*$""").matches(text)) return true
        return false
    }

    private fun firstText(extras: Bundle, vararg keys: String): String {
        for (key in keys) {
            val arr = extras.getCharSequenceArray(key)
            if (!arr.isNullOrEmpty()) {
                val joined = arr.mapNotNull { it?.toString()?.trim() }
                    .filter { it.isNotEmpty() }
                    .joinToString("\n")
                if (joined.isNotEmpty()) return joined
            }
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

    private fun requestAgentStartIfConfigured() {
        val now = System.currentTimeMillis()
        if (now - lastAgentStartAttemptAt < 10_000L) return
        lastAgentStartAttemptAt = now

        val prefs = getSharedPreferences("agent", MODE_PRIVATE)
        val base = prefs.getString("base_url", null)?.trim().orEmpty()
        val rid = prefs.getString("robot_id", null)?.trim().orEmpty()
        val token = prefs.getString("token", null)?.trim().orEmpty()
        if (base.isEmpty() || rid.isEmpty() || token.isEmpty()) {
            Log.w(tag, "cannot start agent from notification: missing saved config")
            return
        }

        val intent = Intent(this, AgentForegroundService::class.java).apply {
            putExtra(AgentForegroundService.EXTRA_BASE_URL, base)
            putExtra(AgentForegroundService.EXTRA_ROBOT_ID, rid)
            putExtra(AgentForegroundService.EXTRA_TOKEN, token)
            putExtra(
                AgentForegroundService.EXTRA_A11Y_INGEST,
                prefs.getBoolean("a11y_ingest", true),
            )
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
            Log.i(tag, "requested AgentForegroundService start from notification")
        } catch (e: Exception) {
            Log.w(tag, "start AgentForegroundService from notification failed", e)
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
