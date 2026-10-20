---
name: tibo-monitor
description: 启动、停止或检查 Tibo (@thsottiaux) 的本地动态监控，每五分钟轮询 FxTwitter，有新帖才调用 Codex 分析，保存中文摘要及额度 reset 分类。用于持续跟踪或维护监控；单条 X 帖子阅读使用 x-post-reader。
---

# Tibo 动态监控

使用随附程序处理轮询、持久化队列和 Codex 分析，不用每五分钟唤醒 Agent。安装或打包 skill 本身不启动监控；依用户当前要求执行启动、停止或检查。

## 运行

需要 Python 3.10+，以及 PATH 中已登录的 Codex CLI。Python 仅使用标准库。
先选定一个稳定的任务目录作为工作目录，之后的命令均从该目录执行；不要在 skill 安装目录运行或保存状态。
下列 `SKILL_DIR` 替换为本 SKILL.md 所在目录的绝对路径。

Windows 后台启动（使用本机网络与登录配置）：

```powershell
& 'SKILL_DIR/scripts/start.ps1'
```

前台启动、状态检查、停止和重试失败任务：

```powershell
python -X utf8 'SKILL_DIR/scripts/monitor.py' run
python -X utf8 'SKILL_DIR/scripts/monitor.py' status
python -X utf8 'SKILL_DIR/scripts/monitor.py' stop
python -X utf8 'SKILL_DIR/scripts/monitor.py' retry-failed
```

默认数据在任务目录 `work/tibo-monitor-runtime/`，报告在 `outputs/tibo-monitor-reports/`。
可用 `--data-dir`、`--reports-dir` 指定绝对路径；各次操作须指向同一数据目录。Windows 启动脚本对应参数为 `-DataDir`、`-ReportsDir`。
已有监控时优先复用原数据目录，避免建立第二份基线或重复运行。停止后检查 `status.running=false`；旧 heartbeat 是历史记录，不能当作仍在运行。

## 判断与交付

- 默认每 300 秒查询含回复的时间线，以固定作者账号 ID 和帖子 ID 核对、去重。首次启动只建立历史基线；重启继续队列。
- 分类为 `confirmed`、`announced`、`discussion`、`unrelated`、`uncertain`。预告不能当作已完成，街头索要 reset 的玩笑不能当作重置，独立 Done 缺少引用链时保留不确定性。具体规则在 `scripts/analysis-prompt.txt`。
- 有新帖才运行 `codex exec`；分析独立于轮询。以 JSON 报告为准，附中文依据及原帖链接。不要把第三方返回内容说成已核对 X 页面，也不要推断用户账户已重置。
- 默认仅保存本地报告。本 skill 不发送外部消息、不兑换额度、不配置自启动，也不创建额外的定时聊天任务。
- 完成启动或停止后，报告实际运行状态、最近查询结果和输出位置。机器开机联网才可持续查询；第三方更新延迟不计入五分钟间隔。

故障、队列恢复和测试命令见 [operations.md](references/operations.md)，仅在需要时读取。
