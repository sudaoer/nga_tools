# 为保存nga安科到html这种东西写的

## 主要是存到markdown那个，不是每个人都有markdown阅读器的，而且没存bbcode

## 使用
将config_template.py复制为config.py然后填入自己的cookie

安价统计运行resuanka.py，因为是垃圾代码所以自己改去吧

## tid=43877379 安价 JSON 统计

运行 `AnchorCounter.py` 会抓取 tid=43877379 的全帖观众回复并缓存到 `output/43877379_all/json/`，按 21552 楼规则统计安价，默认输出给前端读取的 JSON：

```powershell
pixi run python AnchorCounter.py --output anjia-viewer/public/data/anchors_43877379.json
```

统计器会把同一观众的所有未忽略楼一次性发给模型判断；同一普通主题多次提交时取最新楼，不同主题允许散放在多个回复中。送模型前会剥离 NGA 回复自带的 `Reply Post by ...` 引用块，避免把被回复者的安价算到回复者名下。DeepSeek 接口默认启用 thinking mode 和 JSON Output，可用 `--reasoning-effort high|max` 调整思考强度。

默认会以 `--llm-workers 32` 并发请求模型；如果触发限流或想方便排查，可以改成 `--llm-workers 1` 串行运行。

只检查抓取和确定性过滤时可以跳过模型：

```powershell
pixi run python AnchorCounter.py --no-llm --output output/43877379_all/anchor_counter/test.json
```

Vue 查看器在 `anjia-viewer/`，页面会从网站路径 `/data/anchors_43877379.json` 获取统计结果。部署前先把统计 JSON 输出到 `anjia-viewer/public/data/anchors_43877379.json`，再构建前端。

一键统计并部署到 SSH 配置里的 `al:/www/wwwroot/260502.anchor.gmgo.sudoer.cn`：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy_anjia.ps1
```

脚本会运行统计、构建 viewer、用 `scp` 上传 `anjia-viewer/dist` 到远端登录用户的临时目录，再通过 `sudo cp` 发布到 webroot，并执行 `sudo chown -R www:www` 与 `sudo chmod -R 755`。`www` 只是文件 owner，不作为 SSH 登录用户。如果只想复用现有统计 JSON 或构建产物，可加 `-SkipCounter` 或 `-SkipBuild`。

## TODO（永远不会做（不是）的功能）
- 添加config里写配置的功能，省的每次复制粘贴tid aid
- 添加自动更新功能
- 添加图片去重和多页一pdf，这样可能可以节省空间吧
