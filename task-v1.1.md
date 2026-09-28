# Windows Trajectory Recorder V1.1：文件快照

在 V1（已完成的通用轨迹采集）基础上增量开发。V1 的所有行为保持不变。

**本版只做一件事：工程文件快照。** 录制期间为工作目录留存版本，每个 step 都能对应到一个确定的文件状态，并支持按版本恢复或导出。

## 不在本版范围

- **不依赖软件适配层**。本版完全不碰 COM / 软件 API，快照只看磁盘文件。
- **不做主动保存**。用户没保存的内容不在快照里，这是本版接受的限制。
- **不做 VM 快照**。它需要在宿主机执行，属于下一版；本版只在数据结构中预留 `vm_ckpt` 字段，恒为 `null`。
- **不做回退分支管理**。本版只保证"每步有版本、任意版本可取出"。
- **不做"每 N 步快照"**。录制过程中 `step_id` 尚不存在（它由 `trajectory.py` 在录制结束后从 raw 合并生成），因此无法按步触发。注意"每个 step 都有 `file_ckpt`"不等于"每个 step 都 commit"——多个 step 指向同一个最近版本是正常且正确的。

## 一、目录约定

工作目录与轨迹输出目录分开，且**不把** `.git` **放进用户工作目录**：

```
--watch   用户工作目录，纯工程文件，不出现 .git
--output  轨迹输出目录，每个 episode 一个子目录

```

git 仓库放在 episode 目录内，通过环境变量把仓库和工作树分离：

```
<output>/<episode_id>/snapshots/repo.git/    仓库本体（GIT_DIR）
<watch>                                      工作树（GIT_WORK_TREE）

```

所有 git 命令都带上这两个环境变量执行。这样做的好处：

- 每个 episode 的历史天然独立，不会把多次录制混在一起
- 用户工作目录保持干净，不会出现 `.git`
- 不会破坏用户自己可能已有的 git 仓库
- `repo.bundle` 只包含本次 episode 的内容

启动时检查：若 `--output` 位于 `--watch` 之内，直接报错退出并说明原因。否则轨迹数据会被写进快照。

任务说明中需要告知用户：**所有工程文件必须保存在工作目录内**，否则不会被快照。

## 二、快照机制

### 用 git，只在本地

每次快照就是一次 commit，commit hash 即版本 id。仓库不上传到任何远程，录制结束时打包成 `snapshots/repo.bundle` 随 episode 一起收回。git 在这里只是一个做增量存储、去重和 diff 的本地工具。

启动时检查 `git --version`：

- 不可用 → 打印警告，**关闭快照功能但继续录制**，在 `meta.json` 中记 `"snapshot": {"enabled": false, "reason": "git not found"}`
- 可用 → 正常初始化

### 初始化

录制开始时：

- `git init --bare` 创建 `snapshots/repo.git`（每个 episode 都是全新仓库）
- 配置：`core.autocrlf=false`（禁止换行符转换，工程文件是二进制）、`user.name=trajrec`、`user.email=trajrec@local`、`gc.auto=0`（禁止后台 gc 干扰录制）
- 在**工作目录**下写入或补充 `.gitignore`（git 从工作树读取它，不能放进 repo.git）：

```
~$*
*.tmp
*.bak
*.dwl
*.dwl2
*.swp.*
*.err
*.ac$
*.sv$
_discarded/

```

- 做第一次 commit，`reason` 为 `episode_start`，作为初始基准版本

### 触发条件


| 触发   | 条件                          | reason                          |
| ---- | --------------------------- | ------------------------------- |
| 文件变更 | 文件监控报告工作目录内有变更，且此后进入静默（见下文） | `file_changed`                  |
| 主动   | 用户或 supervisor 标记里程碑，见下文    | `milestone`                     |
| 起止   | 录制开始、录制结束各一次                | `episode_start` / `episode_end` |


`--snapshot`（默认开启）/ `--no-snapshot` 控制整个功能开关。

### 主动触发的两种入口

**一、控制文件（给 agent、supervisor、宿主机用）**

Recorder 每 0.5 秒检查一次 `C:\ProgramData\trajrec\MILESTONE` 是否存在：

- 存在 → 读取文件内容作为本次里程碑的名称（可为空）→ **删除该文件** → 执行一次快照
- 删除是必须的，否则会重复触发

触发方只需要一句命令：

```powershell
"完成草图" > C:\ProgramData\trajrec\MILESTONE   # 带名称
New-Item C:\ProgramData\trajrec\MILESTONE       # 不带名称

```

**二、快捷键（给 VM 里的标注员用）**

组合键为 **Ctrl+Alt+Shift+M**。选这个组合是因为专业软件极少占用它。

两个硬性要求：

- 必须在低级键盘钩子里**真正吞掉这组按键**（pynput 的 `suppress_event`），不能只是不写进轨迹。否则按键仍会发给 SolidWorks、AutoCAD 等软件，可能触发它们自己的功能。
- 触发后在屏幕角落显示一个短暂提示（如"已记录检查点"），让人知道生效了。

被吞掉的按键同样不进入轨迹的 action 合成。

### 所有快照共同的前置检查：等文件写完

软件保存大文件需要数秒，中途 commit 会存下残缺文件。因此每次快照执行前：

- 等到工作目录**连续 5 秒没有任何文件事件**再提交
- 最长等待 30 秒；超时仍然提交，但在事件中标记 `quiet_timeout: true`
- 记录实际等待时长 `waited_ms`

这不是独立的触发条件，而是所有触发共用的前置步骤。所需的"最后一次文件事件时间"由 `files.py` 对外暴露一个属性供读取。

### 并发处理

同一时刻只允许一个快照在执行。执行期间（包括等待静默期间）到来的新触发：

- `file_changed` → 丢弃（当前这次快照本来就会把最新状态提交进去）
- `milestone` → 排队，当前快照完成后立即执行一次，保留其名称
- 队列中最多保留一个 milestone，多个合并为一个，名称取最后一个

### 执行

- 快照在**独立线程**中执行，不得阻塞任何采集模块
- 每次都提交，即使没有文件变化（使用 `--allow-empty`），保证每个触发点都有确定版本；git 对未变文件不重复存储，空 commit 开销可忽略
- commit message 格式：`<reason> <name(如有)> t=<requested_t_ms>`
- 提交后取两样东西：
  - **commit hash（前 12 位）**：版本 id，轨迹里的 `file_ckpt` 就是它，恢复时靠它定位
  - **变更文件列表**：`git diff --name-only HEAD~1 HEAD`，本版相对上一版的净变化
- 若快照执行失败（git 报错、仓库损坏等），记录一条错误事件并继续录制，不得中断

### 两个时间戳（重要）

快照要等文件静默，提交时刻可能比触发时刻晚十几秒，期间用户已经做了好几个动作。因此必须记录两个时间：

- `requested_t_ms`：触发时刻
  - `file_changed`：取这一轮连续文件变更中第一个文件事件的时间
  - `milestone`：取 milestone 被触发的时间
  - `episode_start / episode_end`：取对应开始 / 结束时间
- `committed_t_ms`：commit 完成时刻

**关联到 step 时一律使用** `requested_t_ms`，因为它才对应用户真正做出那个动作的时刻。

### files_changed 与 events.files 的区别

两者不重复，都要保留：

- `events.files` 来自文件监控，记录的是**过程**，包含临时文件、反复写入等噪音
- `snapshot.files_changed` 来自 git 比对，记录的是**两个版本之间的净变化**，无噪音

例如软件保存一次，文件监控可能报出十几条事件，而 git 只报告一个文件发生了变化。

### 记录到 raw

每次快照向 `raw/events.jsonl` 写一条事件：

```json
{
  "type": "snapshot",
  "requested_t_ms": 1790052735123,
  "committed_t_ms": 1790052742800,
  "commit": "a3f9c1e8d2b0",
  "reason": "milestone",
  "name": "完成草图",
  "files_changed": ["part1.SLDPRT"],
  "waited_ms": 7677,
  "quiet_timeout": false
}

```

`name` 仅 `milestone` 触发时可能有值，其余为 null。

单个文件超过 200MB 时，额外写一条警告事件（仍然提交，本版不做特殊处理）。

## 三、轨迹新增字段

`trajectory.jsonl` 的每个 step 新增 `snapshot`，表示**这一步动作之后**的版本：

```yaml
snapshot:
  file_ckpt: "a3f9c1e8d2b0"   # 这一步动作之后的文件版本（git commit hash 前 12 位）
  name: null                  # 若该版本来自 milestone，则为其名称
  vm_ckpt: null               # 预留，本版恒为 null

```

生成规则（在 `trajectory.py` 中实现）：

- 按 snapshot 事件的 `requested_t_ms` 归入 step，**不要用** `committed_t_ms`
- `file_ckpt`：取 `requested_t_ms` 落在 `[本 step.t_ms, 下一 step.t_ms)` 窗口内的 snapshot 事件的 commit；窗口内有多个时取最后一个；窗口内没有则沿用上一步的值
- 最后一个 step 的窗口截止到录制结束，因此它会包含 `episode_end` 那次快照
- 快照功能未启用时，整个 `snapshot` 字段为 `null`

多个连续 step 指向同一个 `file_ckpt` 是正常的，说明这期间文件没有变化。

## 四、输出目录新增

```
output/<episode_id>/
├── snapshots/               新增
│   ├── repo.git/            git 仓库本体（GIT_DIR）
│   ├── repo.bundle          录制结束时 git bundle create --all 打包
│   └── index.json           所有快照的清单
└── ...（其余同 V1）

```

`snapshots/index.json` 结构如下，`work_dir` 为用户工作目录的绝对路径：

```json
{
  "work_dir": "C:/task",
  "enabled": true,
  "checkpoints": [
    {"commit": "a3f9c1e8d2b0", "requested_t_ms": 1790052735123,
     "committed_t_ms": 1790052742800, "reason": "milestone",
     "name": "完成草图", "step_id": 12, "files_changed": ["part1.SLDPRT"]}
  ]
}

```

`repo.bundle` 生成后 `repo.git` 可以保留，便于本机直接恢复。

## 五、恢复工具 restore.py

```
python restore.py <episode_dir> --list                            列出所有版本
python restore.py <episode_dir> --step 12 --export D:\check       导出（默认，安全）
python restore.py <episode_dir> --step 12 --in-place              就地恢复工作目录
python restore.py <episode_dir> --step 12 --in-place --kill       同上，自动结束软件进程

```

也支持用 `--commit <hash>` 代替 `--step`。工作目录路径从 `index.json` 的 `work_dir` 读取，可用 `--work-dir` 覆盖（episode 数据被拷到别的机器上时需要）。

### 导出模式（默认）

用 `git archive` 或 `git worktree` 把指定版本的文件解到另一个目录，**不触碰工作目录**。用于验收、比对、生成训练数据。这是更常用的场景。

### 就地恢复模式

1. **检查软件进程**。扫描常见工程软件进程（SLDWORKS、acad、Rhino、ansys 等，列表写在文件顶部常量里，可配置）：
  - 未加 `--kill`：打印进程列表，提示先关闭，等待用户确认后再继续
  - 加了 `--kill`：直接结束这些进程，然后**等待 3 秒**，因为软件退出时仍会写锁文件和临时文件
  原因：软件内存中仍是旧状态，若不关闭，它下次保存会把恢复的文件覆盖回去。
2. `git checkout -f <commit> -- .` 还原被跟踪的文件。
3. 处理未跟踪文件（该版本之后新建的）：**移动**到 `<工作目录>\_discarded\<时间戳>\`，不要删除。
4. 打印恢复结果，并提示用户重新打开软件和文件。

## 六、代码结构

新增两个文件，不改动已有采集模块的职责：

- `snapshot.py`：仓库初始化、触发判断（文件变更 / MILESTONE 文件）、等待静默、并发控制、执行 commit、写 snapshot 事件
- `restore.py`：恢复与导出工具，可独立运行

需要修改的已有文件：

- `main.py`：检查目录嵌套、初始化快照、启动快照线程、结束时打包 bundle
- `trajectory.py`：生成 `snapshot` 字段、写 `snapshots/index.json`
- `files.py`：对外暴露"最后一次文件事件时间"
- `input_recorder.py`：识别并**吞掉** Ctrl+Alt+Shift+M，触发里程碑，且不进入轨迹

git 命令统一封装成一个函数，内部设置 `GIT_DIR` 和 `GIT_WORK_TREE` 环境变量，其他地方不要直接拼 git 命令。

保持代码简单、人类可读、方便单独修改和测试。

## 七、验收

当前开发环境为 macOS，本版功能需在 Windows VM 上验证，不得假设或声称已测试通过。请提供测试说明。

Windows VM 上的验收流程：

1. `--output` 设在 `--watch` 之内时，启动报错并说明原因
2. 开始录制 → 用户工作目录**不出现** `.git`；`<episode>/snapshots/repo.git` 存在，且有一个 `episode_start` commit
3. 在记事本中新建文件保存到工作目录 → 静默 5 秒后产生新 commit，`files_changed` 正确
4. 保存一个较大的文件（≥50MB）→ commit 中的文件完整可打开，未出现残缺，事件中 `waited_ms` 明显大于 0
5. 执行 `"完成草图" > C:\ProgramData\trajrec\MILESTONE` → 产生一次 `milestone` 快照，`name` 正确，控制文件被自动删除
6. 在 SolidWorks 或其他软件中按 Ctrl+Alt+Shift+M → 产生一次 `milestone` 快照，屏幕出现提示，**软件本身没有任何反应**（按键被吞掉），且 `trajectory.jsonl` 中没有对应的 action
7. 保存文件后立刻连续操作十几步 → 该快照的 `requested_t_ms` 对应保存那一刻，`committed_t_ms` 明显更晚，且 `file_ckpt` 归属到保存那一步而不是十几步之后
8. 停止录制 → `repo.bundle` 和 `index.json` 存在，bundle 能在另一台机器上 `git clone` 出来
9. `trajectory.jsonl` 中每个 step 都有 `file_ckpt`，连续多步指向同一版本属正常
10. 从 index.json 任选一个版本执行 `restore.py --export`，导出的文件内容与当时保存的完全一致（包括第 4 条的大文件，用哈希比对验证）
11. 软件运行时执行 `restore.py --in-place`（不带 `--kill`）→ 提示先关闭软件
12. 带 `--kill` 执行 → 软件被关闭，工作目录回到指定版本，后建的文件被移入 `_discarded`

## 八、交付

- 更新 `README.md`：快照机制与触发时机、目录约定（含 GIT_DIR / GIT_WORK_TREE 分离的原因）、两种里程碑触发方式、`restore.py` 用法与注意事项、已知限制（未保存内容不在快照中）
- `requirements.txt` 如有新增依赖需注明（预期无新增，git 通过命令行调用）

