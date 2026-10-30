"""Tibo timeline poller and persistent Codex analysis queue (standard library only)."""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = Path.cwd() / "work" / "tibo-monitor-runtime"
DEFAULT_REPORTS = Path.cwd() / "outputs" / "tibo-monitor-reports"
AUTHOR_ID = "1953337039510003712"
HANDLE = "thsottiaux"
API = "https://api.fxtwitter.com/2"
INTERVAL = 300
MAX_PAGES = 5
MAX_ATTEMPTS = 3
FAILURE_EMAIL = os.environ.get("TIBO_MONITOR_FAILURE_EMAIL", "").strip()
FEISHU_NOTIFY = os.environ.get("TIBO_MONITOR_FEISHU_NOTIFY", "").strip()
ANNOUNCEMENT_WINDOW = 300
RESET_CLASSES = {"confirmed", "announced", "quota_change", "discussion", "unrelated", "uncertain"}
MACOS_BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")


class AnalysisCancelled(Exception):
    """A user stop is not an analysis failure."""


class MailDeliveryError(Exception):
    def __init__(self, message, retryable=False, uncertain=False, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain
        self.retry_after = retry_after


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def find_codex():
    """Prefer the current desktop-app CLI over a potentially stale PATH copy."""
    candidates = [os.environ.get("CODEX_CLI_PATH")]
    if os.name != "nt":
        candidates.append(str(MACOS_BUNDLED_CODEX))
    candidates.append(shutil.which("codex"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("codex CLI not found; set CODEX_CLI_PATH or add codex to PATH")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Separate temp files also make status/report writes safe across threads.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as fp:
        temp = Path(fp.name)
        json.dump(value, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.fp = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("a+b")
        self.fp.seek(0, 2)
        if self.fp.tell() == 0:
            self.fp.write(b"0")
            self.fp.flush()
        self.fp.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fp.close()
            self.fp = None
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError("Monitor is already running") from exc
            raise
        return self

    def __exit__(self, *args):
        self.fp.close()


def is_running(data):
    try:
        with InstanceLock(data / "instance.lock"):
            return False
    except RuntimeError:
        return True


class Store:
    def __init__(self, data):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / "monitor.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY, created INTEGER NOT NULL, raw TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, error TEXT, analysis TEXT
                );
                CREATE TABLE IF NOT EXISTS failure_mail (
                    id INTEGER PRIMARY KEY, post_ids TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, error TEXT, sent_at TEXT
                );
                CREATE TABLE IF NOT EXISTS reset_notification (
                    post_id TEXT PRIMARY KEY, reset_class TEXT NOT NULL,
                    created INTEGER NOT NULL, url TEXT NOT NULL,
                    queued_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT, sent_at TEXT
                );
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def meta(self):
        with self.connect() as db:
            return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM meta")}

    def put_meta(self, **items):
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                           [(k, json.dumps(v)) for k, v in items.items()])

    def recover(self):
        with self.connect() as db:
            # A crash during a call must not discard its input or spend a retry.
            db.execute("UPDATE posts SET state='pending', attempts=max(0,attempts-1) WHERE state='analyzing'")
            # A crash after delivery but before commit cannot safely be resent.
            db.execute("UPDATE failure_mail SET state='unknown',error='Delivery interrupted; check Sent before retrying' "
                       "WHERE state='sending'")
            db.execute("UPDATE reset_notification SET state='unknown',error='Delivery interrupted; check Feishu before retrying' "
                       "WHERE state='sending'")

    def cancel(self, posts):
        with self.connect() as db:
            db.executemany("UPDATE posts SET state='pending',attempts=max(0,attempts-1),retry_at=0 "
                           "WHERE id=? AND state='analyzing'", [(p['id'],) for p in posts])

    def ingest(self, posts, baseline=None):
        queued = 0
        with self.connect() as db:
            for post in posts:
                historical = baseline is not None and post["created_timestamp"] <= baseline
                state = "baseline" if historical else "pending"
                cur = db.execute("INSERT OR IGNORE INTO posts(id,created,raw,state) VALUES(?,?,?,?)",
                                 (post["id"], post["created_timestamp"],
                                  json.dumps(post, ensure_ascii=False), state))
                queued += cur.rowcount if not historical else 0
        return queued

    def claim(self, size=10):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM posts WHERE state='pending' AND retry_at<=? "
                              "ORDER BY created,id LIMIT ?", (time.time(), size)).fetchall()
            for row in rows:
                db.execute("UPDATE posts SET state='analyzing', attempts=attempts+1 WHERE id=?", (row["id"],))
            return [json.loads(r["raw"]) for r in rows]

    def context(self, posts):
        first = min(p["created_timestamp"] for p in posts)
        with self.connect() as db:
            rows = db.execute("SELECT raw FROM posts WHERE created<? AND created>=? ORDER BY created DESC LIMIT 60",
                              (first, first - 3 * 86400)).fetchall()
        prior = [json.loads(r["raw"]) for r in rows]
        # Retain the nearest posts plus relevant reset announcements further back.
        selected = prior[:12] + [p for p in prior[12:] if re.search(r"reset|landing", p.get("text", ""), re.I)][:8]
        return list({p["id"]: p for p in selected}.values())

    def finish(self, post, analysis):
        with self.connect() as db:
            db.execute("UPDATE posts SET state='done',analysis=?,error=NULL WHERE id=?",
                       (json.dumps(analysis, ensure_ascii=False), post["id"]))
            if analysis["reset_class"] in {"announced", "confirmed", "quota_change"}:
                db.execute("INSERT OR IGNORE INTO reset_notification "
                           "(post_id,reset_class,created,url,queued_at) VALUES(?,?,?,?,?)",
                           (post["id"], analysis["reset_class"], post["created_timestamp"],
                            post["url"], time.time()))

    def fail(self, posts, error):
        with self.connect() as db:
            failed_ids = []
            for post in posts:
                row = db.execute("SELECT attempts,state FROM posts WHERE id=?", (post["id"],)).fetchone()
                if row["state"] != "analyzing":
                    continue
                state = "failed" if row["attempts"] >= MAX_ATTEMPTS else "pending"
                db.execute("UPDATE posts SET state=?,retry_at=?,error=? WHERE id=?",
                           (state, time.time() + 300 * 2 ** (row["attempts"] - 1), str(error)[-3000:], post["id"]))
                if state == "failed":
                    failed_ids.append(post["id"])
            if failed_ids:
                db.execute("INSERT INTO failure_mail(post_ids) VALUES (?)", (json.dumps(failed_ids),))

    def stats(self):
        with self.connect() as db:
            return {r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM posts GROUP BY state")}

    def mail_stats(self):
        with self.connect() as db:
            return {r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM failure_mail GROUP BY state")}

    def notification_stats(self):
        with self.connect() as db:
            return {r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM reset_notification GROUP BY state")}


def send_failure_email(post_ids, data):
    if not FAILURE_EMAIL:
        raise MailDeliveryError("TIBO_MONITOR_FAILURE_EMAIL is not configured")
    cli = shutil.which("agently-cli")
    if not cli:
        raise MailDeliveryError("agently-cli not found")
    links = "\n".join(f"https://x.com/{HANDLE}/status/{post_id}" for post_id in post_ids)
    body = (f"Tibo 监控有 {len(post_ids)} 条动态连续分析失败 {MAX_ATTEMPTS} 次，已停止自动重试。\n"
            f"需要你决定：排查模型或网络后，是否执行 retry-failed 重新排队。\n"
            f"帖子：\n{links}\n状态目录：{data}")
    command = [cli, "message", "+send", "--to", FAILURE_EMAIL,
               "--subject", "Tibo 监控：分析三次失败，需要处理", "--body", body, "--confirmed"]
    env = dict(os.environ, AGENTLY_WORKSPACE="codex")
    try:
        result = subprocess.run(command, env=env, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired as exc:
        raise MailDeliveryError("Mail delivery timed out; check Sent before retrying", uncertain=True) from exc
    except OSError as exc:
        raise MailDeliveryError(str(exc)) from exc
    if result.returncode:
        # Keep the local error for diagnosis; never include raw subprocess logs in email.
        detail = (result.stdout or result.stderr).decode("utf-8", errors="replace")[-3000:]
        retry = re.search(r"retry[-_]after(?:_seconds)?[\"\s:=]+(\d+)", detail, re.I)
        raise MailDeliveryError(f"agently-cli exit {result.returncode}: {detail}",
                                retryable=result.returncode in {1, 4, 7},
                                retry_after=int(retry.group(1)) if retry else 0)


def process_failure_mail(store, sender=send_failure_email):
    if sender is send_failure_email and not FAILURE_EMAIL:
        return False
    with store.connect() as db:
        row = db.execute("SELECT * FROM failure_mail WHERE state='pending' AND retry_at<=? ORDER BY id LIMIT 1",
                         (time.time(),)).fetchone()
        if row is None:
            return False
        db.execute("UPDATE failure_mail SET state='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
    try:
        sender(json.loads(row['post_ids']), store.data)
    except Exception as exc:
        attempts = row['attempts'] + 1
        uncertain = isinstance(exc, MailDeliveryError) and exc.uncertain
        retryable = isinstance(exc, MailDeliveryError) and exc.retryable and attempts < 3
        state = 'unknown' if uncertain else ('pending' if retryable else 'error')
        delay = max(300 * 2 ** (attempts - 1), getattr(exc, 'retry_after', 0))
        with store.connect() as db:
            db.execute("UPDATE failure_mail SET state=?,retry_at=?,error=? WHERE id=?",
                       (state, time.time() + delay, str(exc)[-3000:], row['id']))
        logging.exception("failure_mail_not_sent state=%s", state)
    else:
        with store.connect() as db:
            db.execute("UPDATE failure_mail SET state='sent',sent_at=?,error=NULL WHERE id=?",
                       (utc_now(), row['id']))
        logging.info("failure_mail_sent posts=%s", len(json.loads(row['post_ids'])))
    return True


def send_reset_notification(rows):
    if not FEISHU_NOTIFY:
        raise MailDeliveryError("TIBO_MONITOR_FEISHU_NOTIFY is not configured")
    latest = max(rows, key=lambda row: (row["created"], row["post_id"]))
    if latest["reset_class"] == "quota_change":
        analysis = json.loads(latest["analysis"])
        message = "【Tibo 额度公告】\n" + analysis["summary_zh"]
    elif latest["reset_class"] == "confirmed":
        message = "【Tibo Reset】\nTibo 已确认额度 reset 生效。"
    else:
        message = "【Tibo Reset】\nTibo 宣布额度将 reset。"
    message += "\n原帖：" + latest["url"]
    try:
        result = subprocess.run([FEISHU_NOTIFY, message], capture_output=True, timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise MailDeliveryError("Feishu delivery timed out; check Feishu before retrying", uncertain=True) from exc
    except OSError as exc:
        raise MailDeliveryError(str(exc)) from exc
    if result.returncode:
        detail = (result.stdout or result.stderr).decode("utf-8", errors="replace")[-3000:]
        raise MailDeliveryError(f"Feishu notifier exit {result.returncode}: {detail}", retryable=True)


def process_reset_notifications(store, sender=send_reset_notification):
    if sender is send_reset_notification and not FEISHU_NOTIFY:
        return False
    now = time.time()
    with store.connect() as db:
        row = db.execute("SELECT n.*,p.analysis FROM reset_notification n JOIN posts p ON p.id=n.post_id "
                         "WHERE n.state='pending' AND n.retry_at<=? "
                         "AND (n.reset_class IN ('confirmed','quota_change') OR n.queued_at<=?) "
                         "ORDER BY n.created,n.post_id LIMIT 1",
                         (now, now - ANNOUNCEMENT_WINDOW)).fetchone()
        if row is None:
            return False
        if row["reset_class"] == "announced":
            rows = db.execute("SELECT * FROM reset_notification WHERE state='pending' AND reset_class='announced' "
                              "AND created>=? AND created<=? ORDER BY created,post_id",
                              (row["created"], row["created"] + ANNOUNCEMENT_WINDOW)).fetchall()
        else:
            rows = [row]
        ids = [r["post_id"] for r in rows]
        db.executemany("UPDATE reset_notification SET state='sending',attempts=attempts+1 WHERE post_id=?", [(i,) for i in ids])
    try:
        sender(rows)
    except Exception as exc:
        attempts = row["attempts"] + 1
        uncertain = isinstance(exc, MailDeliveryError) and exc.uncertain
        retryable = isinstance(exc, MailDeliveryError) and exc.retryable and attempts < 3
        state = "unknown" if uncertain else ("pending" if retryable else "error")
        with store.connect() as db:
            db.executemany("UPDATE reset_notification SET state=?,retry_at=?,error=? WHERE post_id=?",
                           [(state, time.time() + 300 * 2 ** (attempts - 1), str(exc)[-3000:], i) for i in ids])
        logging.exception("reset_notification_not_sent state=%s", state)
    else:
        with store.connect() as db:
            db.executemany("UPDATE reset_notification SET state='sent',sent_at=?,error=NULL WHERE post_id=?",
                           [(utc_now(), i) for i in ids])
        logging.info("reset_notification_sent posts=%s", len(ids))
    return True


def request_json(path, params=None):
    url = API + path + (("?" + urlencode(params)) if params else "")
    req = Request(url, headers={"User-Agent": "TiboLocalMonitor/1.0 (personal low-frequency reader)",
                                "Accept": "application/json"})
    with urlopen(req, timeout=30) as response:
        if response.status == 204:
            return None
        value = json.load(response)
    if not isinstance(value, dict) or value.get("code") != 200:
        raise ValueError("FxTwitter returned an invalid response")
    return value


def own_post(post):
    author = post.get("author") or {}
    return (post.get("type") == "status" and author.get("id") == AUTHOR_ID
            and author.get("screen_name", "").lower() == HANDLE
            and re.fullmatch(r"\d{2,20}", str(post.get("id", ""))) is not None
            and isinstance(post.get("created_timestamp"), int)
            and isinstance(post.get("text"), str))


def fetch_timeline(cutoff=None, fetch=request_json):
    params = {"count": 40, "with_replies": 1}
    if cutoff is not None:
        params["since"] = cutoff
    found = {}
    visited = set()
    for _ in range(MAX_PAGES):
        result = fetch(f"/profile/{HANDLE}/statuses", params)
        if result is None:
            return list(found.values()), True
        if not isinstance(result.get("results"), list):
            raise ValueError("Missing timeline results")
        posts = [p for p in result["results"] if own_post(p)]
        found.update((p["id"], p) for p in posts)
        cursor = (result.get("cursor") or {}).get("bottom")
        if cutoff is None or not cursor or (posts and min(p["created_timestamp"] for p in posts) <= cutoff):
            return list(found.values()), True
        if cursor in visited:
            return list(found.values()), False
        visited.add(cursor)
        params["cursor"] = cursor
    return list(found.values()), False


def poll_once(store, fetch=request_json):
    meta = store.meta()
    first = "baseline_timestamp" not in meta
    baseline = meta.get("baseline_timestamp", int(time.time()))
    cutoff = None if first else max(baseline, meta["highwater_timestamp"] - 86400)
    posts, covered = fetch_timeline(cutoff, fetch)
    if first:
        # All known history is context; new events start from this launch time.
        store.put_meta(baseline_timestamp=baseline, highwater_timestamp=baseline)
    added = store.ingest(posts, baseline)
    if not covered:
        raise RuntimeError("Timeline coverage gap: page limit/repeated cursor; watermark not advanced")
    newest = max([baseline, meta.get("highwater_timestamp", baseline)]
                 + [p["created_timestamp"] for p in posts])
    store.put_meta(highwater_timestamp=newest, last_poll_ok=utc_now(),
                   consecutive_poll_failures=0, last_poll_error=None)
    logging.info("poll_ok new=%s total_returned=%s baseline=%s", added, len(posts), first)
    return added


def compact(post, depth=0):
    if not post:
        return None
    value = {k: post.get(k) for k in ("id", "type", "text", "url", "created_timestamp", "replying_to")}
    value["author"] = (post.get("author") or {}).get("screen_name")
    value["has_unread_media"] = bool(post.get("media"))
    if depth < 3 and post.get("quote"):
        value["quote"] = compact(post["quote"], depth + 1)
    return value


def supplied_ids(posts):
    ids = set()
    for post in posts:
        if post.get("id"):
            ids.add(post["id"])
        if post.get("quote"):
            ids.update(supplied_ids([post["quote"]]))
    return ids


def validate_analyses(value, posts, context):
    analyses = value.get("analyses") if isinstance(value, dict) else None
    if not isinstance(analyses, list):
        raise ValueError("Missing analyses array")
    expected = {p["id"] for p in posts}
    ids = [a.get("post_id") for a in analyses if isinstance(a, dict)]
    if len(ids) != len(analyses) or len(ids) != len(expected) or set(ids) != expected:
        raise ValueError("Output IDs do not match the input batch exactly")
    allowed = supplied_ids(posts + context)
    schema = json.loads((ROOT / "analysis.schema.json").read_text(encoding="utf-8"))
    required = set(schema["properties"]["analyses"]["items"]["required"])
    for item in analyses:
        if set(item) != required:
            raise ValueError("Invalid analysis fields")
        for field in required - {"evidence_post_ids"}:
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError("Invalid text field: " + field)
        evidence = item["evidence_post_ids"]
        if (not isinstance(evidence, list) or any(not isinstance(x, str) for x in evidence)
                or not set(evidence) <= allowed):
            raise ValueError("Analysis cites an unavailable post")
        if item["reset_class"] not in RESET_CLASSES or item["evidence_strength"] not in {"explicit", "contextual", "none"}:
            raise ValueError("Invalid classification")
        if item["reset_class"] == "quota_change" and item["evidence_strength"] != "explicit":
            raise ValueError("Quota announcement requires explicit evidence")
        if item["reset_class"] in {"confirmed", "announced", "quota_change"} and not evidence:
            raise ValueError("Reset classification has no evidence")
    return analyses


def analyze(posts, context, data, stop=None):
    codex = find_codex()
    stop = stop or threading.Event()
    prompt = (ROOT / "analysis-prompt.txt").read_text(encoding="utf-8")
    prompt += "\n\nINPUT_JSON:\n" + json.dumps({"posts": [compact(p) for p in posts],
                                                 "context": [compact(p) for p in context]}, ensure_ascii=False)
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-", dir=data) as folder:
        result_path = Path(folder) / "result.json"
        command = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                   "--color", "never", "--output-schema", str(ROOT / "analysis.schema.json"),
                   "-o", str(result_path), "-"]
        with (Path(folder) / "stderr.log").open("wb") as err:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=err, cwd=data, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            started = time.monotonic()
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
                while proc.poll() is None:
                    if stop.wait(0.5):
                        raise AnalysisCancelled("Analysis interrupted by monitor stop")
                    if time.monotonic() - started > 600:
                        raise TimeoutError("Codex analysis exceeded 600 seconds")
                if proc.returncode != 0 or not result_path.exists():
                    err.flush()
                    detail = (Path(folder) / "stderr.log").read_text(encoding="utf-8", errors="replace")[-3000:]
                    raise RuntimeError(f"Codex failed ({proc.returncode}): {detail}")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                proc.stdin.close()
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    return validate_analyses(result, posts, context)


def enrich_context(posts, context, fetch=request_json):
    known = supplied_ids(posts + context)
    for post in posts:
        parent_id = (post.get("replying_to") or {}).get("status")
        if parent_id and parent_id not in known and re.fullmatch(r"\d{2,20}", parent_id):
            try:
                result = fetch("/status/" + parent_id)
                parent = (result or {}).get("status")
                if parent and parent.get("id") == parent_id and parent.get("type") == "status":
                    context.append(parent)
                    known.update(supplied_ids([parent]))
            except (OSError, ValueError):
                logging.warning("parent_unavailable id=%s", parent_id)
    return context


def save_report(reports, post, item):
    reports = Path(reports)
    record = {"analyzed_at_utc": utc_now(), "source": "FxTwitter third-party API",
              "post": compact(post), "analysis": item}
    atomic_json(reports / (post["id"] + ".json"), record)
    # JSON is authoritative; Markdown is the human-readable equivalent.
    reports.mkdir(parents=True, exist_ok=True)
    beijing = datetime.fromtimestamp(post["created_timestamp"], timezone(timedelta(hours=8))).isoformat()
    text = (f"# Tibo 动态 · {beijing}\n\n{post['url']}\n\n"
            f"{item['summary_zh']}\n\n分类：{item['reset_class']}；证据：{item['evidence_strength']}\n\n"
            f"范围：{item['scope_zh']}\n\n依据：{item['reason_zh']}\n\n"
            f"证据帖子：{', '.join(item['evidence_post_ids']) or '无'}\n\n"
            f"正文来自 FxTwitter 第三方接口。\n\n原文：\n\n{post['text']}\n")
    (reports / (post["id"] + ".md")).write_text(text, encoding="utf-8")


def process_batch(store, reports, stop, analyzer=analyze, enrich=enrich_context):
    posts = store.claim()
    if not posts:
        return False
    try:
        context = enrich(posts, store.context(posts))
        results = analyzer(posts, context, store.data, stop)
        by_id = {p["id"]: p for p in posts}
        for item in results:
            save_report(reports, by_id[item["post_id"]], item)
            store.finish(by_id[item["post_id"]], item)
        store.put_meta(last_analysis_ok=utc_now(), last_analysis_error=None)
        logging.info("analysis_ok posts=%s", len(posts))
    except AnalysisCancelled:
        store.cancel(posts)
        logging.info("analysis_cancelled posts=%s", len(posts))
    except Exception as exc:
        # Keep already committed members done if a later report write failed.
        with store.connect() as db:
            remaining = {r["id"] for r in db.execute("SELECT id FROM posts WHERE state='analyzing'")}
        store.fail([p for p in posts if p["id"] in remaining], exc)
        store.put_meta(last_analysis_error=str(exc)[-3000:])
        logging.exception("analysis_failed")
    return True


def run(data, reports):
    store = Store(data)
    with InstanceLock(data / "instance.lock"):
        (data / "stop.request").unlink(missing_ok=True)
        store.recover()
        store.put_meta(pid=os.getpid(), started_at=utc_now(), stopped_at=None, interval_seconds=INTERVAL)
        stop = threading.Event()
        wake = threading.Event()

        def worker():
            while not stop.is_set():
                try:
                    analyzed = process_batch(store, reports, stop)
                    mailed = False if stop.is_set() else process_failure_mail(store)
                    notified = False if stop.is_set() else process_reset_notifications(store)
                    if not analyzed and not mailed and not notified:
                        wake.wait(2)
                        wake.clear()
                except Exception as exc:
                    logging.exception("analysis_worker_error")
                    store.put_meta(last_analysis_error=str(exc)[-3000:])
                    stop.wait(30)

        thread = threading.Thread(target=worker, name="codex-analyzer", daemon=True)
        thread.start()
        next_poll = 0.0
        try:
            while not (data / "stop.request").exists():
                if time.monotonic() >= next_poll:
                    poll_started = time.monotonic()
                    delay = INTERVAL
                    try:
                        if poll_once(store):
                            wake.set()
                    except Exception as exc:
                        failures = store.meta().get("consecutive_poll_failures", 0) + 1
                        store.put_meta(consecutive_poll_failures=failures, last_poll_error=str(exc))
                        delay = min(3600, INTERVAL * 2 ** min(failures - 1, 4))
                        if isinstance(exc, HTTPError) and exc.code == 429:
                            retry = exc.headers.get("Retry-After", "")
                            if retry.isdigit():
                                delay = max(delay, int(retry))
                        logging.exception("poll_failed next_attempt_seconds=%s", delay)
                    next_poll = poll_started + delay
                    store.put_meta(next_poll_at_utc=datetime.fromtimestamp(
                        time.time() + max(0, next_poll - time.monotonic()), timezone.utc).isoformat())
                atomic_json(data / "heartbeat.json", {"time": utc_now(), "pid": os.getpid(),
                                                       "analyzer_alive": thread.is_alive(), "stats": store.stats(),
                                                       "failure_mail": store.mail_stats(),
                                                       "reset_notification": store.notification_stats()})
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            wake.set()
            thread.join(timeout=45)
            if thread.is_alive():
                logging.error("Analyzer still stopping; pending jobs recover on restart")
            store.put_meta(stopped_at=utc_now())
            logging.info("monitor_stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "status", "stop", "retry-failed"])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    args = parser.parse_args()
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    if args.command == "run":
        handler = RotatingFileHandler(data / "monitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[handler])
        run(data, args.reports_dir.resolve())
    elif args.command == "status":
        store = Store(data)
        value = {"running": is_running(data), "data_directory": str(data),
                 "reports_directory": str(args.reports_dir.resolve()), "stats": store.stats(), "state": store.meta(),
                 "failure_mail": store.mail_stats(), "reset_notification": store.notification_stats()}
        heartbeat = data / "heartbeat.json"
        if heartbeat.exists():
            value["heartbeat"] = json.loads(heartbeat.read_text(encoding="utf-8"))
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "stop":
        if is_running(data):
            (data / "stop.request").touch()
            print("Stop requested; analysis will be interrupted and retained for restart.")
        else:
            print("Monitor is not running.")
    elif args.command == "retry-failed":
        store = Store(data)
        with store.connect() as db:
            count = db.execute("UPDATE posts SET state='pending',attempts=0,retry_at=0 WHERE state='failed'").rowcount
        print(f"Requeued {count} failed posts")


if __name__ == "__main__":
    main()
