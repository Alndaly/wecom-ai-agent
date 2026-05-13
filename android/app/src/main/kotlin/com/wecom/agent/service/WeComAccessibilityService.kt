package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.util.Log
import android.view.accessibility.AccessibilityEvent

/**
 * Captures events from the WeCom client.
 *
 * MVP1: skeleton — logs window changes and infers the current page (HOME/CHAT/...).
 * MVP1b: parse message list, fire `message.received` to backend.
 */
class WeComAccessibilityService : AccessibilityService() {
    private val tag = "WeComA11y"

    enum class Page { HOME, CHAT, SEARCH, CONTACT, MOMENTS, UNKNOWN }

    @Volatile var currentPage: Page = Page.UNKNOWN
        private set

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            val cls = event.className?.toString().orEmpty()
            currentPage = when {
                cls.contains("LauncherUI", true) -> Page.HOME
                cls.contains("ChatActivity", true) || cls.contains("MessageList", true) -> Page.CHAT
                cls.contains("Search", true) -> Page.SEARCH
                cls.contains("Contact", true) -> Page.CONTACT
                cls.contains("Moments", true) || cls.contains("SNS", true) -> Page.MOMENTS
                else -> Page.UNKNOWN
            }
            Log.d(tag, "page=$currentPage cls=$cls")
        }
    }

    override fun onInterrupt() = Unit

    // TODO(MVP1b): expose suspend functions used by TaskExecutor:
    //   suspend fun openChatWith(externalId: String): Boolean
    //   suspend fun sendText(text: String): Boolean
}
