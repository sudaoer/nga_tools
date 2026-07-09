# NGA 帖子备份器

把 NGA 帖子通过接口抓取到本地，保存楼层历史、本地图片路径 HTML、图片文件，并可按楼层分段生成 PDF。

## 配置

复制配置模板：

```powershell
Copy-Item config.example.json config.json
Copy-Item secrets.example.json secrets.json
```

在 `secrets.json` 中填入自己的 NGA Cookie：

```json
{
  "nga_passport_uid": "",
  "nga_passport_cid": ""
}
```

当前命令会读取帖子配置文件，请确保 `config.json` 中有配置文件路径：

```json
{
  "thread_config_file": "thread_configs.json"
}
```

如需调整输出目录，修改：

```json
{
  "output_dir": "output"
}
```

批量备份默认并行 4 个帖子；NGA API 请求和图片下载分别限流。需要调整时可在
`config.json` 中设置：

```json
{
  "backup_configs_workers": 4,
  "api_concurrency": 4,
  "image_concurrency": 50
}
```

## 使用

扫描版面主题（例如 NGA 二次元跑团综合 `fid=784`）：

```powershell
pixi run python main.py forum list --fid 784 --pages 1
```

根据规则扫描版面，并把匹配主题保存到 `thread_configs.json`：

```powershell
Copy-Item forum_watch_configs.example.json forum_watch_configs.json
pixi run python main.py forum sync
```

规则文件默认读取 `forum_watch_configs.json`。匹配规则为：`include_tids`
中的 tid 会强制保存；否则标题包含任一 `keywords`、不包含
`exclude_keywords`，主题回复数达到 `min_replies`，且 NGA 只看作者页的
`vrows` 达到 `min_author_lous` 时保存；`vrows` 按 NGA 只看作者楼数计数，
包含主楼。`min_replies` 未配置时默认为 500，`min_author_lous` 未配置时
默认为 20。保存时会把主题楼主 `authorid` 写为 `aid`，后续使用
`backup all/sub --name 帖子名` 会默认只看楼主。

`thread_configs.json` 中每项必须包含 `thread_name` 和 `tid`；可额外保存任意
标量字段（字符串、数字、布尔值或 `null`），但不能保存数组或对象。
`description` 只是可选自定义字段，不再强制存在。`forum sync` 会为匹配主题
写入或刷新 `aid`、`link`、标题、作者、版面、回复数和发帖/回复时间等主题
列表元数据，其中 `link` 形如 `https://bbs.nga.cn/read.php?tid=12345678`，
便于在编辑器中直接跳转到原帖；已存在的配置会保留 `thread_name` 和其他
自定义标量字段。

每次 `forum sync` 会先按监控配置里的 `pages` 从远端默认排序抓取对应页数，
并写入 `output/forum_threads.sqlite3`；每个 fid 使用一张表，按
`tid` 去重并更新标题、最后回复时间、回复数等主题列表字段。之后的筛查从
SQLite 中读取该 fid 已保存的主题数据，`pages` 不限制数据库筛查范围。

按主题发布时间倒序扫描版面主题，并写入主题数据库：

```powershell
pixi run python main.py forum sync --full-postdate --fid 784
pixi run python main.py forum sync --full-postdate --fid 784 --page-delay-seconds 5
pixi run python main.py forum sync --full-postdate --refresh --fid 784
```

`--full-postdate` 会调用版面主题列表接口并传入
`order_by=postdatedesc`，可翻阅超过 100 页；默认从远端第一页开始，遇到
数据库中已有 `tid` 后停止后续扫描。加 `--refresh` 时会忽略已有 `tid`，
刷新到远端末页；需要续扫时可与 `--refresh --start-page N` 一起使用。
此模式不修改 `thread_configs.json`，也不会额外请求单贴 API；默认每页请求后
等待 3 秒，可用 `--page-delay-seconds` 调大以减少限流。

抓取整帖并下载图片：

```powershell
pixi run python main.py backup all --name 帖子名
```

图片会按内容 hash 保存到全局 `output/images_unique/`，原始 NGA 图片链接到本地文件的映射保存在 `output/image_index.sqlite3`。下载失败的图片会在 `html_modified` 中指向 `images_unique/download_failed_placeholder.png` 占位图，不写入 SQLite 映射，后续增量备份仍会继续重试原图。
帖子内容会写入每个备份目录自己的 `archive.sqlite3`。默认不输出分页 JSON；需要最近响应缓存和回滚参考时，加 `--write-json` 会额外写入 `json/page_*.json`。生成 HTML、楼层映射和统计只读取 `archive.sqlite3`。

补充本地缺失内容和远端新增内容：

```powershell
pixi run python main.py backup sub --name 帖子名
```

`backup sub` 会补抓缺失的页，并刷新本地尾页到远端最后一页；随后补齐 `html_modified` 和缺失图片。只看作者备份还会重试恢复之前仍为占位的缺失楼内容。
增量刷新到的新页会合并进历史楼层库；同一页新响应中消失的旧楼不会从库中删除。尚未迁移的旧备份目录如果含有 `json/page_*.json` 但 `archive.sqlite3` 不完整，正常备份会报错提示先运行 `backup migrate-store`，不会自动读取旧 JSON。

也可以不使用保存的名称，直接指定 `tid` 和可选的 `aid`：

```powershell
pixi run python main.py backup all --tid 12345678
pixi run python main.py backup all --tid 12345678 --aid 987654
pixi run python main.py backup sub --tid 12345678 --aid 987654
pixi run python main.py backup sub --tid 12345678 --aid 987654 --write-json
```

批量增量备份 `thread_configs.json` 中的所有帖子：

```powershell
pixi run python main.py backup sub --all-threads
pixi run python main.py backup sub --all-threads --workers 4 --api-concurrency 4 --image-concurrency 50
pixi run python main.py backup sub --all-threads --write-json
```

`backup configs` 暂时保留为兼容入口，等价于 `backup sub --all-threads`。
需要批量完整刷新时，可运行 `backup all --all-threads`。

已有只看作者备份缺少楼层映射时，可以单独生成映射：

```powershell
pixi run python main.py backup floors --name 帖子名
pixi run python main.py backup floors --all-threads
```

楼层映射会增量刷新。缺失楼无法唯一确定原帖楼层时，`floor_map.json` 会记录 `candidate_original_lous` 候选列表。

旧备份目录可以显式迁移到每帖 SQLite 存储。迁移不会删除或改写旧 `json/page_*.json`：

```powershell
pixi run python main.py backup migrate-store --name 帖子名
pixi run python main.py backup migrate-store --all
```

并行校验所有本地备份中的已下载图片，损坏或无法打开的图片会被删除：

```powershell
pixi run python main.py image verify
```

也可以只校验指定帖子：

```powershell
pixi run python main.py image verify --name 帖子名
```

统计已有备份中的正文中文字数：

```powershell
pixi run python main.py stats words --name 帖子名
```

统计命令读取本地 `archive.sqlite3`，不联网；会把每个帖子版本的字数字段写回
归档库，但不生成独立统计文件。旧 `json/page_*.json` 只会被 `backup migrate-store`
显式读取。默认会清洗图片、链接、HTML/BBCode、表情、用户引用、回复引用和骰子
标记，并只纳入清洗后 `中文+中文标点数` 不低于 120 的正文楼层。阈值可以通过
`--min-body-chars` 调整：

```powershell
pixi run python main.py stats words --name 帖子名 --min-body-chars 80
pixi run python main.py stats words --all-threads
```

## Web 查看器

安装并构建前端，然后启动本地查看器：

```powershell
pixi run web-serve
```

默认地址为 `http://127.0.0.1:18736/`。普通帖子阅读页只读取本地备份；
管理页会写入正文版本选择并刷新对应 `html_modified/post_*.html`。默认只监听
本机地址；允许局域网访问时，管理功能也会暴露给局域网内访问者：

```powershell
pixi run python main.py web serve --port 18737
pixi run python main.py web serve --host 0.0.0.0 --port 18736
```

Web 查看器读取当前主线备份结构中的 `archive.sqlite3`，并在页面请求时从归档
正文和最新展示元数据重新渲染 HTML；普通阅读器的线程列表默认按作者最后发言倒序，
也会显示已存盘的正文字数并支持按正文字数排序。`html_modified/post_*.html` 仍是备份/PDF流程使用的
产物。旧版只有 `json/page_*.json` 的目录会在页面中显示为“需迁移”，请先运行
`backup migrate-store`。

前端使用 Vue Router 管理页面：`/threads` 是普通帖子阅读器，`/admin` 是
管理界面；旧 `/databases` 会跳转到 `/admin/databases`。管理界面中的数据库
查看器只浏览项目输出目录中的
`forum_threads.sqlite3`、`image_index.sqlite3` 和各帖子目录下的
`archive.sqlite3`，支持查看表结构、分页浏览、排序和关键字筛选，不执行用户
输入的 SQL。

管理界面的“正文版本”会列出同一楼层中正文内容有多个历史版本的楼，左侧备份列表
默认只显示存在多版本楼层的备份，并可切换为显示全部可管理备份。用户只能选择
非最新版历史版本；当前最新版不能被手动固定，以免后续增量备份无法自动更新。选择
会写入备份目录的 `post_version_overrides.json`，普通网页查看和后续 PDF 输入 HTML
都会使用这套有效版本；清除选择后回到自动最新版。预览历史版本时，管理页会显示
该版本与最新版正文的差异，便于判断实际变化。选择文件写入和对应 HTML/manifest
刷新均使用原子替换。

普通帖子阅读页支持为单楼添加 BBCode overlay。overlay 是整楼替换，写入
`post_overlays.json`，保存或清除后会刷新对应 `html_modified/post_*.html`；
网页查看和后续 PDF 导出都会使用同一结果。当前 overlay 不支持 `[img]`、`[flash]`
或渲染后产生图片的 HTML。

## 生成 PDF

先运行 `backup all` 生成 `html_modified` 和图片，再生成 PDF：

```powershell
pixi run python main.py backup pdf --name 帖子名
```

默认每 200 楼生成一个 PDF，可以通过 `--lou-per-pdf` 修改：

```powershell
pixi run python main.py backup pdf --name 帖子名 --lou-per-pdf 100
```

可以通过 `--pdf-workers` 指定整个命令中同时运行的 PDF 生成任务数：

```powershell
pixi run python main.py backup pdf --name 帖子名 --pdf-workers 2
pixi run python main.py backup pdf --all-threads
pixi run python main.py backup pdf --all-threads --workers 4 --pdf-workers 2
```

`--workers` 控制批量模式下同时处理的帖子数；`--pdf-workers` 控制全命令共享的
WeasyPrint 并发数。上面的批量示例会同时处理最多 4 个帖子，但最多只有 2 个
PDF 分段在生成。

PDF 生成功能会调用 `weasyprint` 命令。通过 `pixi run` 启动时会使用 pixi 环境中的 WeasyPrint、Poppler 和字体依赖，支持 Linux、macOS 和 Windows 的 Pixi 环境。
生成后的 PDF 会记录输入中间 HTML 的 hash；后续运行时，如果对应 HTML 没有变化且 PDF 文件仍存在，会跳过该分段。需要强制重新生成时，删除 `pdf/` 下对应 PDF 或 `pdf_input_hashes.json`。
只看作者备份会读取 `floor_map.json`，在 PDF 标题中显示只看作者楼层和原帖楼层，例如 `第12楼（原86楼）`。如果缺少映射，先运行 `backup floors`。

## 输出目录

默认输出到 `output/<tid>_<aid|all>/`：

- `json/`：加 `--write-json` 时输出的接口最近响应分页数据
- `archive.sqlite3`：每帖独立的楼层历史存储，保存页响应快照、正文版本和最新展示元数据；
  完整原始分页响应保存在 `page_snapshots.page_json`，`post_versions` 只按正文内容做版本控制
- `post_version_overrides.json`：管理界面保存的正文历史版本选择；没有选择时可为空
- `post_overlays.json`：网页阅读页保存的单楼 BBCode overlay；保存后会刷新对应
  `html_modified/post_*.html`
- `html_modified/`：替换为本地 `images_unique` 图片路径后的 HTML
- `pdf/`：生成 PDF 时使用的中间 HTML、长图切片和 PDF 文件
- `floor_map.json`：只看作者楼层到原帖楼层的映射

全局图片产物位于 `output/` 根目录：

- `images_unique/`：按内容 hash 保存的实际图片文件
- `images_unique/download_failed_placeholder.png`：下载失败图片的占位图，按需生成
- `image_index.sqlite3`：原始 NGA 图片 URL 到 `images_unique` 文件的映射

## 协议

MIT
