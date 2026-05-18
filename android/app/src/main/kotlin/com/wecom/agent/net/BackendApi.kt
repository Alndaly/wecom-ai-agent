package com.wecom.agent.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

data class UiAnalysisResult(
    val ok: Boolean,
    val page: String = "other",
    val targetMatches: Boolean = false,
    val confidence: Double = 0.0,
    val reason: String = "",
    val suggestedAction: String = "none",
    val error: String? = null,
)

class BackendApi(
    private val baseHttpUrl: String,
    private val token: String,
) {
    private val client = OkHttpClient.Builder()
        .callTimeout(60, TimeUnit.SECONDS)
        .build()

    fun analyzeUi(
        contactName: String,
        currentPage: String,
        tree: String,
        imageBase64: String?,
        mime: String,
    ): UiAnalysisResult? {
        val json = JSONObject().apply {
            put("contact_name", contactName)
            put("current_page", currentPage)
            put("tree", tree)
            put("mime", mime)
            if (!imageBase64.isNullOrBlank()) put("image", imageBase64)
        }
        val body = json.toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("${baseHttpUrl.trimEnd('/')}/ui-analysis")
            .addHeader("Authorization", "Bearer $token")
            .post(body)
            .build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return UiAnalysisResult(false, error = "HTTP ${resp.code}")
            val raw = resp.body?.string().orEmpty()
            val obj = JSONObject(raw)
            return UiAnalysisResult(
                ok = obj.optBoolean("ok", false),
                page = obj.optString("page", "other"),
                targetMatches = obj.optBoolean("target_matches", false),
                confidence = obj.optDouble("confidence", 0.0),
                reason = obj.optString("reason", ""),
                suggestedAction = obj.optString("suggested_action", "none"),
                error = obj.optString("error").takeIf { it.isNotBlank() },
            )
        }
    }

    fun uploadInboundMedia(
        robotId: String,
        type: String,
        file: File,
        mime: String,
        filename: String,
    ): JSONObject? {
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("robot_id", robotId)
            .addFormDataPart("token", token)
            .addFormDataPart("type", type)
            .addFormDataPart("file", filename, file.readBytes().toRequestBody(mime.toMediaType()))
            .build()
        val req = Request.Builder()
            .url("${baseHttpUrl.trimEnd('/')}/android/inbound-media")
            .post(body)
            .build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val raw = resp.body?.string().orEmpty()
            return JSONObject(raw)
        }
    }

    companion object {
        fun httpBaseFromWs(baseWsUrl: String): String =
            baseWsUrl
                .replaceFirst("ws://", "http://", ignoreCase = true)
                .replaceFirst("wss://", "https://", ignoreCase = true)
                .trimEnd('/')
    }
}
