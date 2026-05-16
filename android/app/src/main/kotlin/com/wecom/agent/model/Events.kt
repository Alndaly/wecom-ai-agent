package com.wecom.agent.model

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

@Serializable
data class WireEvent(val event: String, val payload: JsonElement? = null)

@Serializable
data class Contact(
    val external_id: String,
    val nickname: String = "",
    val avatar: String? = null,
)

@Serializable
data class MessageReceivedPayload(
    val contact: Contact,
    val external_msg_id: String? = null,
    val type: String = "text",
    val content: String,
    val sender_type: String = "customer",
    val sent_at: String? = null,
)

@Serializable
data class HeartbeatPayload(
    val current_page: String? = null,
    val battery: Int? = null,
    val device_type: String? = null,
    val device_name: String? = null,
    val manufacturer: String? = null,
    val model: String? = null,
    val android_version: String? = null,
    val sdk_int: Int? = null,
    val app_version: String? = null,
    val screen_width: Int? = null,
    val screen_height: Int? = null,
)

@Serializable
data class UiDumpPayload(
    val reason: String,
    val request_id: String? = null,
    val current_page: String? = null,
    val tree: String,
    val nodes: List<UiNode> = emptyList(),
    val screen_width: Int? = null,
    val screen_height: Int? = null,
)

@Serializable
data class UiNode(
    val id: Int,
    val cls: String,
    val view_id: String = "",
    val text: String = "",
    val desc: String = "",
    val clickable: Boolean = false,
    val focusable: Boolean = false,
    val editable: Boolean = false,
    val scrollable: Boolean = false,
    // bounds in screen pixels [left, top, right, bottom]
    val bounds: List<Int> = emptyList(),
)

@Serializable
data class ScreenFramePayload(
    val image: String? = null,
    val mime: String = "image/jpeg",
    val width: Int? = null,
    val height: Int? = null,
    val error: String? = null,
)

@Serializable
data class DeviceCommandAckPayload(
    val command: String,
    val ok: Boolean,
    val message: String? = null,
)

/** Carries the result of a remote primitive triggered by the ReAct agent.
 *  `data` holds command-specific structured output (e.g. screenshot base64). */
@Serializable
data class DeviceCommandResultPayload(
    val command: String,
    val request_id: String,
    val ok: Boolean,
    val message: String? = null,
    val data: JsonElement? = null,
)
