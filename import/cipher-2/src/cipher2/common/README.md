# common

## 路径职责

本包预留给跨模块共享的窄类型和常量。只有多个运行时模块都需要、且不属于任一业务模块所有权的定义，才能放在这里。

## 应放入这里的内容

- `JSONValue` 等跨模块基础类型。
- 与 `.cipher/` 路径安全相关、被多个模块复用的窄 helper。
- 不携带业务状态的常量。

## 不应放入这里的内容

- `initializer`、`storage`、`mcp` 或 `tools` 的业务逻辑。
- 宽泛 util 集合。
- 需要访问目标仓库文件系统的可变服务对象。

## 共享类型

`JSONValue` 是本仓公共 JSON 类型：

```python
JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
```

使用约束：

- `tools/log` 的 payload、`StorageError.details`、`LogError.details` 和 storage fact payload 都使用该类型。
- 不允许把 Python object、Path、bytes、datetime 或异常对象直接塞入 `JSONValue`；写入前必须转成 string、number、boolean、null、list 或 dict。
- 任何可能包含源码片段、secret 或巨大集合的字段必须先由所属模块执行 redaction/truncation。

## 验证要求

应包含 import smoke test，确认 `cipher2.common` 可被 `storage`、`tools/log` 和 `tools/views` 引用，且不会反向依赖业务模块。
