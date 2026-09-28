# Windows Trajectory Recorder V1

在 Windows VM 中后台记录 computer use 轨迹:鼠标键盘、UIA element、截图/录屏、活动窗口、终端、文件变化、进程变化。详细需求见 [task.md](task.md)。

开发环境是 macOS,最终运行在 Windows VM。**本 README 里标注"未在 Windows 上验证"的部分,只是写了代码、在 macOS 上做过语法/导入检查,没有在真实 Windows 环境跑过,请按下面的步骤在 VM 上逐一验证。**

## 文件说明

| 文件 | 平台 | 做什么 |
|---|---|---|
| [trajectory.py](trajectory.py) | 跨平台(纯 stdlib) | 读取 `raw/events.jsonl` + `raw/terminal/*`,把底层事件合并成 8 种 action、划分 step、生成 `trajectory.jsonl` 和 `trajectory.html`。不依赖任何 Windows 模块,可在 macOS 上直接运行和调试。 |
| [uia.py](uia.py) | Windows | 封装 UIA 查询(`uiautomation` 库):`query_at_point(x,y)` 查坐标下的 element,`query_focused()` 查当前焦点 element,查询超过 2 秒或失败一律返回 `None`。 |
| [screen.py](screen.py) | Windows | `take_screenshot(path)` 截整个虚拟桌面(mss);`Recorder` 类封装 ffmpeg(gdigrab)录屏进程的启动/优雅停止。 |
| [window.py](window.py) | Windows | `get_active_window()` 取前台窗口标题+进程名(pywin32 + psutil);`poll_loop()` 轮询并在变化时回调。 |
| [processes.py](processes.py) | Windows | 每 0.5 秒轮询一次全部进程(psutil),和上一次快照做 diff,回调 start/exit 事件(全量,不过滤)。 |
| [files.py](files.py) | Windows | 用 `watchdog` 监控 `--watch` 指定目录,回调 created/modified/deleted/renamed(必须显式传入目录,不默认监听整盘)。 |
| [terminal.py](terminal.py) | Windows | 写入/清除 `C:\ProgramData\trajrec\current_episode.txt`,告诉 PowerShell profile 钩子把 transcript 写到哪。transcript 本身的解析在 `trajectory.py` 里(纯文本,不需要 Windows API)。 |
| [input_recorder.py](input_recorder.py) | Windows | 全局键鼠钩子(pynput)。回调里只记时间戳入队,UIA 查询和截图分别丢到两个独立的后台线程处理,避免互相卡顿。截图分两种:动作*触发瞬间*立刻截一张("trigger",给 `action.screenshot` 用),键鼠安静下来(`SETTLE_DELAY_MS`)后再截一张("settle",给下一步的 `observation.screenshot` 用)。检测到"打字序列开始"时查询一次 focused UIA element。 |
| [main.py](main.py) | Windows | 启动顺序、协调所有模块、初始观测、等待停止信号、收尾(复制 artifacts、生成 trajectory)。 |
| [install_profile.ps1](install_profile.ps1) | Windows | 把 transcript 钩子装进 Windows PowerShell 5.1 和 PowerShell 7(如果装了)的 AllUsersAllHosts profile。 |
| [requirements.txt](requirements.txt) | — | 全部 Python 依赖,Windows 专用包标了 `sys_platform == "win32"`。 |
| [samples/demo_episode/](samples/demo_episode/) | — | 伪造的一份完整 raw 数据(`task.json` + `raw/events.jsonl` + `raw/terminal/*.txt`),覆盖全部 8 种 action、4 种文件事件、进程 start/exit、terminal 输出、第一步/最后一步,用于验证 `trajectory.py`。 |
| [cursor.png](cursor.png) | — | 鼠标光标图标,`trajectory.py` 生成 HTML 时会读取这个文件、转成 base64 内嵌进页面,用来在截图上标光标位置(找不到这个文件时退化成一个纯色圆点)。 |

## 在 macOS 上测试 trajectory.py

```bash
pip install -r requirements.txt   # 在 macOS 上只会装 psutil/mss/watchdog,Windows 专用包会被跳过
python trajectory.py samples/demo_episode
```

会在 `samples/demo_episode/` 下生成 `trajectory.jsonl`(10 step)和 `trajectory.html`。`trajectory.html` 用 `file://` 直接双击打开即可(截图和 UIA target 框会叠加在截图上,terminal 输出、file/process 事件都能看到);如果双击后截图不显示,大概率是浏览器把相对路径当成了跨域请求,可以本地起个静态服务器看效果:

```bash
cd samples/demo_episode && python3 -m http.server 8000
# 浏览器打开 http://localhost:8000/trajectory.html
```

修改 `trajectory.py` 里的合并规则后,重新跑一遍上面的命令就能看到效果,不需要重新录制。

## 在 Windows VM 上安装

```powershell
git clone <repo> C:\trajrec
cd C:\trajrec
pip install -r requirements.txt
```

另外需要:

- **ffmpeg**:装好并加入 PATH(`screen.py` 直接 shell 出去调用 `ffmpeg`,没有额外的 Python 依赖)。
- **以管理员身份**打开 PowerShell,运行一次:
  ```powershell
  .\install_profile.ps1
  ```
  这一步把 transcript 钩子装进 PowerShell profile,只需要装一次。

## 在 Windows VM 上逐个测试采集模块

每个模块都能单独跑,方便定位问题:

```powershell
python uia.py            # 每 2 秒打印鼠标位置下的 UIA element,把鼠标移到不同控件上看输出变化
python screen.py         # 截一张图存到当前目录,检查分辨率/DPI 是否正确
python window.py         # 每秒打印前台窗口,切换几个应用看标题/进程名对不对
python processes.py      # 打开/关闭一个程序(如记事本),看 start/exit 事件是否出现
python files.py --watch C:\task  # 在 C:\task 下新建/编辑/删除/改名文件,看事件是否正确
python terminal.py <episode_dir>   # 写入标记文件后,按提示打开一个新 PowerShell 窗口执行几条命令,回车结束后检查 <episode_dir>\raw\terminal\ 下有没有生成 transcript
python input_recorder.py # 综合测试:点击、拖拽、打字、按快捷键,看 raw_test\events.jsonl 里事件是否符合预期(这一步依赖 uia.py 和 screen.py,建议放在它们都验证过之后再测)
```

## 整体运行与停止

```powershell
python main.py --output C:\traj --task task.json --watch C:\task
```

- 启动后会打印 `[trajrec] 初始状态已记录,可以开始操作`,这之前的键鼠输入不算数。
- 输出目录:`C:\traj\<episode_id>\`(`episode_id` 是启动时间,如 `20260922_103215`)。
- **停止**:按 `Ctrl+C`,或者在另一个窗口/宿主机创建文件 `C:\ProgramData\trajrec\STOP`。
- 停止后自动生成 `trajectory.jsonl`、`trajectory.html`,并把录制期间有变化的文件复制进 `artifacts/`。

修改合并规则、想从 raw 数据重新生成轨迹(不用重新录制):

```powershell
python trajectory.py C:\traj\20260922_103215
```

## 尚未在 Windows 上验证的部分

以下功能只写了代码、在 macOS 上做过 import/语法检查(不会崩溃、平台判断正常),**没有在真实 Windows 环境跑过**,需要在 VM 上按上面的步骤逐一验证:

- 键鼠全局钩子(`input_recorder.py` 的 pynput 部分)是否能正确捕获、是否会被系统权限/UAC 拦截。
- UIA 查询(`uia.py`)在真实控件(尤其是非标准控件,如 SOLIDWORKS 这类专业软件的自绘 UI)上的准确率和耗时。
- DPI 感知设置后,鼠标坐标、UIA `bounding_rect`、截图像素三者是否真的一一对应。
- `screen.py` 的 ffmpeg gdigrab 录屏是否正常工作、`stop()` 的优雅退出是否总能生成完整 mp4。
- `install_profile.ps1` 对 AllUsersAllHosts profile 的写入是否在目标 Windows 版本上按预期路径生效,`Start-Transcript -IncludeInvocationHeader` 的实际输出格式是否与 `trajectory.py` 的解析逻辑一致(见下面"简化"里的第 3 条)。
- `processes.py` 里 `psutil` 返回的 `username()` 格式(是否总是 `域\用户名` 这种形式)。
- 整个 `main.py` 的启动/停止流程在真实多线程 + 真实 I/O 延迟下是否有竞态(尤其是停止时截图与录屏收尾的时序)。

## task.md 里没规定清楚、按最简单方案处理的地方

1. **截图触发时机与 `action.screenshot` 字段**:每个动作实际对应**两张**不同用途的截图,`type`/`kind` 字段区分:
   - `observation.screenshot`(**settle** 截图):不是在动作*开始*时截,而是等键鼠"安静"下来(`SETTLE_DELAY_MS`,默认 1000ms 内没有新的鼠标/键盘事件)才截——鼠标按下/抬起、每次滚轮 tick、每次按键/松开都会重置这个倒计时。这样截图落在"上一个动作(或一整串打字/滚动)已经做完、UI 已经反应完"的那一刻,而不是"这个动作刚触发、鼠标可能刚移动到目标位置"的那一刻——后者会把"接下来要点哪里"这个信息提前泄露进本该是"动作前"的观测里(比如双击图标前那一步,画面里鼠标已经在图标上了)。`trajectory.py` 用"这个 action 开始前最近的一张已安定截图"作为它的 observation,天然覆盖 step 0(截图前没有任何动作)和最后一步(它前面只有停止时那张)。代价是:如果两个动作之间的间隔比 `SETTLE_DELAY_MS` 还短,后一步可能会复用前一步那张稍旧的图——这是刻意的取舍,比另外两种失败模式(要么漏拍,要么把下一步的鼠标位置泄露进当前观测)更可接受。
   - `action.screenshot`(**trigger** 截图,task.md 原 schema 之外新增的字段):动作*触发瞬间*截的图(鼠标按下、滚动的第一下、几乎每次按键都会触发),用"这个 action 开始后最近的一张 trigger 截图"匹配,展示在 Action 区块、UIA target 下面,配合点击位置标记和 bounding_rect 框一起看,方便确认这一下具体点在了哪里。这张图**不**用作任何 step 的 observation,只是给这个 action 自己看的。
   - `SETTLE_DELAY_MS` 和 trigger 截图的采集延迟都是可调常数(`input_recorder.py` 里),task.md 未规定具体数值。
   - `observation.cursor`(`[x, y]` 或 `null`,同样是 task.md 原 schema 之外新增的字段):这张 settle 截图对应时刻,鼠标光标最后一次已知的位置,取自 `mouse_down`/`mouse_up`/`scroll` 里最近的坐标。HTML 里只画一个光标点,不带 bounding_rect 框(那个框是"即将点哪"的信息,只属于 action.screenshot)。因为没有 hook 鼠标移动事件(`task.md` 的 action 类型里也没有"鼠标移动"这一项),两次点击之间光标的位置只能是"最后一次已知坐标",不是连续轨迹——比如中间隔了一长串纯键盘操作(打字、快捷键),这段时间里光标点在画面上不会动,这是数据本身的限制,不是渲染错误。
2. **轮询间隔**:`window.py` 约 200ms(task.md 未给出数值);`processes.py` 约 500ms(task.md 给了这个建议值)。
3. **shell_command 与 transcript 的对应**:PowerShell transcript 本身不带逐条命令的时间戳(只有整个 transcript 的开始/结束时间),所以按"PowerShell 窗口里出现 Enter 的顺序"依次消费解析出的命令列表,不做时间戳精确匹配。
4. **`--output` 参数语义**:作为输出根目录,实际 episode 目录是 `<output>\<episode_id>`,对应 task.md 示意图里的 `output/<episode_id>/`。
5. **`timestamp` 时区**:固定按 `+08:00` 渲染(task.md 没有说明如何取 VM 实际时区)。
6. **进程过滤**:按用户名后缀匹配(去掉 `NT AUTHORITY\SYSTEM` 这种域/机器前缀)+ 一份系统后台进程名单(svchost、conhost 等)。`exit` 事件如果查不到 user 信息(进程已退出),只要它在 `start` 时被采纳过就仍然保留,避免误删。
7. **type_text 的 focused UIA 查询时机**:在"打字序列可能开始"时查一次(非修饰键、且与上次按键间隔超过 1.5 秒),不是每次按键都查;匹配到具体 action 时,同样用"查询时间 ≥ 打字开始时间"(而不是"之前最近一次"),原因和截图匹配方向一致。
8. **录屏帧率**:ffmpeg 固定用 10fps(task.md 未规定具体数值)。
9. **UIA 查询匹配窗口**:`raw` 里的 `uia_query` 事件与对应 `mouse_down` 的时间差在 0~2.5 秒内都算匹配上(覆盖 task.md 提到的"查询超过 2 秒记为 null"这个上限)。
10. **停止检测轮询间隔**:0.3 秒检查一次 `STOP` 文件。
11. **`artifacts/` 复制依据**:按 `raw/events.jsonl` 里记录的 created/modified/renamed 事件复制对应文件,而不是重新扫描整个监控目录做前后快照对比。
12. **PowerShell 版本覆盖**:只安装到 Windows PowerShell 5.1 和 PowerShell 7(pwsh,如果已安装)的 AllUsersAllHosts profile,不管 Windows PowerShell ISE 等其他宿主。
