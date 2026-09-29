> **AC-005 最新状态（覆盖下文历史方案）：** 用户实测 probe 达到 level=A/events=on/log=on，但 watch 中执行 LINE 导致 AutoCAD `Unhandled Access Violation Reading 0x0000`。已暂时停用全部 COM 事件订阅，固定使用 B 级；主方案没有通过验收。根因未确定，不能把订阅成功或离线测试通过视为回调安全性验证。需要崩溃前完整 watch 输出定位；不要求用户反复复现旧路径。

> 2026-09-29 修订：以下历史待验证标记以 [windows-test-notes.md](windows-test-notes.md) 最新记录为准。用户已报告基础 state 测试通过。事件 sink 改用 `_query_interface_` 返回 IDispatch wrapper（参考 pywin32 genpy 的 sink）；仍保持默认策略，避免回调内转换对象。日志接受 EN，并按实际行内容识别中英文；解析前还原 AutoCAD Unicode 转义，取消优先于 command。EN 的 ANSI 回退按更正后的真实样例设为 cp1252（中文转义、cp1252 引号）；旧日志第三方插件的 GBK 文本不用于判断命令日志编码。命令参数仍关闭；当前样例不足以启用。新增 `test_regressions.py`，Windows 事件修复待复测。

# AutoCAD 适配器开发文档

依据:`adapters/software_trajectory_collector_specification.md`(下称"规范")。接口、记录格式、约束以规范为准,本文只规定 AutoCAD 适配器怎么实现。接口形态参照 `adapters/mock/`;Recorder 侧行为见 `software.py` 与 `task-v1.2.md`。

**验证状态标记**(全文使用):

- **[文档]**:AutoCAD 官方 ActiveX Reference / 系统变量文档或 pywin32 公开文档可确认的接口与语义。
- **[待验证]**:必须在 Windows + AutoCAD 中实测才能确定的行为。**本文撰写时没有在任何 AutoCAD 上运行过,所有 [待验证] 项均未验证。**

**实现状态**:第二阶段已按本文实现(`adapter.py` 等,见同目录 [README.md](README.md))。只在 macOS 上用假的 COM 对象模型验证过纯逻辑部分;本文所有 [待验证] 项仍未验证。实现中对原计划的调整已同步写回本文(5.1、5.3–5.5、6.2、6.3、7、8、9 节)。

---

## 1. 范围

### 1.1 做

- 连接**已在运行**的 AutoCAD(`acad.exe`),通过 COM(ActiveX Automation)只读查询状态、订阅 COM 事件。
- 在操作者预先开启命令行日志(`LOGFILEMODE=1`)时,增量读取官方命令行日志,补充命令输入、取消、报错。
- 实现规范第 3 节的 5 个方法和 `--probe` / `--watch` 调试入口,交付第 12 节的文件。

### 1.2 不做

- 不启动 AutoCAD、不打开/新建/保存/切换文档、不执行命令(不调用 `SendCommand`、`PostCommand`、`Regen`、`ZoomXXX` 等)、不设置任何系统变量(包括不替用户打开 `LOGFILEMODE`)、不选中对象、不修改选择集。
- 不使用 DLL 注入、Windows hook、ObjectARX / .NET 插件、AutoLISP 加载(这些都需要把代码放进 acad.exe 进程)。
- 不使用 Action Recorder(`ACTRECORD`,需要用户手动开始录制且会生成 `.actm` 文件)。
- 不做实体属性的前后值比对(见第 11 节 `parameter_changed`)。
- 不支持 AutoCAD LT(无 ActiveX 接口)。
- 不写任何文件(规范第 6 节"不写盘"),包括不生成 pywin32 makepy 缓存(见 2.3)。

### 1.3 目标版本

- 目标:**AutoCAD 2026 简体中文版**(R25.1,ProgID `AutoCAD.Application.25.1`,`LOCALE` 预期为 `CHS` **[待验证]**)。
- AutoCAD 2021–2025 使用同一套 ActiveX 对象模型,预期也可用,但不在验收范围内。
- 基于 AutoCAD 的行业版(Architecture、Mechanical 等,进程同为 `acad.exe`)不做特殊处理,预期可用 **[待验证]**。
- 界面语言:COM 部分与语言无关(命令名预期仍为英文全局名 **[待验证]**);日志解析与语言有关,支持 `CHS`(目标)和 `ENU`,其余语言日志层关闭(见 6.3、6.4)。

---

## 2. 连接方式

### 2.1 线程与 COM 套间

Recorder 为每个适配器分配一个独立线程,5 个方法都在该线程串行调用,且只在有采集点时才调用(`software.py` 不做周期调用)。

AutoCAD 的 COM 事件是 AutoCAD 主线程**同步**调用我们的事件接收器(sink),AutoCAD 要等回调返回才继续。因此:

- 如果适配器线程是 STA(单线程套间),事件只能在该线程抽取消息时送达;Recorder 只在有操作时才调用适配器,两次调用之间 AutoCAD 会卡在事件回调上。**不允许用 STA 接事件。**
- **主方案:适配器线程初始化为 MTA**(`pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)`)。跨进程事件由 COM 的 RPC 线程池直接调用 sink,不依赖适配器线程抽消息,回调内即可打 `t_ms` **[文档:COM 套间规则;AutoCAD 下的实际表现待验证]**。
- 回调运行在 RPC 线程上,与适配器线程并发:回调只向线程安全队列追加元组,所有 AutoCAD 查询只在适配器线程做。

pywin32 / pythoncom 必须在 `attach()` 内延迟导入:`software.py` 在 Recorder 主线程里导入 `adapter.py`,而 `import pythoncom` 会把导入它的线程初始化为 STA,不能影响 Recorder 主线程。`adapter.py` 模块顶层只导入标准库(同时保证在 macOS 上也能被 `software.py` 导入)。`adapter.py` 由 `software.py` 按文件路径加载,不能用相对导入,所有代码放在 `adapter.py` 一个文件里。

### 2.2 attach(ctx) 步骤

总预算 10 s,每一步失败都 `ctx.log` 原因。

1. 非 Windows,或 `import pythoncom, pywintypes, win32com.client, win32gui, win32process` 失败 → 返回 `False`。
2. `pythoncom.CoInitializeEx(COINIT_MULTITHREADED)`。
   - 返回 `RPC_E_CHANGED_MODE`(0x80010106)说明线程已是 STA → **不订阅事件**,进入降级方案 B(第 7 节),查询照常。
3. 获取 Application:依次尝试 `win32com.client.GetActiveObject(progid)`,progid 列表为 `"AutoCAD.Application.25.1"`、`"AutoCAD.Application"`、`"AutoCAD.Application.25"`、`"AutoCAD.Application.24.3"`、`"AutoCAD.Application.24.2"`、`"AutoCAD.Application.24.1"`、`"AutoCAD.Application.24"`,第一个成功即用 **[文档:GetActiveObject 附着到运行实例;各版本 ProgID 待验证]**。
   - **禁止**使用 `win32com.client.Dispatch("AutoCAD.Application")` 或 `gencache.EnsureDispatch`:前者在没有运行实例时会启动一个新 AutoCAD,后者会写 makepy 缓存。
   - 全部失败(典型错误 `MK_E_UNAVAILABLE` 0x800401E3)→ 返回 `False`。常见原因写入日志提示:AutoCAD 仍在启动;AutoCAD 以管理员身份运行而 Recorder 不是(不同完整性级别的 ROT 互不可见);AutoCAD LT。
4. 校验实例:读 `app.HWND` **[文档]**,用 `win32process.GetWindowThreadProcessId` 得到其 PID,确认进程名为 `acad.exe`;再比较 `win32gui.GetForegroundWindow()` 所属 PID。
   - 若机器上有多个 `acad.exe` 且 ROT 返回的实例不是前台那个 → 返回 `False` 并记录两者 PID(ROT 通常只登记第一个启动的实例,另一个实例无法通过 COM 连接)**[待验证]**。
   - 保存 `self._hwnd`、`self._pid`,供后续存活与忙碌探测使用。
5. 读 `app.Version`、`app.Name` 写入 `ctx.log`。
6. 读初始缓存(均为只读查询):
   - `app.Documents` 中每个文档的 `Name`、`FullName`;
   - 活动文档的 `LOCALE`、`LOGFILEMODE`、`LOGFILENAME`、`CLAYER`、`CTAB` 系统变量(`GetVariable`)。
7. 订阅事件(仅 MTA 时,见 2.3):Application 一个 sink,每个已打开文档一个 sink。
8. 日志层:`LOGFILEMODE == 1` 且 `LOCALE` 在支持列表内 → 对每个文档的 `LOGFILENAME` 记下**当前文件长度**作为起始偏移(attach 之前的历史不读)。否则日志层关闭并记录原因。
9. 返回 `True`。`ctx.log` 一行汇总运行级别:`level=A|B, events=on|off, log=on|off(原因)`。

### 2.3 事件订阅(不依赖 makepy)

pywin32 的 `DispatchWithEvents` / `WithEvents` 需要 makepy 生成的包装模块,会写入 `gen_py` 缓存目录,违反"不写盘";makepy 早绑定包装还会把 AutoCAD 返回的实体按基类接口包装。因此在内存中按类型信息构建 sink,用标准的 `IConnectionPointContainer` / `IConnectionPoint.Advise` 连接 **[文档:COM 连接点;pywin32 `win32com.server.util.wrap`]**:

1. `tlb, _ = app._oleobj_.GetTypeInfo().GetContainingTypeLib()`。
2. 遍历 `tlb` 中 `TKIND_COCLASS` 类型,按名称找到 `AcadApplication` 和 `AcadDocument` **[文档:coclass 名称;待验证类型库中的实际名称]**。
3. 在 coclass 的实现接口中找 `IMPLTYPEFLAG_FSOURCE | IMPLTYPEFLAG_FDEFAULT` 的那个,取其 `ITypeInfo`:得到事件接口 IID(预期名为 `_DAcadApplicationEvents` / `_DAcadDocumentEvents`,不硬编码),并从每个 `FUNCDESC.memid` + `GetNames(memid)[0]` 得到 `{dispid: 事件名}`。
4. 构造 sink 对象:`_com_interfaces_ = []`，`_query_interface_` 在请求事件 IID 时返回 `wrap(self)`,`_public_methods_ = []`,`_dispid_to_func_` 把**事件接口的全部 dispid** 映射到方法:关心的事件映射到对应处理函数,其余映射到空函数 `_noop`(避免未知 dispid 向 AutoCAD 返回 `DISP_E_MEMBERNOTFOUND`)。
5. `wrapped = win32com.server.util.wrap(sink)`(使用默认 `DesignatedWrapPolicy`,参数以原始 `PyIDispatch` 传入;**不用** `EventHandlerPolicy`,它会在回调内对 IDispatch 参数调 `GetTypeInfo`,即回调内反向调用 AutoCAD)。
6. `cp = obj._oleobj_.QueryInterface(pythoncom.IID_IConnectionPointContainer).FindConnectionPoint(iid)`;`cookie = cp.Advise(wrapped)`。保存 `(cp, cookie)`,detach 时 `cp.Unadvise(cookie)`。

任何一步失败 → 事件关闭,进入降级方案 B。

### 2.4 回调规则

每个事件处理函数:

1. 第一行 `t_ms = time.time_ns() // 1_000_000`。
2. 把 `(t_ms, 事件名, 原始参数, 来源文档 sink)` 追加到 `collections.deque`(CPython 下 `append` 线程安全)。
3. 返回 `None`。**不在回调里调用 AutoCAD 的任何方法或属性**,不改写 byref 参数(例如可否决关闭的参数),不调用 `ctx.log`。
4. 整个函数体包在 `try/except BaseException` 里,异常计数存入内部列表,由下一次 `get_*` 在适配器线程写 `ctx.log`。

`ObjectAdded` 的 `Object` 参数以 `PyIDispatch` 原样保存,名称和句柄在适配器线程里解析(见 5.3)。

### 2.5 存活与忙碌探测(每次访问 AutoCAD 前)

AutoCAD 主线程忙(打开大图、重生成、长命令)时,跨进程 COM 调用会一直排队等待,可能超过 `get_state` 的 500 ms 预算;Recorder 连续 10 次超时会停用适配器。所以每次在适配器线程访问 AutoCAD 前:

1. **存活**:`win32gui.IsWindow(self._hwnd)` 为假 → 标记 `dead`,不再发起 COM 调用。
2. **忙碌**:`win32gui.SendMessageTimeout(self._hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, 100)`,超时或失败 → 本次视为忙碌,跳过所有 COM 调用。`WM_NULL` 是空消息,不改变 AutoCAD 任何状态;这是 Windows 标准的"窗口是否响应"检测,不读取 AutoCAD 数据 **[文档:Win32;AutoCAD 下能否可靠反映 COM 可用性待验证]**。
3. COM 调用失败的错误码分类:
   - `RPC_E_CALL_REJECTED`(0x80010001)、`RPC_E_SERVERCALL_RETRYLATER`(0x8001010A):AutoCAD 拒绝外部调用(常见于模态对话框或忙)→ 本次视为忙碌,不重试。
   - `RPC_S_SERVER_UNAVAILABLE`(0x800706BA)、`RPC_E_DISCONNECTED`(0x80010108)、`CO_E_OBJNOTCONNECTED`(0x800401FD)→ 标记 `dead`。
   - 其他 → 该字段为 `null`,`ctx.log` 记一次(同类错误每分钟最多记一次)。

### 2.6 detach 与软件退出

- `detach()`:对所有 sink `cp.Unadvise(cookie)`(AutoCAD 已退出时会抛异常,忽略)→ 释放所有 COM 引用(sink、待解析的 `PyIDispatch`、`app`)→ `pythoncom.CoUninitialize()`(仅当本线程 `CoInitializeEx` 成功过)。预算 5 s。
- AutoCAD 退出后、Recorder 调 `detach` 之前:`get_state()` 返回 `None`;`get_actions()` / `get_events()` 先返回队列里剩余的记录,之后返回 `[]`。
- 同一线程可能再次 `attach`(进程重开),`attach` 需重置全部内部状态。

---

## 3. get_state()

### 3.1 字段

字段集合固定,取不到填 `null`。"来源"列均为只读查询,`doc` 指 `app.ActiveDocument`,`GetVariable(X)` 指 `doc.GetVariable("X")`。

| 字段 | 类型 | 来源 | 取不到(`null`)的情况 | 状态 |
| --- | --- | --- | --- | --- |
| `t_ms` | int | 查询时刻 | — | — |
| `software` | string | 常量 `"AutoCAD"` | — | — |
| `active_document` | string\|null | `doc.FullName`;为空字符串(未保存的新图)时用 `doc.Name`(如 `Drawing1.dwg`) | 无打开文档(`app.Documents.Count == 0`,如只剩"开始"选项卡);忙碌;dead | [文档];"开始"选项卡时 Count 为 0 [待验证] |
| `selection` | list\|null | `doc.PickfirstSelectionSet`,元素 `"<ObjectName>(<Handle>)"`,如 `"AcDbLine(2A3F)"`;最多 `SELECTION_LIMIT` 个(初值 50) | 忙碌;dead;读取出错。无文档时为 `[]` | [文档];读取是否向 `doc.SelectionSets` 留下对象、是否置 `DBMOD` [待验证] |
| `selection_count` | int\|null | `PickfirstSelectionSet.Count` | 同上 | [文档] |
| `selection_truncated` | bool\|null | `selection_count > len(selection)` | 同上 | — |
| `mode` | string\|null | 系统变量 `CMDACTIVE` 位码,加上适配器自身判断,见 3.2 | `GetVariable` 出错 | [文档:CMDACTIVE 位码] |
| `active_command` | string\|null | `GetVariable("CMDNAMES")`,如 `"LINE"`、`"LINE'ZOOM"`;无命令为 `""` | 无文档;忙碌;dead | [文档] |
| `active_layer` | string\|null | `GetVariable("CLAYER")` | 同上 | [文档] |
| `active_layout` | string\|null | `GetVariable("CTAB")`,如 `"Model"`、`"Layout1"` | 同上 | [文档] |
| `ucs` | string\|null | `GetVariable("UCSNAME")` 非空时取之;为空时 `GetVariable("WORLDUCS") == 1` → `"WORLD"`,否则 `"UNNAMED"` | 同上 | [文档] |
| `units` | string\|null | `GetVariable("INSUNITS")` 按官方取值表映射为小写英文名(4 → `"millimeters"`),表外值 → `"insunits_<n>"` | 同上 | [文档] |
| `entity_count` | int\|null | `doc.ModelSpace.Count`(模型空间实体数,不含图纸空间) | 同上 | [文档] |
| `document_count` | int\|null | `app.Documents.Count` | 忙碌;dead | [文档] |
| `unsaved_changes` | bool\|null | `GetVariable("DBMOD") != 0` | 无文档;忙碌;dead | [文档] |
| `read_only` | bool\|null | `doc.ReadOnly` | 同上 | [文档] |
| `view_center` | list[float]\|null | `GetVariable("VIEWCTR")`,当前视口中心,UCS 坐标 `[x, y, z]` | 同上 | [文档] |
| `view_height` | number\|null | `GetVariable("VIEWSIZE")`,当前视口高度,图形单位(见 `units`) | 同上 | [文档] |

不放入 state 的:软件版本(写在 `capabilities.yaml` 和 attach 日志里)、图层列表、实体清单(遍历成本不可控)。

### 3.2 mode 取值

按顺序判断,取第一个命中的:

| 值 | 条件 |
| --- | --- |
| `"busy"` | 2.5 的忙碌探测失败,或 COM 调用返回拒绝类错误。此时除 `t_ms`、`software`、`mode` 外全部为 `null` |
| `"no_document"` | `app.Documents.Count == 0`。此时 `selection = []`、`selection_count = 0`、`selection_truncated = false`、`document_count = 0`,其余文档相关字段为 `null` |
| `"dialog"` | `CMDACTIVE & 8` |
| `"script"` | `CMDACTIVE & 4` |
| `"transparent_command"` | `CMDACTIVE & 2` |
| `"command_active"` | `CMDACTIVE & 1` |
| `"arx_command"` | `CMDACTIVE & 64` |
| `"lisp"` | `CMDACTIVE & 32`(官方说明该位只对 ObjectARX 可见,COM 下可能恒为 0)[待验证] |
| `"idle"` | `CMDACTIVE == 0` |

`CMDACTIVE & 16`(DDE)不单独列出,落入后续判断。

### 3.3 实现要求

- 进入即检查 `dead`(→ 返回 `None`)和忙碌(→ 返回 `mode="busy"` 的记录)。
- 调用顺序:`app.Documents.Count` → `doc` → `FullName`/`Name`/`ReadOnly` → 10 个 `GetVariable` → `ModelSpace.Count` → 选择集。
- 选择集逐项读取 `ObjectName`、`Handle`、`ObjectID`(`ObjectID` 用于 5.3 的删除事件句柄缓存,不放进 state),每项 3 次跨进程调用。
- 时间预算:自身计时,累计超过 350 ms 即停止枚举选择集,已读部分照常返回并置 `selection_truncated = true`。
- 顺带用 `document_count` 与已知文档数比较,不一致则置  `rescan` 标志(见 5.4)。
- 每次跨进程调用的实际耗时 [待验证],`SELECTION_LIMIT` 和 350 ms 阈值在验收第 9 项实测后定稿。

---

## 4. get_actions()

### 4.1 能捕获的 action

| type | name | 来源 | params | 状态 |
| --- | --- | --- | --- | --- |
| `command` | 命令全局名,如 `LINE`、`QSAVE`、`U` | Application 事件 `BeginCommand(CommandName)`,`source="com_event"` | `{}` | [文档];中文版是否仍给英文全局名、透明命令名是否带 `'`、夹点编辑是否表现为 `GRIP_*` 命令 [待验证] |
| `command` | `C:` 后面的名字,如 `DRAWBOX` | Application 事件 `BeginLisp(FirstLine)`,`FirstLine` 匹配 `^\(C:([^\s()]+)\)$`(忽略大小写) | `{"expression": FirstLine}` | [文档:BeginLisp];用户输入 LISP 自定义命令时 FirstLine 的形式 [待验证] |
| `api_call` | `FirstLine` 中第一个符号,如 `command`、`setq`;取不到为 `"AutoLISP"` | `BeginLisp(FirstLine)`,不匹配上一行时 | `{"expression": FirstLine}` | [文档];加载 `acaddoc.lsp` 等启动脚本是否也触发 [待验证] |
| `command` | 当前命令名 | 命令行日志的"提示 + 用户输入"行,`source="log"` | `{"prompt": 提示文本, "input": 用户输入文本}` | 日志格式 [待验证];**第一版关闭,验收确认日志格式后启用**,见 6.3 |
| `command` | 命令名 | 命令行日志 `命令: <NAME>`(英文版 `Command: <NAME>`)行,**仅降级方案 B 下返回**(A 下由 BeginCommand 覆盖,只用作上下文) | `{}` | [待验证] |

`raw` 统一写成可读的原始事件文本,例如 `BeginCommand(CommandName='LINE')`、`BeginLisp(FirstLine='(C:DRAWBOX)')`;日志来源写日志原行。

### 4.2 实现

- BeginCommand / BeginLisp 回调入队(2.4);`get_actions()` 从队列取出 action 类记录,按 `t_ms` 顺序返回。
- 同时维护"命令栈":BeginCommand 入栈,EndCommand 按名称出栈;`get_state` 读到 `CMDACTIVE == 0`,或日志读到 `*Cancel*` 时清空。命令栈用于给 event 填 `command` 参数(5.2)和 UNDO 判断(5.2)。
- 日志行在 `get_actions()` 中读取解析(见 6.3),解析出的 event 暂存,由紧接着的 `get_events()` 返回(`software.py` 每次都是先 `get_actions` 再 `get_events`)。
- 自身计时,超过 150 ms 停止本次日志读取,剩余下次继续。

### 4.3 无法可靠获取的 action

| 内容 | 原因 |
| --- | --- |
| 其他 COM / .NET / ObjectARX 客户端的 API 调用(`api_call`) | COM 没有"某客户端调用了什么方法"的通知。这些调用若经 `SendCommand` 执行命令,只会以 BeginCommand 的 `command` 出现,无法区分来源 |
| 命令参数(COM 来源) | `BeginCommand` 只给命令名,params 恒为 `{}` |
| 鼠标拾取的点坐标、对象捕捉点 | COM 无对应事件;命令行日志只记录提示,不记录拾取坐标 [待验证] |
| "Select objects:" 提示下逐个选择的对象 | 不属于 pickfirst 选择集,`SelectionChanged` 不覆盖;日志只有 `N found` 这类结果 |
| 特性选项板(Properties)、图层特性管理器里的修改 | 通常不走命令,不产生 BeginCommand [待验证];其结果只能以 `object_modified` / `sysvar_changed` 事件出现 |
| 对话框内的操作 | COM 不暴露对话框内容 |
| 鼠标滚轮缩放、平移 | 不经过命令,不产生 BeginCommand [待验证] |

---

## 5. get_events()

### 5.1 订阅的 COM 事件

为避免重复,每个事件只在一个层级订阅:Application 层订阅应用级事件,Document 层只订阅 Application 没有的。

**Application 事件**(一个 sink)[文档:事件名与参数]:

`BeginCommand`、`EndCommand`、`BeginLisp`、`EndLisp`、`LispCancelled`、`NewDrawing`、`EndOpen`、`EndSave`、`SysVarChanged`。

**Document 事件**(每个文档一个 sink)[文档:事件名与参数]:

`ObjectAdded`、`ObjectModified`、`ObjectErased`、`SelectionChanged`、`BeginClose`、`Activate`、`LayoutSwitched`。

其余不订阅:`BeginOpen`/`BeginSave`(可能被取消,以 `End*` 为准)、`BeginDocClose`(见 `document_closed`)、`BeginPlot`/`EndPlot`、`BeginFileDrop`、`BeginDoubleClick`/`BeginRightClick`/`BeginShortcutMenu*`(Recorder 的键鼠采集已覆盖)、`WindowChanged`/`WindowMovedOrResized`/`AppActivate`/`AppDeactivate`(窗口层面,Recorder 窗口追踪已覆盖)、`ARXLoaded`/`ARXUnloaded`,以及 Document 层的 `BeginCommand`/`EndCommand`/`BeginLisp`/`EndLisp`/`SysVarChanged`(与 Application 层重复)。这些事件在 dispid 表中映射到 `_noop`。

### 5.2 事件映射

| event type | 来源 | name | params | 状态 |
| --- | --- | --- | --- | --- |
| `command_executed` | `EndCommand(CommandName)` | 命令名 | `command`: 命令名;`duration_ms`: 与同名 BeginCommand 的时间差,无匹配为 `null` | [文档];命令被 Esc 取消时是否触发 EndCommand [待验证] |
| `undo` | `EndCommand`,命令名为 `U`;或为 `UNDO` 且该命令的 BeginCommand 与 EndCommand 之间收到过至少一个 `ObjectAdded/Modified/Erased` 回调 | 命令名 | `command` | [待验证:Ctrl+Z 是否表现为 `U`;撤销是否触发对象事件] |
| `redo` | `EndCommand`,命令名为 `REDO` 或 `MREDO` | 命令名 | `command` | [待验证] |
| `lisp_ended` | `EndLisp()` | 最近一次 BeginLisp 的 name | `expression`: 对应 FirstLine | [文档] |
| `lisp_cancelled` | `LispCancelled()` | 同上 | 同上 | [文档] |
| `document_created` | `NewDrawing()` 回调记下时刻;下次 rescan(5.4)发现新增且 `FullName` 为空的文档时产生,`t_ms` 取回调时刻 | 新文档 `Name`,如 `Drawing2.dwg` | `name` | [文档:NewDrawing 在新图创建前触发];"开始"选项卡 / `+` 新建是否触发、打开已有文件是否也触发 [待验证] |
| `document_opened` | `EndOpen(FileName)` | 文件名 | `path`: 绝对路径 | [文档] |
| `document_saved` | `EndSave(FileName)`;扩展名不是 `.dwg/.dxf/.dwt/.dws` 的(如自动保存 `.sv$`)不返回,只 `ctx.log` | 文件名 | `path`: 绝对路径 | [文档];自动保存是否触发 EndSave [待验证] |
| `document_closed` | 文档 sink 的 `BeginClose()`;name/path 取该 sink 缓存(回调内不查询) | 文档 Name | `name`;`path`: 绝对路径,未保存的新图为 `null` | [文档:BeginClose 在图纸即将关闭时触发];用户在"是否保存"对话框点"取消"时是否仍触发 [待验证] |
| `document_activated`(自定义) | 文档 sink 的 `Activate()` | 文档 Name | `name`;`path`(同上) | [文档] |
| `object_created` | 文档 sink 的 `ObjectAdded(Object)` | `"<ObjectName>(<Handle>)"`;解析失败为 `"unresolved"` | `handle`、`object_type`(如 `AcDbLine`)、`object_id`、`command`(回调时命令栈顶,无为 `null`),解析失败的字段为 `null` | [文档];也会对非图形对象(图层表记录、字典等)触发 [待验证] |
| `object_modified` | 文档 sink 的 `ObjectModified(Object)` | 同上 | 同 `object_created`;**不含** `from`/`to` | [文档];触发频率 [待验证] |
| `object_deleted` | 文档 sink 的 `ObjectErased(ObjectID)` | 已知句柄时同上;未知为 `"ObjectID(<id>)"` | `object_id`;`handle`、`object_type` 从缓存取(见 5.3),未知为 `null`;`command` | [文档] |
| `selection_changed` | 文档 sink 的 `SelectionChanged()` | 文档 Name | `{}`(事件本身不带选择内容;当前选择以 `get_state().selection` 为准) | [文档:pickfirst 选择集变化时触发];单击时触发次数 [待验证] |
| `layer_changed` | `SysVarChanged("CLAYER", newVal)` | 新图层名 | `from`: 缓存的上一值(attach 时读取或上次事件),未知为 `null`;`to` | [文档:SysVarChanged];CLAYER 切换是否触发 [待验证] |
| `layout_switched`(自定义) | 文档 sink 的 `LayoutSwitched(LayoutName)` | 新布局名 | `from`(CTAB 缓存,未知为 `null`)、`to` | [文档] |
| `sysvar_changed`(自定义) | `SysVarChanged(SysvarName, newVal)`,`CLAYER` 与 `CTAB` 除外 | 系统变量名(大写) | `name`、`from`(缓存上一值,未知为 `null`)、`to`;点坐标类取值转为 list | [文档];哪些系统变量高频无意义触发 [待验证] |
| `command_cancelled`(自定义) | 命令行日志 `*Cancel*` 行 | 日志中当前命令名,未知为 `"unknown"` | `command` | 日志层,[待验证] |
| `error_raised` | 命令行日志的报错行(见 6.3) | `unknown_command` / `lisp_error` | `kind`、`message`;`unknown_command` 另有 `command` | 日志层,[待验证] |

`BeginModal` / `EndModal` / `BeginQuit` 不订阅:模态对话框状态由 `CMDACTIVE` 位 8 反映,退出由存活探测(2.5)发现。

`sysvar_changed` 的忽略名单 `SYSVAR_IGNORE` 初始为空。验收时若发现某些系统变量在普通操作中大量触发且无操作语义,加入该名单,并同步写进 `capabilities.yaml` 与 README。

所有 COM 来源的 event `source="com_event"`,`raw` 为原始事件文本,如 `ObjectAdded(AcDbLine, 2A3F)`、`EndSave(FileName='C:\task\a.dwg')`。路径保持 AutoCAD 返回的 Windows 原样(反斜杠)。

### 5.3 对象解析

回调只保存 `PyIDispatch`,在适配器线程解析:

`ObjectAdded` / `ObjectModified` / `ObjectErased` 三类记录按回调顺序进入同一个"待完成"队列,其他事件直接进入输出队列(Recorder 归并前按 `t_ms` 排序,所以输出顺序不影响结果)。`get_events()` 按顺序处理待完成队列:

1. `ObjectAdded` / `ObjectModified`:需要忙碌探测(2.5)通过,再通过 `IDispatch::Invoke` 读 `ObjectName`、`Handle`、`ObjectID`,然后释放引用。单项读失败(通常是对象在读取前已被删除)→ 该字段为 `null`;三项都拿到时 name 为 `ObjectName(Handle)`,否则为 `ObjectName` 或 `"unresolved"`。
2. `ObjectErased`:不访问 AutoCAD,从缓存取 handle 和类型。它排在前面的对象事件之后,所以"刚创建又被删除"的对象也能先进缓存。
3. AutoCAD 忙(探测失败或调用被拒绝):队首记录等待不超过 5 s 的留到下次;超过 5 s 的不再读取,以 `handle: null` 返回。AutoCAD 已退出:全部立即以 `null` 返回。
4. 解析预算:本次调用开始后 150 ms 内,超出则留到下次。
5. 缓存 `{ObjectID: (Handle, ObjectName)}`,来源:已解析的 `ObjectAdded` / `ObjectModified`,以及 `get_state` 读选择集时见到的对象。上限 100 000 项,超过按插入顺序淘汰。

### 5.4 文档 rescan(绑定新文档 sink)

触发:`NewDrawing` / `EndOpen` / `EndSave` / `BeginClose` 回调置 `rescan` 标志,或 `get_state` 发现文档数变化。执行位置:下一次 `get_actions()` / `get_events()`,先做忙碌探测。`NewDrawing` 在新图创建之前触发,`BeginClose` 在图纸关闭之前触发,所以只要还有未配对的 `NewDrawing`、或 `BeginClose` 的图纸还在,每次调用都继续 rescan(各自最多 10 s)。

文档以其 `HWND` 属性为键(读不到时退用 `Name|FullName`)。

1. 枚举 `app.Documents`,读每个文档的 `Name`、`FullName`。
2. 新文档:建 Document sink 并 Advise(2.3);`FullName` 为空且有待配对的 `NewDrawing` 时刻 → 产生 `document_created`;日志层开启时读其 `LOGFILENAME` 并从当前长度开始跟踪。
3. 已不存在的文档:Unadvise,释放。
4. 超过 10 s 未能配对的 `NewDrawing` 时刻丢弃并 `ctx.log`。
5. 已有文档:刷新缓存的 `Name` / `FullName`(另存为后路径变化;`EndSave` 会触发 rescan)。`document_closed` / `document_activated` 的 name/path 取自该缓存。

新建/打开的文档在 rescan 之前的对象事件收不到(Recorder 在每次键鼠操作后都会调用 `get_*`,通常间隔很短,但由脚本连续执行时可能漏掉)。

### 5.5 队列、合并与限流(规范第 6 节)

- `get_events()` 单次最多返回 200 条,其余留在队列。
- 连续重复合并:只对高频且单条无区分意义的 `object_modified`、`selection_changed`、`sysvar_changed` 做。相邻两条的 `type`、`name`、`source`、`params` 完全相同时合并为一条,保留第一条的 `t_ms`,`params.repeat` 记总条数(只在 ≥2 时出现)。action 和其他 event 不合并(两次相同的 `LINE` 命令、两次撤销都是不同的操作)。
- 输出队列超过 2000 条:保留最早的 2000 条,超出部分按 `type` 聚合,每类一条,`name` 为该 type,`params` 为 `{"aggregated": 条数, "first_t_ms": ..., "last_t_ms": ...}`,`t_ms = first_t_ms`,`source` 取被聚合记录的来源,`raw` 为 `"aggregated <n> <type> records"`,并 `ctx.log` 告警。待完成队列超过 2000 条时,超出部分不再读取,直接以 `handle: null` 进入输出队列,再按上一条规则处理。
- `get_actions()` 采用同样的 200 条上限与聚合规则。

### 5.6 无法可靠获取的 event

| 规范类型 | 情况 |
| --- | --- |
| `parameter_changed` | **不提供**。`ObjectModified` 只给对象,不给被改的属性和前后值;要得到 `from`/`to` 只能事先为对象做属性快照,无法对任意对象可靠做到。实体属性修改以 `object_modified` 出现,系统变量修改以 `sysvar_changed`(含 `from`/`to`)出现 |
| `view_changed` | **不提供**。没有视图变化的 COM 事件;`VIEWCTR`/`VIEWSIZE` 为只读系统变量,是否触发 SysVarChanged [待验证]。视图以 state 的 `view_center`/`view_height` 体现,ZOOM/PAN 命令以 action 体现 |
| `solve_started` / `solve_finished`、`feature_added`、`sketch_entered` / `sketch_exited` 及仿真类 | 不适用于 AutoCAD |
| `command_cancelled`、`error_raised` | 仅日志层开启时有 |
| 命令失败、未知命令 | COM 无对应事件(.NET 的 `CommandFailed`/`UnknownCommand` 不在 ActiveX 中);只能从日志得到 |
| 模态对话框里的修改结果 | 只能通过其后产生的对象/系统变量事件间接体现 |

---

## 6. Journal / 命令历史 / 日志

### 6.1 可用性

| 候选 | 结论 |
| --- | --- |
| Journal | AutoCAD 没有 SolidWorks / Revit 那样的 journal 文件。不可用 |
| 命令行历史窗口 | COM 不提供读取接口。`LASTPROMPT` 系统变量只有最后一行,不用 |
| **命令行日志文件** | 官方功能:`LOGFILEMODE=1` 时把文本窗口内容写入日志;`LOGFILENAME`(只读系统变量)给出当前图纸日志的完整路径 [文档]。**可用,但需要操作者事先打开** |
| Action Recorder | 需要用户手动开始录制,属于侵入操作。不用 |

`LOGFILEMODE` 保存在注册表(当前 Windows 用户的当前 AutoCAD 配置)**[文档]**,适配器不修改,只在 attach 时读取。操作者的设置方法见 6.5,README 同步写明。

### 6.2 读取方式

- 每个文档的 `LOGFILENAME` 在 attach / rescan 时读取一次;多个文档指向同一文件时只跟踪一份。
- 每次读取:`open(path, "rb")` → `seek(offset)` → 读到末尾 → 关闭。不保持文件句柄。
- 编码:按文件开头 BOM 判断(`FF FE` → UTF-16LE,`EF BB BF` → UTF-8);无 BOM 时,在第一次读到非 ASCII 字节时判定:能按 UTF-8 严格解码就用 UTF-8,否则用 AutoCAD 界面语言对应的代码页(`CHS` → GBK,`ENU` → cp1252)。不用 Windows 系统代码页,因为中文 AutoCAD 可能装在英文 Windows 上。
- 末尾不完整的行(无换行符)留在缓冲区,下次拼接。
- 文件变短或 `LOGFILENAME` 变化 → 偏移归零并 `ctx.log`。
- 打开失败(`PermissionError` 等,AutoCAD 独占写入)→ 该文件的日志层关闭,`ctx.log` 一次。

待验证:日志写入是否实时刷新(还是有缓冲延迟)、编码、是否被独占锁定、每个文档是否各有一个日志文件、命令行输入与提示在日志中的实际格式。

### 6.3 行解析规则

日志行没有时间戳(只有文件开头的会话头),**日志来源记录的 `t_ms` 为读到该行的时刻**,在 `capabilities.yaml` 中注明时间精度较差;读取只发生在 Recorder 调用 `get_actions()` 时,延迟取决于调用间隔。

按顺序匹配,命中即停止:

下表的中文与英文字符串都是按 AutoCAD 界面文字写的**候选**,没有对照过真实日志文件 **[待验证]**。冒号同时匹配半角 `:` 与全角 `：`(下文写作 `[:：]`)。

| 规则 | 中文版(CHS,目标) | 英文版(ENU) | 归属 | 输出 |
| --- | --- | --- | --- | --- |
| 命令回显 | `^命令\s*[:：]\s*(\S+)\s*$` | `^Command:\s*(\S+)\s*$` | 上下文;方案 B 下为 action | 设置该文件的"当前命令"(去掉开头的 `_`、`.`、`'`);方案 B 下返回 `command` action,params `{}` |
| 空命令提示 | `^命令\s*[:：]\s*$` | `^Command:\s*$` | 上下文 | 清空"当前命令" |
| 取消(在行尾查找,通常紧跟在提示后面,如 `指定下一点或 [放弃(U)]: *取消*`) | `\*取消\*\s*$` | `\*Cancel\*\s*$` | event | `command_cancelled`,`command` 为当前命令;清空当前命令与命令栈 |
| 未知命令 | `^未知命令\s*[“"](.+?)[”"]` | `^Unknown command "(.+)"\.` | event | `error_raised`,`kind="unknown_command"`,`command`,`message` 为整行 |
| LISP 报错 | `^;\s*错误\s*[:：]\s*(.*)$` | `^; error:\s*(.*)$` | event | `error_raised`,`kind="lisp_error"`,`message` |
| 软件结果 | `找到\s*\d+\s*个`、`总计\s*\d+\s*个` 等(验收时补全) | `^\d+ found`、`^\d+ found, \d+ total`、`^\d+ (was\|were) not in current space` 等 | 不返回 | — |
| 提示 + 输入 | `^(?P<prompt>(指定\|输入\|选择)[^:：]*?(?:\[[^\]]*\])?\s*(?:<[^>]*>)?)[:：]\s*(?P<input>\S.*)$` | `^(?P<prompt>[A-Z][^:]*?(?:\[[^\]]*\])?\s*(?:<[^>]*>)?):\s*(?P<input>\S.*)$` | action | `command`,name 为当前命令,params `{"prompt", "input"}`;要求"当前命令"已知且 `input` 不匹配"软件结果"规则 |
| 其他 | — | — | 不返回 | — |

按规范 4.6,无法确定是用户输入还是软件输出的行一律不返回。正则表集中在 `adapter.py` 的 `LOG_PATTERNS = {"CHS": {...}, "ENU": {...}}` 中。

**启用策略**:

- "命令回显 / 空命令提示 / 取消 / 未知命令 / LISP 报错"这几条即使写错,后果只是匹配不到、少出记录,不会产生错误记录。第一版实现直接启用。
- "提示 + 输入"写错会把软件输出误判为用户输入。第一版实现时关闭(`LOG_INPUT_RULE = {"CHS": False, "ENU": False}`),`capabilities.yaml` 中不声明 log 来源的 `prompt`/`input` 参数。验收第 20 项拿到真实日志样例后,修正正则和"软件结果"排除表并打开,同时补回 `capabilities.yaml` 的声明。

### 6.4 其他语言

`LOCALE` 不是 `CHS`、`ENU` 或 `EN` 时日志层关闭,`ctx.log` 说明原因。COM 事件不受影响。

### 6.5 操作者如何打开命令行日志(录制前做一次)

在虚拟机里,以**将来运行 AutoCAD 和 Recorder 的那个 Windows 用户**登录,启动 AutoCAD 2026,任选一种方法:

- **命令行**:输入 `LOGFILEMODE` 回车,提示输入新值时输入 `1` 回车。
- **对话框**:输入 `OPTIONS` 回车(或右键绘图区 →"选项"),切到"打开和保存"选项卡,在"文件安全措施"一栏勾选"维护日志文件",点"确定"。

确认:命令行输入 `LOGFILEMODE` 回车,显示当前值为 `1`;输入 `LOGFILENAME` 回车,会显示当前图纸日志文件的完整路径。日志存放目录在"选项 →文件 →日志文件位置"中可查看(系统变量 `LOGFILEPATH`)。

这个设置保存在当前 Windows 用户、当前 AutoCAD 配置的注册表里,关闭重开 AutoCAD 后仍然有效;设置好之后再给虚拟机做快照/镜像即可。换了 Windows 用户或切换了 AutoCAD 配置(`OPTIONS` →"配置"),需要重新设置。日志文件会随使用不断增长,可以定期删除日志目录中的旧 `.log` 文件(不要在录制过程中删)。

中文菜单文字是按 AutoCAD 中文版常见译名写的,若界面上名称略有不同,以命令行方法为准。

---

## 7. 主方案与降级方案

| 级别 | 条件 | state | action | event |
| --- | --- | --- | --- | --- |
| **A(主方案)** | attach 成功 + MTA 初始化成功 + Application sink Advise 成功 + 事件自检未失败 | 全部字段 | BeginCommand、BeginLisp;日志开启且"提示 + 输入"规则启用后加命令输入 | 5.2 全部 COM 事件;日志开启时加 `command_cancelled`、`error_raised` |
| **B(无事件)** | attach 成功,但满足下方任一"事件不可用"条件 | 全部字段(不受影响) | 仅日志:`Command:` 回显(params `{}`)与命令输入;日志未开启则无 | 仅日志:`command_cancelled`、`error_raised`;日志未开启则无 |
| **C(不可用)** | attach 失败 | `attach` 返回 `False`,由 Recorder 重试 | — | — |

A 与 B 叠加的日志层独立开关:`LOGFILEMODE != 1`、`LOCALE` 不受支持、或日志文件打不开 → 日志层关闭。

**"事件不可用"的判定**(实现时逐条检查,命中即切换到 B 并 `ctx.log` 具体原因):

1. `CoInitializeEx(MTA)` 返回 `RPC_E_CHANGED_MODE`。
2. 类型库中找不到 `AcadApplication` coclass 或其默认源接口,或 `FindConnectionPoint` / `Advise` 抛异常。
3. **运行时自检**:attach 以来从未收到过任何 `BeginCommand` 回调,而 `get_state()` 先后读到 2 个不同的、attach 时不在运行的 `CMDNAMES` 值 → 判定事件链路不通(Advise 成功但回调送不到)。attach 时正在运行的命令不计入,避免把 attach 前就已开始的命令误判为漏收。现象:`--watch` 中执行命令却没有任何 `com_event` 记录。

切换到 B 后本次连接不再尝试恢复事件(避免反复 Advise),detach 后重新 attach 时重新判定。

**数据完整性差别**:

| 内容 | A | B |
| --- | --- | --- |
| 命令开始时刻 | 回调时刻,精确 | 读日志时刻,延迟取决于 Recorder 调用间隔;日志未开启则无 |
| 命令结束 / 撤销 / 重做 | 有 | 无 |
| 对象增删改 | 有 | 无 |
| 选择变化事件 | 有 | 无(只有 state 快照) |
| 文档新建/打开/保存/关闭/切换 | 有 | 无(只能从 state 的 `active_document` 前后差异看出) |
| 图层、布局、系统变量变化 | 有(含 `from`/`to`) | 无(只有 state 快照) |
| 取消、报错 | 仅日志开启时有 | 仅日志开启时有 |
| state | 完整 | 完整 |

**未采用的方案**:在适配器内另起一个 STA 线程持续抽消息接收事件。它与规范 3.2 / 6 的线程约定("由 Recorder 调用,适配器不自行安排定时任务";"适配器运行在 Recorder 分配的独立线程中")不一致。若验收证明 MTA 下事件无法送达,先报告,确认后再决定,不自行加线程。

---

## 8. capabilities.yaml

完整内容见同目录 [capabilities.yaml](capabilities.yaml),以该文件为准。要点:

- `state`:3.1 表中的 16 个字段,各有 `type` / `source` / `meaning`。
- `actions`:`command`(BeginCommand、LISP 自定义命令、级别 B 下的日志命令回显)、`api_call`(其他 LISP 表达式)。
- `events`:5.2 表中的 19 类。`repeat` 只出现在 `object_modified`、`selection_changed`、`sysvar_changed` 上;聚合记录的格式写在文件末尾的注释里。
- 日志来源(`command_cancelled`、`error_raised`、级别 B 的 `command`)注明"t_ms 为读到该行的时刻,时间精度较差"。
- 日志"提示: 输入"行(params `prompt` / `input`)在启用前不声明;`parameter_changed`、`view_changed` 不声明,原因写在文件末尾的注释里。

实现后以实际返回为准修订,尤其是验收中发现不触发的事件,要从这里删掉或注明条件。

## 9. --probe / --watch

入口:`adapters/autocad/__main__.py` 调 `adapter.main()`,在项目根目录运行 `python -m adapters.autocad --probe|--watch`(与 mock 一致,需要 `adapters/autocad/__init__.py`)。调试入口在主线程运行 attach(MTA 初始化同样发生在该线程)。

### 9.1 --probe

1. 打印 `attach()` 返回值、耗时、以及 attach 汇总行(版本、PID、运行级别 A/B、事件开/关、日志开/关及原因)。
2. 调用一次 `get_state()`,打印 JSON 与耗时。
3. `--probe` 额外打印每个字段的单项耗时(实现时在 `get_state` 内部用可选计时字典收集,仅调试入口打开),用于定 `SELECTION_LIMIT`。`--probe --repeat N` 连续调用 N 次 `get_state()`,打印最小/最大耗时,并对比调用前后的 `SelectionSets.Count` 与 `DBMOD`(只读检查)。
4. `detach()`。

检查点:耗时 < 500 ms;`active_document`、`active_layer` 等与 AutoCAD 界面一致;打开一张图后 probe 前后 `DBMOD` 不变(只读性);重复 probe 100 次后 `doc.SelectionSets.Count` 不变(probe 结束时额外打印该值)。

### 9.2 --watch

1. attach,打印汇总行。
2. 每 500 ms 调用 `get_actions()` 和 `get_events()`,每条记录打印一行 JSON,前面加 `lag=<打印时刻 - t_ms>ms`。
3. 每 5 s 调用一次 `get_state()`,打印 `mode`、`active_command`、`selection_count` 摘要行(便于观察自检与 busy)。
4. Ctrl+C 后打印内部统计(按事件名的回调计数 `cb:*`、忙碌次数、未解析数、合并/聚合数、各类错误计数),再 `detach()`。

检查点:COM 来源记录 `lag` 应不超过约 500 ms(只由轮询间隔决定),且 `t_ms` 早于打印时刻;log 来源记录 `t_ms` 等于读取时刻;执行命令时 AutoCAD 无可感知卡顿。

### 9.3 本地(macOS)可验证的部分

纯逻辑部分写成不依赖 pywin32 的函数,在 macOS 上用临时脚本喂构造数据验证:日志行解析(6.3)、编码/半行处理、队列合并与 2000 条聚合、200 条上限、UNDO 判定、命令栈、`CMDACTIVE` → `mode` 映射、`INSUNITS` 映射。`attach()` 在非 Windows 上返回 `False`。临时脚本不提交。

---

## 10. 验收(在 Windows + AutoCAD 中逐项执行)

准备:AutoCAD 2026 简体中文版,按 6.5 设置 `LOGFILEMODE=1`;另开一个终端运行 `python -m adapters.autocad --watch`,再用 Recorder `--adapters autocad` 录一遍同样的操作,对照 `raw/software_*.jsonl`。所有项目当前状态均为**未验证**。

| # | 操作 | 预期 action | 预期 event | 预期 state / 其他检查 |
| --- | --- | --- | --- | --- |
| 1 | `QNEW` 新建图纸 | `command QNEW`(com_event) | `document_created`(name `DrawingN.dwg`);`document_activated` | `active_document` 为 `DrawingN.dwg`,`entity_count` 0 |
| 2 | `LINE` 键入 `0,0` `100,0` `100,100` 回车 | `command LINE`(日志"提示 + 输入"规则启用后,另有 log 来源 `command LINE` 三条,`input` 分别为三个坐标) | `object_created` ×2(`AcDbLine`,handle 非空,`command`=`LINE`);`command_executed LINE`(`duration_ms` 合理) | `entity_count` 2 |
| 3 | `CIRCLE` 用鼠标拾取圆心、键入半径 `25` | `command CIRCLE`(规则启用后,log 来源只有半径那条,拾取点不记录) | `object_created AcDbCircle`;`command_executed` | — |
| 4 | 单击选中直线(无命令) | — | `selection_changed` | `selection` 为 `["AcDbLine(<handle>)"]`,与第 2 步 handle 一致;`selection_count` 1 |
| 5 | 在特性选项板把圆半径改为 `40` | 预期无(确认是否有命令) | `object_modified`(圆的 handle),无 `from`/`to`;不应出现 `parameter_changed` | — |
| 6 | 选中直线后按 Delete | `command ERASE`(确认实际命令名) | `object_deleted`,handle 与第 4 步一致(来自缓存);`command_executed` | `entity_count` -1,`selection` `[]` |
| 7 | Ctrl+Z | `command U` | `object_created` 或 `object_modified`(确认撤销触发哪类);`command_executed U`;`undo` | `entity_count` 恢复 |
| 8 | `REDO` | `command REDO` | `redo`;对象事件 | — |
| 9 | 图层下拉框切换当前图层到新建图层 `A` | 视实际而定 | `layer_changed` `from:"0" to:"A"`;可能有 `object_created`(图层表记录,若新建图层) | `active_layer` `A` |
| 10 | F3 切换对象捕捉 | — | `sysvar_changed OSMODE`,`from`/`to` 为整数 | — |
| 11 | `SAVEAS` 保存到 `C:\task\a.dwg` | `command SAVEAS` | `document_saved` `path` 为 `C:\task\a.dwg`;`command_executed` | `active_document` 为 `C:\task\a.dwg`;`unsaved_changes` false |
| 12 | 切换到 `Layout1` 选项卡 | — | `layout_switched` `from:"Model" to:"Layout1"` | `active_layout` `Layout1` |
| 13 | `OPEN` 打开另一张图,再切回第一张 | `command OPEN` | `document_opened`(path 正确);`document_activated` ×2 | `document_count` 2 |
| 14 | `CLOSE` 关闭当前图(有修改,弹框点"取消";再次关闭点"否") | `command CLOSE` ×2 | 点"取消"时**不应**有 `document_closed`;点"否"后 `document_closed` | `document_count` 减 1 |
| 15 | `ARRAYRECT` 选一个圆,关联 = 否,50×50 | `command ARRAYRECT` | ~2500 条 `object_created`,每次 `get_events()` ≤200 条分批返回,不同 handle 未被合并;如超 2000 出现聚合记录且 `adapters.log` 有告警 | 用秒表对比接入与未接入时该命令耗时,记录两者数值 |
| 16 | 键入 `FOO` 回车 | — | `error_raised` `kind=unknown_command`(log 来源) | — |
| 17 | `LINE` 后按 Esc | `command LINE` | `command_cancelled`(log);确认是否有 `command_executed` | `mode` 回到 `idle` |
| 18 | 命令行键入 `(+ 1 2)`;再加载一个 `(defun C:ZZ ...)` 并执行 `ZZ` | `api_call +`;`command ZZ` | `lisp_ended` ×2 | — |
| 19 | 打开"选项"对话框时执行 `--probe` | — | — | `mode` 为 `dialog` 或 `busy`,probe 在 500 ms 内返回,AutoCAD 不卡 |
| 20 | 以上操作结束后,把 `LOGFILENAME` 指向的日志文件原样拷出(不要用记事本另存,保留原编码) | — | — | 用该样例修订 6.3 的中文正则与"软件结果"排除表并冻结,打开"提示 + 输入"规则;确认刷新延迟、编码、中文冒号是全角还是半角 |
| 21 | 打开一个大图纸(加载数秒)期间持续操作鼠标 | — | — | `get_state` 返回 `mode=busy` 而非超时;`meta.json` 中 `timeouts` 不持续增长,适配器未被停用 |
| 22 | 退出 AutoCAD | — | 可能有若干 `document_closed` | 适配器不抛异常;之后 `get_state()` 为 `None`、`get_*` 为 `[]`;Recorder `adapters.log` 显示 detach |
| 23 | `--probe` 在选中 0 / 1 / 50 / 1000 个对象时各执行一次 | — | — | 耗时均 < 500 ms;1000 个时 `selection_truncated` true |
| 24 | 对照 Recorder `raw/events.jsonl` | — | — | `command` action 的 `t_ms` 在对应回车 `key_up` 之后约 100 ms 以内;对象事件 `t_ms` 在对应命令的 Begin/End 之间 |
| 25 | 只读性 | — | — | 打开图纸后仅运行适配器(不操作),`DBMOD` 仍为 0;`UNDO` 历史中无适配器引入的条目;`doc.SelectionSets.Count` 不变;`gen_py` 目录无新增文件 |
| 26 | 第二个 AutoCAD 实例在前台 | — | — | attach 返回 False,日志说明 PID 不一致;不会采到另一实例的数据 |
| 27 | `LOGFILEMODE=0` 时重复第 2、16、17 项 | 只有 com_event 来源 | 无 `command_cancelled`/`error_raised` | attach 汇总行显示 `log=off(LOGFILEMODE=0)` |

验收结束后:把实际未触发或触发条件不同的项写进 README 与 `capabilities.yaml`(规范第 7 节"已声明但未出现的,在 README 中说明触发条件")。

---

## 11. 已知限制

1. **未验证**:本文所有 [待验证] 项,以及 MTA 下 AutoCAD 事件能否送达、每次跨进程调用耗时,都没有实测。
2. **`parameter_changed` 不提供**;实体属性修改只有 `object_modified`,没有属性名和前后值。
3. **`view_changed` 不提供**;视图只在 state 中体现。
4. **命令参数**:COM 来源的 `command` 的 params 恒为 `{}`;参数只能从日志得到,且只有键盘输入的文本,没有鼠标拾取点;日志来源时间精度较差。
5. **取消与报错**只能靠日志;中文日志的匹配规则在拿到真实样例前是候选,可能匹配不到。命令输入(`prompt`/`input`)在样例确认前不输出。
6. **`api_call`** 只覆盖 AutoLISP 表达式;其他 COM / .NET / ObjectARX 客户端的调用无法捕获,经 `SendCommand` 执行的命令与用户命令无法区分。
7. **选择**只反映 pickfirst 选择集;`PICKFIRST=0` 时恒为空;命令内"选择对象"提示下的选择不可见;最多 50 项。
8. **新文档对象事件的空窗**:新建/打开文档后到下一次 `get_*` 调用(rescan)之前,该文档的对象事件收不到。
9. **`object_*` 的句柄**:对象在解析前被删除时 handle 为 `null`;`object_deleted` 只对本次会话见过的对象有 handle。`ObjectAdded`/`ObjectModified` 也会对非图形对象触发,且修改一次可能触发多条 `ObjectModified`。
10. **`command` 参数(事件上下文)** 由 Begin/EndCommand 推断,命令被取消时若不触发 EndCommand,在下一次 `CMDACTIVE==0` 或 `*Cancel*` 之前可能不准。
11. **批量操作的性能**:每个对象事件都是 AutoCAD 主线程发起的一次同步跨进程回调;大批量操作会变慢,程度 [待验证](验收第 15 项)。
12. **AutoCAD 忙碌期间** state 为 `mode="busy"`,其余字段为 `null`。
13. **多实例**:只能连接 ROT 中登记的实例;前台是另一实例时不连接。
14. **权限**:AutoCAD 以管理员运行而 Recorder 不是时无法连接(反之亦然)。
15. **AutoCAD LT 不支持**。
16. **路径格式**:`active_document`、`path` 保持 AutoCAD 返回的 Windows 路径(反斜杠),不做规范化。
17. **日志前置条件**:需要操作者预先设置 `LOGFILEMODE=1`;适配器不替用户打开。

---

## 12. 交付文件

全部位于 `adapters/autocad/`,不修改 Recorder 主体文件。

| 文件 | 内容 |
| --- | --- |
| `adapter.py` | `Adapter` 类:`NAME = "AutoCAD"`,`PROCESS_NAMES = ["acad.exe"]`,`SPEC_VERSION = "1"`,5 个方法;模块内私有部分:HRESULT 常量、`INSUNITS` 映射、`CMDACTIVE` 映射、`LOG_PATTERNS`、`SYSVAR_IGNORE`、动态 sink 构建(2.3)、日志 tail(6.2)、队列合并/聚合(5.5);`main()` 实现 `--probe` / `--watch`。顶层只导入标准库,不用相对导入 |
| `capabilities.yaml` | 第 8 节内容,按验收结果修订 |
| `README.md` | 依赖与安装;支持的 AutoCAD 版本(注明未实测);需要的软件设置(`LOGFILEMODE=1`、AutoCAD 与 Recorder 同权限运行、单实例);运行级别 A/B 的判定与差别;字段/事件清单与拿不到的信息(第 4.3、5.6、11 节);已声明但可能不出现的事件及触发条件;调试入口用法;排查步骤 |
| `requirements.txt` | `pywin32>=306; sys_platform == "win32"` 与 `psutil`(均已是 Recorder 的依赖,不引入新包) |
| `__init__.py`、`__main__.py` | 空文件与 `from .adapter import main; main()`,仅为 `python -m adapters.autocad` 可用,与 mock 相同 |

---

## 附:已确认的事项

1. `SPEC_VERSION = "1"`。
2. 目标软件:AutoCAD 2026 简体中文版。
3. 操作者按 6.5 预先打开命令行日志。
4. 使用 2.5 的忙碌探测(`SendMessageTimeout(WM_NULL)`):它不读也不改 AutoCAD 的任何数据,只用于在 AutoCAD 忙时跳过查询,避免 `get_state` 连续超时导致 Recorder 停用适配器。

## 附:Recorder 侧注意事项(不修改,仅说明)

- `software.py` 只在采集点调用 `get_*`,没有规范 3.2 中"每 500 ms 一次"的周期调用(README 第 23 条)。COM 来源记录的 `t_ms` 不受影响;日志来源记录的 `t_ms`(读取时刻)会随之变差。
- Recorder 连续 10 次超时或异常即停用适配器;AutoCAD 长时间忙碌时靠 2.5 的忙碌探测避免超时。若验收第 21 项仍出现连续超时,再讨论是否需要 Recorder 侧调整。
- 目前判断不需要修改 Recorder。
