# ccstory 新手教程

[English](TUTORIAL.md) · [繁體中文](TUTORIAL.zh-TW.md) · [简体中文](TUTORIAL.zh-CN.md)

从安装到看到第一份 recap，5 分钟走完。

> **太长不看版**: `pipx install ccstory && ccstory init && ccstory week`

---

## 开始之前

- Python 3.11+
- 你至少用过几次 Claude Code（CLI）—— ccstory 读取
  `~/.claude/projects/**/*.jsonl`，这是 Claude Code 自动创建的记录文件。

就这些。不用 API key，默认模式不用 `claude` CLI（除非之后你要用 `--rich`）。

---

## Step 1 —— 安装

```bash
pipx install ccstory
```

（如果你不用 pipx，`pip install ccstory` 也行）

确认:

```bash
ccstory --version
# → ccstory 0.2.0
```

---

## Step 2 —— 给你的 projects 分类

```bash
ccstory init
```

它会做的事:

1. 扫描 `~/.claude/projects/` 过去 30 天的 sessions。
2. 列出所有出现过的 project 目录（例如 `my-portfolio-app`、
   `ondc-research`、`personal-blog`）。
3. 发一次 `claude -p` 请求，请 Claude 为每个 project 建议分类 bucket ——
   `coding`、`writing`、`investment` 或 `other`。
4. 把提案写入 `~/.ccstory/config.toml`。这个文件你随时可以编辑。

如果你想先预览，加上 `--dry-run`:

```bash
ccstory init --dry-run
```

跳过确认:

```bash
ccstory init -y
```

> 没装 `claude` CLI？`init` 会 fallback 到对目录名做关键字匹配。
> 较不准但仍可用 —— 之后手动调 config 即可。

---

## Step 3 —— 你的第一份 recap

```bash
ccstory week
```

你会看到类似这样的 panel:

```
╭──── Claude Code Recap · May 5 – 12 ────╮
│  ★ Top focus  coding  10.9h  (53%)     │
│    ↳ 重构 auth middleware…              │
│                                        │
│  Active  20.6h  Sessions  74           │
│  Output  2.92M  Cost      $1,608       │
│                                        │
│  Time by category                      │
│  coding      ████████░░░░░░  10.9h     │
│  investment  █████░░░░░░░░░   9.6h     │
│  writing     ░░░░░░░░░░░░░░   0.1h     │
│                                        │
│  vs previous window (2026-W18)         │
│  total       20.6h  ▲ +47%             │
╰────────────────── ccstory ─────────────╯
```

怎么读:

| 字段 | 含义 |
|---|---|
| **★ Top focus** | 最大的 bucket，加上其中最长 session 的一句叙事 |
| **Active** | 连续消息间隔 ≤ 5 分钟的累计时数（5 分钟 gap 启发式）。间隔超过就视为"离开" |
| **Sessions** | 该时段内 session jsonl 文件的数量（只算有实际交互的，自动调度任务不算） |
| **Output / Cost** | Output tokens 与 API-equivalent 成本。*要精确账单请搭配 [ccusage](https://github.com/ryoppippi/ccusage)。* |
| **Time by category** | 每个 bucket 的时数。Wall-clock 去重确保并行 sessions 不会重复计算 |
| **vs previous window** | 每个 bucket vs. 上一个同长度时段的 ▲/▼ delta。跨期比较用 **output tokens**（不是 `total_tokens`） |

完整的 markdown 报告在 `~/.ccstory/reports/recap-2026-W19.md` —— 包含每个
session 的叙事行，贴进周报文档正合适。

---

## Step 4 —— 自定义分类

打开 `~/.ccstory/config.toml`:

```toml
default_bucket = "coding"
monthly_quota_usd = 3500    # Max 20× 方案，用于计算 "burn %"

[categories]
"work"      = ["company-repo", "internal-tool", "infra"]
"writing"   = ["blog", "newsletter", "essay"]
"learning"  = ["leetcode", "tutorial", "scratch"]
```

规则:

- 分类是对 project 目录 leaf 以 `-` 切分后的 tokens 匹配
  （worktree 后缀与路径前缀会先剥掉）。
- 第一个匹配胜出，大小写不敏感。
- 你的规则**永远**优先于内置默认。
- 对不上的 project 落到 `default_bucket`（没设则落到 `coding`）。

重跑 `ccstory week` 就能看到新分类。分类在报告时计算 —— 无需 rebuild。

---

## Step 5 —— 看更长的弧线

```bash
ccstory trend           # 过去 8 周
ccstory trend --weeks 12
ccstory trend --months 6
```

Sparkline 显示每个 bucket 随时间的形状:

```
Hours by bucket
total          ▁▄▆▇▃█    16.5h   avg 9.0h   ▲ +183%
investment     ▁▃▅█▆█     6.3h   avg 4.0h   ▲ +29%
coding         ▁▂▃▄▁█    10.2h   avg 3.3h   ▲ +1148%

Overall
output         ▁▁▁▄▁█     3.0M   avg 0.8M   ▲ +2460%
cost           ▁▁▂▃▁█   $1,643   avg $463   ▲ +1877%
burn %         ▁▁▂▃▁█     201%   avg 57%    ▲ +1877%
```

`burn %` 是你的 API-equivalent 成本占按比例摊分月 quota 的百分比。
在 config 设 `monthly_quota_usd = 0` 可隐藏此行。

---

## 常用 flags

```bash
ccstory                  # 本月至今
ccstory month            # 同上
ccstory week             # 过去 7 天 + vs previous
ccstory 2026-04          # 特定月份
ccstory all              # 整个历史

ccstory --rich           # 用 `claude -p` 生成 outcome-focused 叙事
                         # （较慢；会花掉真实的 Claude Code turn）
ccstory --no-summary     # 完全跳过 per-session 叙事
ccstory --no-compare     # 跳过 vs-previous 区块
```

---

## FAQ

**Q. ccstory 显示「No engaged sessions in this window」—— 但我有用 Claude Code 啊。**

Engagement 过滤要求 ≥ 2 条真实用户消息，或 1 条消息 + ≥ 60 秒的活动。
非常短或自动触发的 session 会被排除，免得污染报告。如果你觉得有 session
被误排除，欢迎开 issue。

**Q. 为什么有些 session 叙事很短 / 很笼统？**

默认叙事来源是 Claude Code 写入每个 session jsonl 的 `aiTitle`。刚开始的
session 可能还没有 —— 那些会 fallback 到第一条用户消息。要更丰富、
outcome-focused 的句型，加上 `--rich`。

**Q. `--rich` 会花真钱 / 真 quota 吗？**

会 —— `--rich` 会调用你本机的 `claude -p`，用的是你的 Claude Code session。
每个没有缓存叙事的 session 会花一个短 turn。50 个 session 的一周大约是
后台跑 5–10 分钟。默认（不加 flag）是免费且秒开。

**Q. 可以改 5 分钟 gap 阈值吗？**

目前没有 flag —— 它是 `ccstory/time_tracking.py` 里的 `GAP_CAP_SEC`。
如果你有调整需求，开 issue 告诉我们。

**Q. ccstory 会上传任何东西吗？**

不会。零网络调用。可在
[ccstory/session_summarizer.py](ccstory/session_summarizer.py) 自行验证 ——
唯一的子进程是你本机的 `claude -p`（而且只有 `--rich` 才会用到）。

**Q. 这跟 `ccusage` 差在哪？**

[ccusage](https://github.com/ryoppippi/ccusage) 是**成本 / token 精度**的
标准工具 —— 跟 ccstory 一起用。README 表格有完整比较。简单版: ccusage
答"花了多少"，ccstory 答"花在什么上"。

---

## 接下来

- **Issues / 想法**: <https://github.com/atomchung/ccstory/issues>
- **Roadmap**: 见 README
- **搭配 ccusage**: `ccusage monthly && ccstory month`
