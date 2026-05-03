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

只检查抓取和确定性过滤时可以跳过模型：

```powershell
pixi run python AnchorCounter.py --no-llm --output output/43877379_all/anchor_counter/test.json
```

Vue 查看器在 `anjia-viewer/`，默认读取 `public/data/anchors_43877379.json`。

## TODO（永远不会做（不是）的功能）
- 添加config里写配置的功能，省的每次复制粘贴tid aid
- 添加自动更新功能
- 添加图片去重和多页一pdf，这样可能可以节省空间吧