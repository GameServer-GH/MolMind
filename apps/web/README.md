# apps/web

Quality-Max 静态入口：沿用 GameGhost 简历页的 glass 卡片 / 文字样式，无 Online/Offline 单选。

本地 vendor（相对路径 `./vendor/...`，可直接用浏览器打开 `static/index.html`）：

- `static/vendor/tailwindcss/tailwindcdn.js`
- `static/vendor/fonts/inter/`（Inter ttf + css）
- `static/vendor/fonts/material-symbols/`（Material Symbols ttf + css）

服务端访问：`/` 会重定向到 `/static/index.html`，相对资源路径即可解析。
