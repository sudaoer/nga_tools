# 代理指南

## 职责范围

- 保持 `main.py` 为薄启动器，仅导入并调用 `nga_tools.cli.main`。
- CLI 解析、帮助格式化、验证和命令分发放在 `nga_tools.cli` 中。
- 可复用的实现逻辑放在 `nga_tools` 包下。
- NGA API 抓取逻辑放在 `nga_tools/ngaclient` 内。
- 避免顶层脚本与包模块之间的逻辑重复。

## 指南维护

- 若本文件与当前项目不一致或已过时，请告知用户问题所在并建议更新。
- 除非用户明确允许，否则不要编辑此文件。

## 命令

- 使用 pixi 运行项目命令。
- 通过 `pixi run python main.py ...` 运行 CLI。
- 通过 `pixi run typecheck` 对包代码进行严格类型检查。
- 通过 `pixi run python -m py_compile <文件>` 进行语法检查。

## 依赖管理

- 共享依赖放在 `pixi.toml` 的 `[dependencies]` 中。
- 平台特定依赖放在 `[target.<平台>.dependencies]` 下。
- Linux 专属的 PDF 支持放在 `[target.linux.dependencies]` 下。
- 不要假定 `weasyprint` 在 pixi 环境之外存在。

## 配置与输出

- 不要提交本地的 `config.json`、`secrets.json`、cookies、下载输出、生成的 PDF、图片或临时 JSON 导出文件。
- 使用 `config.example.json`、`secrets.example.json` 和 `nga_tools.config` 作为配置键的跟踪契约。
- 敏感配置（如 NGA cookies）放在 `secrets.json` 中；非敏感运行时设置放在 `config.json` 中。
- 除非用户明确要求修改已保存的主题数据，否则保留 `thread_configs.json`。

## 代码规范

- 为新添加或迁移的包代码添加类型注解。
- 保持 `nga_tools` 下的代码通过严格的 Pyright 检查。
- 优先使用职责清晰的小模块，避免向 `main.py` 添加过多逻辑。
- 在将代码迁移到包中时，保持网络访问、文件写入、HTML 转换、图片处理和 PDF 生成彼此分离。
- 除非用户明确要求行为变更，否则保持现有 CLI 行为不变。
