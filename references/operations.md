# 运行与验证

## 状态与恢复

以同一任务工作目录执行 `scripts/monitor.py status`。系统文件锁对应 `running`；`heartbeat`、`last_poll_ok`、`next_poll_at_utc` 与队列统计辅助判断运行健康。历史 PID、heartbeat 和下次查询时间在停止后可能保留，不代表进程仍在运行。

首次成功查询前建立时间基线，旧帖作为上下文保存。以后按作者 ID 和帖子 ID 去重；重启保留基线，补查停机期间帖子。SQLite 和轮转日志在 `--data-dir` 中，不打包进 skill。

分析最多自动尝试三次，第一次失败等待约五分钟、第二次失败等待约十分钟，第三次失败保留为 `failed`。修复网络或登录问题后用 `retry-failed` 重新排队。崩溃时的 `analyzing` 任务在重启后恢复，报告写入完成后才标记 `done`。

设置 `TIBO_MONITOR_FAILURE_EMAIL` 并取得用户对邮件通知的明确授权后，程序会在新任务第三次分析失败时，将同批进入 `failed` 的帖子合并为一封邮件。正文只包含失败背景、帖子链接、状态目录和是否重新排队的待决策事项；不发送原始模型日志。未配置收件人时，告警保留在本地队列。主动停止保留队列并退还本次尝试，不产生失败告警。已有历史 `failed` 不自动补发。

正式监控必须在非沙箱本机环境启动（Codex 工具用 `require_escalated`），以便邮件子进程通过 `AGENTLY_WORKSPACE=codex agently-cli message +send ... --confirmed` 复用钥匙串授权。首次使用先阅读 `agently-mail` skill，在相同环境以 `AGENTLY_WORKSPACE=codex agently-cli +me` 检查授权；不要把测试邮件当作必要检查。邮件收件人通过 `TIBO_MONITOR_FAILURE_EMAIL` 配置，不要把个人邮箱或密钥写入仓库。

飞书通知脚本路径通过 `TIBO_MONITOR_FEISHU_NOTIFY` 配置；未设置时不尝试外发，通知保留在本地队列。不要提交个人路径、webhook 或访问凭据。

邮件队列与分析状态保存在同一 SQLite 中。`status.failure_mail` 显示 `pending`、`sending`、`sent`、`error`、`unknown` 的数量。发送成功才标记 `sent`；网络/服务错误最多发送尝试三次，等待约五、十分钟。其他明确失败记为 `error`；超时或发送中崩溃记为 `unknown`，需先核对发件箱，不能盲目重发。发送失败不改动帖子的 `failed` 状态，不把排队等同于已发送。

查询异常指数退避至最多一小时；429 尊重 `Retry-After`。每轮最多五页，覆盖不足或游标重复时不推进水位，日志提示 coverage gap。连续缺口需要调查分页或增加覆盖范围，不能宣称无新帖或无遗漏。

首次采集只读取当前一页历史；模型上下文包括已保存的先前帖子、引用及能够读取的被回复帖子。独立 Done 不能仅凭时间接近升级为明确完成。

每批最多十条，Codex 调用超时十分钟；输出 ID、字段、分类和证据 ID 验证通过才保存。报告覆盖同一帖子 ID，保证崩溃重放不产生多份结果。

停止请求一般数秒生效，若正在 HTTP 查询或模型进程清理，等待至多约一分钟再检查文件锁。停止不删除队列，重启会继续未完成分析。

## 验证

从任意任务目录，使用绝对路径执行本地测试，不联网、不调用模型：

```powershell
python -X utf8 -B 'SKILL_DIR/scripts/test_monitor.py'
```

只有需要验证模型链路时执行十条合成动态分类，消耗一次 Codex 分析用量：

```powershell
python -X utf8 -B 'SKILL_DIR/scripts/verify_codex.py' --output-dir 'TASK_DIR/outputs/tibo-monitor-validation'
```

默认样本随附在 `references/generic-validation-fixtures.json`，覆盖不同措辞的预告、已完成、简短关联回应、缺失证据、未来证据、请求、条件句和非额度重置；这些合成动态只作测试，不是真实公告，也不嵌入分析 prompt。与正式队列隔离，输出包括逐条中文报告和 `validation-result.json`。历史样本保留在 `references/validation-fixtures.json`，需要时可用 `--fixtures` 显式指定。这些样本用于验证调用与关键边界，不代表对未来所有动态的准确率保证。

## 协议来源

- [FxTwitter 时间线](https://docs.fxembed.com/api/twitter/operations/2profilehandlestatuses/)
- [FxTwitter API 概览与限流](https://docs.fxembed.com/api/introduction/)
- [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)

接口和登录方式变化时核对当前文档，避免将网络限制误判为账号无动态。

## 额度公告队列

`quota_change` 表示具体额度政策变化，沿用 `reset_notification` 表保存（无需改库或重建基线），与 reset 确认一样即时投递。仅 reset 预告等待五分钟合并。额度通知内容取对应分析摘要；保留原有帖子 ID 去重及 unknown 不盲目重发规则。变更规则默认只作用于之后分析的新帖，不批量重分析或重发历史报告。

额度公告验证用例见 `quota-validation-fixtures.json`，可通过 `verify_codex.py --fixtures` 指定；全部为合成动态，不外发通知。

重置时间通过 `reset_time_zh` 保存并显示在飞书通知中，支持明确时间点和大致范围；无依据时为空。旧分析没有该字段仍可读取，不批量改写或重发历史已发送通知。预告合并查询同时读取对应分析，以免时间字段在队列中丢失。
