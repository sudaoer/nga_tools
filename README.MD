# NGA 帖子备份器

把 NGA 帖子通过接口抓取到本地，保存原始 JSON、转换后的 HTML、图片文件，并可按楼层分段生成 PDF。

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

## 使用

添加帖子配置：

```powershell
pixi run python main.py thread add --name 帖子名 --tid 12345678
```

只备份某个作者的回复时添加 `--aid`：

```powershell
pixi run python main.py thread add --name 帖子名 --tid 12345678 --aid 987654
```

查看已保存的帖子配置：

```powershell
pixi run python main.py thread list
```

抓取整帖并下载图片：

```powershell
pixi run python main.py backup all --name 帖子名
```

补充本地缺失内容和远端新增内容：

```powershell
pixi run python main.py backup sub --name 帖子名
```

`backup sub` 会补抓缺失的 JSON 页，并刷新本地尾页到远端最后一页；随后补齐 HTML、`html_modified` 和缺失图片。

也可以不使用保存的名称，直接指定 `tid` 和可选的 `aid`：

```powershell
pixi run python main.py backup all --tid 12345678
pixi run python main.py backup all --tid 12345678 --aid 987654
pixi run python main.py backup sub --tid 12345678 --aid 987654
```

已有只看作者备份缺少楼层映射时，可以单独生成映射：

```powershell
pixi run python main.py backup floors --name 帖子名
```

楼层映射会增量刷新。缺失楼无法唯一确定原帖楼层时，`floor_map.json` 会记录 `candidate_original_lous` 候选列表。

校验已下载图片，损坏或无法打开的图片会被删除：

```powershell
pixi run python main.py image verify --name 帖子名
```

## 生成 PDF

先运行 `backup all` 生成 HTML 和图片，再生成 PDF：

```powershell
pixi run python main.py backup pdf --name 帖子名
```

默认每 200 楼生成一个 PDF，可以通过 `--lou_per_pdf` 修改：

```powershell
pixi run python main.py backup pdf --name 帖子名 --lou_per_pdf 100
```

可以通过 `--pdf_workers` 指定同时运行的 PDF 生成任务数：

```powershell
pixi run python main.py backup pdf --name 帖子名 --pdf_workers 2
```

PDF 生成功能会调用 `weasyprint` 命令。通过 `pixi run` 启动时会使用 pixi 环境中的命令。
只看作者备份会读取 `floor_map.json`，在 PDF 标题中显示只看作者楼层和原帖楼层，例如 `第12楼（原86楼）`。如果缺少映射，先运行 `backup floors`。

## 输出目录

默认输出到 `output/<tid>_<aid|all>/`：

- `json/`：接口返回的原始分页数据
- `html/`：由 BBCode 转换出的单楼 HTML
- `html_modified/`：替换本地图片路径后的 HTML
- `images/`：下载的图片
- `pdf/`：生成 PDF 时使用的中间 HTML、长图切片和 PDF 文件
- `floor_map.json`：只看作者楼层到原帖楼层的映射

## 协议

MIT
