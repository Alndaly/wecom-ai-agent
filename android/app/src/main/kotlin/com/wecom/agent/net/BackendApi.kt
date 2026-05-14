package com.wecom.agent.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
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
}
