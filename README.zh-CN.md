# ccstory

[English](README.md) · [繁體中文](README.zh-TW.md) · [简体中文](README.zh-CN.md)

> **ccusage 告诉你账单，ccstory 告诉你故事。**

一个 Claude Code 用量回顾工具，回答 token 计数器答不出来的问题:
**你这周到底在做什么？**

```
╭──────────────── Claude Code Recap · May 5 – 12, 2026 ────────────────╮
│                                                                      │
│  ★ Top focus  coding  10.9h  (53% of active time)                    │
│    ↳ Built /show-routine slash command using bash+python to fetch…   │
│                                                                      │
│  Active  20.6h  Sessions  74   Output  2.92M                         │
│  Turns   3,692  Cache     96%  Cost    $1,608                        │
│                                                                      │
│  Time by category                                                    │
│  coding          ███████████████░░░░░░░░░░░░░   10.9h    53%         │
│  investment      █████████████░░░░░░░░░░░░░░░    9.6h    47%         │
│  writing         ░░░░░░░░░░░░░░░░░░░░░░░░░░░░    0.1h     0%         │
│                                                                      │
│  Full report → ~/.ccstory/reports/recap-2026-W19.md                  │
│                                                                      │
╰────────────────────────────── ccstory ───────────────────────────────╯
```

（实际在终端里每个 bucket 都有自己的颜色 —— investment 绿、coding 青、
writing 洋红 ——「★ Top focus」会用最长 session 的叙事高亮最大那块。）

完整的 markdown 报告会再深入一层 —— 每个 session 一句叙事。默认使用 Claude
Code 自己写入 session jsonl 的 `aiTitle` 记录；加上 `--rich` 则改由本机
`claude -p` 撰写，用更 outcome-focused 的句型:

```
### investment

- 2026-05-09 10:59 · 28m · 76 msg — 评估 ONDS 财报前的加减仓策略，
  并为 AI 半导体敞口定义评分指标。
- 2026-05-08 23:53 · 22m · 88 msg — 在大型科技财报潮后筛选 Q1 AI
  应用层赢家，锁定下一波 setup。
```

## ccstory 提供了 ccusage 没有的东西

| | [ccusage](https://github.com/ryoppippi/ccusage) | **ccstory** |
|---|---|---|
| 角色 | 账单 | 故事 |
| Token / 成本精度 | ✅ daily/monthly/session/5h-block | (粗估) |
| 按模型分解 | ✅ | ✅ |
| **活跃时数**（5 分钟 gap 启发式） | ❌ | ✅ |
| **活动分类**（不只是目录名） | ❌ | ✅ |
| **每个 session 一句叙事** | ❌ | ✅ 来自 Claude Code 自己写的 `aiTitle`（零额外 API 调用） |
| **基于 output tokens 的跨期比较** | ❌（用 total_tokens） | ✅ |
| 实时 quota | ⚠️ 通过 `blocks` | ❌ |

**两者互补，不是竞品。** 一起用:

```bash
ccusage monthly        # 你花了多少钱
ccstory month          # 你花在什么上
```

## 安装

```bash
pipx install ccstory
# 或一次性使用:
pip install ccstory
```

需要 Python 3.11+。默认快速路径不需要任何外部 CLI —— ccstory 直接读取
Claude Code 已经写入每个 session jsonl 的 `aiTitle` 记录（CLI 顶部那行
灰色标题）。如果要更 outcome-focused 的叙事（"重构 auth middleware…"），
加上 `--rich`，当没有 `aiTitle` 时会 fallback 到本机 `claude -p`。

**新手吗？** 先看 [5 分钟教程](TUTORIAL.zh-CN.md) 再开始。

## 使用方式

```bash
ccstory init             # 一次性: 扫描近期 sessions，建议 bucket
ccstory init --dry-run   # 预览，不写入 config

ccstory                  # 本月至今（默认）
ccstory week             # 过去 7 天
ccstory 2026-04          # 任意指定月份
ccstory all              # 整个历史

ccstory trend            # 过去 8 周 sparkline
ccstory trend --weeks 12 # 自定义范围
ccstory trend --months 6 # 以日历月为单位

ccstory --rich           # 用本机 `claude -p` 写 outcome-focused 叙事
ccstory --no-summary     # 完全跳过 per-session 叙事
ccstory --no-compare     # 跳过 vs-previous 比较区块
```

**建议的第一次运行**: `ccstory init` 会扫描最近 30 天的 sessions，通过一次
`claude -p` 调用（约 15 秒）为每个 project 建议分类 bucket。它会写一份起手式
`~/.ccstory/config.toml`，之后可以随时编辑。

`ccstory week` / `ccstory month` 会自动附上 **vs-previous-window** 比较
（每个 bucket ▲/▼ delta）。`ccstory trend` 显示 per-bucket sparkline，让你
一眼看到 N 周/月的趋势形状:

```
Hours by bucket
total          ▁▄▆▇▃█    16.5h   avg 9.0h   ▲ +183%
investment     ▁▃▅█▆█     6.3h   avg 4.0h   ▲ +29%
coding         ▁▂▃▄▁█    10.2h   avg 3.3h   ▲ +1148%
writing        ▁▇█▆▁▁     0.1h   avg 1.8h   ▼ -51%

Overall
output         ▁▁▁▄▁█     3.0M   avg 0.8M   ▲ +2460%
cost           ▁▁▂▃▁█   $1,643   avg $463   ▲ +1877%
burn %         ▁▁▂▃▁█     201%   avg 57%    ▲ +1877%
```

`burn %` 那行显示 API-equivalent 成本占你按比例摊分月 quota 的百分比 ——
在 `~/.ccstory/config.toml` 设置 `monthly_quota_usd`
（默认 $3,500 ≈ Max 20x 方案）。设为 `0` 可隐藏这行。

第一次运行会 scaffold `~/.ccstory/config.toml`，并显示你的 projects 被
如何分类。

## 分类

四个默认 bucket，与 project 目录名匹配:

| Bucket | 关键字（示例） |
|---|---|
| `investment` | investment, stock, portfolio, trading, ticker, etf, finance |
| `writing` | blog, newsletter, post, docs, content, article |
| `coding` | app, sdk, cli, plugin, mcp, server, frontend, backend, lib, … |
| `other` | playground, scratch, sandbox, experiment |

对不上的 project 默认落到 `coding` —— 根据 2026 Pragmatic Engineer
开发者调查，~46% 的 Claude Code 用途是软件开发。

在 `~/.ccstory/config.toml` 自定义:

```toml
default_bucket = "coding"

[categories]
"work"    = ["company-repo", "internal-tool"]
"writing" = ["blog", "newsletter", "essay"]
```

匹配规则:

- Tokens 是从**规范化**后的 project leaf 以 `-` 切分
  （worktree 后缀与路径前缀会先剥掉）。
- 第一个匹配胜出，大小写不敏感。
- 你自定义的规则永远优先于内置默认。

## 隐私

一切都在本机运行。ccstory 不会把你的对话数据送到任何地方。

- **数据来源**: `~/.claude/projects/**/*.jsonl` —— Claude Code 自己的记录。
- **叙事（默认）**: 从 Claude Code 自己写入 session jsonl 的 `aiTitle`
  记录读取。纯本机文件读取，无 LLM 调用。
- **叙事（`--rich`）**: 子进程调用你*本机的* `claude -p`，用的是你自己的
  Claude Code session / quota。不需 API key，ccstory 不收费。
- **缓存**: `~/.ccstory/cache.db`（sqlite，每个 session 一条摘要）。
- **报告**: `~/.ccstory/reports/recap-*.md`。

无 telemetry、无网络调用、无上传按钮。可在
[ccstory/session_summarizer.py](ccstory/session_summarizer.py) 自行验证。

## 时间如何度量

5 分钟 gap 启发式: 连续消息间隔 ≤ 5 分钟视为活跃；间隔超过视为"离开"。
不精确，但跨期比较足够稳定。Wall-clock 去重确保并行 session 不会重复计算。

## 跨期比较

当你跨多个时段运行 ccstory，markdown 报告用 **output tokens** 做比较，
而不是 `total_tokens`。为什么？在典型用法下，96% 以上的 `total_tokens` 是
`cache_read`，会随 turn 数量与 system prompt 长度膨胀 —— 不是工作量的
稳定信号。Output tokens 在月度间维持可比性。

## Roadmap

- [x] v0.1 —— 时间 + tokens + per-session 叙事 + 4 bucket 默认
- [x] v0.1.1 —— Per-bucket 颜色、日期范围标题、★ Top focus 高亮
- [x] v0.1.2 —— vs-previous-window 比较 + `ccstory trend` sparkline
- [x] v0.1.3 —— `ccstory init` 自动分类 + trend 的 quota burn %
- [x] v0.2 —— 默认读取 jsonl 中的 `aiTitle`（秒开，不调 `claude -p`）；
      `--rich` opt-in 获取 outcome-focused 叙事
- [ ] v0.3 —— Per-category 聚合叙事（2-3 句"整个 bucket 这期在做什么"）
- [ ] v0.3 —— Session 层级内容感知分类（用 `claude -p` 覆盖目录 bucket）
- [ ] v0.4 —— Claude Code plugin 形式（`/ccstory` slash command）
- [ ] v0.5 —— 可选的 PNG 卡片导出

## 许可

MIT —— 见 [LICENSE](LICENSE)。
