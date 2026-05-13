package com.wecom.agent.service

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log

/**
 * Fallback inbound channel — when the user is not in the chat page, we still
 * want to know a message arrived (used to wake AccessibilityService and pull it).
 *
 * MVP1: log only.
 */
class MessageNotificationListener : NotificationListenerService() {
    private val tag = "MsgNotifListener"

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        sbn ?: return
        if (sbn.packageName != "com.tencent.wework") return
        val title = sbn.notification?.extras?.getString("android.title")
        val text = sbn.notification?.extras?.getString("android.text")
        Log.d(tag, "notif: $title / $text")
    }
}
