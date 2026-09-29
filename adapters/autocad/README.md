# AutoCAD 适配器

按 [适配器规范](../software_trajectory_collector_specification.md) 实现,连接**已在运行**的 AutoCAD,只读地提供 state / action / event 三类记录。设计细节、每个字段的来源和逐项验收表见 [task-adapter-autocad.md](task-adapter-autocad.md)。

> **当前状态：Windows 实测中，事件订阅成功后运行 `--watch` 并绘制 LINE 导致 AutoCAD 访问冲突崩溃（AC-005）。当前已暂停全部 COM 事件订阅，固定使用 B 级（状态查询＋日志），适用于独立调试和 Recorder。** 这只是临时隔离，崩溃根因和降级后的稳定性尚未实测确认；不能宣称适配器验收通过。基础 state 测试此前由用户报告通过，当前 7 项离线回归通过。详见 [测试记录](windows-test-notes.md)。

## 文件

| 文件 | 内容 |
| --- | --- |
| `adapter.py` | `Adapter` 类(`NAME="AutoCAD"`、`PROCESS_NAMES=["acad.exe"]`、`SPEC_VERSION="1"`)与 `--probe` / `--watch` 调试入口 |
| `capabilities.yaml` | 返回的全部字段、action、event 及其来源 |
| `requirements.txt` | `pywin32`、`psutil`(Recorder 已依赖这两个包,不引入新包) |
| `__init__.py`、`__main__.py` | 仅为 `python -m adapters.autocad ...` 可用 |
| `task-adapter-autocad.md` | 开发文档 |

## 依赖与版本

- Windows,Python 与 Recorder 相同;`pip install -r adapters/autocad/requirements.txt`(装过 Recorder 的依赖就不用再装)。
- **不需要**运行 pywin32 的 makepy,适配器不生成也不读取 `gen_py` 缓存。
- 目标版本:**AutoCAD 2026 简体中文版**(R25.1)。连接时依次尝试 `AutoCAD.Application.25.1`、`AutoCAD.Application`、`AutoCAD.Application.25`、`AutoCAD.Application.24.x`,所以 2021–2025 理论上也能连,但不在验收范围。
- 不支持 AutoCAD LT(没有 COM 接口)。

## AutoCAD 侧需要的设置

1. **打开命令行日志**(用于"命令取消""未知命令""LISP 报错"三类事件;不开也能用,只是没有这三类)。以将来运行 AutoCAD 和 Recorder 的那个 Windows 用户操作,做一次即可,设置保存在注册表里,重启 AutoCAD 后仍有效:
   - 命令行输入 `LOGFILEMODE` 回车,再输入 `1` 回车;或者
   - `OPTIONS` →"打开和保存"选项卡 →"文件安全措施"→ 勾选"维护日志文件"→ 确定。

   确认方法:输入 `LOGFILEMODE` 显示 `1`,输入 `LOGFILENAME` 显示当前图纸日志文件的路径。日志会一直变大,可以定期删除日志目录(`OPTIONS` →"文件"→"日志文件位置")里的旧 `.log` 文件,但不要在录制过程中删。换 Windows 用户或切换 AutoCAD 配置后要重新设置。
2. **AutoCAD 与 Recorder 以相同权限运行**:都用普通权限,或都"以管理员身份运行"。权限不同时 COM 连不上(`adapters.log` 里会有提示)。
3. **同一时间只开一个 AutoCAD**。开了多个实例时,COM 只能连接到第一个启动的实例;前台是另一个实例时适配器不会连接,以免采到错误实例的数据。

适配器自己不修改任何 AutoCAD 设置或系统变量。

## 启用

```powershell
python main.py --output C:\traj --task task.json --watch C:\task                    # 默认 --adapters auto,会自动加载本适配器
python main.py --output C:\traj --task task.json --watch C:\task --adapters autocad  # 只加载本适配器
```

`acad.exe` 到前台时 Recorder 调用 `attach()`。attach 成功后,`raw/adapters.log` 里会有一行汇总:

```
[AutoCAD] attach 成功: AutoCAD <Application.Version> PID <pid> level=A events=on log=on locale=CHS documents=1
```

## 运行级别

| 级别 | 条件 | 能得到的 |
| --- | --- | --- |
| A | COM 事件订阅成功且在正常送达 | 完整 state;命令开始(精确时间);命令完成、撤销/重做、对象增删改、选择变化、文档新建/打开/保存/关闭/切换、图层/布局/系统变量变化;日志开着时另有取消与报错 |
| B | 连上了 AutoCAD,但事件不可用 | 完整 state;日志开着时:命令开始(读日志的时刻,精度差)、取消、报错;日志没开则只有 state |

**什么情况会从 A 降到 B**(`adapters.log` 里会写 `切换到 level B` 或 `events=off(原因)`):

- 适配器线程无法初始化为 MTA(`RPC_E_CHANGED_MODE`);
- AutoCAD 类型库里找不到事件接口,或 `Advise` 失败;
- 运行时自检:attach 之后 `get_state` 读到 2 个不同的新命令正在运行(`CMDNAMES`),却一次 `BeginCommand` 事件都没收到。

## 采集内容

完整声明见 [capabilities.yaml](capabilities.yaml)。

- **state**:`active_document`、`selection`(pickfirst 选择集,`AcDbLine(2A3F)` 格式,最多 50 项)、`selection_count`、`selection_truncated`、`mode`(`idle` / `command_active` / `transparent_command` / `script` / `dialog` / `arx_command` / `lisp` / `no_document` / `busy`)、`active_command`、`active_layer`、`active_layout`、`ucs`、`units`、`entity_count`、`document_count`、`unsaved_changes`、`read_only`、`view_center`、`view_height`。
- **action**:`command`(`BeginCommand`;LISP 自定义命令 `(C:XXX)`;级别 B 下日志中的命令回显)、`api_call`(其他 LISP 表达式)。
- **event**:`command_executed`、`undo`、`redo`、`lisp_ended`、`lisp_cancelled`、`document_created`、`document_opened`、`document_saved`、`document_closed`、`document_activated`、`object_created`、`object_modified`、`object_deleted`、`selection_changed`、`layer_changed`、`layout_switched`、`sysvar_changed`、`command_cancelled`、`error_raised`。

`mode = "busy"` 表示 AutoCAD 主线程 100 ms 内没有响应,或拒绝了 COM 调用(比如打开大图纸、弹出模态对话框时)。这一次不再查询,其余字段为 `null`。这样查询不会一直等下去,不会连续超时导致 Recorder 停用适配器。

## 拿不到的信息

- **`parameter_changed`(带 `from`/`to`)**:COM 的 `ObjectModified` 只告诉你哪个对象变了,不告诉改了哪个属性、原值是什么。实体属性修改只能以 `object_modified` 体现;系统变量修改以 `sysvar_changed`(含 `from`/`to`)体现。
- **`view_changed`**:没有对应的 COM 事件。视图只在 state 的 `view_center` / `view_height` 里;`ZOOM` / `PAN` 命令以 action 出现,鼠标滚轮缩放、平移不经过命令,不会出现。
- **命令参数**:COM 来源的 `command` 的 `params` 恒为 `{}`。鼠标拾取的点坐标拿不到。日志里"提示: 输入"行(如 `指定第一个点: 0,0`)的解析**暂未启用**,要等拿到真实日志样例、核对格式后再打开(见下文)。
- **其他程序的 API 调用**:别的 COM / .NET / ObjectARX 程序调用了 AutoCAD 的什么方法,COM 不会通知。它们通过 `SendCommand` 执行的命令会以 `command` 出现,但无法和用户命令区分。
- **"选择对象:"提示下的选择**:不属于 pickfirst 选择集,`selection` 和 `selection_changed` 都不反映。`PICKFIRST=0` 时 `selection` 恒为空。
- **命令失败 / 未知命令 / 取消**:COM 没有这些事件,只能靠命令行日志(需 `LOGFILEMODE=1`)。
- **新建/打开图纸后的短暂空窗**:要等下一次 `get_actions` / `get_events`(Recorder 每次键鼠操作后都会调)订阅上该图纸,之前的对象事件收不到。

## 可能不出现的事件及触发条件

`capabilities.yaml` 中声明了、但只在特定条件下才出现的:

| 事件 | 触发条件 |
| --- | --- |
| `undo` | `U`(Ctrl+Z 预期也是 `U`),或 `UNDO` 命令执行期间确实发生了对象变化;`UNDO` 的"标记""开始""结束"等选项不算 |
| `redo` | `REDO` / `MREDO` |
| `document_created` | `NewDrawing` 之后 10 s 内枚举到新的未保存图纸;从"开始"选项卡或 `+` 新建是否触发 `NewDrawing` 待验证 |
| `document_saved` | 保存为 `.dwg/.dxf/.dwt/.dws`;自动保存(`.sv$`)不返回 |
| `document_closed` | `BeginClose`;"是否保存"对话框里点"取消"时是否仍触发待验证 |
| `layer_changed` | 当前图层改变(`CLAYER`) |
| `layout_switched` | 切换模型 / 布局选项卡 |
| `lisp_ended` / `lisp_cancelled` / `api_call` | 执行 LISP 表达式或 LISP 自定义命令 |
| `command_cancelled` / `error_raised` | `LOGFILEMODE=1` 且 `LOCALE` 为 `CHS` / `ENU` / `EN`;按实际行内容识别中英文。中文转义命令/取消已用真实样例离线核对，未知命令已用更正后的真实样例离线核对，LISP 报错和实时送达仍待实测 |

## 调试入口

在项目根目录运行:

```powershell
python -m adapters.autocad --probe              # 连接,打印一次 get_state()、耗时、每个字段的耗时,以及只读检查(SelectionSets.Count / DBMOD 前后对比)
python -m adapters.autocad --probe --repeat 100 # 连续 100 次 get_state(),看耗时范围和只读检查
python -m adapters.autocad --watch              # 每 500 ms 打印 get_actions()/get_events(),每 5 s 打印一行 state 摘要;Ctrl+C 结束时打印统计
```

`--watch` 每条记录前的 `lag=` 是打印时刻减去 `t_ms`。COM 来源的记录应在 0–600 ms 之间(由 500 ms 轮询决定),说明时间戳是在事件发生时打上的;日志来源的记录接近 0。

## 首次在虚拟机上验证

1. 按上文设置 `LOGFILEMODE=1`,打开 AutoCAD 2026,新建一张图纸。
2. 在项目根目录运行 `python -m adapters.autocad --probe`,把完整输出保存下来。检查 `attach -> True`、`level` 为 `A`、`get_state` 耗时小于 500 ms,且只读检查前后数值不变。
3. 运行 `python -m adapters.autocad --watch`,按 [task-adapter-autocad.md](task-adapter-autocad.md) 第 10 节的表格逐项操作,保存完整输出。
4. 操作结束后,把命令行输入 `LOGFILENAME` 显示的那个日志文件**原样拷贝**出来(直接复制文件,不要用记事本打开再另存,以保留原编码)。
5. 把第 2–4 步的输出和日志文件交回。据此修正中文日志规则、打开"提示: 输入"解析,并把实测结果(哪些事件不触发、系统变量噪声、耗时)写回本 README 和 `capabilities.yaml`。

## 排查

1. `adapters.log` 里没有 `attach 成功`:看它前面那行原因。`未找到运行中的 AutoCAD` → AutoCAD 仍在启动、权限不同或是 LT 版;`前台 acad.exe (PID x) 不是 COM 能连接到的实例` → 开了多个 AutoCAD;`导入 pywin32 失败` → 装依赖。Recorder 失败后 30 s 重试,共 3 次。
2. `level=B`:看 `events=off(...)` 或 `切换到 level B` 后面的原因。
3. `log=off(...)`:`LOGFILEMODE=0` → 按上文打开;`LOCALE=xxx 没有对应的日志解析规则` → 界面语言不是简体中文或英文。
4. state 频繁 `mode=busy`:AutoCAD 正忙或开着模态对话框,属正常;对话框关闭后应恢复。
5. 大量 `object_created` 的 `name` 为 `unresolved`:对象在读取前已被删除,或 AutoCAD 持续忙超过 5 s。
6. `adapters.log` 出现 `队列超过 2000 条`:Recorder 两次调用之间事件太多(比如脚本批量操作而没有键鼠操作),超出部分已按类型聚合。

## 开发说明

- `adapter.py` 顶部集中放了可调常量:`SELECTION_LIMIT`、`STATE_BUDGET_S`、`PROBE_TIMEOUT_MS`、`SYSVAR_IGNORE`、`LOG_PATTERNS`、`LOG_INPUT_RULE`、`LOG_ANSI_ENCODING` 等。改了影响输出的常量,要同步改 `capabilities.yaml` 和本 README。
- 模块顶层只导入标准库。pywin32 在 `attach()` 里、在适配器线程上导入,并临时设置 `sys.coinit_flags = 0`,避免首次 `import pythoncom` 把该线程初始化成 STA。
- 事件回调运行在 COM 的 RPC 线程上,只做"打时间戳 + 追加到 deque"。所有对 AutoCAD 的查询都在适配器线程里做,包括读取新对象的 `ObjectName` / `Handle`。

## 2026-09-29 订阅修复后的历史复测（已因 AC-005 暂停）

更新 Windows 上的 `adapter.py` 后，在 PowerShell 执行：

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
python -m adapters.autocad --probe 2>&1 | Tee-Object -FilePath autocad-probe-fixed.txt
python -m adapters.autocad --watch 2>&1 | Tee-Object -FilePath autocad-watch-fixed.txt
```

CLI 自行设置 UTF-8 输出，PowerShell 仍需对应解码。上述为 AC-005 发生前的测试命令；现在不要继续旧版 events=on 的 watch 测试。当前 probe 应为 level=B / events=off(AC-005 原因)，日志已开启时 log=on。保留已有崩溃前终端输出用于诊断。

日志先解析 `\U+XXXX` 转义，再识别中英文提示，raw 保留原行；取消优先于命令识别。EN 不等同于英文日志：更正后的 EN 样例含中文转义和 cp1252 引号，EN 的非 UTF-8 回退使用 cp1252；旧样例中的第三方插件 GBK 文本可能乱码，不参与命令匹配。命令参数解析保持关闭。

离线回归：`python -m unittest adapters.autocad.test_regressions -v`。用户更正后的样例包含 ZZZ_NONEEXISTENT 未知命令和 LINE 取消，二者均已通过离线事件生成检查；实时采集仍需 Windows 复测。

## AC-005 临时隔离的能力边界

`COM_EVENTS_ENABLED=False`：attach 不订阅 Application / Document 事件，`_advise` 也拒绝订阅。没有新增 Recorder 接口、后台线程或对 AutoCAD 的写操作。原有 COM 事件实现保留待诊断，不能因 constants 可编辑就视为已验证可重新启用。

当前可用路径：state 查询；日志已启用时的 command、command_cancelled、error_raised（时间戳取读到日志时刻）。不会产生 COM 来源的对象、选择、文档生命周期、command_executed、undo/redo 或 LISP 事件，也不提供 api_call。日志实时刷新和降级后的稳定性仍待 Windows 验证。
