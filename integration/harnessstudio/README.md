# 接入 HarnessStudio / vm_manager

vm_manager 在虚拟机进入 `running` 后，自动调用虚拟机内 Agent 服务（`C:\oem\server\main.py`，端口 5000）的 `POST /recording/start`；在停止或删除虚拟机之前，先调用 `POST /recording/stop`。本目录给 Agent 服务补上这两个接口，由它在用户桌面会话里启停采集器，再把结果放到 vm_manager 的采集目录（虚拟机里的 G 盘）。

| 文件 | 作用 |
| --- | --- |
| `trajrec_routes.py` | Agent 服务的录制接口，放在 `main.py` 旁边，由 `main.py` 注册 |
| `install_recorder.ps1` | 安装脚本：采集器、Python 环境、PowerShell 记录钩子、接口文件，可选修改 `main.py` |
| `test_trajrec_routes.py` | 接口的单元测试，用假采集器，Linux/macOS 上也能运行 |

## 一次采集的过程

1. vm_manager 调用 `/recording/start {recording_id, output_dir: "G:\<recording_id>", project_id, task_id, vm_id, chat_id}`。
2. 接口读取 `G:\task.json`（HarnessStudio 创建虚拟机时写入任务标题、目标等信息，没有时用默认值），把上面这些 ID 合并进去，然后启动：
   ```
   C:\trajrec\venv\Scripts\python.exe C:\trajrec\recorder\main.py --output C:\trajrec\episodes --episode-id <recording_id>
       --task C:\ProgramData\trajrec\<recording_id>\task.json --watch D:\ --no-watch-gitignore
       --stop-file C:\ProgramData\trajrec\<recording_id>\STOP --milestone-file C:\ProgramData\trajrec\<recording_id>\MILESTONE
   ```
   等到采集器打印“初始状态已记录”后返回 200，最多等 40 秒。
3. 录制期间：
   - 采集器写本地盘 `C:\trajrec\episodes\<recording_id>`。
   - 截图每分钟复制一次到 `G:\<recording_id>\screenshots`。
   - 带 `X-HarnessStudio-Operation: <操作 ID>` 请求头的 Agent 服务请求（HarnessStudio 的 Agent 工具调用），会把开始、结束时间（虚拟机时钟）追加到 `agent_requests.jsonl`，用来区分哪些步骤是 Agent 做的，哪些是人做的。
4. vm_manager 调用 `/recording/stop {recording_id}`：
   - 接口写入 STOP 文件，等采集器生成 `trajectory.jsonl` / `trajectory.html`。
   - 再把整个 episode 复制到 `G:\<recording_id>`，然后删除本地副本。
   - 50 秒内完成就返回结果，超时先返回 200，在后台继续完成。
5. 采集器异常退出或虚拟机中途关机时，本地留下的 episode 在 Agent 服务下次启动时处理：缺少 `trajectory.jsonl` 的先用 `trajectory.py` 从 `raw/` 重新生成，再复制到 G 盘。

宿主机上的结果目录是 `<data_root>/projects/<project_id>/recordings/<vm_id>/<recording_id>/`，即 vm_manager 返回的 `result_path`。

另外两个接口供 HarnessStudio 使用：

- `POST /recording/milestone {"name": "草图完成"}`：给正在进行的录制打一个命名检查点，对应一次文件快照。
- `GET /recording/status`：查询正在进行的录制，以及还在收尾的录制。

### 为什么不直接写 G 盘

G 盘是 SMB 网络盘。在虚拟机里实测，往 G 盘追加一行要 10–50 ms，本地盘只要 0.4 ms。采集器是在键鼠钩子回调里同步写 `raw/events.jsonl` 的，直接写 G 盘时：

- 输入被拖慢：Agent 输入同一段 44 个字符要 5.7 秒，写本地盘时只要 2.0 秒。
- 按键丢失：测试中 “hello trajectory” 只记下了 “hell … rajctory”。

改成写本地盘后，一次约 100 秒、434 个按键的连续输入，一个都没有丢。

录制期间只复制截图，是因为截图每张只写一次。robocopy 读文件时，采集器再以追加方式打开同一个文件会报 `Permission denied`（实测 15 次追加里失败 5 次）。这个异常发生在 pynput 的回调里，会让键盘监听永久停止，之后的按键全部丢失。这个问题出现过一次，所以 `events.jsonl`、日志和快照仓库都只在采集器结束后才复制。

## 修改镜像的步骤

在用来制作镜像的虚拟机里，以管理员身份执行以下步骤。以 AutoCAD 镜像 `data_v2_autocad.img` 和联合环境镜像 `data_v2_autocad_sw_ansys.img` 为例，两个镜像都要改。

1. 准备文件。把本仓库（不需要 `.git`）和 Python 依赖包拷进虚拟机，例如：
   - `F:\trajrec-bundle\recorder`：本仓库；
   - `F:\trajrec-bundle\wheels`：依赖包。

   依赖包可以在 Linux 宿主机上下载：
   ```bash
   pip download --only-binary=:all: --platform win_amd64 --python-version 3.10 --implementation cp -d wheels pywin32 uiautomation psutil mss watchdog
   pip download --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.10 -d wheels pynput six
   ```
   pynput 要单独加 `--no-deps`：否则 pip 会按宿主机 Linux 解析依赖，拉进 evdev，最后退回旧版 1.6.8。

   虚拟机能上网时可以不准备依赖包，去掉下一步的 `-Wheels` 参数即可。
2. 运行安装脚本：
   ```powershell
   powershell -ExecutionPolicy Bypass -File F:\trajrec-bundle\recorder\integration\harnessstudio\install_recorder.ps1 -Wheels F:\trajrec-bundle\wheels -PatchAgent
   ```
   脚本可以重复执行，会完成以下工作：
   - 把采集器复制到 `C:\trajrec\recorder`；
   - 用 Agent 服务所用的 Python 3.10（`C:\Users\Docker\AppData\Local\Programs\Python\Python310`）创建 `C:\trajrec\venv` 并安装依赖，不影响 Agent 服务自己的包；
   - 检查 ffmpeg（镜像里已有 `C:\ffmpeg\bin`）和 git（镜像里已有 `C:\Program Files\Git`）；
   - 安装 PowerShell 记录钩子（`install_profile.ps1`）；
   - 复制 `trajrec_routes.py` 到 `C:\oem\server`；
   - 在 `main.py` 的 `if __name__ == '__main__':` 之前加上下面两行，原文件备份为 `main.py.before-trajrec`：
     ```python
     import trajrec_routes  # HarnessStudio trajectory recording (/recording/*)
     trajrec_routes.register(app)
     ```
   - 最后检查采集器的所有模块都能导入。
3. 让 Windows 把硬件时钟当作 UTC：
   ```powershell
   reg add HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation /v RealTimeIsUniversal /t REG_DWORD /d 1 /f
   ```
   不加这一项会出问题。容器里 QEMU 用 `-rtc base=localtime` 启动，而容器时区是 UTC，所以硬件时钟里是 UTC 时间；镜像的 Windows 时区是太平洋时间，会把它当本地时间读。结果是：
   - 虚拟机每次重启后系统时间快 7 小时；
   - 大约 2 分钟后，Windows 跟 time.windows.com 同步，时间又跳回来；
   - 这正好落在开机后自动开始的那段录制里，事件时间倒退约 25000 秒，视频对齐也随之错乱。

   在测试虚拟机里加上这一项后重启，时间从开机起就正确，录制中没有再出现跳变。
4. 正常关机，保存镜像。

   注意：第 2 步修改 `main.py` 时，正在运行的 Agent 服务会被 Flask 的 debug reloader 重启。之前在运行中的虚拟机里遇到过重启后服务失去响应、被健康检查判为失败的情况。制作镜像时不受影响，因为接着就会关机。不要在 vm_manager 管理的、正在运行的虚拟机里执行带 `-PatchAgent` 的安装。

### vm_manager 配置

- 现有配置（`recorder_start_path: /recording/start`、`recorder_stop_path: /recording/stop`、`recorder_timeout_seconds: 60`）可以直接使用，不需要修改。
- 停止接口最多等 50 秒，可以用机器级环境变量 `TRAJREC_STOP_BUDGET` 调整，必须小于 `recorder_timeout_seconds`。
- 两小时级别的长录制，生成轨迹需要多久还没有实测。如果停止经常超过 50 秒，可以同时调大这两个值，例如 `recorder_timeout_seconds: 300`、`TRAJREC_STOP_BUDGET=280`。否则停止接口超时后，vm_manager 会马上关机，剩下的收尾工作要等这台虚拟机下次启动时才能完成；如果虚拟机直接被删除，这部分就丢了。

## 已经验证

测试环境：测试虚拟机 vm_ad7fd663（win11-autocad2026），测试项目 trajrec-test-20261019。

- 用 vm_manager 手动开始、停止，以及开机自动开始，都能正常录制。
  - 开始约 11 秒。
  - 停止 3–9 秒（2 分钟、445 张截图的录制，停止用了 5.7 秒）。
  - `trajectory.jsonl`、`trajectory.html`、视频、快照（`episode_start`、`file_changed`、`milestone`、`episode_end`）、`agent_requests.jsonl` 都完整。
  - D 盘里没有写入 `.gitignore`。
- 用 `-PatchAgent` 修改 `main.py` 并重启虚拟机后，接口由 `main.py` 自己注册，开机自动开始的录制能读到 `G:\task.json`。
- 本地留下的、缺少 `trajectory.jsonl` 的 episode，在下次启动时会重新生成并复制到 G 盘。
- Agent 服务重启后，会接管仍在运行的采集器（按 pid 文件找回）。
- 约 100 秒连续输入，期间经过多次定期复制，434 个按键没有丢失。

## 采集器本身的问题（未修改）

- `JsonlWriter.write` 在钩子回调里每个事件打开一次文件。只要别的进程（杀毒软件、索引服务、备份工具）恰好占用这个文件，回调就会抛异常，pynput 随即停止监听，之后的键鼠输入全部丢失，进程却还在运行。建议在 `write` 里捕获异常后重试，或者改成由单独的线程写文件。
- 每次按键都截一张触发截图，每张约 240 KB。两分钟的 Agent 输入产生了 445 张、106 MB，长时间录制会占很多空间。
- 早一轮测试里，每段连续输入的第一个键之后，`uia_focused` 查询期间丢过 2 个键（钩子停顿约 0.8 秒）。之后 434 个按键的长测试里没有再出现。
- `process_event` 里包含采集器自己做快照时启动的 `git.exe`。

## 测试

```bash
cd integration/harnessstudio && python -m unittest test_trajrec_routes   # 需要 Flask
cd ../.. && python -m unittest test_control_files
```
