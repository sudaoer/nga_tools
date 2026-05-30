# hbrgo 安价整理 2026-05-29

本目录用于整理 hbrgo 在只看楼主 6353-6356 楼发布的安价活动。

## 流程

1. 下载全贴 JSON：

   ```bash
   pixi run python hbrgo-anjia-260529/download_full_thread.py
   ```

2. 从全贴 JSON 提取安价投稿：

   ```bash
   pixi run python hbrgo-anjia-260529/extract_anjia.py
   ```

3. 生成 xlsx 汇总：

   ```bash
   pixi run python hbrgo-anjia-260529/build_xlsx.py
   ```

## 输出

- `thread_json/page_*.json`：全贴分页备份。
- `thread_meta.json`：下载元数据；下载脚本会从第 2515 页开始刷新已有分页，以覆盖可能的编辑。
- `anjia/anjia_*.json`：逐条安价投稿数据。
- `hbrgo-anjia-260529.xlsx`：汇总表格，只导出每个 UID 最后一条安价，不包含用户名列和单独的安价内容列；未采纳内容会空一行后放在有效内容之后，且不分配编号。

`rules.md` 是人工整理规则，`rules.json` 是脚本读取的规则配置。
