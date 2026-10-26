# 镜像接入轨迹采集：操作步骤

2026-10-20 整理，供 richard 自己修改 Windows 镜像时照着做。路径和命令都已对照本机上的文件核对过。

标注说明：

- **已验证**：2026-10-19 在测试虚拟机 vm_ad7fd663（win11-autocad2026，项目 trajrec-test-20261019）上实际跑通。
- **未验证**：没有在虚拟机上跑过，是根据代码推断的。

依据的代码：

- winvm_traj_recorder 本地 main 分支的 HEAD 是 `119be44`，上一个提交是 `5962d75`。两个提交都还没有推送。
- 虚拟机内接口 `integration/harnessstudio/trajrec_routes.py`，安装脚本 `integration/harnessstudio/install_recorder.ps1`，原有说明 `integration/harnessstudio/README.md`。本文在原有说明的基础上，补充了 main.py 的另外两处修改、vm_manager 配置和验证方法。
- 旧补丁 `test-results/engine-e2e/repair/vm-image-recording-routes.py` 只录 ffmpeg 视频，已经被取代，不要再用。

## 最简步骤

实际要做的只有两件事，外加一条建议执行的注册表命令。下文的其余部分是背景说明和验证方法。

以下操作都在制作镜像的虚拟机里进行，用管理员身份打开 PowerShell：

1. **放入录制代码并安装环境。**
   - 现成的安装包在宿主机 `/data/hdd/vm_manager/projects/trajrec-test-20261019/shared/trajrec-bundle/`，里面有代码和离线依赖包。
   - 先把本目录下的 `oem-server-main.py` 复制进这个安装包。该目录属于 root，要用 `sudo cp`。
   - 再把整个安装包复制进虚拟机，例如放到 `F:\trajrec-bundle`，然后执行：
     ```powershell
     powershell -ExecutionPolicy Bypass -File F:\trajrec-bundle\recorder\integration\harnessstudio\install_recorder.ps1 -Wheels F:\trajrec-bundle\wheels
     ```
   - 这个脚本会完成以下工作，最后打印 `[install] done.`：
     - 把代码放到 `C:\trajrec\recorder`；
     - 创建 `C:\trajrec\venv` 并安装依赖；
     - 安装 PowerShell 记录钩子；
     - 把 `trajrec_routes.py` 复制到 `C:\oem\server`（不改动 main.py）。
2. **替换 main.py。**
   - `oem-server-main.py` 是按镜像里的原文件改好的，只改了两处：
     - 在 `if __name__ == '__main__':` 之前加了 3 行：注册录制接口，并把访问日志降到 WARNING；
     - 把 `app.run` 改为 `debug=False, threaded=True`。
   - 执行下面的命令。它会先比对原文件，一致才替换：
     ```powershell
     if ((Get-FileHash C:\oem\server\main.py).Hash -eq '9FE691352F2C13245AF6EFAF3504B52EFAC5BCCE1CE0C67837EB5C4D9B9AEA85') { Copy-Item F:\trajrec-bundle\oem-server-main.py C:\oem\server\main.py -Force; 'replaced' } else { 'main.py 与底稿不同，没有替换' }
     ```
   - 底稿是 2026-10-16 从联合环境虚拟机读出的 main.py。如果提示不同（例如 AutoCAD 镜像的 main.py 不一样），就按第 2 节手工改：加那 3 行，再改 `app.run` 这一行。
3. **建议执行：硬件时钟按 UTC。** 不加的话，每次开机后的录制里都会出现 7 小时的时间跳变（见 1.4）：
   ```powershell
   reg add HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation /v RealTimeIsUniversal /t REG_DWORD /d 1 /f
   ```
4. **关机，保存镜像。** 替换 main.py 后，正在运行的 Agent 服务会自动重启一次，不用管，直接关机即可。所以替换 main.py 要放在最后做。

vm_manager 不改也能用，现在的 60 秒超时实测够用。改成 300 秒只是为了留出余量，见 4.1。

## 0. 整体流程

vm_manager 会在虚拟机进入 running 后，自动调用虚拟机内 Agent 服务（5000 端口）的 `POST /recording/start`；在停止或删除虚拟机之前，调用 `POST /recording/stop`。这部分 vm_manager 已经实现，开机自动开始录制的行为保持不变，镜像里只需要提供这两个接口。

一次录制的过程：

1. **start**：`trajrec_routes.py` 读取 `G:\task.json`，在用户桌面会话里启动采集器。等采集器打印“初始状态已记录”后返回。启动命令：
   ```
   C:\trajrec\venv\Scripts\python.exe C:\trajrec\recorder\main.py --output C:\trajrec\episodes --episode-id <recording_id>
       --task C:\ProgramData\trajrec\<recording_id>\task.json --watch D:\ --no-watch-gitignore
       --stop-file C:\ProgramData\trajrec\<recording_id>\STOP --milestone-file C:\ProgramData\trajrec\<recording_id>\MILESTONE
   ```
2. **录制中**：数据写在本地盘 `C:\trajrec\episodes\<recording_id>`。截图每分钟用 robocopy 复制一次到 `G:\<recording_id>\screenshots`。
3. **stop**：写入 STOP 文件，等采集器生成轨迹，再把整个 episode 复制到 `G:\<recording_id>`，然后删除本地副本。最多等 50 秒；超时先返回 200，剩下的工作在后台继续完成。
4. **宿主机上的结果**：`/data/hdd/vm_manager/projects/<project_id>/recordings/<vm_id>/<recording_id>/`。

为什么不直接写 G 盘：G 盘是 SMB 网络盘，追加一行要 10–50 ms，本地盘只要 0.4 ms。采集器在键鼠钩子回调里同步写事件，直接写 G 盘时，输入会变慢，还会丢键。实测输入 “hello trajectory”，只记下了 “hell … rajctory”（rec_2e434d7c）。（已验证）

需要修改的两个镜像都在 `/data/hdd/ProSoftAgentArena/src/win-arena-container/vm/storage/`：

| 环境 ID | 镜像文件 |
| --- | --- |
| win11-autocad2026 | data_v2_autocad.img |
| win11-autocad2026-solidworks2025-ansys2024r1 | data_v2_autocad_sw_ansys.img |

**不要覆盖这两个文件。** 已有虚拟机的 C 盘是建在它们之上的增量盘，覆盖之后这些 C 盘会损坏。新镜像请另存为新文件名，例如 `data_v3_autocad.img`，然后按第 4.2 节添加。

## 1. 镜像里要放的东西

以下操作都在制作镜像的虚拟机里进行，用管理员身份打开 PowerShell。

### 1.1 在宿主机上准备安装包

可以直接使用上次测试用的安装包：`/data/hdd/vm_manager/projects/trajrec-test-20261019/shared/trajrec-bundle/`。

- 其中 `recorder/` 与当前仓库代码一致（比较过，只有 README 不同）。
- 其中 `wheels/` 是 Python 3.10 的依赖包：comtypes-1.4.17、mss-10.2.0、psutil-7.2.2、pynput-1.8.2、pywin32-312（cp310）、six-1.17.0、uiautomation-2.0.29、watchdog-6.0.0。

如果要重新打包：

```bash
cd /data/hdd/codes/HarnessStudio/winvm_traj_recorder
git log -1 --oneline        # 应为 119be44
T=$(mktemp -d)/trajrec-bundle && mkdir -p "$T"
git archive --format=tar --prefix=recorder/ HEAD | tar -x -C "$T"
pip download --only-binary=:all: --platform win_amd64 --python-version 3.10 --implementation cp -d "$T/wheels" pywin32 uiautomation psutil mss watchdog
pip download --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.10 -d "$T/wheels" pynput six
echo "$T"
```

pynput 必须单独下载并加 `--no-deps`。否则 pip 会按宿主机 Linux 解析依赖，拉进 evdev，最后退回旧版 1.6.8。

然后把 `trajrec-bundle` 放进制作镜像的虚拟机：

- 如果用 vm_manager 的虚拟机制作镜像，F 盘就是宿主机的 `/data/hdd/vm_manager/projects/<project_id>/shared/`。这个目录属于 root，复制时要用 `sudo cp -r "$T" /data/hdd/vm_manager/projects/<project_id>/shared/`。
- 下面的命令都假设安装包在 `F:\trajrec-bundle`。放在别处时，替换成实际路径即可。

### 1.2 检查镜像里已有的组件

```powershell
$py = "C:\Users\Docker\AppData\Local\Programs\Python\Python310\python.exe"   # Agent 服务用的 Python
Test-Path $py                                   # True
& $py -c "import flask; print('flask', flask.__version__)"
Test-Path C:\ffmpeg\bin\ffmpeg.exe              # True
& C:\ffmpeg\bin\ffmpeg.exe -version | Select-Object -First 1
git --version                                   # 文件快照需要 git
Test-Path C:\oem\server\main.py                 # True
```

- **ffmpeg**：采集器用它录制 `recording.mp4`。镜像的系统 PATH 里已经有 `C:\ffmpeg\bin`，`trajrec_routes.py` 启动采集器时也会把它加到 PATH 最前面，所以不需要改 PATH。如果 ffmpeg 装在别处，设置机器级环境变量 `TRAJREC_FFMPEG_DIR` 指向它所在的目录。
- **git**：镜像里已有 `C:\Program Files\Git`，PATH 中有 `...\Git\cmd`。没有 git 也能录制，只是没有文件快照。
- **Python**：镜像的 PATH 里还有 `C:\Python37`，不要用它。venv 必须用上面的 Python 3.10 创建，安装脚本默认就是这样。

### 1.3 运行安装脚本（先装到暂存目录）

运行中的 Agent 服务开启了 Flask 的 debug 自动重载，修改 `C:\oem\server\main.py` 会让服务立即重启。之前在运行中的虚拟机里遇到过：服务重启后不响应，被健康检查判为失败。

所以先把 main.py 复制到暂存目录，在副本上修改，到最后一步（2.6）再换进去，并且马上关机。

```powershell
New-Item -ItemType Directory C:\trajrec\agent-staging -Force | Out-Null
Copy-Item C:\oem\server\main.py C:\trajrec\agent-staging\main.py -Force
powershell -ExecutionPolicy Bypass -File F:\trajrec-bundle\recorder\integration\harnessstudio\install_recorder.ps1 -Wheels F:\trajrec-bundle\wheels -AgentDir C:\trajrec\agent-staging -PatchAgent
```

脚本可以重复执行。正常结束时会打印 `[install] recorder imports ok` 和 `[install] done.`。它会做以下几件事：

1. 把采集器复制到 `C:\trajrec\recorder`，不包括 `.git`、`samples` 和 `__pycache__`。
2. 用 Python 3.10 创建 `C:\trajrec\venv`，离线安装 `requirements.txt` 里的包（pywin32、uiautomation、pynput、psutil、mss、watchdog）。这些包只装在 venv 里，不影响 Agent 服务自己的包。
3. 检查 ffmpeg 和 git。
4. 安装 PowerShell 记录钩子 `install_profile.ps1`：写入 Windows PowerShell 5.1 和 PowerShell 7 的 AllUsersAllHosts `profile.ps1`。录制期间新打开的 PowerShell 窗口会记录到 `raw/terminal/pwsh_<pid>.txt`。
5. 把 `trajrec_routes.py` 复制到暂存目录，并在暂存的 main.py 中 `if __name__ == '__main__':` 之前插入注册接口的两行。插入前的副本备份为 `main.py.before-trajrec`。
6. 检查采集器的各个模块都能导入。

测试虚拟机里用同样的暂存方式安装过，当时的辅助脚本是 `trajrec-bundle/stage-patch.ps1`。（已验证）

### 1.4 让 Windows 把硬件时钟当作 UTC

```powershell
reg add HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation /v RealTimeIsUniversal /t REG_DWORD /d 1 /f
```

为什么需要：容器里的 QEMU 以 `-rtc base=localtime` 启动，而容器时区是 UTC，所以硬件时钟里是 UTC 时间；镜像里 Windows 的时区是太平洋时间，会把它当作本地时间读。

不加这一项的后果：

- 每次开机后，系统时间快 7 小时；
- 大约 2 分钟后，Windows 与 time.windows.com 同步，时间又跳回来；
- 这次跳变正好落在开机后自动开始的录制里。例如 rec_c72d7164 的最后一步，时间倒退了 25118 秒。

在测试虚拟机里加上这一项并重启后，时间从开机起就正确。（已验证）

### 1.5 可选项（未验证）

2026-10-16 的健康检查报告（`test-results/engine-e2e/repair/06-repair-report.md`）还建议：在镜像里关闭 Windows 自动维护（`\Microsoft\Windows\Defrag\ScheduledDefrag`）和 Defender 计划扫描，或者在制作镜像时预先跑完。这一项与录制无关，是否要做由你决定。

## 2. 修改 main.py

在暂存副本 `C:\trajrec\agent-staging\main.py` 上修改，共三处。

镜像里 main.py 有一份副本：2026-10-16 排查健康检查时，从失败的联合环境虚拟机 C 盘只读读出，共 3204 行，存在 `/tmp/claude-1000/-data-hdd-codes-HarnessStudio/4d483e2a-7e3d-452e-971b-cf39ffd2230c/scratchpad/vmdisk/vm-main.py`。下面的行号按这份副本。AutoCAD 镜像的 main.py 没有读过，应该相同（未验证），以你实际看到的为准。相关的行：

| 行 | 内容 | 说明 |
| --- | --- | --- |
| 65 | `logging.basicConfig(filename='server.log', level=logging.DEBUG, filemode='w')` | 服务的工作目录是 `C:\OEM`，所以日志在 `C:\OEM\server.log`；每次启动都清空 |
| 68 | `logger = logging.getLogger('werkzeug')` | main.py 自己的日志也用 werkzeug logger |
| 69 | `custom_logger = Logger(logger)` | 把 `print()` 的输出重定向成 werkzeug logger 的 INFO 日志 |
| 136 | `@app.errorhandler(Exception)` | 所有异常（包括 404）都返回 500，所以旧镜像上调用 `/recording/start` 得到的是 HTTP 500 |
| 3199–3200 | `if __name__ == '__main__':` / `    app.run(debug=True, host="0.0.0.0", port=args.port)` | |

### 2.1 找到相关的行

```powershell
$f = "C:\trajrec\agent-staging\main.py"
Select-String -Path $f -Pattern "^import logging|basicConfig|werkzeug|setLevel|app = Flask|/recording/|trajrec_routes|__main__|app\.run\(" | ForEach-Object { "{0,5}: {1}" -f $_.LineNumber, $_.Line.Trim() }
```

- **检查是否有旧补丁**：如果输出里有以 `/recording/` 开头的 `@app.route`，说明装过旧的 ffmpeg 补丁。这段代码位于 `# --- vm_manager trajectory recording contract` 和 `# --- end of recording contract ---` 之间，要整段删掉。否则旧接口先注册，会抢在新接口之前处理请求。2026-10-16 的报告说旧补丁没有装进镜像，这里只是确认一下。
- **检查安装脚本插入的两行**：输出里应该有 `import trajrec_routes ...` 和 `trajrec_routes.register(app)`，位于 `if __name__ == '__main__':` 之前。

### 2.2 注册录制接口（安装脚本已完成）

```python
import trajrec_routes  # HarnessStudio trajectory recording (/recording/*)
trajrec_routes.register(app)
```

- 这两行必须放在模块级（不缩进），位置在 `app = Flask(...)` 和所有 `@app.route` 之后、`app.run(...)` 之前。
- 如果安装脚本报错 `cannot find "if __name__ == '__main__':"`，就手工把这两行加在调用 `app.run` 的代码块之前。
- `register(app)` 做三件事：
  - 注册 `/recording/start`、`/recording/stop`、`/recording/milestone`、`/recording/status` 四个接口；
  - 加上钩子，记录带 `X-HarnessStudio-Operation` 请求头的请求；
  - 在后台处理上次没有处理完的 episode。

### 2.3 访问日志降到 WARNING

紧接在上面两行之后（仍在 `if __name__` 之前）加一行：

```python
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # no access-log line per request (2026-10-16 health check)
```

- 文件开头要有 `import logging`，2.1 的输出里应该能看到。如果没有，就加在文件最上面。
- 如果 `if __name__ == '__main__':` 里面又给 werkzeug 设置了日志级别，就把这一行移到那之后、`app.run` 之前。
- 修改后的效果：
  - `C:\OEM\server.log` 不再每 2 秒写一行 `INFO:werkzeug:... "GET /probe HTTP/1.1" 200`。
  - main.py 自己通过 werkzeug logger 写的 INFO 消息（例如 `recording dir set to ...`），以及第 69 行重定向过来的所有 `print()` 输出，也会一起消失，这是预期的。
  - WARNING、ERROR 和异常堆栈照常记录。
  - `trajrec_routes` 使用自己的 logger，不受影响。

### 2.4 关闭 debug，开启多线程

把 `if __name__ == '__main__':` 里的 `app.run(...)` 改为 `debug=False, threaded=True`，host、port 等其他参数保持原样：

```python
# 原来（第 3200 行）
    app.run(debug=True, host="0.0.0.0", port=args.port)
# 改为
    app.run(debug=False, threaded=True, host="0.0.0.0", port=args.port)
```

如果还有 `use_reloader=True` 或 `use_debugger=True`，一并删掉。

修改后的效果：

- **不再自动重载**：以后修改 main.py，服务不会自己重启。另外，debug 重载时，重载器的父进程也会执行一遍 main.py，录制接口的恢复线程会在两个进程里各跑一次。这一点是按 Werkzeug 的机制推断的，没有实际观察到问题。
- **不再有交互式调试器**：现在 server.log 里会打印 Debugger PIN，而宿主机能通过 Agent 端口访问这个服务。关掉调试器更安全。
- **`threaded=True`**：Flask 1.0 以后本来就是默认值，这里写明是为了不依赖默认值。

### 2.5 检查副本

```powershell
$py = "C:\Users\Docker\AppData\Local\Programs\Python\Python310\python.exe"
& $py -m py_compile C:\trajrec\agent-staging\main.py; if ($LASTEXITCODE -eq 0) { "main.py syntax ok" }
& $py -c "import sys; sys.path.insert(0, r'C:\trajrec\agent-staging'); import trajrec_routes; print('trajrec_routes import ok')"
Select-String -Path C:\trajrec\agent-staging\main.py -Pattern "trajrec_routes|getLogger\(.werkzeug.\)|app\.run\(" | ForEach-Object { "{0,5}: {1}" -f $_.LineNumber, $_.Line.Trim() }
```

最后一条命令的输出里，至少要按顺序出现以下四行：

1. `import trajrec_routes`
2. `trajrec_routes.register(app)`
3. `logging.getLogger("werkzeug").setLevel(logging.WARNING)`
4. `app.run(... debug=False, threaded=True)`

### 2.6 清理、换入、关机（最后一步）

```powershell
# 镜像里不能留下录制数据：新虚拟机开机时，会把 C:\trajrec\episodes 里的残留当作上次没处理完的录制，复制到它自己的 G 盘
Remove-Item C:\trajrec\episodes\* -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem C:\ProgramData\trajrec -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
if (-not (Test-Path C:\oem\server\main.py.before-trajrec)) { Copy-Item C:\oem\server\main.py C:\oem\server\main.py.before-trajrec }
Copy-Item C:\trajrec\agent-staging\trajrec_routes.py C:\oem\server\trajrec_routes.py -Force
Copy-Item C:\trajrec\agent-staging\main.py C:\oem\server\main.py -Force
Remove-Item C:\trajrec\agent-staging -Recurse -Force
shutdown /s /t 0
```

- 换入 main.py 后，旧服务会被 debug 重载器重启一次。因为马上就关机，不受影响。
- 关机后，按你平时的方法把磁盘另存为新的镜像文件。
- 建议不要在这台虚拟机里验证，而是用新镜像另建一台测试虚拟机来验证（第 5 节）。如果关机前又启动过这台虚拟机（例如想先看一眼），要注意：vm_manager 管理的虚拟机一开机就会自动开始录制。保存镜像前，要重新执行上面的两条清理命令。

## 3. 没有 Studio 的 task.json 时会怎样

先更正一点：Studio 这一侧其实已经写好，并且正在运行。

- harnessstudio 仓库 vm-manager-seat 分支有提交 `9ec45e5`（2026-10-19 19:32Z），实现了以下功能：
  - 在创建或启动虚拟机之前写 `G:\task.json`，内容包括 title、task_instruction、constraints、checklist、outputs，以及 `harnessstudio` 下的 project_id、task_id、task_code、environment_id、vm_id、owner、chat_id；
  - Agent 发往虚拟机的请求带 `X-HarnessStudio-Operation` 请求头；
  - 软件页可以回放录制、下载 ZIP。
- 正在运行的 Studio 容器 `harnessstudio-studio-1`，是 2026-10-19 19:59:19Z 用 19:33Z 构建的镜像启动的，镜像里包含这段代码。
- 宿主机上已经有 Studio 写的 task.json：vm_87c920d1（19:40Z）和 vm_b11ad0d0（10-20 02:06Z）。
- 但是虚拟机镜像里还没有录制接口，所以“Studio → 录制 → 回放”整条链路还没有在真实镜像上跑过。（未验证）

`G:\task.json` 就是宿主机上的 `/data/hdd/vm_manager/projects/<project_id>/recordings/<vm_id>/task.json`。每次调用 `/recording/start` 时读取一次。

以下几种情况都按“没有 task.json”处理：文件不存在、不是合法的 JSON、不是 JSON 对象、是带 BOM 的 UTF-8。这时不报错，录制照常进行，使用默认内容：

```json
{
  "title": "HarnessStudio 任务 <task_id>",
  "task_instruction": "",
  "harnessstudio": {"recording_id": "...", "project_id": "...", "task_id": "...", "vm_id": "...", "chat_id": null}
}
```

- 这份 task.json 会存进每条录制（`<recording_id>/task.json`），只用于 `trajectory.html` 顶部的任务卡片。`trajectory.jsonl` 里没有任务字段，步骤、截图、视频和快照都不受影响。
- 实例：vm_ad7fd663 上的 rec_75d73ce4 录制时没有 `G:\task.json`，标题是 “HarnessStudio 任务 recorder-install”，其余内容完整。（已验证）
- 如果开始录制时 G 盘 20 秒内连不上，同样读不到文件，也使用默认值。

现在只有两种情况会没有 task.json：

- 不经过 Studio、直接调用 vm_manager API 创建的虚拟机；
- Studio 写文件失败。Studio 的写入是尽力而为，失败时不报错。

需要时可以手工补一个。在 `POST /v1/vms` 返回 vm_id 之后、虚拟机进入 running 之前写入（首次开机需要 6～8.5 分钟，重新开机需要 1.5～2 分钟）：

```bash
sudo tee /data/hdd/vm_manager/projects/<project_id>/recordings/<vm_id>/task.json >/dev/null <<'EOF'
{"title": "镜像录制验证", "task_instruction": "在 D: 新建 notes.txt，输入一段文字并保存"}
EOF
```

- 文件必须是不带 BOM 的 UTF-8。在 Windows PowerShell 5.1 里用 `Set-Content -Encoding UTF8` 或 `Out-File -Encoding utf8` 写出的文件带 BOM，会被当成没有。
- 录制开始后再修改，只影响下一次录制。每次开机、每次 start 都是一条新的录制。

## 4. vm_manager 配置

### 4.1 recorder_timeout_seconds：60 改为 300

正在运行的 vm_manager 是 `/data/hdd/codes/vm_manager/main.py`（2026-10-16 03:19 PDT 启动，pid 449687），使用的配置文件是 `/data/hdd/codes/vm_manager/settings.json`。`/data/hdd/codes/HarnessStudio/vm_manager/` 是另一份拷贝，改它不起作用。

当前配置（只读查看过）：

```
"recorder_start_path": "/recording/start",
"recorder_stop_path": "/recording/stop",
"recorder_timeout_seconds": 60
```

两个路径不用改。建议把 `recorder_timeout_seconds` 改为 300。

这个值是 vm_manager 调用 start 和 stop 接口时的 HTTP 超时：

- **start**：最坏情况约 80 秒。G 盘和 D 盘还没重新连上时各等 20 秒，再等采集器就绪最多 40 秒。实测一般约 11 秒。设为 60 秒时，慢的那次会被当作失败并重试，最多重试 3 次。第二次调用会返回 `already recording`，通常没有后果。改为 300 秒可以留足余量。
- **stop**：虚拟机里的接口最多等 `TRAJREC_STOP_BUDGET`（默认 50 秒）就返回，剩下的工作在后台完成。所以只改这一项，stop 的行为不会变化。
- **如果要让 stop 等更久**：例如两小时的录制生成轨迹可能更慢（没有实测）。这时还要在镜像里设置机器级环境变量 `TRAJREC_STOP_BUDGET`，值必须小于 recorder_timeout_seconds，例如：
  ```powershell
  [Environment]::SetEnvironmentVariable("TRAJREC_STOP_BUDGET", "240", "Machine")
  ```
  但要注意总时长：
  - vm_manager 的停止和删除接口是同步的，要等录制停止和容器停止（`stop_timeout_seconds`，120 秒）都完成才返回；
  - Studio 等待停止、删除、冻结的时间是 `vmStopTimeoutMs`，默认 200 秒；
  - 预算设为 240 秒时，一次停止可能要 360 秒，Studio 会先报超时。

  所以在调大 Studio 的 vmStopTimeoutMs 之前，建议 `TRAJREC_STOP_BUDGET` 保持默认的 50。
- **代价**：如果虚拟机里的 Agent 服务卡住（像 10-16 那次），每次 start 或 stop 都要等满 300 秒才放弃。这期间这台虚拟机的实例锁一直被占用，手动停止会返回 VM_BUSY；失败虚拟机的关机也会推迟，最多 300 秒。

修改配置：

```bash
cd /data/hdd/codes/vm_manager
cp settings.json settings.json.bak-20261020
sed -i 's/"recorder_timeout_seconds": 60/"recorder_timeout_seconds": 300/' settings.json
grep -n recorder_timeout_seconds settings.json
python3 -m json.tool settings.json > /dev/null && echo settings.json ok
```

配置只在启动时读取，改完要重启 vm_manager。按 vm_manager README 的说法，重启不会停止任何虚拟机，服务启动后会恢复原来的状态。建议在没有虚拟机正在创建或停止时操作：

```bash
sudo ss -ltnp 'sport = :8000'                     # 找到监听 8000 端口的 python 进程
sudo kill -TERM <上一步看到的 python pid>
(setsid sudo env PYTHONDONTWRITEBYTECODE=1 $PWD/.venv/bin/python $PWD/main.py >> logs/vmm.log 2>&1 < /dev/null &)
sudo ss -ltnp 'sport = :8000'                     # 只能有一个监听进程
tail -n 5 logs/vmm.log                            # 应有 Application startup complete，没有 address already in use
curl -sS --noproxy '*' -m 8 http://127.0.0.1:8000/v1/vms | head -c 200
```

注意：2026-10-20 01:55:29Z 有人又启动过一个 vm_manager，因为 8000 端口已被占用，它立刻退出了（见 `logs/vmm.log` 末尾）。所以现在运行的仍是 10-16 启动的那个进程，超时仍是 60 秒。

### 4.2 为新镜像添加环境条目

`environments.json`（同一目录）也只在启动时读取，和 4.1 一起重启即可。有两种做法：

- **做法 A**（vm_manager README 的规则）：新增一个环境条目。复制原条目，改掉 `environment_id`（例如 `win11-autocad2026-rec`）和 `base_img`（新文件名）。Studio 通过 `GET /v1/environments` 获取环境列表，新 ID 会出现在 studio_software_catalog 里。
- **做法 B**：在原条目里只把 `base_img` 改成新文件名。每台虚拟机创建时，都会把当时的环境配置存进数据库（`environment_json`），所以已有的虚拟机继续用旧文件，新建的虚拟机用新文件，Studio 这边的环境 ID 也不用变。这是根据代码推断的。（未验证）在所有旧虚拟机删除之前，旧镜像文件不能删除，也不能修改。

建议先用做法 A 加一个测试条目来验证新镜像。验证通过后，再用做法 B 切换原来的两个条目。

两种做法都要求 `base_format` 与新文件的实际格式一致（现在是 `raw`），否则 vm_manager 启动检查不通过，会直接退出。

## 5. 重建镜像后怎样验证

用新镜像在一个临时项目里创建测试虚拟机，不要用正式项目。

### 5.1 创建测试虚拟机

```bash
VMM=http://127.0.0.1:8000
curl -sS --noproxy '*' -X POST $VMM/v1/vms -H 'Content-Type: application/json' \
  -d '{"project_id": "trajrec-image-check-20261020", "task_id": "check", "environment_id": "<新环境 ID>", "spec": {"cpu_cores": 4, "ram_gb": 8}, "lifecycle": {"timeout_minutes": 120}}'
VM=<返回的 vm_id>
```

创建后马上按第 3 节写入 task.json。这一步是可选的，用来检查任务卡片。也可以在 Studio 里新建任务，从软件页打开虚拟机，Studio 会自己写 task.json。

### 5.2 开机后确认录制已开始

```bash
curl -sS --noproxy '*' $VMM/v1/vms/$VM | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(d["status"], d["current_recording_id"], d["access"]["agent_port"])'
curl -sS --noproxy '*' "$VMM/v1/recordings?vm_id=$VM" | python3 -m json.tool
AP=<上面的 agent_port>
curl -sS --noproxy '*' -m 10 http://127.0.0.1:$AP/recording/status
```

- **正常情况**：状态变为 running 后一分钟内，`current_recording_id` 有值，采集记录的状态是 `recording`；`/recording/status` 返回 `"recording_id": "rec_..."` 和 pid。
- **接口没有注册**：如果采集记录是 `failed`，错误为 `开始采集失败 3 次：/recording/start: HTTP 500`，说明 main.py 还是旧的。现在正在运行的 vm_b11ad0d0（旧镜像）就是这种情况：它的 rec_736d1849 失败了，只留下一个空目录，不影响其他功能。
- **确认 debug 已关闭**：在虚拟机里执行 `Get-Content C:\OEM\server.log -Tail 20`。输出里不应有 `Debugger is active!`，也不应有每 2 秒一行的 `GET /probe`。

### 5.3 录一段操作

通过 VNC 在虚拟机里操作：

1. 打开记事本，输入一句已知的话，例如 `hello trajectory 12345`，保存为 `D:\notes.txt`。
2. 打开一个 PowerShell 窗口，执行 `dir D:\`。
3. 按 Ctrl+Alt+Shift+M 打一个检查点。也可以在宿主机上调用接口：
   ```bash
   curl -sS --noproxy '*' -m 10 -X POST http://127.0.0.1:$AP/recording/milestone -H 'Content-Type: application/json' -d '{"name": "check-1"}'
   curl -sS --noproxy '*' -m 10 -H 'X-HarnessStudio-Operation: op-check-1' http://127.0.0.1:$AP/probe
   ```
   第二条命令模拟一次 Agent 请求，会记入 `agent_requests.jsonl`。

录制开始 1～2 分钟后，宿主机的 `.../recordings/$VM/<recording_id>/screenshots/` 里应开始出现截图。录制期间只复制截图。

### 5.4 停止

```bash
curl -sS --noproxy '*' -X POST $VMM/v1/vms/$VM/stop
```

这个接口是同步的：先停止录制（实测 3～9 秒），再关机（最多 120 秒）。

如果只想停止录制、不关机：

```bash
curl -sS --noproxy '*' -X POST $VMM/v1/recordings/<recording_id>/stop
```

### 5.5 检查结果目录

结果在 `/data/hdd/vm_manager/projects/trajrec-image-check-20261020/recordings/$VM/` 下：

```
task.json                     Studio 或你写的 G:\task.json（没有写就不存在）
<recording_id>/
  task.json                   合并了 recording_id 等 ID；没有 G:\task.json 时是默认值
  meta.json                   watch_dir、快照是否启用、软件适配器
  recorder.log                最后一行是 “[trajrec] 完成: C:\trajrec\episodes\<recording_id>”
  recording.mp4               屏幕录像
  screenshots/                000000.png …（每次按键、点击各一张）
  trajectory.jsonl            每行一个步骤
  trajectory.html             离线查看页面
  raw/events.jsonl            原始的键鼠、窗口、进程事件
  raw/adapters/、raw/adapters.log   软件适配器（AutoCAD）
  raw/terminal/pwsh_<pid>.txt 录制期间新打开的 PowerShell 窗口（这次接入测试中没有录到过，未验证）
  snapshots/index.json、repo.bundle、repo.git/   D 盘文件快照
  artifacts/                  录制期间改动过的 D 盘文件
  agent_requests.jsonl        只有收到带 X-HarnessStudio-Operation 的请求时才有
```

**检查 trajectory.jsonl。** 下面的脚本只读不写，用 Python 3 在宿主机上运行，不需要 sudo。它检查以下几项：

- 必需的文件都存在；
- 每一行都是合法的 JSON；
- step_id 从 0 开始连续递增；
- t_ms 没有倒退；
- 步骤里引用的截图都存在；
- 快照里有 episode_start 和 episode_end；
- recorder.log 里有“完成:”。

```bash
D=/data/hdd/vm_manager/projects/trajrec-image-check-20261020/recordings/$VM/<recording_id>
python3 - "$D" <<'EOF'
import json, os, sys
d = sys.argv[1]
ok = True
def bad(msg):
    global ok
    ok = False
    print("  [缺失/异常]", msg)
for name in ("task.json", "meta.json", "recorder.log", "recording.mp4", "trajectory.jsonl", "trajectory.html",
             "raw/events.jsonl", "snapshots/index.json", "snapshots/repo.bundle"):
    if not os.path.isfile(os.path.join(d, name)):
        bad(name)
steps = []
try:
    with open(os.path.join(d, "trajectory.jsonl"), encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            try:
                steps.append(json.loads(line))
            except ValueError:
                bad("trajectory.jsonl 第 %d 行不是 JSON" % n)
except OSError:
    pass
ids = [s.get("step_id") for s in steps]
if ids != list(range(len(steps))):
    bad("step_id 不是从 0 连续递增")
times = [s.get("t_ms") or 0 for s in steps]
if any(b < a for a, b in zip(times, times[1:])):
    bad("t_ms 有倒退（虚拟机时钟跳变，见 1.4）")
shots = [p for s in steps for p in ((s.get("observation") or {}).get("screenshot"), (s.get("action") or {}).get("screenshot")) if p]
missing = [p for p in shots if not os.path.isfile(os.path.join(d, p))]
if missing:
    bad("%d 张引用的截图不存在，例如 %s" % (len(missing), missing[0]))
kinds = {}
for s in steps:
    k = (s.get("action") or {}).get("type") or "(无动作)"
    kinds[k] = kinds.get(k, 0) + 1
try:
    with open(os.path.join(d, "snapshots", "index.json"), encoding="utf-8") as f:
        reasons = [c.get("reason") for c in json.load(f).get("checkpoints", [])]
except (OSError, ValueError):
    reasons = []
if reasons and ("episode_start" not in reasons or "episode_end" not in reasons):
    bad("快照缺少 episode_start / episode_end")
try:
    with open(os.path.join(d, "recorder.log"), encoding="utf-8", errors="replace") as f:
        log = f.read()
except OSError:
    log = ""
if "完成:" not in log:
    bad("recorder.log 里没有“完成:”，采集器可能没有正常结束（轨迹可能是下次开机时从 raw/ 补生成的）")
try:
    with open(os.path.join(d, "agent_requests.jsonl"), encoding="utf-8") as f:
        agent = sum(1 for _ in f)
except OSError:
    agent = 0
try:
    with open(os.path.join(d, "task.json"), encoding="utf-8") as f:
        task = json.load(f)
except (OSError, ValueError):
    task = {}
print("  步数 %d，动作 %s" % (len(steps), kinds))
print("  截图文件 %d 张，快照 %s，Agent 请求 %d 条" % (len(os.listdir(os.path.join(d, "screenshots"))) if os.path.isdir(os.path.join(d, "screenshots")) else 0, reasons, agent))
print("  任务标题：%s" % task.get("title"))
print("  结论：%s" % ("通过" if ok else "有问题，见上"))
EOF
```

这个脚本用测试虚拟机的旧录制试过：

- rec_75d73ce4 输出“通过”：6 步、20 张截图、快照 `['episode_start', 'file_changed', 'milestone', 'episode_end']`、3 条 Agent 请求、标题是默认值。
- rec_c72d7164 报告了 `t_ms 有倒退`，这正是加 1.4 注册表项之前录的那条。

**检查是否丢键。** 上面的脚本发现不了丢键，需要把记录下的文字和你实际输入的对照一下：

```bash
python3 -c "import json,sys; [print(s['step_id'], a['type'], a.get('text') or a.get('key') or a.get('keys')) for s in map(json.loads, open(sys.argv[1]+'/trajectory.jsonl', encoding='utf-8')) for a in [s.get('action')] if a]" "$D"
```

`hello trajectory 12345` 应该完整出现。空格会单独记成一个 `key_press space` 步骤，前后的文字分别是 `type_text`。如果像 rec_2e434d7c 那样只剩 “hell … rajctory”，说明丢键了。

**检查 trajectory.html。** 把整个 `<recording_id>` 目录复制到你自己的电脑上（例如用 `scp -r`），用浏览器打开其中的 `trajectory.html`。应该能看到：

- 顶部任务卡片的标题；
- 每一步都有截图、动作说明和窗口标题。

也可以在 Studio 软件页的录制列表里点“回放”或“下载 ZIP”。这个功能已经部署，但还没有用真实录制端到端检查过。（未验证）

**其他检查项：**

- D 盘里没有被写入 `.gitignore`：
  ```bash
  ls -la /data/hdd/vm_manager/projects/trajrec-image-check-20261020/tasks/check/
  ```
- 验证完删除测试虚拟机。这会删除虚拟机和它的 C 盘增量盘，录制结果目录会保留：
  ```bash
  curl -sS --noproxy '*' -X DELETE $VMM/v1/vms/$VM
  ```

## 6. 已知问题

1. **偶尔丢键。** 在早一轮测试中，每段连续输入的第一个键之后会做一次 `uia_focused` 查询，查询期间键盘钩子停顿约 0.8 秒，60 多个键里丢了 2 个。之后约 100 秒、434 个键的连续输入没有再丢。这个问题没有修，根因也没有确认：UIA 查询是在单独的线程里执行的，为什么会拖住钩子，还不清楚。
2. **文件被占用时，键盘监听会永久停止。** `JsonlWriter` 每写一个事件就打开一次文件。如果别的进程（杀毒软件、索引服务、备份工具）恰好占用 `events.jsonl`，钩子回调会抛异常，pynput 随即停止监听。进程还在运行，但之后的输入全部丢失。录制期间不复制 `events.jsonl`，就是为了避免这个问题。采集器本身没有修改；建议在写入时捕获异常后重试，或者改由单独的线程写文件。
3. **截图占空间。** 每次按键都截一张图，每张约 240 KB。2 分钟的 Agent 输入产生了 445 张、106 MB（rec_c72d7164）。录制期间整个 episode 都存在 C 盘上，两小时的录制需要多少空间没有实测，镜像的 C 盘要留足空间。
4. **长录制的停止时间没有实测。** 如果收尾超过 50 秒，stop 接口会先返回 200，vm_manager 随即关机，剩下的工作要等这台虚拟机下次开机时才完成。如果在这之前虚拟机被删除，除截图以外还没复制的部分就丢失了。
5. **时钟跳变。** 不加 1.4 的注册表项，开机后的录制里会出现 7 小时的时间跳变（rec_c72d7164 中倒退了 25118 秒）。加上之后已确认正常。
6. **进程事件里有噪声。** 采集器自己做快照时启动的 git.exe（例如 `git commit --allow-empty -m episode_start`）也会出现在进程事件里。
7. **每台虚拟机同时只能有一条录制。** 键鼠钩子是全局的，录制进行中再调用 start 会返回 409。vm_manager 本来也只会开一条。
8. **main.py 的三项新修改还没有实际运行过。** `debug=False`、`threaded=True` 和 WARNING 日志级别都还没有在任何虚拟机上跑过。（未验证）它们针对的是 10-16 的健康检查失败，而那次失败的根因（同步写访问日志时卡在 C 盘）本身也只是推断。由 main.py 注册录制接口这一点，已经在 debug=True 的情况下验证过。
9. **旧镜像上采集会失败。** 在旧镜像上，vm_manager 开始采集会失败 3 次（HTTP 500），留下一个空的 `rec_` 目录，例如 vm_b11ad0d0 的 rec_736d1849。不影响其他功能。
10. **测试虚拟机里有两条不完整的录制。** vm_ad7fd663 中，rec_f45d050e 是空目录，rec_931a92af 只有 `snapshots/repo.git`。它们是开发早期版本留下的，没有再追查原因。

## 附录：已经验证的内容（2026-10-19，vm_ad7fd663）

- 通过 vm_manager 手动开始、手动停止，以及开机自动开始，都能正常录制。
  - 开始约 11 秒。
  - 停止 3–9 秒。一条 2 分钟、445 张截图的录制，停止用了 5.7 秒。
- 以下结果都完整：`trajectory.jsonl`、`trajectory.html`、视频、快照（episode_start、file_changed、milestone、episode_end）、`agent_requests.jsonl`。D 盘里没有写入 `.gitignore`。
- 修改 main.py 并重启虚拟机后，接口由 main.py 自己注册（当时是 debug=True），开机自动开始的录制能读到 `G:\task.json`。
- 本地留下的、缺少 `trajectory.jsonl` 的 episode，在下次开机时会重新生成，并复制到 G 盘。
- Agent 服务重启后，会通过 pid 文件找回仍在运行的采集器并接管。
- 约 100 秒的连续输入，期间经过多次定期复制，434 个按键全部记录，没有丢失。
