# Software Trajectory Collector Specification v1

## 1. 范围

适配器（Adapter）是一个 Python 模块，连接一个专业软件，向 Recorder 提供三类记录：

| 记录 | 内容 | 并入轨迹的位置 |
| --- | --- | --- |
| state | 软件当前状态 | `observation.software_state` |
| action | 用户或脚本发起的指令 | `action.software_actions`（列表，一次操作可能触发多条） |
| event | 软件产生的结果 | `events.software` |

适配器不独立运行，由 Recorder 加载并调用。Recorder 按时间戳把记录并入 `trajectory.jsonl`。

适配器只使用软件官方接口：COM、插件 API、官方脚本接口，或软件自身输出的日志、journal、命令行历史。不使用 DLL 注入或 hook。

## 2. 交付物

```
adapters/<software>/
├── adapter.py          实现第 3 节的接口，并提供第 3.3 节的调试入口
├── capabilities.yaml   声明本适配器返回的所有字段与类别（第 5 节）
├── README.md           依赖、软件版本、需要的软件设置、已知限制、拿不到的信息
└── requirements.txt
```

## 3. 接口

### 3.1 方法

```python
class Adapter:
    NAME = "AutoCAD"                # 写入每条记录的 software 字段
    PROCESS_NAMES = ["acad.exe"]    # 前台进程匹配到时启用本适配器
    SPEC_VERSION = "0.3"

    def attach(self, ctx) -> bool:
        """连接已在运行的软件。成功返回 True，失败返回 False。
        ctx.log(msg) 记录调试信息。"""

    def detach(self) -> None:
        """断开连接，释放资源。"""

    def get_state(self) -> dict | None:
        """查询并返回当前状态，格式见第 4.1 节。取不到返回 None。"""

    def get_actions(self) -> list[dict]:
        """返回自上次调用以来新产生的 action 记录，格式见第 4.2 节。
        没有则返回空列表。已返回过的记录不得重复返回。"""

    def get_events(self) -> list[dict]:
        """返回自上次调用以来新产生的 event 记录，格式见第 4.3 节。
        没有则返回空列表。已返回过的记录不得重复返回。"""
```

所有方法返回 Python 的 dict / list，由 Recorder 序列化为 JSON。

### 3.2 调用方式与耗时上限

由 Recorder 调用，适配器不自行安排定时任务。

| 方法 | 调用时机 | 耗时上限 | 超时处理 |
| --- | --- | --- | --- |
| attach | 匹配的进程首次出现在前台时 | 10 s | 标记不可用，30 s 后重试一次 |
| get_state | Recorder 采集 observation 时；空闲时每 5 s 一次 | 500 ms | 本次记为 null |
| get_actions | 每次 GUI 动作或 shell 命令之后；另每 500 ms 一次；录制结束前一次 | 200 ms | 丢弃本次结果 |
| get_events | 同上 | 200 ms | 丢弃本次结果 |
| detach | 录制结束或软件退出时 | 5 s | 强制结束 |

`get_state` 是一次查询，返回调用时刻的状态。

`get_actions` 和 `get_events` 是取走队列：软件产生的记录先在适配器内部排队，调用时一次性取走。因此**记录的 `t_ms` 必须在事件产生的第一时间打上**（如 COM 回调内），不得等到被调用时才生成。取走的时刻不影响记录的时间戳。

从日志或 journal 解析出的记录，日志中有时间则用日志时间；没有则用读到该行的时间，并在 `capabilities.yaml` 中注明时间精度较差。

### 3.3 调试入口

`adapter.py` 需支持直接运行，用于开发和排查问题，不是运行方式：

```
python -m adapters.<software> --probe    连接软件，打印一次 get_state() 结果及耗时
python -m adapters.<software> --watch    每 500 ms 调用 get_actions() 和 get_events()，持续打印
```

## 4. 记录格式

### 4.1 state

必须包含以下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| t_ms | int | Unix 毫秒时间戳（查询时刻） |
| software | string | 与 `NAME` 一致 |
| active_document | string \| null | 当前文档的绝对路径；未保存的新文档填标题；无文档填 null |
| selection | list | 当前选中对象；元素格式由适配器定义 |
| mode | string \| null | 当前模式；取值由适配器定义 |

其余字段由适配器自行决定，与上述字段写在同一层。

举例：

```json
{
  "t_ms": 1790052735123,
  "software": "AutoCAD",
  "active_document": "C:/task/Drawing1.dwg",
  "selection": ["Line(2A3F)", "Circle(2A41)"],
  "mode": "command_active",
  "active_layer": "0",
  "ucs": "WORLD",
  "entity_count": 42
}
```

```json
{
  "t_ms": 1790052735123,
  "software": "SolidWorks",
  "active_document": "C:/task/Part1.SLDPRT",
  "selection": ["Front Plane"],
  "mode": "sketch",
  "feature_tree": ["Front Plane", "Top Plane", "Sketch1"],
  "units": "MMGS"
}
```

要求：

- 字段集合固定。某次取不到的字段填 `null`，不得省略。
- `null` 表示取不到；空列表或空字符串表示确实为空。
- 跨软件语义相同的字段使用相同名称，如 `active_layer`、`units`、`active_command`。
- 每个字段在 `capabilities.yaml` 中声明。

### 4.2 action

用户或脚本发起的指令。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| t_ms | int | 指令发起时刻 |
| software | string | 与 `NAME` 一致 |
| type | string | `command`（软件命令）或 `api_call`（API / COM 调用） |
| name | string | 命令名或方法名，保持软件原名 |
| params | object | 参数；无参数填 `{}` |
| source | string | 数据来源，如 `com_event` / `commandline` / `journal` / `log` |
| raw | string | 原始文本或事件参数 |

```json
{
  "t_ms": 1790052735123,
  "software": "AutoCAD",
  "type": "command",
  "name": "EXTRUDE",
  "params": {"height": 10},
  "source": "commandline",
  "raw": "Command: EXTRUDE  Specify height of extrusion: 10"
}
```

### 4.3 event

软件产生的结果。字段与 action 相同，`type` 取第 4.4 节的类别。

```json
{
  "t_ms": 1790052736400,
  "software": "SolidWorks",
  "type": "parameter_changed",
  "name": "D1@Sketch1",
  "params": {"from": 10, "to": 20, "unit": "mm"},
  "source": "com_event",
  "raw": "DimensionChangeNotify: D1@Sketch1 10->20"
}
```

### 4.4 event 的 type

能拿到对应事件时，优先使用下列名称：

**通用**

```
document_opened  document_created  document_saved  document_closed
command_executed  undo  redo
solve_started  solve_finished
error_raised
```

**CAD / 建模类**

```
object_created  object_modified  object_deleted
feature_added  parameter_changed  selection_changed
sketch_entered  sketch_exited  layer_changed  view_changed
```

**仿真类**

```
model_loaded  mesh_generated  material_assigned
boundary_condition_set  solver_configured  result_exported
```

表中没有的事件，自行命名（小写下划线风格，建议保留软件原生语义），并在 `capabilities.yaml` 中声明。不要把语义不同的事件塞进上表的名称。

### 4.5 params

参数内容由适配器定义，并在 `capabilities.yaml` 中声明。三条统一约定：

- 变更类事件用 `from` 和 `to`
- 路径用 `path`，绝对路径
- 数值带单位：增加 `unit` 字段，或在字段名中带单位后缀（如 `depth_mm`）

### 4.6 划分 action 与 event

| 内容 | 归属 |
| --- | --- |
| 用户或脚本发起的指令 | action |
| 软件回应的结果 | event |
| 提示、回显、无内容的行 | 不返回 |

journal 和命令行日志中两类内容混杂，需逐行拆分：

- 能确认是主动指令 → action
- 能确认是软件结果 → event
- 无法判断 → 默认不返回，不得归入 action 或 event


## 5. capabilities.yaml

声明本适配器返回的全部内容。Recorder 侧据此验证适配器是否按声明工作。

```yaml
software: AutoCAD
spec_version: "0.3"
software_version: "AutoCAD 2024"
connection: "win32com GetActiveObject('AutoCAD.Application')"

state:
  active_document:
    type: string|null
    source: "ActiveDocument.FullName"
    meaning: "当前图纸绝对路径"
  selection:
    type: list
    source: "ActiveDocument.PickfirstSelectionSet"
    meaning: "当前选中实体，元素格式 ObjectName(Handle)"
  mode:
    type: string
    source: "系统变量 CMDACTIVE"
    values: ["idle", "command_active"]
    meaning: "是否有命令正在执行"
  active_layer:
    type: string
    source: "ActiveDocument.ActiveLayer.Name"
    meaning: "当前图层"
  entity_count:
    type: int
    source: "ModelSpace.Count"
    meaning: "模型空间实体总数"

actions:
  command:
    source: "COM 事件 BeginCommand"
    params:
      command: {type: string, meaning: "命令名，大写"}
      height: {type: number, meaning: "部分命令可从命令行日志解析出的参数"}

events:
  object_created:
    source: "COM 事件 ObjectAdded"
    params:
      handle: {type: string, meaning: "实体句柄，图纸内唯一"}
      object_type: {type: string, meaning: "实体类型，如 AcDbLine"}
  document_saved:
    source: "COM 事件 EndSave"
    params:
      path: {type: string, meaning: "保存的绝对路径"}
  lisp_ended:
    source: "COM 事件 LispEnded"
    params:
      function: {type: string, meaning: "执行完成的 LISP 函数名"}
```

每个 state 字段须有 `type`、`source`、`meaning`；每个 action / event 须有 `source` 和 `params`；每个 param 须有 `type` 和 `meaning`。时间精度较差的来源需注明。

## 6. 约束

- **只读**：不得选中对象、切换文档、改变视角、执行命令、修改系统变量或软件设置。
- **不阻塞**：适配器运行在 Recorder 分配的独立线程中，COM 初始化在 `attach` 内完成。
- **不抛异常**：内部捕获所有异常，转为返回 `None` 或空列表，并用 `ctx.log` 记录。
- **不写盘**：记录由 Recorder 写入文件；适配器不自行创建文件或目录。
- **时间戳**：`t_ms` 使用 `time.time_ns() // 1_000_000`，在事件产生的第一时间打上。
- **限流**：
  - 单次 `get_events()` 最多返回 200 条，其余留在内部队列，下次调用继续返回。
  - 不得合并语义不同的事件。参数不同的同类事件（如 `10 → 20` 与 `20 → 30`）是不同事件，必须各自返回。
  - 仅当同一对象、同一类型且参数完全相同的事件连续重复时，可合并为一条，并在 `params` 中记 `repeat` 次数。
  - 内部队列长度超过 2000 条时，对超出部分按事件类型聚合为一条，`params` 中记 `aggregated` 数量及起止时间，并用 `ctx.log` 告警。

## 7. 验证

接入后按 `capabilities.yaml` 逐项验证。在目标软件中依次操作，对应记录应当出现：

1. 新建文档 → `document_created`
2. 创建一个对象 → `object_created` 或 `feature_added`
3. 修改一个参数 → `parameter_changed`，params 含 `from` 和 `to`
4. 选中一个对象 → `selection_changed`，且 `get_state()` 的 `selection` 同步变化
5. 保存 → `document_saved`，`params.path` 正确
6. 关闭文档 → `document_closed`
7. 批量操作（阵列、批量修改）→ 事件分批返回，语义不同的事件未被合并，软件无明显卡顿
8. 关闭软件 → 适配器不崩溃，后续调用返回 None 或空列表
9. `get_state()` 耗时在 500 ms 以内，软件操作手感与未接入时一致
10. 每条记录的 `t_ms` 与操作发生的实际时刻相符

`capabilities.yaml` 中未声明的类别不做要求；已声明但未出现的，在 README 中说明触发条件。