---
name: workbuddy-action-checkin
description: 巡检 WorkBuddy「Buddy 加油站」每日签到在 GitHub Actions 上的执行情况并回传运行摘要。当需要「确认今天的 WorkBuddy 签到跑了没」「看 Actions 的签到 summary」「定时巡检 Buddy 签到」，或 Actions 没执行/失败需要补触发时使用。调用 GitHub REST API 判断当日（UTC+8）是否有成功运行，缺失或失败则用 PAT 补触发 workflow_dispatch 并等待完成，输出该次运行的 Job Summary；Actions 方案最终未签到成功时给出回退信号，交由 totorosir-workbuddy-checkin 技能本地补签。
agent_created: true
---

# WorkBuddy Actions 签到巡检

判断 WorkBuddy 每日签到在 GitHub Actions 上「今天跑没跑、跑成什么样」，没跑或跑挂了就补触发，最终把 Job Summary 原样带回来；Actions 这条路彻底不行时回退本地签到技能。

适用于 `workbuddy-checkin-public` 这类「fork 仓库 + 外部 cron 触发」的签到部署：GitHub 自带的 `schedule` 在 fork 里不会触发，定时由外部服务调 `workflow_dispatch` 接口驱动，本技能负责巡检与兜底。

## 安装

1. 把整个 `workbuddy-action-checkin/` 目录拷到 `~/.workbuddy/skills/` 下。
2. 编辑 `config.json`，把 `repo` 改成你自己的 `owner/repo`。
3. 准备 PAT（fine-grained token，Repository access 只选该仓库，Permissions → Actions: **Read and write**），写入 `token_file` 指向的文件，例如：

```bash
mkdir -p ~/.workbuddy/secrets
printf '%s' 'github_pat_xxx' > ~/.workbuddy/secrets/workbuddy-checkin.pat
```

> 不要把 token 写进 `config.json` 的 `token` 字段再提交到版本库；真要写内联字段，请确保该文件不会被推送。

## 执行

```bash
python ~/.workbuddy/skills/workbuddy-action-checkin/scripts/watch.py
```

单次耗时：正常情况 10–30 秒；需要补触发时约 1–2 分钟（脚本自己轮询，不要中途打断，也不要并行再跑一遍）。

脚本内部流程：查最近运行 → 找「今天（UTC+8）」的记录 → 今天只有「仅查询」/ 失败 / 没有记录 → 补触发 workflow_dispatch → 等运行结束 → 下载日志 zip → 从「输出运行摘要」步骤截出 Markdown → 判定签到结果。

> 前提：签到工作流的「输出运行摘要」步骤需要同时写到 stdout，否则摘要只存在于 Actions 页面、API 取不到。对应写法：
>
> ```bash
> python scripts/summarize_result.py result.json | tee -a "$GITHUB_STEP_SUMMARY"
> ```

## 输出判读

stdout 固定三段：`STATUS:` / `RUN:` / `---- SUMMARY ----` 加上摘要正文。

| STATUS | 退出码 | 含义 | 后续动作 |
| --- | --- | --- | --- |
| `OK` | 0 | 今天已有成功运行，未重复触发 | 直接把摘要报给用户 |
| `RETRIGGERED` | 0 | 原本没有今天的运行 / 只跑了仅查询 / 上次失败，已补触发且签到成功 | 汇报时说明「已自动补触发」 |
| `FALLBACK` | 1 | Actions 最终没签到成功 | **立即加载 `totorosir-workbuddy-checkin` 技能本地补签**，见下节 |
| `ERROR` | 2 | token 失效、网络异常、配置缺失 | 原样转述 `REASON`，不要盲目重试 |

判定依据（以日志里的业务证据为准，不看运行结论）：

- **失败**：`"checkin_http"` ≥ 400、`"transient_failure": true`、`| 结果 | 失败 |`、`❌ 签到失败`、`"status": "error"`，或出现「脚本说今日已签到、但状态查询 `today_checked_in: false`」这种自相矛盾。
- **成功**：`| 结果 | 成功 |`、`✅ 签到成功`、`⏭️ 今日已签到`、`"action": "clicked"`、`"action": "skip_already_signed"`（且不与状态查询矛盾）。
- **无证据**：以上都没有 → 判 `unknown`，按 FALLBACK 处理。

> ⚠️ 不要用 `conclusion == success` 推断签到成功。2026-11-04 出现过「签到接口 HTTP 500 / code=10001 被脚本误判成已签到 → 工作流绿灯 → 实际漏签」的假成功，运行结论恰恰是 success。

## FALLBACK 的标准处理

1. 调用 Skill 工具加载 `totorosir-workbuddy-checkin`，按其说明执行本地签到。
2. 汇报里把两条链路都说清楚：Actions 的 `RUN` 号与失败原因 + 本地签到的结果（积分、连签天数）。
3. 本地签到若提示登录态失效/需要授权，停止并引导用户按该技能「首次使用」重新授权——不要反复重试。
4. 若本地也已签到（返回「今日已签到」），说明当天已领取，如实汇报，不要当成失败。

## 汇报格式

先给结论一句话（今天跑了没、是否补触发、签到结果），再把 `---- SUMMARY ----` 之后的 Markdown 原样贴出（它是仓库里 `scripts/summarize_result.py` 生成的 Job Summary，含结果、动作、points、balance、msg、streak_days）。不要自行改写摘要里的数字。

## 配置

`config.json`（与本文件同级）：

| 字段 | 说明 |
| --- | --- |
| `repo` | `owner/repo`，必填，改成你自己的签到仓库 |
| `workflow_file` | 工作流文件名，默认 `checkin.yml` |
| `ref` | 触发分支，默认 `main` |
| `token_file` | PAT 文件路径，默认 `~/.workbuddy/secrets/workbuddy-checkin.pat`（也支持环境变量 `GITHUB_PAT` 覆盖，或直接写 `token` 字段） |
| `timezone_offset_hours` | 判定「今天」的时区，默认 8（UTC+8） |
| `dispatch_wait_seconds` | 触发后等新记录出现的上限，默认 150 |
| `run_wait_seconds` | 等待运行结束的上限，默认 300 |

## 排障

- **401**：PAT 过期或被撤销。fine-grained token 有有效期，重新生成后覆盖 `token_file` 即可；如果同时用外部定时服务（如 cron-job.org）触发签到，记得同步更新那边的 `Authorization` 头，否则定时本身也会一起失效。
- **404**：`repo` / `workflow_file` 写错，或 PAT 没有该仓库权限。
- **已触发但等不到记录**：GitHub 队列拥堵，调大 `dispatch_wait_seconds` 或稍后重跑。
- **摘要为空但 STATUS 是 OK**：说明日志里确实有签到证据，只是没截到摘要表；脚本会打印 `NOTE:`。若连证据都没有，脚本会判 `FALLBACK` 而不是 OK。
- **日志取不到分步骤文件**：GitHub 会在运行结束一段时间后重打包日志，`1_输出运行摘要.txt` 之类会退化成单个 `0_执行签到.txt`。脚本已回退到全量日志按锚点截取，不必处理。
- **App 没在 09:10 运行**：自动化会在下次启动时补跑（24 小时内），但补跑只能事后兜底。真正的韧性来自签到脚本自身的重试（`MAX_RETRY=3`，5/10/15 秒退避）——瞬时 5xx 在 Actions 内部就自愈，不必等本机。

## 约束

- **不要打印 token 明文**，也不要提交到版本库、写进日志或贴到聊天里。
- 不要一天内反复触发：脚本会先判断当天是否已有成功运行，复用它而不是手工 dispatch。
- 只读 `config.json` 与 GitHub API，不修改签到仓库的代码。
