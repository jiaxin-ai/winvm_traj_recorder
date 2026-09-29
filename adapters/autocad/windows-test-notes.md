# Windows + AutoCAD 首轮测试记录

记录日期：2026-09-29。证据来自用户在对话中提供的 Windows 截图及后续测试反馈；本地未运行 AutoCAD。尚未取得完整 probe 输出、原始命令日志或 Recorder episode。本文不表示完整验收通过。

## 当前环境与结果

- Windows，Python 3.10，pywin32 310，psutil 7.2.2（截图所示）。
- AutoCAD 窗口标题为 2026；probe 返回版本 `25.1s (LMS Tech)`、`LOCALE=EN`。
- 最新 probe：`attach -> True (125 ms)`；`get_state (3.9 ms)`。
- 截图状态返回 `Drawing1.dwg`、`idle`、空选择、实体数 0、文档数 1；后续用户报告已完成上一轮给出的状态测试且全部通过，详见下文。
- 当前 `level=B`、`events=off`、`log=off`、`log_files=[]`，只能确认状态查询链路可用，不能确认 action/event 采集。
- 用户报告 100 次查询耗时与 read-only check 通过；尚未收到完整文本，不记录未经提供的 min/max 数值。

## 用户反馈：状态查询测试通过

用户在收到上一轮测试步骤后回复“我全部测完了都通过了”。按该轮明确列出的范围记录为用户实测通过：

- `--probe --repeat 100`：max <500 ms，SelectionSets.Count / DBMOD 前后一致。
- LINE 创建两段线后，entity_count 相对增加 2。
- 选中一条线后，selection_count=1，selection 含 AcDbLine(handle)。
- 保存后 active_document 为实际保存路径，未继续修改时 unsaved_changes=false。
- 切换图层、布局、图纸后，对应 state 字段符合实际。

这些结果不代表 COM 事件、日志解析或 Recorder 完整集成通过；AC-002 / AC-003 仍待修。模态对话框、批量选择等未在上一轮具体操作清单中要求的项目，也不自动标记为通过。

## 问题清单

### AC-001：终端输出编码（已通过环境设置绕过，代码未修）

最初经 PowerShell 管道打印含中文的 summary 时，在 cp1252 编码处抛出 UnicodeEncodeError。设置 Python 输出 UTF-8 后不再崩溃，但管道显示乱码；统一 PowerShell 解码后，最新截图中文字正常。

当前窗口使用：

```powershell
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

后续处理：完善调试入口的输出编码兼容性及 README。需在未设置这些环境项的新 PowerShell 窗口回归。

### AC-002：COM 事件订阅失败（阻塞）

实际输出：`订阅 COM 事件失败: (-2147467262, 'No such interface supported', None, None)`。

连接成功，但事件订阅失败并进入 B 级。当前信息不能定位是取得连接点接口、查询事件连接点还是 Advise 的哪一步失败。

后续处理：增加分阶段诊断和异常堆栈，定位 `_setup_events` / `_advise` 的失败位置后修复。修复后不仅要求 events=on，还要用 LINE 验证 BeginCommand、EndCommand、ObjectAdded 实际送达。

受影响：命令、对象增删改、选择变化、文档生命周期等 COM 事件验收；批量事件限流、事件时间戳和完整 Recorder 事件链路先标为阻塞，不逐项记作独立缺陷。

### AC-003：实际 LOCALE=EN 未匹配日志规则（阻塞）

实际输出：`log=off(LOCALE=EN 没有对应的日志解析规则)`，`log_files=[]`。

当前实现只匹配 CHS / ENU。需取得 LOGFILENAME 指向的原始文件，核对日志实际语言、编码、行格式后修正语言映射；不应仅根据界面语言或 EN 字符串直接认定日志语言。

受影响：日志来源 command（B 级）、command_cancelled、error_raised。修复后需重新 attach，并用未知命令与 LINE + Esc 验证真实记录。

## 状态测试清单（上述项目已由用户报告通过，其余保留待测）

1. 空闲状态运行 `--probe --repeat 100`，保存完整输出；max <500 ms；无人操作时 SelectionSets.Count 和 DBMOD 前后相同。
2. 空图用 LINE 创建两段线，结束命令后重新 probe：模型空间实体数相对增加 2。
3. 无命令执行时选中一条线，重新 probe：selection_count=1，selection 含 AcDbLine(handle)。需要核对切换终端后 AutoCAD 是否保留选择。
4. 保存图纸到已知路径，再 probe：active_document 为该路径；未继续修改时 unsaved_changes=false。
5. 切换当前图层、布局、文档后分别 probe，核对对应字段；结果以实际图纸内容为准。
6. 打开模态对话框时 probe，检查 get_state 能否在 500 ms 内返回 busy/dialog；关闭后重新 probe 应恢复。
7. 保存完整 probe 的只读检查结果。既有 unsaved_changes=true 不能单独证明适配器有写操作。

可以补测 Recorder 的 state 落盘与正常停止，但 action/event 和完整集成验收仍受 AC-002 / AC-003 阻塞。建议完成以上状态检查后集中修复两个阻塞项，再执行开发文档第 10 节完整验收。

## 待收集证据

- probe / probe --repeat 100 的完整文本，包括 per-field ms 和 read-only check。
- AutoCAD 的 LOGFILENAME 指向的原始日志文件（不要打开后另存）。
- 修复后 watch 输出及 Recorder episode，包含 raw/software_*.jsonl、raw/adapters.log、meta.json、trajectory.jsonl。

## 2026-09-29 收到真实日志后的修订

已读取用户提供的 Drawing1_1c1288f9e.log。无 BOM，含 GBK 文本及 AutoCAD Unicode 转义；命令提示实际为中文，不能把 LOCALE=EN 直接当作英文。样例可解析 LOGFILEMODE、取消和 QUIT，没有 LINE / ZZZ_NONEXISTENT。

- AC-001：CLI 已设置 UTF-8 和 backslashreplace；summary 输出纳入 finally 的清理保护。Windows 新进程复测待完成，PowerShell 解码设置仍需要保留。
- AC-002：按 pywin32 genpy 的 `_query_interface_` 模式修订 sink，避免请求未知原生事件接口 gateway。新增 wrap / QueryInterface / FindConnectionPoint / Advise 阶段日志与完整堆栈。此为有源码依据的候选修复，真实失败位置和 Windows 回调送达仍待复测。参考：https://github.com/mhammond/pywin32/blob/main/com/win32com/client/genpy.py
- AC-003：接受 EN；按实际行内容识别中英文；匹配前还原 Unicode 转义；raw 保留原行；EN 的 ANSI 回退依据当前样例为 GBK，其他环境仍待验证。
- AC-004：修复 `命令: *取消*` 被当作 command 的优先级问题。

验证：4 项 unittest 通过（真实样例形式、合成中英文错误、分块读取、sink 查询和入队逻辑）；用户原始文件离线解析通过。以上不代替 Windows COM/日志实时验证。

## 用户更正日志后的结论（取代前述 EN→GBK 判断）

用户提供 Drawing1_1de6ed425.log，含实际输入 ZZZ_NONEEXISTENT 及 LINE 后取消。未知命令行的引号是原始 0x93 / 0x94（cp1252），中文为 ASCII Unicode 转义。先前根据旧文件第三方插件 GBK 文本选择 EN→GBK 会破坏引号和相邻命令字符，已改为 EN→cp1252。旧文件可能混有第三方插件采用不同编码的输出，不据此判断核心命令日志编码。

更正后的完整原始文件经当前 LogTail → LogParser → _on_log_line 离线验证：生成 error_raised(kind=unknown_command, command=ZZZ_NONEEXISTENT) 和 command_cancelled(command=LINE)。新增 cp1252 引号/转义/分块读取回归，5 项测试通过。raw 保留转义文本。Windows 实时日志读取及 COM 回调仍待复测；应重新复制最新 adapter.py 后再 probe/watch。

## AC-005：watch 期间 AutoCAD 访问冲突崩溃（阻塞，尚未修复根因）

用户先提供 probe 成功截图：attach 127 ms、get_state 4.6 ms，level=A / events=on / log=on。随后报告：不运行 watch 时操作正常；运行 watch 后输入命令会崩溃。截图为 LINE 输入第一个点 100,100 后，AutoCAD 弹出 `Unhandled Access Violation Reading 0x0000 Exception at 66C996B8h`。尚无原始 watch 输出或崩溃 dump，不能确定故障调用栈。

这推翻了“事件功能已修好”的结论：目前只验证过订阅成功。可能涉及事件 gateway、回调中得到的 COM 对象跨线程保留/读取、查询与事件的交互；这些只是排查方向，并非已证实根因。Autodesk 说明事件常发生于命令处理中，事件处理有约束，但官方资料本身不能定位此崩溃：https://help.autodesk.com/cloudhelp/2021/ENU/AutoCAD-ActiveX/files/GUID-2FF2F1B5-FFAC-420A-A741-15D1FC1A571E.htm

临时隔离：COM_EVENTS_ENABLED=False；attach 跳过 Application / Document 订阅，_advise 也禁止订阅。CLI 和 Recorder 同样生效，不修改 Recorder 主体。保留 state 与日志 B 级路径，不把此措施称作根因修复或保证不会崩溃。

7 项离线回归通过：新增检查两种 apartment 条件下均不调用订阅、直接订阅被拦截、日志 command/cancel/error 仍能输出。Windows 降级后稳定性未验证。下一份所需证据为本次崩溃前已有的 autocad-watch-fixed.txt 或完整 PowerShell 输出；不要求重新运行旧版以复现。

## AC-005 隔离后的 Windows watch 结果

用户提供更新 adapter 后的 `autocad-watch-fixed.txt`（UTF-16，24 行）。结果：

- attach 成功，72 ms，PID 5024，level=B，events=off(AC-005)，log=on，locale=EN。确认当前运行的订阅隔离生效。
- 实时记录 3 条日志来源 command：LINE、ZZZ_NONEXISTENT、LINE。
- 实时记录 error_raised(kind=unknown_command, command=ZZZ_NONEXISTENT)。
- 实时记录 command_cancelled(command=LINE)。
- state 共 16 条，12 条 idle、4 条 busy；包含连续 3 个采样点 busy，随后恢复 idle，文件最后也是 idle。原因不能从 state 摘要确定，不能一概视作正常或证明崩溃。
- 文件未见 Python traceback、AutoCAD 断开信息；也没有 Ctrl+C 后 stats，故只能证明所提供片段内采集持续恢复，不能证明完整关闭流程和长期稳定性。
- 所有 action/event 为 source=log，lag=0 ms 表示从读取日志到打印的时间，不能证明事件发生到采集的真实延迟为零。

结论：B 级日志实时最小操作链通过，AC-003 的 EN/转义/未知命令/取消路径得到 Windows 证据；COM 事件崩溃 AC-005 仍未解决，主方案和 Recorder 集成仍未验收。无需重测相同日志步骤。

## AC-006：Recorder attach 因图纸暂时忙碌而耗尽重试

用户提供 adapters.log：首次 GetActiveObject 返回 -2147221021；后两次已经取得 Application、通过身份检查，却在初始 _rescan 的 Documents.Count 查询中遇到 -2147417846（application is busy），被包装成 _Busy 后导致 attach=False。不能据此断定权限不匹配，也不是 COM 事件订阅失败。

修订：在已通过进程身份检查的情况下，初始图纸扫描/系统变量缓存读取遇到 _Busy 时保留连接，设置待初始化标志，在后续 get_actions/get_events 的维护阶段重试。无额外线程、定时器或 sleep，不修改 Recorder 三次重试机制；未取得 Application、身份检查失败、断开或非忙碌异常仍按原失败处理。延后初始化期间日志可能尚未开始跟踪，因此不能保证覆盖初始化前操作，复测应等图纸初始化完成后开始。COM 事件仍禁用。

新增 3 项离线回归：扫描忙后恢复、缓存忙后重新安排初始化、断开和意外异常不吞掉。合计 10 项通过。Windows Recorder 复测待完成。
