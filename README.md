# Windows Trajectory Recorder V1 + V1.1

在 Windows VM 中后台记录 computer use 轨迹:鼠标键盘、UIA element、截图/录屏、活动窗口、终端、文件变化、进程变化(V1),外加录制期间对工程文件的 git 快照(V1.1)。详细需求见 [task.md](task.md)(V1)和 [task-v1.1.md](task-v1.1.md)(V1.1,文件快照)。

开发环境是 macOS,最终运行在 Windows VM。**本 README 里标注"未在 Windows 上验证"的部分,只是写了代码、在 macOS 上做过语法/导入检查(V1.1 的快照核心逻辑还做过 macOS 本地 git 集成测试,见下文),没有在真实 Windows 环境跑过,请按下面的步骤在 VM 上逐一验证。**

## 文件说明

| 文件 | 平台 | 做什么 |
|---|---|---|
| [trajectory.py](trajectory.py) | 跨平台(纯 stdlib) | 读取 `raw/events.jsonl` + `raw/terminal/*`,把底层事件合并成 8 种 action、划分 step、生成 `trajectory.jsonl` 和 `trajectory.html`。不依赖任何 Windows 模块,可在 macOS 上直接运行和调试。V1.1:额外读 `meta.json`,给每个 step 填 `snapshot` 字段,并生成 `snapshots/index.json`。 |
| [uia.py](uia.py) | Windows | 封装 UIA 查询(`uiautomation` 库):`query_at_point(x,y)` 查坐标下的 element,`query_focused()` 查当前焦点 element,查询超过 2 秒或失败一律返回 `None`。 |
| [screen.py](screen.py) | Windows | `take_screenshot(path)` 截整个虚拟桌面(mss);`Recorder` 类封装 ffmpeg(gdigrab)录屏进程的启动/优雅停止。 |
| [window.py](window.py) | Windows | `get_active_window()` 取前台窗口标题+进程名(pywin32 + psutil);`poll_loop()` 轮询并在变化时回调。 |
| [processes.py](processes.py) | Windows | 每 0.5 秒轮询一次全部进程(psutil),和上一次快照做 diff,回调 start/exit 事件(全量,不过滤)。 |
| [files.py](files.py) | Windows | 用 `watchdog` 监控 `--watch` 指定目录,回调 created/modified/deleted/renamed(必须显式传入目录,不默认监听整盘)。 |
| [terminal.py](terminal.py) | Windows | 写入/清除 `C:\ProgramData\trajrec\current_episode.txt`,告诉 PowerShell profile 钩子把 transcript 写到哪。transcript 本身的解析在 `trajectory.py` 里(纯文本,不需要 Windows API)。 |
| [input_recorder.py](input_recorder.py) | Windows | 全局键鼠钩子(pynput)。回调里只记时间戳入队,UIA 查询和截图分别丢到两个独立的后台线程处理,避免互相卡顿。截图分两种:动作*触发瞬间*立刻截一张("trigger",给 `action.screenshot` 用),键鼠安静下来(`SETTLE_DELAY_MS`)后再截一张("settle",给下一步的 `observation.screenshot` 用)。检测到"打字序列开始"时查询一次 focused UIA element。V1.1:额外用 `win32_event_filter` 真正吞掉 Ctrl+Alt+Shift+M(milestone 快捷键),不进入轨迹。 |
| [snapshot.py](snapshot.py) | 跨平台(git 本身跨平台;由 Windows-only 的 `main.py` 驱动) | V1.1 新增。管理每个 episode 自己的 bare git 仓库,在文件变更/milestone/录制起止时提交一次快照。唯一封装 git 调用的地方(`run_git`),`restore.py` 也复用它。 |
| [restore.py](restore.py) | 跨平台(导出模式)/ Windows(就地恢复模式实际会用到) | V1.1 新增。独立命令行工具:列出/导出/就地恢复某个 episode 的某个文件版本。 |
| [main.py](main.py) | Windows | 启动顺序、协调所有模块、初始观测、等待停止信号、收尾(复制 artifacts、生成 trajectory)。V1.1:检查 `--output`/`--watch` 不嵌套、初始化/收尾 snapshot 仓库、写 `meta.json`。 |
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
- **git**(V1.1):装好并加入 PATH。检测不到就自动关闭文件快照功能、继续正常录制,不影响 V1 的其余部分。
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
python snapshot.py --watch C:\task --episode-dir C:\tmp\snap_test  # V1.1:在 C:\task 下新建/编辑文件,等 3 秒看是否自动 commit;回车触发一次 milestone;Ctrl+C 结束看 repo.bundle 是否生成
```

## 整体运行与停止

```powershell
python main.py --output C:\traj --task task.json --watch C:\task
```

- 启动后会打印 `[trajrec] 初始状态已记录,可以开始操作`,这之前的键鼠输入不算数。
- 输出目录:`C:\traj\<episode_id>\`(`episode_id` 是启动时间,如 `20260922_103215`)。
- **停止**:按 `Ctrl+C`,或者在另一个窗口/宿主机创建文件 `C:\ProgramData\trajrec\STOP`。
- 停止后自动生成 `trajectory.jsonl`、`trajectory.html`,并把录制期间有变化的文件复制进 `artifacts/`。
- V1.1:文件快照默认开启,加 `--no-snapshot` 关闭。**`--output` 不能位于 `--watch` 之内**,否则启动时直接报错退出(轨迹数据会被快照吃进去)。所有要被快照的工程文件都必须放在 `--watch` 目录里,别的地方不会被快照。

修改合并规则、想从 raw 数据重新生成轨迹(不用重新录制):

```powershell
python trajectory.py C:\traj\20260922_103215
```

## 文件快照(V1.1)

录制期间,`--watch` 目录会被当作一个本地 git 工作树,在关键时刻自动 commit 一次,让每个 step 都能对应到一个确定的文件版本。完整设计见 [task-v1.1.md](task-v1.1.md),这里只讲怎么用、怎么验证。

### 目录约定:为什么 GIT_DIR 和 GIT_WORK_TREE 分开

- `--watch`(工作目录):纯工程文件,**不会**出现 `.git`。
- `--output\<episode_id>\snapshots\repo.git\`:这个 episode 专属的 **bare** git 仓库(`GIT_DIR`)。
- 每次 git 调用都通过 [snapshot.py](snapshot.py) 唯一的 `run_git()` 封装,同时设置 `GIT_DIR=...\repo.git` 和 `GIT_WORK_TREE=<watch 目录>` 两个环境变量,而不是在 `--watch` 里放一个真正的 `.git`。这样做的好处:
  - 每个 episode 的提交历史天然独立,不会把多次录制混在一起,也不会因为忘记清理而越滚越大。
  - `--watch` 目录本身保持干净,不会意外把 `.git` 暴露给用户或被 SolidWorks/AutoCAD 这类软件的文件浏览器扫到。
  - 不会跟用户自己在 `--watch` 目录里可能已有的 git 仓库冲突。
  - `.gitignore`(过滤 `~$*`、`*.tmp` 等编辑器/软件锁文件、临时文件)按 git 的规则必须放在工作树里,所以是写进 `--watch\.gitignore`,不是仓库本体。

  唯一的例外是 `git init --bare` 本身:这条命令如果检测到 `GIT_WORK_TREE` 被设置就会直接拒绝执行(bare 仓库按定义不允许有工作树),所以 `run_git()` 留了一个 `set_work_tree=False` 开关专门给这一次初始化调用用,别的地方都不需要关心这个细节。

### 触发时机

| 触发 | 条件 | reason |
|---|---|---|
| 文件变更 | `--watch` 下有变化,且此后连续 3 秒没有新的文件事件(最长等 30 秒,超时仍提交并标记 `quiet_timeout`) | `file_changed` |
| 主动(milestone) | 见下面两种入口 | `milestone` |
| 起止 | 录制开始、录制结束各一次 | `episode_start` / `episode_end` |

同一时刻只有一个快照在执行(等静默期间也算在内)。这期间新来的 `file_changed` 直接丢弃(反正当前这次快照本来就会把最新状态提交进去);新来的 milestone 会排队,当前快照做完后立刻补一次,多个 milestone 会合并成一个、名称取最后一个。

两种触发 milestone 的方式:

1. **控制文件**(给 agent / supervisor / 宿主机用):
   ```powershell
   "完成草图" > C:\ProgramData\trajrec\MILESTONE   # 带名称
   New-Item C:\ProgramData\trajrec\MILESTONE       # 不带名称
   ```
   Recorder 每 0.5 秒检查一次,读到内容后会**删除该文件**再执行快照(不删除会重复触发)。
2. **快捷键**(给 VM 里的标注员用):**Ctrl+Alt+Shift+M**。这个组合键会在底层键盘钩子里被真正"吞掉"(`pynput` 的 `win32_event_filter` + `suppress_event`),不会传给 SolidWorks/AutoCAD 等前台软件,也不会出现在 `trajectory.jsonl` 里。触发后屏幕右下角会弹出一个短暂提示("已记录检查点"),让人知道生效了。

### 轨迹里的 `snapshot` 字段

`trajectory.jsonl` 每个 step 会多一个 `snapshot` 字段,表示"这一步动作之后"的文件版本:

```yaml
snapshot:
  file_ckpt: "a3f9c1e8d2b0"   # git commit hash 前 12 位
  name: null                  # 若这个版本来自 milestone,则为其名称
  vm_ckpt: null                # 预留给下一版的 VM 快照,本版恒为 null
```

连续多个 step 指向同一个 `file_ckpt` 是正常的,说明这期间文件没有变化。快照功能关闭时,整个字段是 `null`。`snapshots/index.json` 里能看到全部快照的清单(含每个快照归属的 `step_id`)。

### 用 restore.py 恢复或导出

```powershell
python restore.py <episode_dir> --list                            # 列出所有版本
python restore.py <episode_dir> --step 12 --export D:\check       # 导出到另一个目录(默认,安全,不碰工作目录)
python restore.py <episode_dir> --step 12 --in-place              # 就地恢复 --watch 目录本身
python restore.py <episode_dir> --step 12 --in-place --kill       # 同上,自动结束相关软件进程
```

也可以用 `--commit <hash>` 代替 `--step`;`--work-dir` 用来覆盖 `index.json` 里记录的原始工作目录路径(episode 数据被拷到别的机器上时需要)。

**⚠️ 就地恢复(`--in-place`)前必须先关闭正在打开这些文件的软件**,否则它内存里还是恢复前的旧状态,下次它自己保存就会把刚恢复的文件又覆盖回去。不加 `--kill` 时会打印检测到的相关软件进程列表,等你手动关闭后按回车继续;加 `--kill` 会直接结束这些进程(等 3 秒让它们把锁文件/临时文件清理干净)。恢复后,任何"该版本之后才新建"的文件不会被删除,而是移进 `--watch\_discarded\<时间戳>\`。

导出模式(默认)只读 git 对象(`git archive`),完全不碰 `--watch` 目录,可以在任何机器上跑,常用来做验收、比对、生成训练数据。

### 已知限制

- **软件里没保存的改动不在快照里**——本版完全不碰 COM/软件 API,快照只看磁盘上的文件,这是任务本身接受的限制。
- **真正意义上的空文件夹不会被记录、也恢复不出来**——这是 git 本身的限制(git 只认文件,不单独追踪目录),不是这个工具的 bug。文件夹里只要有文件,文件夹本身会随着文件路径一起被正确恢复(已用嵌套两层的目录验证过);但如果某个空目录本身要求必须存在(比如某软件依赖一个空的输出目录),快照/恢复这块处理不了。
- git 不可用,或初始化仓库失败,快照功能会自动关闭并继续正常录制(`meta.json` 里的 `snapshot.reason` 会记原因),不影响 V1 的其余采集。
- 单个变更文件超过 200MB 时只会额外记一条警告事件,不做特殊处理(仍然正常提交)。

## 尚未在 Windows 上验证的部分

以下功能只写了代码、在 macOS 上做过 import/语法检查(不会崩溃、平台判断正常),**没有在真实 Windows 环境跑过**,需要在 VM 上按上面的步骤逐一验证:

- 键鼠全局钩子(`input_recorder.py` 的 pynput 部分)是否能正确捕获、是否会被系统权限/UAC 拦截。
- UIA 查询(`uia.py`)在真实控件(尤其是非标准控件,如 SOLIDWORKS 这类专业软件的自绘 UI)上的准确率和耗时。
- DPI 感知设置后,鼠标坐标、UIA `bounding_rect`、截图像素三者是否真的一一对应。
- `screen.py` 的 ffmpeg gdigrab 录屏是否正常工作、`stop()` 的优雅退出是否总能生成完整 mp4。
- `install_profile.ps1` 对 AllUsersAllHosts profile 的写入是否在目标 Windows 版本上按预期路径生效,`Start-Transcript -IncludeInvocationHeader` 的实际输出格式是否与 `trajectory.py` 的解析逻辑一致(见下面"简化"里的第 3 条)。
- `processes.py` 里 `psutil` 返回的 `username()` 格式(是否总是 `域\用户名` 这种形式)。
- 整个 `main.py` 的启动/停止流程在真实多线程 + 真实 I/O 延迟下是否有竞态(尤其是停止时截图与录屏收尾的时序)。
- **V1.1、Windows 特有、完全没测过**:
  - Ctrl+Alt+Shift+M 是否真的会在 `win32_event_filter` 里被吞掉,专业软件(SolidWorks/AutoCAD 等)是否真的收不到这个组合键、不会触发它们自己的功能。
  - 屏幕角落的 tkinter 提示是否会正常弹出并消失(尤其在软件全屏/独占显示模式下)、是否会抢焦点。
  - `C:\ProgramData\trajrec\MILESTONE` 控制文件轮询在真实文件系统延迟下是否可靠,以及大文件(≥50MB)保存时"静默等待"的实际耗时是否符合预期。
  - `restore.py --in-place --kill` 里的进程名单(`SOFTWARE_PROCESS_NAMES`)是否覆盖了实际会用到的软件,进程 kill 后 3 秒等待是否够用。
- **V1.1、跨平台逻辑、已在 macOS 上用真实本地 git 仓库测过**(不算"未验证"里,但列出来说明验证范围):`snapshot.py` 的触发合并/丢弃规则、quiet-wait、episode_start/episode_end 提交、`repo.bundle` 打包,以及 `restore.py` 的 `--list`/`--export`/`--in-place`(含"版本之后新建文件移入 `_discarded`"这条)——见开发过程中跑的临时脚本,逻辑已用真实 commit 验证过,只是没在 Windows 真实文件系统 + 真实专业软件的场景下跑过。

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

以下是 task-v1.1.md 里没规定清楚、按最简单方案处理的地方:

13. **第一次 commit 的 `files_changed`**:task-v1.1.md 给的命令是 `git diff --name-only HEAD~1 HEAD`,但 episode_start 是仓库的根 commit,没有 `HEAD~1`。改用 `git diff-tree --no-commit-id --name-only -r --root HEAD` 列出根 commit 里的全部文件。
14. **快照关闭时 `index.json` 仍然写**:不是干脆不生成这个文件,而是写 `{"work_dir": ..., "enabled": false, "checkpoints": []}`,方便下游统一按同一份 schema 读取,不用先判断文件存不存在。
15. **`meta.json`**:V1 里没有这个文件,V1.1 新增,只放两样东西——`watch_dir`(绝对路径)和 `snapshot.enabled`/`snapshot.reason`。`trajectory.py` 生成 `snapshot` 字段和 `index.json` 时以这里的 `enabled` 为准,不是靠"有没有 snapshot 类型的 raw 事件"去反推(这样即使某次录制所有 commit 都意外失败,字段语义也不会变成"功能被关闭")。
16. **quiet-wait 对 `episode_start`/`episode_end` 同样生效**:task-v1.1.md 说这是"所有触发共用的前置步骤",没有为起止两个触发单独开后门,所以 `main.py` 停止录制时最长可能多等 30 秒(如果停止前 `--watch` 目录还在被写)。`episode_start` 因为发生在用户能操作之前,实际总是秒过。
17. **大文件警告事件的字段**:task-v1.1.md 只说"额外写一条警告事件",没给 schema。定义为 `{"type": "snapshot_large_file", "commit", "path", "size"}`。
18. **milestone 快捷键只吞 M 本身**:Ctrl/Alt/Shift 三个修饰键自己的 key_down/key_up 仍然正常写入轨迹(它们单独按不会触发专业软件的功能,保留下来对理解动作序列也无害),只有触发条件满足那一下 M 被吞掉、不进轨迹。
19. **`restore.py` 不加 `--kill` 时的行为**:task-v1.1.md 写"提示先关闭,等待用户确认后再继续",理解为打印进程列表后 `input()` 阻塞等回车,而不是直接报错退出——用户手动关掉软件后回车即可继续,不用重新执行命令。
20. **`restore.py --in-place` 判断"该版本之后新建的文件"**:没有用 `git status`(它检测不到"当前 HEAD 仍追踪、但目标版本没有"的文件,`git checkout -f <commit> -- .` 不会把这类文件从索引里摘掉),改成直接对比 `git ls-tree -r --name-only <commit>` 和工作目录实际文件列表,凡是磁盘上有、目标版本里没有的都移进 `_discarded`。
