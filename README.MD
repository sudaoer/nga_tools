# NGA 帖子备份器

把 NGA 帖子通过接口抓取到本地，保存原始 JSON、转换后的 HTML、图片文件，并可按楼层分段生成 PDF。

## 配置

复制配置模板：

```powershell
Copy-Item config_template.py config.py
```

在 `config.py` 中填入自己的 NGA Cookie：

```python
NGAPASSPORTUID = ""
NGAPASSPORTCID = ""
```

当前命令会读取帖子配置文件，请确保 `config.py` 中有配置文件路径：

```python
THREAD_CONFIG_FILE = "threads.json"
```

如需调整输出目录，修改：

```python
OUTPUT_DIR = "output"
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

也可以不使用保存的名称，直接指定 `tid` 和可选的 `aid`：

```powershell
pixi run python main.py backup all --tid 12345678
pixi run python main.py backup all --tid 12345678 --aid 987654
```

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

PDF 生成功能会调用系统中的 `weasyprint` 命令，需要先确保它可以在命令行中直接运行。

## 输出目录

默认输出到 `output/<tid>_<aid|all>/`：

- `json/`：接口返回的原始分页数据
- `html/`：由 BBCode 转换出的单楼 HTML
- `html_modified/`：替换本地图片路径后的 HTML
- `images/`：下载的图片
- `pdf/`：生成 PDF 时使用的中间 HTML、长图切片和 PDF 文件

## 协议

MIT
