# Windows Trajectory Recorder V1

## 目标

在 Windows VM 内运行一个后台 Recorder，自动记录 computer use 的轨迹。

V1 采集：

* 鼠标、键盘
* 对应 UIA element
* screenshot + screen recording
* 当前活动窗口 / 软件
* terminal
* 文件变化
* 进程变化

暂不做 COM、软件专用 API、DLL injection、kernel hook。

## 输出

每次录制生成一个独立目录：

```
output/<episode_id>/
├── task.json
├── recording.mp4
├── trajectory.jsonl
├── trajectory.html
├── screenshots/
├── artifacts/
└── raw/
    ├── events.jsonl
    └── terminal/
```

其中：

* `task.json`：任务说明，由启动参数 `--task` 传入的文件复制而来，至少包含 `title`、`task_instruction`
* `trajectory.jsonl`：正式轨迹，下游只读这个文件
* `trajectory.html`：由 JSONL 自动生成，用浏览器直接查看截图、动作、事件和录像
* `artifacts/`：录制结束时，把监控目录中录制期间新建或修改过的文件复制到这里；单个文件超过 500MB 的跳过，只在 JSONL 事件中保留记录
* `raw/events.jsonl`：底层原始事件（mouse_down/up、key_down/up、UIA 查询结果等），仅用于调试和重新生成 trajectory.jsonl
* `raw/terminal/`：终端原始记录（如 PowerShell transcript）

`episode_id` 默认使用启动时间，如 `20260922_103215`。

`trajectory.jsonl` 必须可以由 `raw/` 重新生成：修改合并规则后，不需要重新录制。

## 轨迹结构

轨迹按照：

```
Observation → Action → Events → Next Observation
```

每一行 JSONL 是一个 step：

```
step_id: 12
t_ms: 1790052735123
timestamp: "2026-09-22T10:32:15.123+08:00"

observation:
  screenshot: "screenshots/000012.png"
  video_time: 30.2

  window:
    title: "Part1 - SOLIDWORKS"
    process: "SLDWORKS.exe"

  terminal:
    stdout: ""
    stderr: null

action:
  type: mouse_click
  button: left
  position: [812, 436]

  target:
    source: uia
    name: "Extruded Boss/Base"
    control_type: "Button"
    automation_id: "..."
    window: "SOLIDWORKS"
    bounding_rect: [780, 420, 860, 455]

events:
  files:
    - op: created
      path: "C:/task/a.txt"
      size: 20480

  processes:
    - op: start
      name: "python.exe"
      cmdline: "python test.py"
      pid: 1234
```

* `step_id` 从 0 开始连续编号。
* `t_ms` 是 Unix 毫秒时间戳，用于排序和计算；`timestamp` 是同一时刻的 ISO 8601 写法，带日期和时区，供人读。
* `observation` 表示 action 发生前的状态。
* `events` 表示这个 action 之后、下一个 action 之前发生的文件/进程变化；没有变化时为空列表。
* 坐标（`position`、`start`、`end`、`bounding_rect`）统一使用虚拟桌面的物理像素，与截图像素一一对应。

### 第一步与最后一步

**第一步（step 0）**：程序启动、各模块就绪后，先记录一次初始观测（截图、活动窗口），
再提示用户开始操作。

* step 0 的 `observation` 使用这次初始观测，而不是第一个动作发生时的截图
* step 0 的 `action` 为用户的第一个动作
* step 0 的 `events` 覆盖从录制开始到 step 1 之间的变化

**最后一步**：录制结束时截一张最终截图，作为最后一个 step：

* `observation` 为结束时的状态
* `action` 为 `null`
* `events` 为最后一个 action 之后到录制结束之间的变化

## Action Types

```
mouse_click
    button
    position
    target

mouse_double_click
    button
    position
    target

mouse_drag
    button
    start
    end
    target

scroll
    position
    dx
    dy
    target

key_press
    key

hotkey
    keys

type_text
    text
    target

shell_command
    command
```

字段约定：

* `button`：`left` / `right` / `middle`
* `dx` / `dy`：滚轮格数，向下为负；连续滚动（间隔 ≤ 400ms）合并为一次
* `key`：小写键名，如 `enter`、`tab`、`esc`、`f5`、`up`
* `keys`：小写列表，修饰键在前，顺序固定为 ctrl、alt、shift、win，如 `["ctrl", "shift", "s"]`

底层可以采集 `mouse_down/up`、`key_down/up`，但最终轨迹应合并成上面的 action。

例如：

```
CTRL down + S down + ... → hotkey ["ctrl", "s"]

h e l l o → type_text "hello"
```

合并规则：

* 按下与松开距离 ≤ 5px 为 click，否则为 drag
* 两次同按钮 click 间隔 ≤ 500ms 且距离 ≤ 5px 合并为 double_click
* 连续可打印字符合并为 type_text；间隔超过 1.5s 或遇到其他 action 时结束；输入过程中的退格删除末尾字符
* 按住 ctrl / alt / win 时按下其他键为 hotkey；单独按下的修饰键忽略
* 在 powershell 窗口内的输入不生成 type_text / key_press，按 enter 时合并为一条 shell_command

## UIA Target

鼠标操作发生时（按下的那一刻），根据坐标查询 UIA element：

```
target:
  source: uia
  name: "Save"
  control_type: "Button"
  automation_id: "..."
  window: "Notepad"
  bounding_rect: [x1, y1, x2, y2]
```

`type_text` 使用当前 focused UIA element。

查不到，或查询超过 2 秒：

```
"target": null
```

不能因此丢掉 action。

## File / Process

文件事件：

```
created
modified
deleted
renamed
```

* `renamed` 带 `path`（原路径）和 `new_path`（新路径）
* `deleted` 不带 `size`
* 同一文件在同一 step 内多次 modified 只保留最后一条

进程事件：

```
start
exit
```

* 只记录当前登录用户的进程；系统后台进程（svchost、conhost 等）不进轨迹，但保留在 `raw/events.jsonl`
* 进程可以用轮询方式（间隔约 500ms）检测，允许遗漏极短命的进程

文件监控目录必须通过参数配置（如 `--watch C:\task`），不要默认监听整个系统盘。

## Terminal

只支持 PowerShell，不考虑 cmd 和其他终端。

实现：提供一个安装脚本，在所有用户的 PowerShell profile 里加入
`Start-Transcript -IncludeInvocationHeader`，把每个 PowerShell 窗口的
命令和输出记录到 `raw/terminal/`。trajectory.py 解析 transcript 得到每条命令及其输出。

* 用户在 PowerShell 里按回车执行命令时，生成一个 `shell_command` action，命令内容从 transcript 中取
* 命令的输出写入下一个 step 的 `observation.terminal.stdout`
* transcript 无法区分 stdout 和 stderr，全部写入 `stdout`，`stderr` 为 `null`
* 录制过程中没有打开过 PowerShell 时，`observation.terminal` 为 `null`
* 录制开始时，Recorder 把当前 episode 路径写入 `C:\ProgramData\trajrec\current_episode.txt`，
  profile 钩子读取这个文件决定 transcript 写到哪里；录制结束时删除该文件。
* 只有录制开始后新打开的 PowerShell 窗口会被记录。

`observation.terminal` 只保存自上一个 step 后新增的输出，不重复保存完整历史。


## 运行约束

* 必须在已登录用户的桌面会话中以管理员身份运行，不能做成 Windows 服务（Session 0 看不到用户桌面）。
* 程序启动后第一件事是设置 DPI 感知（`SetProcessDpiAwareness(2)`），保证鼠标坐标、UIA 矩形、截图像素一致。
* 键鼠钩子回调里不做耗时操作；UIA 查询、截图都放后台线程，否则整个桌面会卡顿。
* 所有模块使用同一个时钟（Unix 毫秒）。
* 录屏用 ffmpeg（gdigrab），停止时正常结束 ffmpeg 进程，避免 mp4 文件损坏。

## 启动与停止

```
python main.py --output C:\traj --task task.json --watch C:\task
```

启动顺序：

1. 创建输出目录，启动所有采集模块（录屏、键鼠、窗口、文件、进程等）
2. 记录初始观测：截图并记录当前活动窗口
3. 在控制台打印提示：`[trajrec] 初始状态已记录，可以开始操作`
4. 此后的键鼠输入才作为 action 记录；提示出现之前的输入忽略


停止方式：

* Ctrl+C
* 创建文件 `C:\ProgramData\trajrec\STOP`（方便宿主机远程停止）

停止后自动生成 `trajectory.jsonl`、`trajectory.html`，并复制 `artifacts/`。

另外提供单独的生成入口，用于修改合并规则后从 `raw/` 重新生成：

```
python trajectory.py output/<episode_id>
```

## HTML

`trajectory.html` 必须可以直接双击打开。

双击打开是 `file://` 协议，浏览器不允许页面用 fetch 读取本地文件，所以生成 HTML 时把轨迹数据直接嵌入页面；截图和录像用相对路径引用。

按 step 顺序显示：

```
Step / Timestamp / Video time
Screenshot
Window
Terminal
Action
UIA target
File events
Process events
```

与真实操作顺序对应。

* 在截图上标出点击位置和 target 的 bounding_rect
* 点击 video time 可跳转到录像对应位置

## 代码结构

不要一个文件写完所有功能。

* 各采集模块负责自己的数据（如 `input_recorder.py`、`uia.py`、`screen.py`、`window.py`、`terminal.py`、`files.py`、`processes.py`），只往 `raw/` 写原始事件
* `trajectory.py` 负责把 `raw/` 数据组织成完整的 JSONL，以及生成 HTML
* `main.py` 负责启动、停止和协调

保持代码简单、人类可读、方便单独修改和测试。

## 验收

当前开发环境为 macOS，Windows-specific 功能无法在本地真实验证。必须提供独立测试入口和 Windows VM 测试说明，不得假设或声称 Windows 测试已通过。
测试说明：

* 该脚本完成什么功能
* 如何测试

同时提供：
- requirements.txt：全部 Python 依赖
- README.md：安装、启动、停止、测试方法
