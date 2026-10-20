# Tibo Monitor

本地轮询 Tibo (`@thsottiaux`) 的公开动态；发现新帖后用 Codex CLI 分类额度 reset 相关信息，并保存中文 JSON 报告。默认每 5 分钟查询一次，使用 SQLite 保存队列和运行状态。首次启动只建立历史基线。

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

连续三次分析失败的告警由 `agently-cli` 发送，运行时使用 `AGENTLY_WORKSPACE=codex`。Reset 通知通过指定的飞书脚本发送。未配置收件人或脚本时，通知留在本地队列。不要将邮箱、个人路径、webhook 或访问凭据提交到仓库。

## 维护与验证

故障排查和队列恢复说明见 [`references/operations.md`](references/operations.md)。离线单元测试：

```sh
python3 -B scripts/test_monitor.py
```

可选的模型链路验证会调用一次 Codex CLI：

```sh
python3 scripts/verify_codex.py
```

默认测试样本为合成动态，不代表真实公告或未来分类准确率。
