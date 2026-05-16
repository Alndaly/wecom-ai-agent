# 14 · 真机上手与企微 UI 校准

> 本章告诉你：把一台 Android 真机变成 WeCom Agent 节点，让它**真的**监听企微消息、
> **真的**驱动企微输入框 + 发送键。

## 14.1 关键现实

企微 (`com.tencent.wework`) 不开放官方客户消息 API，我们走 **AccessibilityService + NotificationListener** 双通道：

| 通道 | 角色 | 触发时机 | 局限 |
| --- | --- | --- | --- |
| **NotificationListener** | 主入口 | 每条新消息弹通知都触发 | 群里被 @ / 长文本可能截断 |
| **AccessibilityService** | 校准 + 主动操控 | 用户/RPA 在企微界面时 | 后台时拿不到节点树 |

**坦白讲**：企微每次发版都可能改控件结构。这里给出的代码用 *特征 + 位置* 兜底（不靠
View ID），但仍可能在某个版本失效。**「采集 UI 树」按钮**就是为此准备的。

## 14.2 上手 6 步

### 第 1 步 · 编译 APK

```bash
cd android
./gradlew assembleDebug              # 输出: app/build/outputs/apk/debug/app-debug.apk
adb install app/build/outputs/apk/debug/app-debug.apk
```

### 第 2 步 · 后端可达

打开 APP → 填三项：

- **ws://<你电脑的局域网 IP>:8000** （真机连不到 `localhost`，模拟器才能用 `10.0.2.2`）
- `robot_id`：在 Web `/devices` 创建一个,把字符串复制过来
- `token`：创建时**只显示一次**的密钥

### 第 3 步 · 授权

APP 最上方有「权限状态」一行：

- 无障碍 ❌ → 点「打开无障碍」→ 在系统设置里找到 `WeCom Agent`,开启
- 通知监听 ❌ → 点「打开通知监听」→ 勾上 `WeCom Agent`
- 电池白名单 ❌ → 点「电池白名单」→ 同意

回到 APP 三栏都 ✅ 后继续。

### 第 4 步 · 干跑模式启动

勾上「Dry-run」→「启动 Agent」。这时：
- WebSocket 连上后端,设备显示在线
- 后端 ReAct 发来的 `device.command` 会走 dry-run 分支,**不真碰企微 UI**
- NotificationListener **仍然会真的**抓企微通知 → 上报后端

去 Web 工作台试试：
1. 用你绑定企微的另一个手机/账号给企微发条消息
2. 真机收到通知 → Web 工作台几乎实时显示这条消息（`message.received` 走通）

走到这一步，**入向链路就活了**。

### 第 5 步 · 采集 UI 树（校准前置）

把企微切到聊天页 → 回到 Agent APP → 点「采集当前 UI 树」。

```
adb logcat -s WeComA11y AgentSvc | head -200
```

或者直接看后端文件：

```bash
ls -lh backend/var/ui_dumps/
cat backend/var/ui_dumps/robot_xxxx-YYYY...-CHAT-manual.txt
```

会看到类似：

```
=== UI dump pkg=com.tencent.wework page=CHAT ===
[FrameLayout]
  [LinearLayout]
    [TextView] txt="张三"
    [EditText] id=edit_text E F   <-- 这是输入框
    [Button]   txt="发送" C F     <-- 这是发送键
    ...
```

如果后端 ReAct 无法定位输入框或发送按钮，优先看 dump 中的真实节点文本、可编辑属性、
clickable 属性和 bounds。不要把某个机型的坐标、固定节点 id 或固定屏幕尺寸写进代码；
应该补充可泛化的观察字段或后端 locator 规则。

### 第 6 步 · 关掉 dry-run，真发一条

填好「测试联系人昵称」+「测试文本」→「本地发送测试」。

预期：
- 后端 ReAct 通过 `device.command` 驱动 Agent 打开企微 → 搜索联系人 → 进入聊天 → 输入文本 → 点发送
- APP 日志显示 `本地测试发送成功`
- 真实企微聊天里收到这条文本

成功后取消「Dry-run」→ 重启 Agent。此时 **AI 自动回复 / 人工客服回复**
都会真的下发到这台机器,**真的在企微里发送**。

## 14.3 校准失败排查

| 现象 | 看 | 通常原因 |
| --- | --- | --- |
| 任务一直 `failed: accessibility service not running` | 无障碍权限 | 系统重启 / 应用更新后被关 |
| `WeCom not in foreground after open` | 启动 intent | 企微启动慢,把 `waitForPackage` 超时调大 |
| `no search result for '<name>'` | 检查搜索结果页 UI dump | 昵称不匹配,或搜索页改版 |
| `could not type into chat input` | dump 看 EditText 节点 | EditText 不可获焦,常见于"折叠面板未展开" |
| `could not find send button` | dump 看右下角 | 企微版本切了 IconButton 变成 ImageView,把策略 2 兜底 |

每次失败 Automator 会自动 dump 节点树到 logcat 并上传后端 `var/ui_dumps/` —— 不用手点。

## 14.4 已知局限（路线图）

- [ ] **群组消息** 当前归 `sender#groupName` 一个 contact,后续在协议层加 `group_id` 字段
- [ ] **图片 / 文件** 当前主链路只实现 `send_text`，`send_image` / `send_file` 仍待接入后端队列和 ReAct 原语
- [ ] **多账号同机** 企微多账号切换没做,目前 1 robot = 1 账号
- [ ] **OCR 兜底** 没有真实抠图,极端复杂版式的消息可能漏抓——会在 NotificationListener 命中率掉到 < 90% 时上 ML Kit
- [ ] **企微大版本兼容** 没有版本检测自动切策略,只能靠你 dump 后改 Automator

## 14.5 安全提示

- **不要**把生产 `robot_token` 写进 git。APP 把它存在 `SharedPreferences("agent")`,
  系统 backup 已经在 manifest 里关了 (`android:allowBackup="false"`)
- AccessibilityService 是高权限——任何 APP 都能在你解锁屏幕时读取所有 UI。
  **真机仅限作业用，不要装其他应用**

## 14.6 对照表：协议 → Android 代码

| 后端事件 | Android 侧处理 |
| --- | --- |
| `device.command` | `AgentForegroundService.handleReactCommand` → `WeComAutomator` 通用原语 |
| `device.command_result` ← | Android 执行原语后的结果回传 |
| `device.ui_dump` ← | `dumpAndUpload` (按 「采集 UI 树」 触发) |
| `message.received` ← | `MessageNotificationListener.onMessage` / `WeComAccessibilityService.onChatMessage` |
| `device.hello` / `device.heartbeat` ← | `AgentForegroundService.heartbeatJob` |
