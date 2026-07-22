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

批量备份默认并行 4 个帖子；NGA API 请求、图片下载和 BGM 音频下载分别限流。需要调整时可在
`config.json` 中设置：

```json
{
  "backup_configs_workers": 4,
  "api_concurrency": 4,
  "image_concurrency": 50,
  "audio_concurrency": 8,
  "ankebak_full_backup_interval_hours": 168,
  "ankebak_missing_floor_immediate_retry_hours": 168,
  "ankebak_missing_floor_retry_max_interval_hours": 720,
  "backup_image_retry_max_interval_hours": 168,
  "backup_audio_retry_max_interval_hours": 168
}
```

`ankebak_full_backup_interval_hours` 控制智能备份执行 `backup all` 的
最长等待时间，单位为小时，默认 168 小时（7 天），必须大于 0。
`ankebak_missing_floor_immediate_retry_hours` 控制缺失楼的积极重试窗口，
默认 168 小时（7 天）；窗口内每次运行 ankebak 都会重试。
`ankebak_missing_floor_retry_max_interval_hours` 控制较旧缺失楼的最长重试间隔，
默认 720 小时（30 天）。两个缺失楼配置都以小时为单位且必须大于 0。
`backup_image_retry_max_interval_hours` 控制长期失败图片的最长重试等待时间，
单位为小时，默认 168 小时（7 天），必须大于 0。
`backup_audio_retry_max_interval_hours` 对 BGM 音频采用相同的重试上限，
默认也是 168 小时（7 天）。

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
包含主楼。NGA 只看作者楼号从 0 开始，因此最大有效作者楼号是 `vrows - 1`，
不要把 `vrows` 当成最大楼号。`min_replies` 未配置时默认为 500，
`min_author_lous` 未配置时默认为 20。保存时会把主题楼主 `authorid` 写为 `aid`，后续使用
`backup all/sub --name 帖子名` 会默认只看楼主。
需要验证 `min_author_lous` 的候选会在完成标题、排除词、回复数和已有配置检查后，
按 `api_concurrency` 并行请求只看作者第一页；匹配结果仍按数据库读取顺序应用。

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

抓取整帖并下载图片和 BGM 音频：

```powershell
pixi run python main.py backup all --name 帖子名
```

图片会按内容 hash 保存到全局 `output/images_unique/`，原始 NGA 图片链接到本地文件的映射保存在持久数据文件 `output/image_index.sqlite3`，文件校验结果另存于可重建的 `output/image_cache.sqlite3`。下载失败的图片不写入 SQLite 映射；PDF 按需渲染时会使用 `images_unique/download_failed_placeholder.png` 占位图，后续增量备份仍会继续重试原图。
正文 `<audio src="...">` 中完整、规范的 NGA MP3 URL 会按 SHA-256 内容 hash
保存到全局 `output/audio_unique/`，URL、文件路径、内容 hash、大小和时长保存在
持久数据文件 `output/audio_index.sqlite3`。首次启用时会扫描每帖
`archive.sqlite3` 中的全部正文历史版本，之后只扫描新增版本；因此已经不在最新版
正文中的历史 BGM 也能补存。无效或下载失败的音频不写入成功映射，并保存在每帖
`archive_state.sqlite3` 的待重试队列中。
帖子内容会写入每个备份目录自己的 `archive.sqlite3`，派生处理状态和可重建缓存分别写入 `archive_state.sqlite3` 与 `archive_cache.sqlite3`。归档只为每个页码保存最新的 `totalPage`、`vrows` 和观察时间等紧凑状态，不保存 NGA 原始分页响应。默认不输出分页 JSON；需要最近响应缓存和回滚参考时，加 `--write-json` 会额外写入 `json/page_*.json`。Web 和 PDF 均从 `archive.sqlite3` 按需生成各自的目标 HTML，不保存逐楼中间 HTML。

补充本地缺失内容和远端新增内容：

```powershell
pixi run python main.py backup sub --name 帖子名
```

`backup sub` 会补抓缺失的页，并刷新本地尾页到远端最后一页。`backup all` 仍会完整抓取远端所有页面；抓取完成后，同一主题的本批页面会使用一个归档连接和一个事务写入，任一页面写入失败都会回滚本批全部页面。

完整或增量备份会复用 `archive_state.sqlite3` 中的楼层处理状态和图片引用集合状态。页数、`vrows` 或楼层映射输入变化时只刷新楼层派生状态；归档正文、overlay、手动版本选择和图片引用解析版本未变化时，不再读取完整归档或批量展开全部帖子图片引用，只重试上次未完成的图片 URL。该状态同时适用于只看作者和没有 `aid` 的原帖备份；`backup all` 的状态复用判定发生在完整网络抓取及归档写入之后，不会跳过远端刷新。

正文来源实际变化后才会重建图片引用集合，并复用 `archive_cache.sqlite3` 中的帖子级图片引用派生缓存，只重新解析新增或正文、overlay、手动版本选择发生变化的楼层。下载失败的图片待重试队列保存在 `archive_state.sqlite3`：新 URL、HTTP 429、5xx、网络、payload 和本地存储失败会在下一次相关备份继续尝试；已知 HTTP 3xx 和除 429 外的 4xx 会根据距上次尝试的时间概率重试。累计触发概率为 `x^3`（`x` 是已等待时间占最大间隔的比例），同一 URL 在一个重试周期内使用稳定票据，不会因为频繁运行而反复抽签；达到 `backup_image_retry_max_interval_hours` 后必定重试。音频失败使用同样的稳定概率规则，并以 `backup_audio_retry_max_interval_hours` 为最长间隔。概率延后的 URL 不会单独触发 `backup auto` 本地维护，新 URL 始终立即尝试，不再被正文引用的图片 URL 会移出图片队列；音频队列覆盖所有历史正文版本，因此历史引用仍会保留。状态复用不会重新校验已成功的历史图片，本地文件后续被删除或损坏时请运行 `image verify` 检测。

只看作者备份还会重试恢复缺失楼内容；恢复出的匿名正文会提升归档修订并使图片集合按需更新。ankebak 会把连续缺失楼组成一个缺口，并用缺口后第一个有效楼的发帖时间估算缺失时间；尾部缺口没有下一个有效楼时按当前时间处理。默认 7 天内的缺口每次运行都重试；更旧的缺口使用稳定的 `x^3` 累计概率，并随缺失楼年龄把最长等待逐步增加到 30 天。同一帖的所有缺口共享一张稳定票据，不会因缺口数量或频繁运行增加抽中概率；达到当前最长间隔后必定重试。首次出现或旧状态没有重试记录的缺失楼会立即尝试一次，逐楼重试时间保存在该帖 `archive_state.sqlite3` 的 `backup_pending_missing_floors` 表中。概率延后的缺口不会额外请求原帖页面，但本轮因其他工作已经取得的页面仍可顺带恢复它们。手工 `backup all/sub` 始终立即重试全部缺失楼，不应用 ankebak 延后策略。

需要忽略所有派生状态并强制重新处理时，可给 `backup all` 或 `backup sub` 加 `--force-processing`（兼容 `--force_processing`）；此选项会立即重试全部仍被引用的失败图片并重扫、重试全部历史 BGM，但不会禁用帖子级图片引用缓存。
增量刷新到的新页会合并进历史楼层库；同一页新响应中消失的旧楼不会从库中删除。历史 `json/page_*.json` 不会被读取、导入或改写；不存在 `archive.sqlite3` 时，正常备份会直接创建当前格式数据库。

也可以不使用保存的名称，直接指定 `tid` 和可选的 `aid`：

```powershell
pixi run python main.py backup all --tid 12345678
pixi run python main.py backup all --tid 12345678 --aid 987654
pixi run python main.py backup sub --tid 12345678 --aid 987654
pixi run python main.py backup sub --tid 12345678 --aid 987654 --write-json
pixi run python main.py backup all --tid 12345678 --force-processing
```

批量增量备份 `thread_configs.json` 中的所有帖子：

```powershell
pixi run python main.py backup sub --all-threads
pixi run python main.py backup sub --all-threads --workers 4 --api-concurrency 4 --image-concurrency 50 --audio-concurrency 8
pixi run python main.py backup sub --all-threads --write-json
pixi run python main.py backup all --all-threads --force-processing
```

需要批量完整刷新时，可运行 `backup all --all-threads`。

推荐日常使用智能备份入口：

```powershell
pixi run ankebak
```

该任务等价于 `pixi run python main.py backup auto`：先执行默认的
`forum sync`，再用本轮实际抓到的主题列表与上次成功备份水位比较。
`replies` 和 `lastpost` 均未变化且没有本地待重试工作的帖子会跳过；发生变化
时执行增量备份。增量路径会从本地尾页向前探测远端分页信息；只看作者帖若 `vrows`
和分页数也未变化，只校验尾页并处理本轮应重试的图片、音频、缺失楼或本地派生输入。作者筛选空页会使用向前探测到的有效页分页信息合成。周期完整备份依据距
上次成功 `backup all` 的时间使用稳定的 `x^3` 累计概率选择，频繁运行不会重复
抽签；达到 `ankebak_full_backup_interval_hours` 后必定执行，以发现日常增量路径
无法覆盖的旧页编辑。首次启用时所有尚无 ankebak 成功水位的帖子会立即完整
备份。成功水位保存在 `output/backup_state.sqlite3` 的
`ankebak_thread_state` 表中；`forum_threads.sqlite3` 只保存版面主题数据，
单帖失败不会推进水位。

批量备份遇到 NGA 返回“帖子被设为隐藏”“帖子被删除”或“帖子正等待审核”时会记录
警告并计为状态异常；如果本批只有这类状态异常，命令仍以 0 退出。其他异常仍会使
批量命令最终以非 0 退出。

### 离线重放服务

已有备份可以作为只读语料启动本地 NGA 重放服务，用于后续在不访问公网的
情况下压测完整抓取路径。先复制并调整限速配置：

```powershell
Copy-Item replay_profile.example.json replay_profile.json
pixi run python main.py replay serve --source-output output --profile replay_profile.json
```

`--thread-config` 默认使用 `config.json` 中的 `thread_config_file`，也可以显式
指定：

```powershell
pixi run python main.py replay serve --source-output output `
  --thread-config thread_configs.json --profile replay_profile.json --port 8765
```

服务启动时以 SQLite `immutable` 只读模式打开各帖 `archive.sqlite3` 和全局
`image_index.sqlite3`、`audio_index.sqlite3`，从最新归一化正文、作者、时间和
附件元数据合成 API 分页并预载入内存；不会读取归档中的 NGA 原始分页响应。
图片和音频分别从 `images_unique/`、`audio_unique/` 流式读取。源数据库必须已经完成 WAL 检查点；如果 WAL 仍有
内容，说明快照尚未静止，服务会拒绝启动。启动后源目录应保持不变，图片若被
删除、替换或修改，该 URL 会按缺失处理；音频还会校验索引记录的 SHA-256，
内容不符同样按缺失处理。服务不会访问 NGA，也不会修改源数据库或媒体内容。

当 `thread_configs.json` 中的帖子只有 `tid_aid` 归档而没有 `tid_all` 归档时，
服务会根据楼层映射生成可供完整抓取流程消费的原帖分页。已知作者楼和已恢复的
匿名楼会按原楼层放回归一化正文及附件元数据；已知被删除或存在歧义的楼层会保留
空洞，其余无法恢复的非作者楼使用空正文占位。因此占位部分不代表真实原帖正文或
图片负载。如果存在 `tid_all/archive.sqlite3`，其原帖分页同样从最新归一化内容
合成，不依赖记录的 NGA 原始响应。

限速配置分为 `api`、`image` 和 `audio` 三组：`latency_ms` 是每个请求的固定首包延迟，
`bandwidth_bytes_per_second` 是该组所有并发请求共享的全局带宽，设为 `0` 表示
不限带宽；`max_inflight` 是该组最大在途请求数，`chunk_bytes` 是流式发送块
大小。省略 `audio` 时会复用 `image` 配置。限速不加入随机抖动，因此同一语料
和配置适合做稳定的 A/B 对比。

重放端点如下：

- `POST /app_api.php?__lib=post&__act=list`：按 `tid`、`page` 和可选
  `authorid` 返回帖子分页。
- `GET /__replay__/image?url=<原始图片URL>`：按全局图片索引返回图片。
- `GET /__replay__/audio?url=<原始音频URL>`：按全局音频索引返回 MP3。
- `GET /__replay__/health`：健康状态、语料 ID、限速配置 ID 和在途请求数。
- `GET /__replay__/manifest`：语料范围、分页与图片/音频统计以及完整限速配置。
- `GET /__replay__/metrics`：API/图片/音频请求数、字节数、状态码和等待耗时。
- `POST /__replay__/reset`：无在途请求时清空指标并重置带宽时间线。

`corpus_id` 标识本次冻结的归一化内容合成分页、楼层映射及图片/音频索引状态，
`profile_id` 标识规范化后的限速配置。启动服务后可在另一个终端运行共享
`backup all` 路径：

```powershell
pixi run python main.py replay run `
  --server-url http://127.0.0.1:8765 `
  --source-output output --target-output replay-output/run-a `
  --initial-state warm --all-threads --workers 8 `
  --api-concurrency 4 --image-concurrency 50 --audio-concurrency 8
```

也可以使用一次性测试入口自动启动重放服务进程，等待服务就绪后再启动独立的
模拟运行进程：

```powershell
pixi run python main.py replay test `
  --source-output output --profile replay_profile.json `
  --target-output replay-output/run-a `
  --initial-state warm --all-threads --workers 8 `
  --api-concurrency 4 --image-concurrency 50 --audio-concurrency 8
```

`replay test` 默认仅在 `127.0.0.1` 上自动选择空闲端口，也可以通过
`--port` 指定端口。语料加载与服务就绪检查合计最多等待 300 秒，健康检查通过
后才会启动模拟运行；模拟运行成功、失败或被 Ctrl+C 中断时都会停止服务并回收
两个子进程。该入口不接收 `--server-url`，目标准备、网络隔离、验收和
`replay_run-*.json` 报告均复用 `replay run` 的实现。

`empty` 要求目标不存在或为空，从零创建归档；`warm` 通过 SQLite Online Backup
分别复制现有 data/state/cache 数据库，并复制或 reflink 图片与音频；正文版本选择随
每帖 `archive.sqlite3` 一起复制。
缓存键使用 `images_unique/...` 相对路径，不再从旧版 `image_index.sqlite3` 读取或
改写绝对路径缓存。两者都会写入 replay 目标归属
标记；`existing` 只接受带有同一 `source-output` 归属标记的目标，可继续一次中断的
重放运行。源与目标相同或互相嵌套、目标使用符号链接，或目标文件与源文件共享
inode 时会拒绝运行；`empty` 和 `warm` 不会覆盖非空目标。Windows 当前只检查
`target-output` 是目录并校验归属标记，暂不检查符号链接、directory junction 或
共享文件 ID；在 Windows 上使用 `existing` 时必须由用户保证目标是独立的物理目录。

重放运行不会读取环境代理或发送 NGA Cookie，API、图片与音频的实际请求 origin 必须
等于 `server-url`。准备耗时不计入备份墙钟；结束后会在目标根目录原子写入
`replay_run-<timestamp>.json`，记录 Git commit、语料与 profile ID、并发参数、
服务端指标、处理状态复用、失败分类、批量计时日志和数据库/内容验收结果。

### 运行日志

警告详情仍写入每帖输出目录的 `warnings.log`，每次帖子任务开始时覆盖旧内容。
控制台不逐条显示警告：有警告的帖子结束时会输出按业务类别统计的小计，整个
CLI 命令结束时会输出警告总数、涉及帖子数和分类合计；没有警告的帖子不输出
小计，命令总计仍会显示 0 条。

启用 `timing_log_enabled` 时，每帖计时日志使用
`timing-YYYYMMDDTHHMMSSffffff.log`，批量计时摘要使用
`output/batch_timing-YYYYMMDDTHHMMSSffffff.log`。每个帖子目录独立保留最近
5 份计时日志，`output` 根目录也独立保留最近 5 份批量摘要；旧的固定名
`timing.log`、`batch_timing.log` 会计入这 5 份并随时间自然淘汰。若运行时能
从 Git 仓库取得当前提交，日志头会包含完整的 `Commit ID`；无法取得时省略该
字段，不影响任务执行。关闭计时日志后不会创建、轮转或删除任何计时文件。
`backup auto` 的批量摘要还会把论坛同步拆分为版面页请求、限流等待、水位读取、
SQLite 读写、库内本地筛选、只看作者请求和配置保存，并记录请求页数、重试数与
筛查记录数；独立运行 `forum sync` 时控制台输出保持简洁，不打印这份耗时明细。

只看作者备份会在正常备份流程中增量刷新楼层映射。如果已有备份缺少映射，
可以强制重新执行派生处理：

```powershell
pixi run python main.py backup sub --name 帖子名 --force-processing
pixi run python main.py backup sub --all-threads --force-processing
```

楼层映射会增量刷新并保存在同一备份的 `archive.sqlite3` 中。缺失楼无法唯一确定原帖楼层时，数据库会保存候选原楼层；若能从匿名原帖恢复正文，正文也会直接写入该数据库。

## 数据库格式与交付

当前布局给每个 SQLite 文件写入 `storage_metadata`，其中包含角色、布局版本和
存储 ID；每帖状态库与缓存库还会绑定对应 `archive.sqlite3` 的存储 ID，避免把
不同备份的派生文件混用。交付完整备份时按以下边界处理：

| 分类 | 文件 | 交付要求 |
| --- | --- | --- |
| 持久数据 | `forum_threads.sqlite3`、各帖 `archive.sqlite3`、`image_index.sqlite3`、`images_unique/`、`audio_index.sqlite3`、`audio_unique/` | 必须保留；它们包含离线时无法重新从本机推导的版面数据、正文历史、图片/音频 URL 映射和媒体内容 |
| 用户选择 | `thread_configs.json`；各帖正文版本选择位于 `archive.sqlite3` 的 `post_version_selections` 表 | 必须保留；正文版本选择已包含在持久数据库中 |
| 运行状态 | `backup_state.sqlite3`、各帖 `archive_state.sqlite3` | 建议随完整工作副本保留；只读交付可省略，之后会从持久数据重建派生状态，但智能备份水位和失败图片/音频重试历史会丢失 |
| 可重建缓存 | `image_cache.sqlite3`、各帖 `archive_cache.sqlite3` | 可以不交付或删除；程序不会把它们当作持久数据，缺失时会按当前格式重新创建 |
| 可选产物 | `json/`、`pdf/`、日志、`replay_run-*.json` | 按用途保留；当前 SQLite 数据完整时不属于恢复 Web/PDF/replay 读取能力的必要输入 |

只复制持久数据和用户选择后，接收方仍可在不访问 NGA 的情况下使用 Web、生成
PDF 或启动 `replay serve`；首次需要派生处理时会新建 state/cache 文件。若要保留
现成 PDF，也应额外复制各帖 `pdf/`。交付时可同时提供不含 Cookie 的
`config.json` 并让接收方修改 `output_dir`；不要把 `secrets.json`、Cookie 或其他
凭据交给不应获得 NGA 账号权限的人。

当前程序只接受当前数据库格式。所有已存在的 SQLite 文件都会校验
`storage_metadata`、角色、来源绑定、字段、约束、索引以及归档 `user_version`。
明确的写入或 `ensure` 路径会在一个事务内通过 `CREATE TABLE/INDEX IF NOT EXISTS`
补齐身份和版本均有效的当前库所缺少的业务表与索引，再校验完整格式；若校验失败，
本次补建会整体回滚。纯读取路径不会创建数据库文件、补表、隔离或重建已有文件；
可选数据库不存在时按空状态处理，已有数据库缺表或格式错误时则报格式不受支持。

额外旧表、字段或约束不匹配、错误角色、错误来源绑定以及旧格式版本仍会被拒绝，
并保持原库不变。程序不再提供布局、正文或 JSON 存储迁移命令，也不会自动补列、
执行 `ALTER TABLE` 或转换正文。

`post_versions.content` 必须是由 Python 3.14 标准库 `compression.zstd`、固定
level 3 写入的 BLOB。旧 TEXT 正文和旧分页、状态、缓存表不再兼容读取。数据库文件
完全不存在时，只有写入或显式 `ensure` 才会创建当前格式；可重建的
state/cache 文件如需重置，先在程序外备份并删除对应文件，再由正常写入路径重新
创建。仅含历史
`json/page_*.json` 的目录不会出现在 Web 帖子列表中。

手动下载一张完整NGA图片URL并写入全局图片索引：

```powershell
pixi run python main.py image add --url https://img.nga.178.com/attachments/mon_202506/06/example.png
```

已有有效映射时该命令不会重复下载；缺失或损坏时会下载到
`output/images_unique/` 并更新 `output/image_index.sqlite3`。命令只接受完整的
NGA图片URL，可用于先准备图片，再把同一URL保存到Web管理页的BBCode overlay。

并行校验所有本地备份中的已下载图片，损坏或无法打开的图片会被删除：

```powershell
pixi run python main.py image verify
```

也可以只校验指定帖子：

```powershell
pixi run python main.py image verify --name 帖子名
```

## Web 查看器

安装并构建前端，然后启动本地查看器：

```powershell
pixi run web-serve
```

默认地址为 `http://127.0.0.1:18736/`。普通帖子阅读页只读取本地备份；
管理页会写入正文版本选择或 BBCode overlay，后续页面请求和 PDF 导出会直接使用这些设置。默认只监听
本机地址；允许局域网访问时，管理功能也会暴露给局域网内访问者：

```powershell
pixi run python main.py web serve --port 18737
pixi run python main.py web serve --host 0.0.0.0 --port 18736
```

Web 查看器读取当前主线备份结构中的 `archive.sqlite3`，并在页面请求时从归档
正文和最新展示元数据重新渲染 HTML；普通阅读器的线程列表默认按作者最后发言倒序，
也会显示已存盘的正文字数并支持按正文字数排序。只有 `json/page_*.json`、没有
当前格式 `archive.sqlite3` 的历史目录不会显示在线程列表中。

前端使用 Vue Router 管理页面：`/threads` 是普通帖子阅读器，`/admin` 是
管理界面；旧 `/databases` 会跳转到 `/admin/databases`。管理界面中的数据库
查看器只浏览项目输出目录中的
`forum_threads.sqlite3`、`backup_state.sqlite3`、`image_index.sqlite3`、
`image_cache.sqlite3`、`audio_index.sqlite3` 和各帖子目录下的 `archive.sqlite3`、
`archive_state.sqlite3`、`archive_cache.sqlite3`，支持查看表结构、分页浏览、
排序和关键字筛选，不执行用户输入的 SQL。

管理界面的“正文版本”会列出同一楼层中正文内容有多个历史版本的楼，左侧备份列表
默认只显示存在多版本楼层的备份，并可切换为显示全部可管理备份。用户只能选择
非最新版历史版本；当前最新版不能被手动固定，以免后续增量备份无法自动更新。选择
会写入每帖 `archive.sqlite3` 的 `post_version_selections` 表，普通网页查看和后续
PDF 目标 HTML 都会按需使用这套有效版本；清除选择后回到自动最新版。预览历史
版本时，管理页会显示该版本与最新版正文的差异，便于判断实际变化。

普通帖子阅读页支持为单楼添加 BBCode overlay。overlay 是整楼替换，写入每帖
`archive.sqlite3` 的 `post_overlays` 表；网页查看和后续 PDF 导出都会从该 BBCode 按需生成目标 HTML。
overlay 可以保存空内容；空内容仍是有效覆盖，清除 overlay 后才恢复原正文。图片仅支持
`[img]完整NGA图片URL[/img]`，并且该 URL 必须已存在于 `image_index.sqlite3`、对应
`images_unique` 文件有效。新URL可先通过 `image add --url URL` 下载；overlay 本身
不会下载图片，也不允许 `[flash]`、外站图片或直接写入 `<img>` HTML。
正文渲染只识别正文中明确写出的完整NGA图片URL，不会再根据 NGA 响应中的附件
列表猜测或补全损坏链接。无效的 `[img]...[/img]` 会原样显示，便于用户核对后
通过 overlay 修正；附件元数据仍保留在归档中。

正文中的 NGA BGM 会渲染为浏览器原生音频控件，使用 `preload=none` 且不会
自动播放；开始播放一个 BGM 时会暂停页面中的其他 BGM。播放器只使用经过索引、
文件存在且 SHA-256 校验正确的 `audio_unique` 本地文件，并支持浏览器 Range
请求。缺失或损坏时显示“音频未下载或不可用”，不会回退请求 NGA 或其他远端地址。
PDF 不嵌入音频。

## 生成 PDF

先运行 `backup all` 写入 archive 并下载图片，再生成 PDF：

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

PDF 生成功能会通过当前 pixi Python 子进程调用 WeasyPrint。通过 `pixi run` 启动时会使用 pixi 环境中的 WeasyPrint、Poppler 和字体依赖，支持 Linux、macOS 和 Windows 的 Pixi 环境。
生成后的 PDF 会记录输入中间 HTML 的 hash；后续运行时，如果对应 HTML 没有变化且 PDF 文件仍存在，会跳过该分段。需要强制重新生成时，删除 `pdf/` 下对应 PDF 或 `pdf_input_hashes.json`。
只看作者备份会读取 `archive.sqlite3` 中的楼层映射，在 PDF 标题中显示只看作者楼层和原帖楼层，例如 `第12楼（原86楼）`。如果缺少映射，运行 `backup sub --name 帖子名 --force-processing` 刷新备份。

## 输出目录

默认输出到 `output/<tid>_<aid|all>/`：

- `json/`：加 `--write-json` 时输出的接口最近响应分页数据
- `archive.sqlite3`：每帖持久数据。保存每页最新的紧凑分页状态、正文版本、最新
  展示元数据、用户正文版本选择、楼层映射与候选原楼层、归档修订和管理页
  BBCode overlay；不保存 NGA 原始分页响应。`post_versions.content` 始终以 zstd
  BLOB 保存，Web、PDF 和 replay 从此库读取解压后的正文
- `archive_state.sqlite3`：每帖运行状态。保存楼层/图片引用处理水位、逐楼引用
  清单、URL 引用计数、缺失楼逐楼重试时间、音频历史版本扫描水位以及失败图片/音频待重试队列；删除后可从 `archive.sqlite3`
  重建，但会丢失尚未完成工作的历史状态
- `archive_cache.sqlite3`：每帖可重建缓存。当前保存帖子级图片引用解析结果；
  缺失时按当前格式创建；格式或 `archive.sqlite3` 存储 ID 不匹配时拒绝读取
- `pdf/`：生成 PDF 时使用的中间 HTML、长图切片和 PDF 文件

全局数据库与媒体产物位于 `output/` 根目录：

- `forum_threads.sqlite3`：持久的版面主题列表数据
- `backup_state.sqlite3`：`backup auto` 的成功水位和完整备份调度状态
- `images_unique/`：按内容 hash 保存的实际图片文件
- `images_unique/download_failed_placeholder.png`：下载失败图片的占位图，按需生成
- `image_index.sqlite3`：持久的原始 NGA 图片 URL 到 `images_unique` 文件映射
- `image_cache.sqlite3`：以 `images_unique/...` 相对路径为键的文件校验缓存；可删除重建
- `audio_unique/`：按 SHA-256 内容 hash 保存的实际 MP3 文件
- `audio_index.sqlite3`：持久的原始 NGA 音频 URL 到 `audio_unique` 文件映射及内容元数据
旧版本生成的 `html_modified/`、`backup_state.json`、`floor_map.json`、
`post_overlays.json` 和 `overlay/post_*.html` 不再读取，
程序也不会自动删除或改写这些历史产物。旧格式 SQLite 会被拒绝且不会自动修改。

## 协议

MIT
