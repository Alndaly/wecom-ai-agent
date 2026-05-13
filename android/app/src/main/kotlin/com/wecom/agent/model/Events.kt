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
    val sent_at: String? = null,
)

@Serializable
data class TaskDispatchPayload(
    val task_id: Long,
    val type: String,
    val payload: JsonElement,
)

@Serializable
data class TaskAckPayload(
    val task_id: Long,
    val error: String? = null,
)

@Serializable
data class HeartbeatPayload(
    val current_page: String? = null,
    val battery: Int? = null,
)
