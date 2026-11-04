#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy GitHub Actions 签到巡检器。

职责：
  1. 查目标仓库的 workflow run，判断「今天（CST）」是否已经跑过每日签到；
  2. 没有跑 / 跑失败了 / 只是跑了个「仅查询」→ 用 PAT 调 workflow_dispatch 补触发，并等它跑完；
  3. 拉取该次运行的日志，从「输出运行摘要」步骤里截出 Job Summary 原文输出；
  4. 若 Actions 这条路最终没签到成功，以退出码 1 提示上层回退本地签到技能
     （totorosir-workbuddy-checkin）。

输出格式（stdout）：
  STATUS: OK | RETRIGGERED | FALLBACK | ERROR
  RUN: <id> <event> <conclusion> <created_at CST>
  ---- SUMMARY ----
  <markdown 摘要>

退出码：
  0  OK / RETRIGGERED（Actions 侧已确认签到成功）
  1  FALLBACK（Actions 不可用或签到未成功，需回退本地签到）
  2  ERROR（配置 / 网络 / token 问题，未产生结论）
"""

import io
import json
import os
import re
import sys
import time
import zipfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(SKILL_DIR, "config.json")
API = "https://api.github.com"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS_PREFIX = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")
SUMMARY_ANCHOR = "## WorkBuddy 签到结果"

EXIT_OK = 0
EXIT_FALLBACK = 1
EXIT_ERROR = 2


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        raise SystemExit(f"缺少配置文件：{CONFIG_PATH}")
    with io.open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)

    token = os.environ.get("GITHUB_PAT") or ""
    if not token:
        token_file = cfg.get("token_file") or ""
        if token_file:
            path = os.path.expanduser(token_file)
            if os.path.isfile(path):
                with io.open(path, encoding="utf-8") as fh:
                    token = fh.read().strip()
    if not token:
        token = cfg.get("token") or ""
    if not token:
        raise SystemExit(
            "缺少 GitHub token：请把 PAT 写入 config.json 的 token_file 指向的文件，"
            "或设置环境变量 GITHUB_PAT"
        )

    cfg["token"] = token
    cfg.setdefault("ref", "main")
    cfg.setdefault("timezone_offset_hours", 8)
    cfg.setdefault("dispatch_wait_seconds", 150)
    cfg.setdefault("run_wait_seconds", 300)
    return cfg


def request(cfg, path, method="GET", body=None, raw=False):
    url = path if path.startswith("http") else API + path
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {cfg['token']}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "workbuddy-action-checkin",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            status = resp.status
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        return error.code, None, detail
    except Exception as error:  # 网络层异常
        return 0, None, str(error)
    if raw:
        return status, payload, ""
    if not payload:
        return status, None, ""
    try:
        return status, json.loads(payload.decode("utf-8")), ""
    except Exception:
        return status, None, payload.decode("utf-8", "replace")[:400]


def cst_date(cfg, iso_text):
    if not iso_text:
        return ""
    moment = datetime.fromisoformat(iso_text.replace("Z", "+00:00")).astimezone(
        timezone(timedelta(hours=cfg["timezone_offset_hours"]))
    )
    return moment.strftime("%Y-%m-%d")


def cst_text(cfg, iso_text):
    if not iso_text:
        return "-"
    moment = datetime.fromisoformat(iso_text.replace("Z", "+00:00")).astimezone(
        timezone(timedelta(hours=cfg["timezone_offset_hours"]))
    )
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def list_runs(cfg, limit=20):
    path = f"/repos/{cfg['repo']}/actions/workflows/{cfg['workflow_file']}/runs?per_page={limit}"
    status, payload, detail = request(cfg, path)
    if status == 401:
        raise RuntimeError("PAT 已失效（401）。请重新生成 fine-grained PAT 并更新 token 文件")
    if status == 404:
        raise RuntimeError(f"仓库或工作流不存在（404）：{cfg['repo']} / {cfg['workflow_file']}")
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"查询运行记录失败 HTTP {status} {detail}")
    return payload.get("workflow_runs", [])


def dispatch(cfg):
    path = f"/repos/{cfg['repo']}/actions/workflows/{cfg['workflow_file']}/dispatches"
    status, _, detail = request(cfg, path, method="POST", body={"ref": cfg["ref"]})
    if status == 401:
        raise RuntimeError("PAT 已失效（401），无法触发 workflow_dispatch")
    if status not in (201, 204):
        raise RuntimeError(f"触发失败 HTTP {status} {detail}")
    return True


def wait_for_new_run(cfg, known_ids, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(8)
        for run in list_runs(cfg):
            if run["id"] not in known_ids:
                return run
    raise RuntimeError("已触发但没等到新的运行记录（GitHub 队列可能拥堵，稍后重跑）")


def wait_for_completion(cfg, run, timeout):
    deadline = time.time() + timeout
    current = run
    while time.time() < deadline:
        if current.get("status") == "completed":
            return current
        time.sleep(10)
        status, payload, _ = request(cfg, f"/repos/{cfg['repo']}/actions/runs/{current['id']}")
        if status == 200 and isinstance(payload, dict):
            current = payload
    return current


def fetch_logs(cfg, run_id):
    """返回 {"summary": 摘要正文来源, "all": 全部日志拼接}。

    ⚠️ 不能只依赖文件名：GitHub 会在运行结束一段时间后重打包日志，
    分步骤文件（如 `1_输出运行摘要.txt`）会退化成单个 `0_执行签到.txt`，
    此时按「文件名含摘要」找分块会一无所获（2026-11-04 因此误判为 OK）。
    因此：有摘要分块就用它，没有就退回全量日志，由 extract_summary 按锚点截取。
    """
    empty = {"summary": "", "all": ""}
    status, payload, _ = request(
        cfg, f"/repos/{cfg['repo']}/actions/runs/{run_id}/logs", raw=True
    )
    if status not in (200, 302) or not payload:
        return empty
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except Exception:
        return empty
    chunks = []
    for name in archive.namelist():
        if not name.lower().endswith(".txt"):
            continue
        try:
            chunks.append((name, archive.read(name).decode("utf-8", "replace")))
        except Exception:
            continue
    chunks.sort(key=lambda item: item[0])
    all_text = "\n".join(text for _, text in chunks)
    summary_text = "\n".join(text for name, text in chunks if "摘要" in name)
    return {"summary": summary_text or all_text, "all": all_text}


def clean_log(text):
    lines = []
    for line in text.splitlines():
        line = ANSI.sub("", line).replace("\ufeff", "")
        line = TS_PREFIX.sub("", line.rstrip())
        lines.append(line)
    return "\n".join(lines)


def extract_summary(text):
    """从摘要步骤日志里截出 Job Summary 正文；取最后一次出现，避开命令回显。"""
    cleaned = clean_log(text)
    index = cleaned.rfind(SUMMARY_ANCHOR)
    if index == -1:
        return ""
    lines = []
    for line in cleaned[index:].splitlines():
        stripped = line.strip()
        # 摘要正文到此为止：后续是该步骤的收尾输出
        if stripped.startswith(("Post job cleanup", "##[", "[command]", "Cleaning up orphan")):
            break
        lines.append(line)
    return "\n".join(lines).strip("\n")


def classify(summary, logs, conclusion):
    """返回 (state, reason)，state ∈ {"ok", "checkonly", "fail", "unknown"}。

    判定原则：**宁可判 unknown 走兜底，也不在没有证据时宣称成功。**
    2026-11-04 事故：签到接口 500 + code=10001 被脚本当成「今日已签到」，
    工作流结论是 success；旧版这里只要 conclusion == success 就返回 ok，
    于是把一次真实漏签报成了 OK。现在必须以日志里的业务证据为准。
    """
    text = (summary or "") + "\n" + clean_log(logs)

    # 1) 仅查询，不算签到
    if "🔍 仅查询" in text or '"action": "skip_check_only"' in text:
        return "checkonly", "本次只是「仅查询」，没有真正领取"

    # 2) 硬失败证据（优先于任何「成功」字样）
    http_hit = re.search(r'"checkin_http":\s*(\d{3})', text)
    if http_hit and int(http_hit.group(1)) >= 400:
        return "fail", f"签到接口返回 HTTP {http_hit.group(1)}"
    if '"transient_failure": true' in text:
        return "fail", "签到接口瞬时故障，重试后仍未成功"
    if "| 结果 | 失败 |" in text or "❌ 签到失败" in text or '"status": "error"' in text:
        return "fail", "日志/摘要显示签到失败"
    # 状态查询明确说「今天还没签」，却给了「今日已签到」的结论 → 自相矛盾，不信
    if '"action": "skip_already_signed"' in text and '"today_checked_in": false' in text:
        return "fail", "脚本称「今日已签到」，但状态查询显示今日未签到（结论矛盾）"

    # 3) 成功证据：必须是业务动作层面的，不能用泛指的成功字样
    if (
        "| 结果 | 成功 |" in text
        or "✅ 签到成功" in text
        or "⏭️ 今日已签到" in text
        or "今日已签到，无需重复领取" in text
        or '"action": "clicked"' in text
        or '"action": "skip_already_signed"' in text
    ):
        return "ok", ""

    # 4) 无证据：不猜
    if conclusion == "success":
        return "unknown", "运行结论为 success，但日志里找不到签到成功的证据"
    return "unknown", f"运行结论为 {conclusion or '未知'}，且日志里找不到签到证据"


def main():
    try:
        cfg = load_config()
    except SystemExit as error:
        print(f"STATUS: ERROR\nREASON: {error}")
        return EXIT_ERROR

    try:
        runs = list_runs(cfg)
    except RuntimeError as error:
        print(f"STATUS: ERROR\nREASON: {error}")
        return EXIT_ERROR

    today = datetime.now(timezone(timedelta(hours=cfg["timezone_offset_hours"]))).strftime("%Y-%m-%d")
    todays = [run for run in runs if cst_date(cfg, run.get("created_at")) == today]
    todays.sort(key=lambda run: run.get("created_at", ""), reverse=True)

    retriggered = False

    if todays:
        target = wait_for_completion(cfg, todays[0], cfg["run_wait_seconds"])
    else:
        known = {run["id"] for run in runs}
        dispatch(cfg)
        target = wait_for_new_run(cfg, known, cfg["dispatch_wait_seconds"])
        target = wait_for_completion(cfg, target, cfg["run_wait_seconds"])
        retriggered = True

    logs = fetch_logs(cfg, target["id"])
    summary = extract_summary(logs["summary"])
    state, reason = classify(summary, logs["all"], target.get("conclusion") or target.get("status"))

    # 今天只有「仅查询」/ 失败 / 拿不到证据 → 补触发一次真实签到
    # （补触发的运行日志是新生成的，能正常按步骤分块取到摘要，顺带把「读不到」变成「读得到」）
    if state in ("checkonly", "fail", "unknown"):
        known = {run["id"] for run in list_runs(cfg)}
        dispatch(cfg)
        target = wait_for_new_run(cfg, known, cfg["dispatch_wait_seconds"])
        target = wait_for_completion(cfg, target, cfg["run_wait_seconds"])
        retriggered = True
        logs = fetch_logs(cfg, target["id"])
        summary = extract_summary(logs["summary"])
        state, reason = classify(summary, logs["all"], target.get("conclusion") or target.get("status"))

    header = (
        f"RUN: {target['id']} {target.get('event')} "
        f"{target.get('conclusion') or target.get('status')} "
        f"{cst_text(cfg, target.get('created_at'))}"
    )

    if state == "ok":
        print(f"STATUS: {'RETRIGGERED' if retriggered else 'OK'}")
        print(header)
        if reason:
            print(f"NOTE: {reason}")
        print("---- SUMMARY ----")
        print(summary or "（未能从日志提取摘要，但运行结论为 success）")
        return EXIT_OK

    print("STATUS: FALLBACK")
    print(header)
    print(f"REASON: {reason}；运行结论={target.get('conclusion') or target.get('status')}")
    print("---- SUMMARY ----")
    print(summary or "（无摘要）")
    return EXIT_FALLBACK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # 兜底，避免堆栈污染输出
        print(f"STATUS: ERROR\nREASON: {error}")
        sys.exit(EXIT_ERROR)
