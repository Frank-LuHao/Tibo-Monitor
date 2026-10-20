# 运行与验证

## 状态与恢复

以同一任务工作目录执行 `scripts/monitor.py status`。系统文件锁对应 `running`；`heartbeat`、`last_poll_ok`、`next_poll_at_utc` 与队列统计辅助判断运行健康。历史 PID、heartbeat 和下次查询时间在停止后可能保留，不代表进程仍在运行。

首次成功查询前建立时间基线，旧帖作为上下文保存。以后按作者 ID 和帖子 ID 去重；重启保留基线，补查停机期间帖子。SQLite 和轮转日志在 `--data-dir` 中，不打包进 skill。

分析最多自动尝试三次，第一次失败等待约五分钟、第二次失败等待约十分钟，第三次失败保留为 `failed`。修复网络或登录问题后用 `retry-failed` 重新排队。崩溃时的 `analyzing` 任务在重启后恢复，报告写入完成后才标记 `done`。

查询异常指数退避至最多一小时；429 尊重 `Retry-After`。每轮最多五页，覆盖不足或游标重复时不推进水位，日志提示 coverage gap。连续缺口需要调查分页或增加覆盖范围，不能宣称无新帖或无遗漏。

首次采集只读取当前一页历史；模型上下文包括已保存的先前帖子、引用及能够读取的被回复帖子。独立 Done 不能仅凭时间接近升级为明确完成。

每批最多十条，Codex 调用超时十分钟；输出 ID、字段、分类和证据 ID 验证通过才保存。报告覆盖同一帖子 ID，保证崩溃重放不产生多份结果。

停止请求一般数秒生效，若正在 HTTP 查询或模型进程清理，等待至多约一分钟再检查文件锁。停止不删除队列，重启会继续未完成分析。

## 验证

从任意任务目录，使用绝对路径执行本地测试，不联网、不调用模型：

```powershell
python -X utf8 -B 'SKILL_DIR/scripts/test_monitor.py'
```

只有需要验证模型链路时执行五条真实历史帖分类，消耗一次 Codex 分析用量：

```powershell
python -X utf8 -B 'SKILL_DIR/scripts/verify_codex.py' --output-dir 'TASK_DIR/outputs/tibo-monitor-validation'
```

测试样本随附在 `references/validation-fixtures.json`，无需原工作区档案；与正式队列隔离。输出包括逐条中文报告和 `validation-result.json`。这些样本用于验证调用与关键边界，不代表对未来所有动态的准确率保证。

## 协议来源

- [FxTwitter 时间线](https://docs.fxembed.com/api/twitter/operations/2profilehandlestatuses/)
- [FxTwitter API 概览与限流](https://docs.fxembed.com/api/introduction/)
- [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)

接口和登录方式变化时核对当前文档，避免将网络限制误判为账号无动态。
