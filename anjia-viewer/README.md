# 安价统计查看器

这个 Vue 前端从网站静态路径 `/data/anchors_43877379.json` 读取 `AnchorCounter.py` 输出的 JSON，展示各主题安价、重复复核、忽略楼层、规则解析和运行告警。

生成默认数据：

```powershell
cd ..
pixi run python AnchorCounter.py --output anjia-viewer/public/data/anchors_43877379.json
```

启动前端：

```powershell
npm install
npm run dev
```

构建静态文件：

```powershell
npm run build
```

部署 `dist/` 时，`public/data/anchors_43877379.json` 会被 Vite 复制到 `dist/data/anchors_43877379.json`。