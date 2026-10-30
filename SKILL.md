---
name: tibo-monitor
description: 启动、停止或检查 Tibo (@thsottiaux) 的本地动态监控，每五分钟轮询 FxTwitter，有新帖才调用 Codex 分析，保存中文摘要及额度 reset 与额度调整分类；按用户授权向飞书发送公告通知。用于持续跟踪或维护监控；单条 X 帖子阅读使用 x-post-reader。
---

# Tibo 动态监控

使用随附程序处理轮询、持久化队列和 Codex 分析，不用每五分钟唤醒 Agent。安装或打包 skill 本身不启动监控；依用户当前要求执行启动、停止或检查。

## 运行

需要 Python 3.10+，以及已登录的 Codex CLI。Python 仅使用标准库。macOS 上分析子进程优先使用 ChatGPT 桌面应用内置 CLI，避免 PATH 中旧版 CLI 不支持当前模型；它继承用户的模型与推理配置。
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
- 分类为 `confirmed`、`announced`、`quota_change`、`discussion`、`unrelated`、`uncertain`。按语义和发布时间可用的证据区分预告、完成与证据不足；请求、玩笑和假设不构成承诺，简短回应须核对动作对象与引用、回复链。具体规则在 `scripts/analysis-prompt.txt`。
- 有新帖才运行 `codex exec`；分析独立于轮询。以 JSON 报告为准，附中文依据及原帖链接。不要把第三方返回内容说成已核对 X 页面，也不要推断用户账户已重置。
- 默认仅保存本地报告。用户授权飞书通知时，按下列规范调用 `feishu-notifier`；不兑换额度、不配置自启动，也不创建额外的定时聊天任务。
- 配置 `TIBO_MONITOR_FAILURE_EMAIL` 并明确授权邮件通知后，本监控的动态分析连续失败三次进入 `failed` 时，通过 `agently-mail` 发一封简短告警。同批失败合并，成功发送后不因重启重复通知；主动停止不算失败。未配置收件人时告警留在本地队列。须在非沙箱本机环境运行正式监控，Codex 工具启动时用 `require_escalated`；邮件子进程固定使用 `AGENTLY_WORKSPACE=codex` 复用授权。详见 [operations.md](references/operations.md)。
- 完成启动或停止后，报告实际运行状态、最近查询结果和输出位置。机器开机联网才可持续查询；第三方更新延迟不计入五分钟间隔。

## 飞书额度通知

设置 `TIBO_MONITOR_FEISHU_NOTIFY` 为本地通知脚本路径并取得用户对通知的明确授权后，对 `announced`、`confirmed` 和 `quota_change` 发送通知；`discussion`、`uncertain` 与 `unrelated` 不发送。未配置脚本时通知留在本地队列。

- 对同一次 reset 的连续公告等待最多五分钟收集补充帖，只发一条公告通知。使用最具体的预告帖作为原帖链接。
- `announced` 且存在明确或可由相对时间计算的生效时间时：

  ```text
  【Tibo Reset】
  Tibo 宣布额度将 reset，预计北京时间 <MM/DD HH:mm> 生效。
  原帖：<url>
  ```

- `announced` 但没有可计算时间时，省略预计时间这句话：

  ```text
  【Tibo Reset】
  Tibo 宣布额度将 reset。
  原帖：<url>
  ```

- `confirmed`：

  ```text
  【Tibo Reset】
  Tibo 已确认额度 reset 生效。
  原帖：<url>
  ```

通知中不添加状态后缀、括号说明、适用范围推断或关于用户个人账户的推断。`原帖：` 与链接始终同行。

故障、队列恢复和测试命令见 [operations.md](references/operations.md)，仅在需要时读取。

### 额度调整公告

用户明确授权后，明确的额度扩容、恢复、缩减、取消降额、优惠额度延长/结束及生效或到期安排也推送飞书，分类为 `quota_change`，分析完成后即时发送。普通讨论、请求、猜测和无新增信息的转述不发送。消息格式为 `【Tibo 额度公告】`、准确的中文摘要、`原帖：<url>`，各占一行；摘要保留已披露的档位、倍数、时间及例外，不推断个人账户状态。取消原定降额不能描述为现有额度再翻倍。

复用原有按帖子 ID 的持久化去重、失败及投递不明处理，默认不重发历史已完成帖子。原有 reset 公告仍合并五分钟、确认生效仍即时推送。
