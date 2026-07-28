# apps/web

Quality-Max 静态入口：沿用 GameGhost 简历页的 glass 卡片 / 文字样式，无 Online/Offline 单选。

本地 vendor（相对路径 `./vendor/...`，可直接用浏览器打开 `static/index.html`）：

- `static/vendor/tailwindcss/tailwind.css`（Tailwind CLI 预构建，生产可用）
- `static/vendor/fonts/inter/`（Inter ttf + css）
- `static/vendor/fonts/material-symbols/`（Material Symbols ttf + css）

服务端访问：`/` 会重定向到 `/static/index.html`，相对资源路径即可解析。

## Agent 界面

### 对话历史

- 对话历史按浏览器本地隔离，不同电脑或浏览器不会共享同一份历史列表。
- 当前浏览器使用 `localStorage` 保存会话 ID 和会话摘要缓存；会话正文及产物仍由后端会话接口提供。
- 页面进入时会在后台请求 `/api/agent/sessions` 并增量更新本地缓存。
- 打开历史抽屉时优先直接渲染本地缓存，不等待网络请求；后台同步完成后再刷新列表。
- 历史项标题下方显示最新一条用户问题，长文本使用单行省略号。
- 历史抽屉顶部提供清空按钮。确认清空后仅移除当前浏览器的历史索引和摘要缓存，不删除云端会话。
- 单条会话仍支持重命名和删除；删除会同时移除对应的本地缓存。

当前使用的本地缓存键：

- `molmind_agent_history_local_v1`：当前浏览器拥有的会话 ID。
- `molmind_agent_history_cache_v1`：历史列表摘要缓存。

### Profile 详情

点击顶部 `MolMind · MASLD` Profile 标识可打开详情弹窗，展示：

- 应用名称及研究方向。
- 从后端 `/health` 自动获取的项目版本和后端构建标识。
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
