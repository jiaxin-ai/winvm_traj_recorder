# Windows Trajectory Recorder V1.2：软件适配器接入

在 V1（通用轨迹采集）和 V1.1（文件快照）基础上增量开发，已有行为保持不变。

**本版只做一件事：为软件适配器留出接入点。** 适配器本身不在本版范围——没有适配器时，Recorder 行为与 V1.1 完全一致，轨迹中新增的三个字段为 `null` 或空列表。

适配器一侧的接口与数据格式见 `adapters/software_trajectory_collector_specification.md`（v1），本文档只讲 Recorder 这一侧怎么改。两者冲突时以 `software_trajectory_collector_specification.md` 为准。

## 一、总体

新增一个模块 `software.py`，负责加载适配器、调用其方法、把返回的记录写入 `raw/`。已有的采集模块只在自己原有的采集点上多通知一次，不直接调用适配器。

## 二、加载与连接

```
adapters/<software>/
├── adapter.py          含 Adapter 类
└── capabilities.yaml
```

启动时扫描 `adapters/` 各子目录，导入 `Adapter`，读取 `NAME`、`PROCESS_NAMES`、`SPEC_VERSION`。导入失败则跳过并打印警告。

参数 `--adapters`：`auto`（默认，按前台进程自动匹配）、`none`（禁用）、或指定名称列表如 `mock,autocad`。

连接与断开：

- 窗口追踪报告的前台进程名匹配到某适配器的 `PROCESS_NAMES` 时，调用 `attach(ctx)`
- `attach` 成功后**立刻调用一次 `get_state()`**，作为该软件出现后第一个 step 的观测
- 返回 `False` 或超时 → 30 s 后重试，连续 3 次失败则本次录制不再重试
- 对应进程退出 → 调用 `detach()`；进程再次出现时重新 `attach()`
- 录制结束 → 对所有已连接的适配器调用 `detach()`

`ctx` 提供 `ctx.episode_dir` 和 `ctx.log(msg)`，日志写入 `raw/adapters.log`。

### 隔离

每个适配器一个独立线程，所有方法都在该线程中调用（COM 要求同一线程）。调用方投递请求后等待结果，超过上限即放弃，不阻塞采集：

| 方法 | 上限 | 超时处理 |
| --- | --- | --- |
| attach | 10 s | 见上 |
| get_state | 500 ms | 本次记为 `null` |
| get_actions / get_events | 200 ms | 丢弃本次结果 |
| detach | 5 s | 强制结束线程 |

适配器抛异常时捕获并记入 `adapters.log`，结果按空处理。**连续 10 次超时或异常则停用该适配器本次录制**，录制继续。

## 三、调用时机

不新增任何采集时机，跟随 Recorder 已有的采集点：

| 方法 | 跟随的采集点 |
| --- | --- |
| `get_state()` | 与 observation 截图完全相同的时机 |
| `get_actions()` / `get_events()` | 与主轨迹采集 action 和 event 的时机相同 |

另外两条：

- `attach()` 成功后立刻调用一次 `get_state()`
- 录制结束前最后调用一次 `get_actions()` 和 `get_events()`，否则最后一个动作产生的记录会丢

同一适配器的调用串行执行，不得并发。

`get_actions()` / `get_events()` 取走的是适配器内部队列，记录的 `t_ms` 由适配器在事件产生时打上，与取走时刻无关，因此归并不受调用时机影响。

## 四、raw 新增文件

```
raw/
├── software_state.jsonl      每次 get_state() 的返回值，原样写入
├── software_actions.jsonl    每次 get_actions() 返回的记录，逐条写入
├── software_events.jsonl     每次 get_events() 返回的记录，逐条写入
├── adapters.log              日志、超时、异常、停用记录
└── adapters/                 启动时复制各适配器的 capabilities.yaml 到此
```

记录原样写入，不修改适配器返回的任何字段。缺少 `t_ms` 或格式非法的记录丢弃，并在 `adapters.log` 中记原因。`get_state()` 返回 `None` 时不写入。

`meta.json` 增加：

```json
{
  "adapters": [
    {"name": "AutoCAD", "spec_version": "0.3", "attached": true,
     "timeouts": 2, "errors": 0, "disabled": false}
  ]
}
```

## 五、trajectory.jsonl 新增字段

```yaml
observation:
  screenshot: ...
  window: ...
  terminal: ...
  software_state:              # 新增；取不到为 null
    software: "AutoCAD"
    active_document: "C:/task/Drawing1.dwg"
    selection: ["Line(2A3F)"]
    mode: "command_active"
    active_layer: "0"

action:
  type: mouse_click
  ...
  software_actions: []         # 新增；列表

events:
  files: []
  processes: []
  software: []                 # 新增；列表
```

归并规则与已有字段一致。记本 step 的动作时刻为 `T`，下一 step 为 `T_next`：

| 字段 | 规则 |
| --- | --- |
| `observation.software_state` | 取与本 step 的 observation 截图时刻最接近的一条；相差超过 3 s 则为 `null` |
| `action.software_actions` | `t_ms` 落在 `[T - 300ms, T_next)` 的全部 action 记录，按 `t_ms` 升序 |
| `events.software` | `t_ms` 落在 `[T, T_next)` 的全部 event 记录，按 `t_ms` 升序 |

向前容忍 300 ms 是因为软件内部记录的时间戳可能略早于鼠标松开时刻。最后一个 step 的窗口截止到录制结束。

多软件同时运行时：`software_state` 只取当前前台软件对应适配器的记录；`software_actions` 和 `events.software` 保留所有适配器的记录，每条自带 `software` 字段。

没有适配器时三个字段分别为 `null`、`[]`、`[]`。`trajectory.html` 中相应展示，为空时不显示该区块。

## 六、mock 适配器

用于在没有专业软件的环境下验证接入是否正确，也是后续开发真适配器的最小样例。

`adapters/mock/`：

- `NAME = "MockCAD"`，`PROCESS_NAMES = ["notepad.exe"]`
- `get_state()`：返回固定结构，`active_document` 取记事本窗口标题
- `get_actions()` / `get_events()`：按 `SPEC.md` 格式产生若干条记录，`t_ms` 为产生时刻
- 环境变量 `MOCK_FAULT` 注入故障，用于验证隔离：`slow`（get_state 睡 2 s）、`crash`（抛异常）、`flood`（单次返回 5000 条事件）
- 附 `capabilities.yaml`，内容与实际返回一致

## 七、代码结构

新增 `software.py` 和 `adapters/mock/`。

修改：

- `main.py`：解析 `--adapters`，启停 `software.py`，把适配器状态写入 `meta.json`
- `window.py`：前台进程变化时通知 `software.py`
- 现有的 observation 采集点和 action / event 采集点：各多通知一次 `software.py`
- `trajectory.py`：生成三个新字段并在 HTML 中展示

## 八、验收

在 Windows VM 上验证。

**`--adapters none`**

1. 全流程与 V1.1 一致，`raw/` 下不产生 software 相关文件
2. `software_state` 为 `null`，另两个字段为 `[]`，HTML 不显示对应区块

**mock 适配器（打开记事本）**

3. 记事本出现在前台 → `adapters.log` 显示 attach 成功，且立刻产生一条 state 记录
4. 操作若干步 → `raw/` 下三个 jsonl 均有内容，`raw/adapters/mock.capabilities.yaml` 存在
5. 每个 step 的 `software_state` 有值；产生记录的步骤 `software_actions` 和 `events.software` 有对应内容
6. 时间归属正确：某一步之后立刻产生的事件落在该步的 `events.software`，而不是下一步
7. 关闭记事本 → `detach()` 被调用，后续 `software_state` 为 `null`，录制继续；重新打开后恢复

**故障注入**

8. `MOCK_FAULT=slow` → `get_state()` 超时，该步 `software_state` 为 `null`，录制无卡顿
9. `MOCK_FAULT=crash` → 异常被捕获，连续 10 次后适配器停用，`meta.json` 中 `disabled: true`，录制继续
10. `MOCK_FAULT=flood` → Recorder 不卡顿，轨迹正常生成

## 九、交付

- 更新 `README.md`：适配器如何放置与启用、`--adapters` 用法、三个新字段的含义、mock 适配器与故障注入开关、适配器不工作时如何排查
- 当前开发环境为 macOS，Windows 相关部分不得假设或声称已测试通过；说明哪些部分可用 mock 适配器在本地验证