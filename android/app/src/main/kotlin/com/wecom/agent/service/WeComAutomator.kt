package com.wecom.agent.service

import android.accessibilityservice.AccessibilityService
import android.content.ComponentName
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.graphics.Rect
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.util.UUID

data class NodeExpectation(
    val cls: String = "",
    val viewId: String = "",
    val text: String = "",
    val desc: String = "",
    val bounds: List<Int>? = null,
    val editable: Boolean? = null,
    val clickable: Boolean? = null,
)

data class SavedInboundMedia(
    val file: File,
    val mime: String,
    val filename: String,
    val sizeBytes: Long,
)

/**
 * Generic device-level primitives the backend ReAct agent calls through
 * `device.command`. There is **no** WeCom-specific heuristic in here anymore
 * — the LLM observes the UI tree and decides which primitive to invoke.
 *
 * The only WeCom-aware method left is [openWeCom], used as a pre-flight by
 * the backend to bring the app to foreground before reasoning starts.
 */
class WeComAutomator(
    private val ctx: Context,
    private val log: (String) -> Unit,
) {
    private val tag = "WeComAuto"
    private val wecomPkg = "com.tencent.wework"
    private val http = OkHttpClient()

    /** UI operations have to happen on the main looper; we just poll. */
    private suspend fun a11y(): AccessibilityService? {
        val svc = withTimeoutOrNull(3_000) {
            while (WeComAccessibilityService.instance == null) delay(100)
            WeComAccessibilityService.instance
        }
        if (svc == null) log("AccessibilityService 未运行（请到「设置 → 无障碍」中打开）")
        return svc
    }

    /** Bring WeCom to the foreground. Returns null on success or an error msg. */
    suspend fun openWeCom(): Pair<Boolean, String> {
        return try {
            val intent = ctx.packageManager.getLaunchIntentForPackage(wecomPkg)
                ?: Intent().apply {
                    component = ComponentName(wecomPkg, "com.tencent.wework.launch.LauncherActivity")
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                }
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
            ctx.startActivity(intent)
            val svc = a11y() ?: return false to "无障碍未启用"
            val ready = withTimeoutOrNull(5_000) {
                while (true) {
                    val root = svc.rootInActiveWindow
                    if (root?.packageName?.toString() == wecomPkg && root.childCount > 0) {
                        return@withTimeoutOrNull true
                    }
                    delay(200)
                }
            } == true
            if (ready) {
                Pair(true, "已打开 WeCom")
            } else {
                // Vendors like Huawei EMUI/HarmonyOS silently block background
                // startActivity. Fall back to tapping the launcher icon via a11y
                // — same path a user takes, no special permission needed.
                val tapped = tapLauncherIconByLabel(svc, listOf("企业微信", "WeCom"))
                if (tapped) {
                    val ok = withTimeoutOrNull(5_000) {
                        while (true) {
                            val root = svc.rootInActiveWindow
                            if (root?.packageName?.toString() == wecomPkg && root.childCount > 0) {
                                return@withTimeoutOrNull true
                            }
                            delay(200)
                        }
                    } == true
                    if (ok) return Pair(true, "已通过桌面图标打开 WeCom")
                }
                val root = svc.rootInActiveWindow
                val pkg = root?.packageName?.toString() ?: "null"
                Pair(false, "已发送打开请求，但未进入 WeCom 前台：pkg=$pkg children=${root?.childCount ?: 0}")
            }
        } catch (e: Exception) {
            Pair(false, "openWeCom: ${e.message}")
        }
    }

    private fun tapLauncherIconByLabel(
        svc: AccessibilityService,
        labels: List<String>,
    ): Boolean {
        val root = svc.rootInActiveWindow ?: return false
        val match = root.findFirst { node ->
            val text = (node.text?.toString() ?: node.contentDescription?.toString() ?: "").trim()
            text in labels
        } ?: return false
        var n: AccessibilityNodeInfo? = match
        while (n != null && !n.isClickable) n = n.parent
        return (n ?: match).tap()
    }

    // ====================================================================
    //  Primitive ops for the backend ReAct agent. Generic — no WeCom-specific
    //  heuristics; the agent decides what to do based on UI tree + screenshot.
    // ====================================================================

    suspend fun reactTapText(text: String): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val root = svc.rootInActiveWindow ?: return false to "无活动窗口"
        val node = root.findFirst { matchesText(it, text) }
            ?: return false to "未找到包含「$text」的节点"
        var n: AccessibilityNodeInfo? = node
        while (n != null && !n.isClickable) n = n.parent
        val target = n ?: node
        val ok = target.tap()
        return ok to if (ok) "已点击「$text」" else "节点不可点击"
    }

    suspend fun reactTapNode(
        nodeId: Int,
        fallbackX: Int?,
        fallbackY: Int?,
        expected: NodeExpectation = NodeExpectation(),
    ): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val root = svc.rootInActiveWindow ?: return false to "无活动窗口"
        val node = root.findByDumpId(nodeId)
        if (node != null) {
            node.expectationMismatch(expected)?.let { return false to "节点 [$nodeId] 快照不匹配：$it" }
            val target = node.clickTarget()
            if (target != null && target.tap()) {
                val label = target.label().ifBlank { target.className?.toString()?.substringAfterLast('.') ?: "node" }
                return true to "已通过节点 ACTION_CLICK 点击 [$nodeId] $label"
            }
        }
        if (node != null && fallbackX != null && fallbackY != null) {
            val ok = gestureTap(svc, fallbackX.toFloat(), fallbackY.toFloat())
            val reason = "节点 ACTION_CLICK 失败"
            return ok to if (ok) "$reason，已坐标兜底 ($fallbackX, $fallbackY)" else "$reason，坐标兜底也失败"
        }
        return false to if (node == null) "未找到节点 [$nodeId]" else "节点 ACTION_CLICK 失败且缺少坐标兜底"
    }

    suspend fun reactTapXY(x: Int, y: Int): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = gestureTap(svc, x.toFloat(), y.toFloat())
        return ok to if (ok) "已在 ($x, $y) 点击" else "手势失败"
    }

    suspend fun reactDoubleTapNode(
        nodeId: Int,
        fallbackX: Int?,
        fallbackY: Int?,
        expected: NodeExpectation = NodeExpectation(),
    ): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val root = svc.rootInActiveWindow ?: return false to "无活动窗口"
        val node = root.findByDumpId(nodeId)
        if (node != null) {
            node.expectationMismatch(expected)?.let { return false to "节点 [$nodeId] 快照不匹配：$it" }
            val target = node.clickTarget()
            if (target != null && target.tap()) {
                delay(120)
                val ok = target.tap()
                val label = target.label().ifBlank { target.className?.toString()?.substringAfterLast('.') ?: "node" }
                if (ok) return true to "已通过节点 ACTION_CLICK 双击 [$nodeId] $label"
            }
        }
        if (node != null && fallbackX != null && fallbackY != null) {
            val ok = gestureDoubleTap(svc, fallbackX.toFloat(), fallbackY.toFloat())
            val reason = "节点双击 ACTION_CLICK 失败"
            return ok to if (ok) "$reason，已坐标双击兜底 ($fallbackX, $fallbackY)" else "$reason，坐标双击也失败"
        }
        return false to if (node == null) "未找到节点 [$nodeId]" else "节点双击失败且缺少坐标兜底"
    }

    suspend fun reactDoubleTapXY(x: Int, y: Int): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = gestureDoubleTap(svc, x.toFloat(), y.toFloat())
        return ok to if (ok) "已在 ($x, $y) 双击" else "双击手势失败"
    }

    suspend fun reactLongPressNode(
        nodeId: Int,
        fallbackX: Int?,
        fallbackY: Int?,
        expected: NodeExpectation = NodeExpectation(),
        durationMs: Long = 650,
    ): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val root = svc.rootInActiveWindow ?: return false to "无活动窗口"
        val node = root.findByDumpId(nodeId)
        if (node != null) {
            node.expectationMismatch(expected)?.let { return false to "节点 [$nodeId] 快照不匹配：$it" }
            val target = node.longClickTarget()
            if (target != null && target.longPress()) {
                val label = target.label().ifBlank { target.className?.toString()?.substringAfterLast('.') ?: "node" }
                return true to "已通过节点 ACTION_LONG_CLICK 长按 [$nodeId] $label"
            }
        }
        if (node != null && fallbackX != null && fallbackY != null) {
            val dur = durationMs.coerceIn(350L, 3_000L)
            val ok = gestureLongPress(svc, fallbackX.toFloat(), fallbackY.toFloat(), dur)
            val reason = "节点 ACTION_LONG_CLICK 失败"
            return ok to if (ok) "$reason，已长按坐标兜底 ($fallbackX, $fallbackY) ${dur}ms" else "$reason，坐标长按也失败"
        }
        return false to if (node == null) "未找到节点 [$nodeId]" else "节点 ACTION_LONG_CLICK 失败且缺少坐标兜底"
    }

    suspend fun reactLongPressXY(x: Int, y: Int, durationMs: Long = 650): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val dur = durationMs.coerceIn(350L, 3_000L)
        val ok = gestureLongPress(svc, x.toFloat(), y.toFloat(), dur)
        return ok to if (ok) "已在 ($x, $y) 长按 ${dur}ms" else "长按手势失败"
    }

    suspend fun reactDragXY(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long = 450): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val dur = durationMs.coerceIn(120L, 5_000L)
        val ok = gestureSwipe(svc, x1.toFloat(), y1.toFloat(), x2.toFloat(), y2.toFloat(), dur)
        return ok to if (ok) "已拖拽 ($x1,$y1)→($x2,$y2) ${dur}ms" else "拖拽手势失败"
    }

    suspend fun reactSwipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long = 300): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = gestureSwipe(svc, x1.toFloat(), y1.toFloat(), x2.toFloat(), y2.toFloat(), durationMs)
        return ok to if (ok) "已滑动 ($x1,$y1)→($x2,$y2)" else "手势失败"
    }

    suspend fun reactInputText(
        text: String,
        mode: String = "replace",
        nodeId: Int? = null,
        expected: NodeExpectation = NodeExpectation(),
    ): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val root = svc.rootInActiveWindow ?: return false to "无活动窗口"
        val edit = if (nodeId != null) {
            val node = root.findByDumpId(nodeId) ?: return false to "未找到节点 [$nodeId]"
            node.expectationMismatch(expected)?.let { return false to "节点 [$nodeId] 快照不匹配：$it" }
            if (!node.isEditable) return false to "节点 [$nodeId] 不是可编辑输入框"
            node
        } else {
            // Prefer the currently focused editable; fall back to any editable.
            root.findFirst { it.isEditable && it.isFocused }
                ?: root.findFirst { it.isEditable }
                ?: return false to "未找到可编辑输入框"
        }
        val normalizedMode = mode.lowercase()
        val nextText = when (normalizedMode) {
            "append" -> edit.text?.toString().orEmpty() + text
            "clear" -> ""
            else -> text
        }
        val ok = edit.replaceText(nextText)
        val label = when (normalizedMode) {
            "append" -> "已追加文本"
            "clear" -> "已清空文本"
            else -> "已输入文本"
        }
        return ok to if (ok) label else "ACTION_SET_TEXT 失败"
    }

    suspend fun reactBack(): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        return ok to if (ok) "已返回" else "返回手势失败"
    }

    suspend fun reactHome(): Pair<Boolean, String> {
        val svc = a11y() ?: return false to "无障碍未启用"
        val ok = svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
        return ok to if (ok) "已回主屏" else "Home 手势失败"
    }

    suspend fun saveInboundImageFromBubble(bounds: List<Int>): Triple<Boolean, String, SavedInboundMedia?> =
        withContext(Dispatchers.IO) {
            if (bounds.size != 4) return@withContext Triple(false, "缺少图片气泡坐标", null)
            val svc = a11y() ?: return@withContext Triple(false, "无障碍未启用", null)
            val before = latestImageSnapshot()
            val cx = ((bounds[0] + bounds[2]) / 2).toFloat()
            val cy = ((bounds[1] + bounds[3]) / 2).toFloat()

            val savedFromBubble = longPressAndTapSave(svc, cx, cy, before)
            if (savedFromBubble != null) {
                return@withContext Triple(true, "已从图片气泡保存原图", savedFromBubble)
            }

            // Some WeCom builds only expose save from the full-screen viewer.
            if (!gestureTap(svc, cx, cy)) return@withContext Triple(false, "点击图片气泡失败", null)
            delay(700)
            val w = ctx.resources.displayMetrics.widthPixels / 2f
            val h = ctx.resources.displayMetrics.heightPixels / 2f
            val savedFromViewer = longPressAndTapSave(svc, w, h, before)
            if (savedFromViewer != null) {
                svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                return@withContext Triple(true, "已打开图片并保存原图", savedFromViewer)
            }
            svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            Triple(false, "未找到企微图片保存菜单或未检测到新保存图片", null)
        }

    private suspend fun longPressAndTapSave(
        svc: AccessibilityService,
        x: Float,
        y: Float,
        before: MediaSnapshot?,
    ): SavedInboundMedia? {
        if (!gestureLongPress(svc, x, y, 750L)) return null
        delay(500)
        val labels = listOf("保存图片", "保存到手机", "保存到相册", "保存")
        val root = svc.rootInActiveWindow
        val saveNode = labels.firstNotNullOfOrNull { label ->
            root?.findFirst { matchesText(it, label) }
        }
        if (saveNode == null) {
            svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
            return null
        }
        val target = saveNode.clickTarget() ?: saveNode
        if (!target.tap()) return null
        delay(1_000)
        val saved = waitForNewImage(before)
        return saved?.let { copyMediaToCache(it) }
    }

    private data class MediaSnapshot(val id: Long, val dateAdded: Long, val size: Long)
    private data class MediaCandidate(val uri: Uri, val displayName: String, val mime: String, val size: Long)

    private suspend fun latestImageSnapshot(): MediaSnapshot? = withContext(Dispatchers.IO) {
        queryLatestImage()?.let { MediaSnapshot(it.uri.lastPathSegment?.toLongOrNull() ?: 0L, 0L, it.size) }
    }

    private suspend fun waitForNewImage(before: MediaSnapshot?, maxWaitMs: Long = 5_000L): MediaCandidate? =
        withContext(Dispatchers.IO) {
            val deadline = System.currentTimeMillis() + maxWaitMs
            while (System.currentTimeMillis() < deadline) {
                val latest = queryLatestImage()
                if (latest != null && (before == null || latest.size != before.size || latest.uri.lastPathSegment?.toLongOrNull() != before.id)) {
                    return@withContext latest
                }
                delay(250)
            }
            null
        }

    private fun queryLatestImage(): MediaCandidate? {
        val collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        val projection = arrayOf(
            MediaStore.Images.Media._ID,
            MediaStore.Images.Media.DISPLAY_NAME,
            MediaStore.Images.Media.MIME_TYPE,
            MediaStore.Images.Media.SIZE,
        )
        val sort = "${MediaStore.Images.Media.DATE_ADDED} DESC"
        val cursor: Cursor = ctx.contentResolver.query(collection, projection, null, null, sort) ?: return null
        cursor.use {
            if (!it.moveToFirst()) return null
            val id = it.getLong(0)
            val name = it.getString(1) ?: "wecom-inbound.jpg"
            val mime = it.getString(2) ?: "image/jpeg"
            val size = it.getLong(3)
            val uri = Uri.withAppendedPath(collection, id.toString())
            return MediaCandidate(uri, name, mime, size)
        }
    }

    private fun copyMediaToCache(media: MediaCandidate): SavedInboundMedia {
        val safeName = safeMediaName(media.displayName).ifBlank { "wecom-inbound.jpg" }
        val target = File(ctx.cacheDir, "inbound_${UUID.randomUUID().toString().replace("-", "").take(8)}_$safeName")
        ctx.contentResolver.openInputStream(media.uri)?.use { input ->
            FileOutputStream(target).use { output -> input.copyTo(output) }
        } ?: error("无法读取保存后的图片")
        return SavedInboundMedia(
            file = target,
            mime = media.mime,
            filename = safeName,
            sizeBytes = target.length().takeIf { it > 0 } ?: media.size,
        )
    }

    /**
     * Download the media file and publish it to MediaStore under
     * `Pictures/WeComAgent/` so WeCom's gallery picker can see it. The
     * subsequent ReAct phase taps "+" → 图片 → 选最新一张 → 发送 to actually
     * deliver the message. We don't auto-delete — operators clean the
     * dedicated album when needed.
     *
     * Returns (ok, message, dataMap). On success dataMap carries:
     *   - uri: content:// URI of the inserted entry
     *   - display_name: filename as it appears in MediaStore
     *   - taken_at_ms: System.currentTimeMillis() at insert (used by the
     *     agent to identify "the latest picture" in the gallery)
     *   - relative_path: Pictures/WeComAgent/
     */
    suspend fun stageMedia(
        downloadUrl: String,
        mime: String,
        filename: String,
    ): Triple<Boolean, String, Map<String, String>?> = withContext(Dispatchers.IO) {
        if (!mime.startsWith("image/") && !mime.startsWith("video/")) {
            return@withContext Triple(false, "不支持的媒体类型：$mime", null)
        }
        try {
            val bytes = downloadMediaBytes(downloadUrl)
            val safeName = safeMediaName(filename).let { base ->
                // Disambiguate so MediaStore doesn't dedup against an earlier
                // stage with the same source filename.
                val dot = base.lastIndexOf('.')
                val stem = if (dot > 0) base.substring(0, dot) else base
                val ext = if (dot > 0) base.substring(dot) else ""
                val suffix = UUID.randomUUID().toString().replace("-", "").take(8)
                "${stem}_$suffix$ext"
            }
            val isVideo = mime.startsWith("video/")
            val takenAt = System.currentTimeMillis()
            val uri = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                insertMediaStoreQ(safeName, mime, bytes, isVideo)
            } else {
                insertMediaStoreLegacy(safeName, mime, bytes, isVideo)
            }
            // Confirm the file is queryable + matches the expected byte size
            // before we let the ReAct agent open the picker. Without this,
            // the agent can tap "first thumbnail" while the staged image
            // hasn't shown up in WeCom's gallery yet, and end up sending
            // some older photo. The wait is capped — if MediaStore never
            // resolves the URI we fail the command so the backend retries
            // rather than silently sending the wrong file.
            val verified = verifyMediaVisible(uri, expectedBytes = bytes.size.toLong())
            if (!verified) {
                return@withContext Triple(
                    false,
                    "媒体已写入但 MediaStore 未在限定时间内确认可见，拒绝继续以避免发错文件",
                    null,
                )
            }
            val data = mapOf(
                "uri" to uri.toString(),
                "display_name" to safeName,
                "taken_at_ms" to takenAt.toString(),
                "relative_path" to "Pictures/WeComAgent/",
                "mime" to mime,
                "size_bytes" to bytes.size.toString(),
            )
            Triple(true, "媒体已落入 Pictures/WeComAgent/ ($safeName)", data)
        } catch (e: Exception) {
            Log.w(tag, "stage media failed", e)
            Triple(false, e.message ?: e::class.java.simpleName, null)
        }
    }

    /**
     * Wait until a freshly-inserted MediaStore URI is queryable AND its
     * reported size matches what we wrote. Picker apps (WeCom included)
     * read from MediaStore queries, so this is the moment when the file
     * actually becomes "selectable from the gallery". On API 29+ this is
     * usually instant once IS_PENDING=0, but the ContentResolver
     * notification can lag a few hundred ms on some ROMs; on legacy we're
     * waiting for the MediaScanner pass.
     *
     * @return true if visible within `maxWaitMs`, false otherwise (caller
     *   should treat that as a failure rather than gamble on the gallery
     *   selecting some unrelated older photo).
     */
    private suspend fun verifyMediaVisible(
        uri: Uri,
        expectedBytes: Long,
        maxWaitMs: Long = 4_000L,
        pollIntervalMs: Long = 150L,
    ): Boolean = withContext(Dispatchers.IO) {
        val deadline = System.currentTimeMillis() + maxWaitMs
        val resolver = ctx.contentResolver
        val projection = arrayOf(MediaStore.MediaColumns.SIZE, MediaStore.MediaColumns.IS_PENDING)
        while (System.currentTimeMillis() < deadline) {
            try {
                resolver.query(uri, projection, null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst()) {
                        val size = cursor.getLong(0)
                        val pendingIdx = cursor.getColumnIndex(MediaStore.MediaColumns.IS_PENDING)
                        val pending = if (pendingIdx >= 0) cursor.getInt(pendingIdx) else 0
                        if (pending == 0 && size == expectedBytes) {
                            return@withContext true
                        }
                    }
                }
            } catch (_: Exception) {
                // Some legacy ROMs throw on IS_PENDING projection; fall
                // back to a size-only check on the next loop.
            }
            try {
                resolver.query(uri, arrayOf(MediaStore.MediaColumns.SIZE), null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst() && cursor.getLong(0) == expectedBytes) {
                        return@withContext true
                    }
                }
            } catch (_: Exception) {
            }
            delay(pollIntervalMs)
        }
        false
    }

    private suspend fun downloadMediaBytes(downloadUrl: String): ByteArray = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(downloadUrl).build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) error("下载媒体失败：HTTP ${response.code}")
            val body = response.body ?: error("媒体响应为空")
            body.bytes()
        }
    }

    private fun insertMediaStoreQ(
        displayName: String,
        mime: String,
        bytes: ByteArray,
        isVideo: Boolean,
    ): Uri {
        // Use scoped storage: insert into Pictures/WeComAgent with IS_PENDING
        // so partial writes aren't picked up by other apps mid-stream.
        val collection = if (isVideo) {
            MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        } else {
            MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        }
        val relativePath = (if (isVideo) Environment.DIRECTORY_MOVIES else Environment.DIRECTORY_PICTURES) + "/WeComAgent"
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, displayName)
            put(MediaStore.MediaColumns.MIME_TYPE, mime)
            put(MediaStore.MediaColumns.RELATIVE_PATH, relativePath)
            put(MediaStore.MediaColumns.DATE_ADDED, System.currentTimeMillis() / 1000)
            put(MediaStore.MediaColumns.DATE_MODIFIED, System.currentTimeMillis() / 1000)
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val resolver = ctx.contentResolver
        val uri = resolver.insert(collection, values) ?: error("MediaStore.insert 返回 null")
        try {
            resolver.openOutputStream(uri)?.use { out -> out.write(bytes) }
                ?: error("无法打开 MediaStore 输出流")
            val finalize = ContentValues().apply {
                put(MediaStore.MediaColumns.IS_PENDING, 0)
            }
            resolver.update(uri, finalize, null, null)
        } catch (e: Exception) {
            resolver.delete(uri, null, null)
            throw e
        }
        return uri
    }

    private suspend fun insertMediaStoreLegacy(
        displayName: String,
        mime: String,
        bytes: ByteArray,
        isVideo: Boolean,
    ): Uri = withContext(Dispatchers.IO) {
        // API ≤28: write to the public Pictures/Movies dir + MediaScanner so
        // the system gallery picker indexes it. Needs WRITE_EXTERNAL_STORAGE
        // declared with maxSdkVersion=28 in the manifest.
        @Suppress("DEPRECATION")
        val publicDir = Environment.getExternalStoragePublicDirectory(
            if (isVideo) Environment.DIRECTORY_MOVIES else Environment.DIRECTORY_PICTURES
        )
        val targetDir = File(publicDir, "WeComAgent").apply {
            if (!exists() && !mkdirs()) error("无法创建目录 $absolutePath")
        }
        val target = File(targetDir, displayName)
        target.outputStream().use { out -> out.write(bytes) }
        // MediaScannerConnection.scanFile is callback-based; wrap into a
        // suspend point so we return only after the URI is known.
        val scannedUri = withTimeoutOrNull(5_000L) {
            kotlinx.coroutines.suspendCancellableCoroutine<Uri?> { cont ->
                MediaScannerConnection.scanFile(
                    ctx,
                    arrayOf(target.absolutePath),
                    arrayOf(mime),
                ) { _, uri ->
                    if (cont.isActive) cont.resumeWith(Result.success(uri))
                }
            }
        }
        scannedUri ?: Uri.fromFile(target)
    }

    private fun safeMediaName(filename: String): String {
        val name = filename.replace('\\', '/').substringAfterLast('/').ifBlank { "media" }
        return name.replace(Regex("[^A-Za-z0-9._-]+"), "_").take(120).ifBlank { "media" }
    }

    private fun gestureTap(svc: AccessibilityService, x: Float, y: Float): Boolean {
        val path = android.graphics.Path().apply { moveTo(x, y) }
        val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, 80)
        val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
        return svc.dispatchGesture(gesture, null, null)
    }

    private suspend fun gestureDoubleTap(svc: AccessibilityService, x: Float, y: Float): Boolean {
        val first = gestureTap(svc, x, y)
        if (!first) return false
        delay(120)
        return gestureTap(svc, x, y)
    }

    private fun gestureLongPress(
        svc: AccessibilityService,
        x: Float,
        y: Float,
        durationMs: Long,
    ): Boolean {
        val path = android.graphics.Path().apply { moveTo(x, y) }
        val dur = durationMs.coerceIn(350L, 3_000L)
        val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, dur)
        val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
        return svc.dispatchGesture(gesture, null, null)
    }

    private fun gestureSwipe(
        svc: AccessibilityService,
        x1: Float, y1: Float, x2: Float, y2: Float,
        durationMs: Long,
    ): Boolean {
        val path = android.graphics.Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(path, 0, durationMs)
        val gesture = android.accessibilityservice.GestureDescription.Builder().addStroke(stroke).build()
        return svc.dispatchGesture(gesture, null, null)
    }

    // -------------------------------------------------------- dump helper
    fun dumpTree(svc: AccessibilityService, reason: String) {
        val root = svc.rootInActiveWindow ?: run {
            log("dump[$reason]: rootInActiveWindow is null")
            return
        }
        val sb = StringBuilder()
        sb.append("=== UI dump (").append(reason).append(") pkg=").append(root.packageName).append(" ===\n")
        printNode(root, 0, sb)
        Log.i(tag, sb.toString())
        log("已写入 UI 树到 logcat（tag=$tag, reason=$reason）。adb logcat -s $tag 查看。")
    }

    private fun printNode(n: AccessibilityNodeInfo?, depth: Int, sb: StringBuilder) {
        n ?: return
        sb.append("  ".repeat(depth))
        val cls = n.className?.toString()?.substringAfterLast('.') ?: "?"
        val txt = n.text?.toString() ?: ""
        val desc = n.contentDescription?.toString() ?: ""
        val id = n.viewIdResourceName?.substringAfterLast('/') ?: ""
        val flags = buildString {
            if (n.isClickable) append("C")
            if (n.isFocusable) append("F")
            if (n.isEditable) append("E")
            if (n.isScrollable) append("S")
            if (n.isCheckable) append("K")
        }
        sb.append("[$cls]")
        if (id.isNotEmpty()) sb.append(" id=$id")
        if (txt.isNotEmpty()) sb.append(" txt=\"$txt\"")
        if (desc.isNotEmpty()) sb.append(" desc=\"$desc\"")
        if (flags.isNotEmpty()) sb.append(" $flags")
        sb.append('\n')
        for (i in 0 until n.childCount) printNode(n.getChild(i), depth + 1, sb)
    }
}

// ---- AccessibilityNodeInfo helpers ----
private fun AccessibilityNodeInfo.findFirst(pred: (AccessibilityNodeInfo) -> Boolean): AccessibilityNodeInfo? {
    if (pred(this)) return this
    for (i in 0 until childCount) {
        val c = getChild(i) ?: continue
        c.findFirst(pred)?.let { return it }
    }
    return null
}

private fun AccessibilityNodeInfo.findByDumpId(targetId: Int): AccessibilityNodeInfo? {
    var seen = 0
    fun walk(n: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        n ?: return null
        seen += 1
        if (seen == targetId) return n
        for (i in 0 until n.childCount) {
            walk(n.getChild(i))?.let { return it }
        }
        return null
    }
    return walk(this)
}

private fun AccessibilityNodeInfo.expectationMismatch(expected: NodeExpectation): String? {
    val cls = className?.toString()?.substringAfterLast('.') ?: ""
    if (expected.cls.isNotBlank() && cls != expected.cls) return "cls $cls != ${expected.cls}"
    val viewId = viewIdResourceName?.substringAfterLast('/').orEmpty()
    if (expected.viewId.isNotBlank() && viewId != expected.viewId) return "view_id $viewId != ${expected.viewId}"
    val textValue = text?.toString().orEmpty()
    if (expected.text.isNotBlank() && textValue != expected.text) return "text $textValue != ${expected.text}"
    val descValue = contentDescription?.toString().orEmpty()
    if (expected.desc.isNotBlank() && descValue != expected.desc) return "desc $descValue != ${expected.desc}"
    expected.editable?.let { if (isEditable != it) return "editable $isEditable != $it" }
    expected.clickable?.let { if (isClickable != it) return "clickable $isClickable != $it" }
    val expectedBounds = expected.bounds
    if (expectedBounds != null && expectedBounds.size == 4) {
        val actual = Rect()
        getBoundsInScreen(actual)
        val maxDelta = listOf(
            kotlin.math.abs(actual.left - expectedBounds[0]),
            kotlin.math.abs(actual.top - expectedBounds[1]),
            kotlin.math.abs(actual.right - expectedBounds[2]),
            kotlin.math.abs(actual.bottom - expectedBounds[3]),
        ).maxOrNull() ?: 0
        if (maxDelta > 32) {
            return "bounds [${actual.left},${actual.top},${actual.right},${actual.bottom}] != $expectedBounds"
        }
    }
    return null
}

private fun matchesText(n: AccessibilityNodeInfo, s: String): Boolean {
    val a = n.text?.toString().orEmpty()
    val b = n.contentDescription?.toString().orEmpty()
    return a.contains(s) || b.contains(s)
}

private fun AccessibilityNodeInfo.clickTarget(): AccessibilityNodeInfo? {
    var n: AccessibilityNodeInfo? = this
    while (n != null && !n.isClickable) n = n.parent
    return n
}

private fun AccessibilityNodeInfo.longClickTarget(): AccessibilityNodeInfo? {
    var n: AccessibilityNodeInfo? = this
    while (n != null && !n.isLongClickable) n = n.parent
    return n
}

private fun AccessibilityNodeInfo.label(): String {
    return text?.toString()?.takeIf { it.isNotBlank() }
        ?: contentDescription?.toString()?.takeIf { it.isNotBlank() }
        ?: ""
}

private fun AccessibilityNodeInfo.tap(): Boolean {
    return performAction(AccessibilityNodeInfo.ACTION_CLICK)
}

private fun AccessibilityNodeInfo.longPress(): Boolean {
    return performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK)
}

/** Named distinctly to avoid colliding with the deprecated AccessibilityNodeInfo.setText
 *  (which returns Unit and would otherwise win overload resolution). */
private fun AccessibilityNodeInfo.replaceText(text: String): Boolean {
    val bundle = Bundle().apply {
        putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
    }
    return performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)
}
