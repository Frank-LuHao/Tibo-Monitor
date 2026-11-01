# Tibo Monitor

本地轮询 Tibo (`@thsottiaux`) 的公开动态；发现新帖后用 Codex CLI 分类额度 reset 及额度调整公告，并保存中文 JSON 报告。默认每 5 分钟查询一次，使用 SQLite 保存队列和运行状态。首次启动只建立历史基线。

## 环境要求

- Python 3.10+（仅使用标准库）
- 已登录的 Codex CLI；macOS 上优先使用 ChatGPT 桌面应用内置 CLI，也可通过 `CODEX_CLI_PATH` 指定可执行文件
- 可访问 FxTwitter API 的网络

## 运行

从你希望保存运行数据的工作目录执行。默认状态目录为 `work/tibo-monitor-runtime/`，报告目录为 `outputs/tibo-monitor-reports/`；仓库根目录下这两个路径已加入 `.gitignore`，避免把采集内容和本地分析结果提交到 Git。

```sh
python3 scripts/monitor.py run
```

检查状态、停止或重新排队分析失败的帖子：

```sh
python3 scripts/monitor.py status
python3 scripts/monitor.py stop
python3 scripts/monitor.py retry-failed
```

可用 `--data-dir` 和 `--reports-dir` 指定绝对路径；每次操作须使用相同的数据目录。Windows 后台启动可使用 `scripts/start.ps1`。

## 可选通知

邮件和飞书通知默认不外发。只在取得相应用户授权后再配置：

```sh
export TIBO_MONITOR_FAILURE_EMAIL='收件人邮箱'
export TIBO_MONITOR_FEISHU_NOTIFY='/path/to/notify-script'
```

连续三次分析失败的告警由 `agently-cli` 发送，运行时使用 `AGENTLY_WORKSPACE=codex`。Reset 与额度调整公告通过指定的飞书脚本发送。未配置收件人或脚本时，通知留在本地队列。不要将邮箱、个人路径、webhook 或访问凭据提交到仓库。

## 维护与验证

故障排查和队列恢复说明见 [`references/operations.md`](references/operations.md)。离线单元测试：

```sh
python3 -B -m unittest discover -s scripts -p 'test_*.py'
```

可选的模型链路验证会调用一次 Codex CLI：

```sh
python3 scripts/verify_codex.py
```

默认测试样本为合成动态，不代表真实公告或未来分类准确率。

## 额度调整公告

除 reset 预告与生效确认外，明确的额度扩容、恢复、缩减、取消降额、优惠延长及到期安排也归为 `quota_change`，分析完成后即时发送。消息包含中文摘要与原帖链接，保留明确的档位、倍数、时间和例外；不会把限额调整写成一次 reset。普通讨论、请求、猜测及无新增信息的转述不触发。

原有 reset 预告仍等待五分钟合并，生效确认仍即时发送；沿用帖子 ID 去重、失败保留和投递不明不盲目重发机制。更新默认只处理之后的新帖。

单独验证额度分类链路（调用一次模型，不发送通知）：

```sh
python3 scripts/verify_codex.py --fixtures references/quota-validation-fixtures.json --output-dir outputs/quota-validation
```

## Reset 时间展示

Reset 通知保留原帖明确给出的时间点或大致范围，可可靠换算的时间点和区间优先显示北京时间。时区不明的“接下来两天”等日历范围保留原话，并附北京时间发帖时间，避免把发送当天当作时间锚点。没有重置时间依据时省略时间行，不混入其他功能的发布时间。

旧分析没有 `reset_time_zh` 仍可读取；本次更新不会自动重分析或重发历史已发送帖子。
