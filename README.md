# herdr-run

一个给 AI coding agent（Claude Code 等）用的 [Herdr](https://herdr.dev) 技能：agent 需要跑长时进程（服务端、LLM 代理、训练、测评……）时，不再 `nohup` 到你看不见的地方，而是把它放进 Herdr 里一个专门的 `background` 工作区——**进程就在终端 pane 里以前台方式跑着**，你随时切过去看输出、Ctrl-C 停掉，日志同时完整落盘。

## 它解决什么问题

让 agent 起个后台服务，通常的结局是：

- 进程挂在某个 detached shell 里，你不知道它在不在、卡没卡
- 想看输出？输出早就丢了，或者散落在 agent 的临时文件里
- 想停掉？先找 PID 吧
- 起了五个服务，谁占用 8000 端口全靠猜

herdr-run 把这类进程统一管起来。你要做的只是正常跟 agent 说话，剩下的是它和 Herdr 的事。

## 安装

前置要求：[herdr](https://herdr.dev)（`herdr --version` 可用；macOS / Linux / Windows 均可，Windows 用 `irm https://herdr.dev/install.ps1 | iex` 安装，或走 WSL）和 Python 3。**请在Herdr中使用本skill以获得最佳体验**。

### 一键安装（推荐，40+ agent 通用）

```bash
npx skills add yhj137/herdr-run
```

`npx skills` 是 [agent skills 生态](https://github.com/antfu/skills-cli)的通用安装器，会自动识别你机器上装了哪些 agent（Claude Code、Codex、Cursor、Gemini CLI、OpenCode……）并把技能装进各自的目录。

### Claude Code 手动安装

```bash
# 全局（所有项目可用）
git clone https://github.com/yhj137/herdr-run ~/.claude/skills/herdr-run

# 或项目级（仅当前项目）
git clone https://github.com/yhj137/herdr-run <项目>/.claude/skills/herdr-run
```

### 其他 agent

`SKILL.md` 已是开放标准（[Agent Skills](https://code.claude.com/docs/en/skills)），主流 agent（Codex、Cursor、Gemini CLI、OpenCode 等）都能识别——把本仓库放进你 agent 的 skills 目录即可；不确定目录位置就用上面的一键安装。

## 装好之后你会看到什么

Herdr 里多出一个 `background` 工作区，所有后台进程都在里面，按用途分 tab，每 tab 最多 4 个 pane、田字排列，第 5 个同类进程自动开下一个 tab：

```
background 工作区
├── llm_proxy_1            ← 用途 llm_proxy 的第 1 个 tab
│   ┌────────────────┬────────────────┐
│   │ vllm api       │ vllm api       │   ← 每个 pane 一个前台进程
│   │ proxy:7890     │ proxy:7891     │      名字 = 备注:端口
│   ├────────────────┼────────────────┤
│   │ vllm api       │  (空闲槽位)    │
│   │ proxy:7892     │                │
│   └────────────────┴────────────────┘
└── rollout_1
```

你可以：

- **看**：切到 `background` 工作区，输出就在屏幕上滚动
- **停**：选中那个 pane 按 Ctrl-C，跟你在自己终端里一样
- **找日志**：每个进程的完整输出都 tee 在 `<项目>/logs/<用途>/<日期时间>-<备注>.log`
- **查历史**：启动记录 `data/launches.jsonl`（全局安装即 `~/.claude/skills/herdr-run/data/launches.jsonl`）记着每一次启动（时间、完整命令、端口、pane、日志路径），跨项目可查

## 怎么用

不用记命令，正常说话即可，比如：

> 帮我起一个 vLLM 的代理服务，监听 7890，确认能访问了告诉我地址
> 把训练任务放到后台跑，我要随时能看进度
> 这 5 个 watcher 都在跑吗？

agent 会调用本技能完成放置、命名、日志与记录，然后告诉你 pane 位置和日志路径。

## 用途命名：agent 会先问你

tab 和日志目录都按**用途**（`llm_proxy`、`rollout`、`sft` 这样的短名）组织。用途名是全局共享的词汇表，避免 agent 随手发明 `proxy2`、`server_final` 让 tab 越开越乱：

- 已登记的用途，agent 直接复用——同一个用途的第 2、3、4 个进程进同一个 tab 系列
- **全新**的用途，agent 会先向你确认名字再登记（你说的话里已经起了名就算确认过）
- 注册表**没有独立的文件**：已知用途 = 启动记录文件 ∪ 现存的 `{用途}_n` tab。启动记录默认在 `~/.claude/skills/herdr-run/data/launches.jsonl`（项目级安装则在 `<项目>/.claude/skills/herdr-run/data/`），每行一次启动。想清理或修正用途词汇表，直接编辑这份 JSONL 即可，删掉的用途下次启动就会被当作新用途重新询问
- 已登记的用途一览：`python3 ~/.claude/skills/herdr-run/scripts/herdr_run.py list --purposes`；在跑的进程一览：同命令不带参数

## 命令参考

一般用不到，备查（`$S` = 技能安装目录下的 `scripts/herdr_run.py`，下例为 Claude Code 全局安装）：

```bash
S=~/.claude/skills/herdr-run/scripts/herdr_run.py

python3 $S launch <用途> "<完整命令>" [--note 备注] [--port 端口]   # 启动
python3 $S list                                 # 在跑的进程一览
python3 $S list --purposes                      # 已登记的用途一览
python3 $S list --history                       # 含已退出的全部记录
```

启动选项：`--cwd` 工作目录（默认当前目录）、`--log-dir` 日志根目录（默认 `<cwd>/logs`）、`--new-purpose` 登记新用途、`--no-record` 不写全局记录、`--focus` 启动后聚焦新 tab、`--dry-run` 只打印放置计划。

对进程的后续操作走 herdr 本身：`herdr pane read <pane_id> --source recent-unwrapped` 看输出、`herdr pane send-keys <pane_id> ctrl+c` 停止。

## FAQ

**进程退出后 pane 会怎样？**
pane 保留，显示最后的输出，状态变 idle；同一 tab 系列再启新进程时，空闲的根 pane 会被复用，不会无限增殖。

**日志文件名里的备注是哪来的？**
agent 启动时给的简短备注（`--note`），同时也是 pane 标题。比如 `260829-221549-vllm-api-proxy.log`。

**为什么 pane 名字里有 `:7890` 这样的端口？**
凡是监听端口的进程，端口都会标进 pane 名，端口冲突一眼可见。多个端口逗号分隔。

**全局启动记录放在哪？**
默认 `~/.claude/skills/herdr-run/data/launches.jsonl`（项目级安装则随项目，在 `<项目>/.claude/skills/herdr-run/data/`）。想换位置：环境变量 `HERDR_RUN_RECORD_FILE` 或启动时加 `--record-file`；某次不想记录就 `--no-record`。

**支持 Windows 吗？**
支持。Windows 上 pane 是 PowerShell，脚本会自动生成 `Tee-Object` 管道与 PS 转义；走 WSL 也完全可用。
