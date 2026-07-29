# apps/web

Quality-Max 静态入口：沿用 GameGhost 简历页的 glass 卡片 / 文字样式，无 Online/Offline 单选。

本地 vendor（相对路径 `./vendor/...`，可直接用浏览器打开 `static/index.html`）：

- `static/vendor/tailwindcss/tailwind.css`（Tailwind CLI 预构建，生产可用）
- `static/vendor/fonts/inter/`（Inter ttf + css）
- `static/vendor/fonts/material-symbols/`（Material Symbols ttf + css）

服务端访问：`/` 会重定向到 `/static/index.html`，相对资源路径即可解析。

## Agent 界面

### 对话历史

- 对话历史以当前 MolMind 实例保存的会话为准，不依赖浏览器本地缓存。
- 只有打开历史抽屉时，前端才请求 `/api/agent/sessions?limit=50`，并直接渲染接口返回的会话。
- 历史项标题下方显示最新一条用户问题，长文本使用单行省略号。
- 历史抽屉顶部提供清空按钮。确认后会删除当前 MolMind 实例保存的全部会话。
- 单条会话仍支持重命名和删除；下次打开或刷新抽屉时会从服务端重新读取列表。

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
