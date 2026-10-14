# SageQL core 依赖

factory-agent 通过项目内 wheel 依赖使用 SageQL core，不在运行时引用相邻源码目录。

- 包：`sageql-core==0.1.0`，协议版本 `1`。
- 来源：`C:/codes/sageql/packages/sageql-core`。
- 文件：`sageql_core-0.1.0-py3-none-any.whl`。
- SHA-256：`845880220c310d1cafafa7152a289c28af3a0ed4232bd605d5540387da2f4e67`。
- 许可：MIT；wheel 内含许可和 `py.typed`。
- 声明 Python 范围：`>=3.9`；仅依赖 Pydantic。factory-agent 使用 Python 3.12。
- 本地执行验证使用 Python 3.12.14；Python 3.9 已做语法和严格类型检查，未使用真实 3.9 解释器运行。

`pyproject.toml` 和 `uv.lock` 锁定此文件及其摘要，Docker 构建复制同一 wheel。
正常安装使用 `uv sync --frozen`。更新时在 SageQL 仓库构建新 wheel，更新文件与本记录，
再由 uv 生成锁文件并运行独立消费与宿主回归；不手工改锁文件摘要。
