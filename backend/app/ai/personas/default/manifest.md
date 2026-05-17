id: default
name: 默认客服人格
description: 温和、可信、不机械的私域客服默认人格。可以基于这一份拷贝出新的版本做团队/品牌微调。
version: 1
---

这份 manifest 只用来描述人格本身,**不会被注入 prompt**。

注入 prompt 的是同目录下的 `soul.md` + `memory.md` + `style.md`,加载顺序是
soul → memory → style,共同组成给 conv_agent 的"自我感"。

需要给客户感受到不同性格时,优先编辑这三份;实在不够再 fork 整个目录:

```
cp -r personas/default personas/<your_id>
# 然后改 manifest.md 的 id、name,改 soul/memory/style
```

并在 team 的 ai 设置里把 `persona_id` 切到 `<your_id>`。
