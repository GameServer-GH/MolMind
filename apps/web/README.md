# apps/web

Quality-Max 静态入口：沿用 GameGhost 简历页的 glass 卡片 / 文字样式，无 Online/Offline 单选。

本地 vendor（经服务端 `/static/...` 提供）：

- `static/vendor/tailwindcss/tailwind.css`（Tailwind CLI 预构建，生产可用）
- `static/vendor/fonts/inter/`（Inter ttf + css）
- `static/vendor/fonts/material-symbols/`（Material Symbols ttf + css）

服务端访问：`/` 直接返回页面（不再 302 到 `/static/index.html`）；CSS/JS/字体等仍挂在 `/static/...`。

当前产品版本以根目录 `pyproject.toml` 为准（现为 **0.2.3**），经 `GET /health` 展示，不在前端硬编码。

## Agent 界面

### 对话历史

- 对话历史以当前 MolMind 实例保存的会话为准，不依赖浏览器本地缓存作真源。
- 只有打开历史抽屉时，前端才请求 `/api/agent/sessions?limit=50`，并直接渲染接口返回的会话。
- 历史项标题下方显示最新一条用户问题，长文本使用单行省略号；列表可展示活动 `run_status`。
- 历史抽屉顶部提供清空按钮。确认后会删除当前 MolMind 实例保存的全部会话。
- 单条会话仍支持重命名和删除；运行中的会话不可删除。下次打开或刷新抽屉时会从服务端重新读取列表。

### 运行态、排队与忙碌门禁

- Session 服务端持久化 `active_run` / `revision`。刷新或切回会话后，可重放底部步骤条与右侧工具清单，并经 `events?after_seq=` 或 SSE 增量跟随。
- 活动态（`queued/running/cancel_requested`）下，前端禁用发送、改附件、改执行配置、删除与清空；服务端对冲突写操作返回 `409 session_busy`。忙碌时新提示会进入持久化 Turn 队列（上限 3），右侧展示排队卡片与附件 chip。
- 发送区提供硬停止按钮：对当前 Run 调用 `interrupt`，请求 `cancel_requested`，不自动入队 guidance。
- SDF 上传进行中，上传按钮进入 loading / 禁用态，避免重复提交。
- 机制 PDF / SCP 等后台 Job 支持取消与崩溃回收后的状态回放。

### Turn 附件

- 可在发送前暂存 Turn 级附件（与会话 staged 列表同步）；上传走 `turn-attachments`，不改写进行中的 Run。
- 排队卡片展示附件文件名 / 类型摘要；切换会话或刷新后从服务端 `staged_attachments` 恢复。

### Profile 详情

点击顶部 `MolMind · MASLD` Profile 标识可打开详情弹窗，展示：

- 应用名称及研究方向。
- 从后端 `/health` 自动获取的项目版本和后端构建标识（打开弹窗时刷新）。
- GitHub 仓库：<https://github.com/GameServer-GH/MolMind>
- 线上服务：<https://molmind.cn/>
- 当前部署环境的 API 文档：`/docs`

弹窗支持点击关闭按钮、背景区域或按 `Esc` 关闭。外部链接和 API 文档在新标签页打开。

## 重建 Tailwind CSS

改了 `static/index.html` / `static/app.js` 里的 utility class，或改了 `tailwind.config.js` 主题后：

```bash
cd apps/web
npm install
npm run build:css
```

会把压缩后的 CSS 写到 `static/vendor/tailwindcss/tailwind.css`（可入库；运行时不需要 Node）。
