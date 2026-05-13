# ccstory

[English](README.md) · [繁體中文](README.zh-TW.md) · [简体中文](README.zh-CN.md)

> **ccusage 告訴你帳單，ccstory 告訴你故事。**

一個 Claude Code 用量回顧工具，回答 token 計數器答不出來的問題:
**你這週到底在做什麼？**

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

（實際在終端機上每個 bucket 都有自己的顏色 —— investment 綠、coding 青、
writing 洋紅 ——「★ Top focus」會用最長 session 的敘事 highlight 最大那塊。）

完整的 markdown 報告會再深入一層 —— 每個 session 一句敘事。預設使用 Claude
Code 自己寫進 session jsonl 的 `aiTitle` 記錄；加上 `--rich` 則會改由你本機
的 `claude -p` 撰寫，用更 outcome-focused 的句型:

```
### investment

- 2026-05-09 10:59 · 28m · 76 msg — 評估 ONDS 財報前的加碼/減碼策略，
  並為 AI 半導體曝險定義評分指標。
- 2026-05-08 23:53 · 22m · 88 msg — 在大型科技財報潮後篩選 Q1 AI
  應用層贏家，鎖定下一波 setup。
```

## ccstory 提供了 ccusage 沒有的東西

| | [ccusage](https://github.com/ryoppippi/ccusage) | **ccstory** |
|---|---|---|
| 角色 | 帳單 | 故事 |
| Token / 成本精度 | ✅ daily/monthly/session/5h-block | (粗估) |
| Per-model 分解 | ✅ | ✅ |
| **活躍時數**（5 分鐘 gap 啟發式） | ❌ | ✅ |
| **活動分類**（不只是資料夾名稱） | ❌ | ✅ |
| **每個 session 一句敘事** | ❌ | ✅ 來自 Claude Code 自己寫的 `aiTitle`（零額外 API 呼叫） |
| **基於 output tokens 的跨期比較** | ❌（用 total_tokens） | ✅ |
| 即時 quota | ⚠️ 透過 `blocks` | ❌ |

**兩者互補，不是競品。** 一起用:

```bash
ccusage monthly        # 你花了多少錢
ccstory month          # 你花在什麼上
```

## 安裝

```bash
pipx install ccstory
# 或一次性使用：
pip install ccstory
```

需要 Python 3.11+。預設快速路徑不需要任何外部 CLI —— ccstory 直接讀取
Claude Code 已經寫進每個 session jsonl 的 `aiTitle` 記錄（也就是 CLI 上方那
行灰字標題）。如果要更 outcome-focused 的敘事（「重構 auth middleware…」），
加上 `--rich`，當沒有 `aiTitle` 時會 fallback 到本機 `claude -p`。

**新手嗎？** 先看 [5 分鐘教學](TUTORIAL.zh-TW.md) 再開始。

## 使用方式

```bash
ccstory init             # 一次性：掃描近期 sessions，建議 bucket
ccstory init --dry-run   # 預覽，不寫入 config

ccstory                  # 本月迄今（預設）
ccstory week             # 過去 7 天
ccstory 2026-04          # 任意指定月份
ccstory all              # 整個歷史

ccstory trend            # 過去 8 週 sparkline
ccstory trend --weeks 12 # 自訂範圍
ccstory trend --months 6 # 以日曆月為單位

ccstory --rich           # 用本機 `claude -p` 寫 outcome-focused 敘事
ccstory --no-summary     # 完全跳過 per-session 敘事
ccstory --no-compare     # 跳過 vs-previous 比較區塊
```

**建議的第一次執行**: `ccstory init` 會掃描最近 30 天的 sessions，透過一次
`claude -p` 呼叫（約 15 秒）為每個 project 建議分類 bucket。它會寫一份起手式
`~/.ccstory/config.toml`，你之後可以隨時編輯。

`ccstory week` / `ccstory month` 會自動附上 **vs-previous-window** 比較
（per-bucket ▲/▼ delta）。`ccstory trend` 顯示 per-bucket sparkline，讓你一
眼看到 N 週/月的趨勢形狀:

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

`burn %` 那行顯示 API-equivalent 成本佔你按比例分攤的月 quota 百分比 ——
在 `~/.ccstory/config.toml` 設定 `monthly_quota_usd`
（預設 $3,500 ≈ Max 20x 方案）。設為 `0` 可隱藏這行。

第一次執行會 scaffold `~/.ccstory/config.toml`，並顯示你的 projects 被
如何分類。

## 分類

四個預設 bucket，與 project 資料夾名稱配對:

| Bucket | 關鍵字（範例） |
|---|---|
| `investment` | investment, stock, portfolio, trading, ticker, etf, finance |
| `writing` | blog, newsletter, post, docs, content, article |
| `coding` | app, sdk, cli, plugin, mcp, server, frontend, backend, lib, … |
| `other` | playground, scratch, sandbox, experiment |

對不上的 project 預設掉到 `coding` —— 依據 2026 Pragmatic Engineer
開發者調查，~46% 的 Claude Code 用途是軟體開發。

在 `~/.ccstory/config.toml` 自訂:

```toml
default_bucket = "coding"

[categories]
"work"    = ["company-repo", "internal-tool"]
"writing" = ["blog", "newsletter", "essay"]
```

配對規則:

- Tokens 是從**正規化**後的 project leaf 上以 `-` 切分
  （worktree 後綴與路徑前綴會先剝掉）。
- 第一個匹配勝出，大小寫不敏感。
- 你自訂的規則永遠優先於內建預設。

## 隱私

一切都在本機執行。ccstory 不會把你的對話資料送到任何地方。

- **資料來源**: `~/.claude/projects/**/*.jsonl` —— Claude Code 自己的紀錄。
- **敘事（預設）**: 從 Claude Code 自己寫入 session jsonl 的 `aiTitle`
  記錄讀取。純本機檔案讀取，無 LLM 呼叫。
- **敘事（`--rich`）**: 子程序呼叫你*本機的* `claude -p`，使用你自己的
  Claude Code session / quota。不需 API key，ccstory 不收費。
- **快取**: `~/.ccstory/cache.db`（sqlite，每個 session 一筆摘要）。
- **報告**: `~/.ccstory/reports/recap-*.md`。

無 telemetry、無網路呼叫、無上傳按鈕。可在
[ccstory/session_summarizer.py](ccstory/session_summarizer.py) 自行驗證。

## 時間如何測量

5 分鐘 gap 啟發式: 連續訊息間隔 ≤ 5 分鐘視為活躍；間隔超過視為「離開」。
不精確，但跨期比較足夠穩定。Wall-clock 去重確保平行 session 不會雙計。

## 跨期比較

當你跨多個期間執行 ccstory，markdown 報告用 **output tokens** 做比較，
而非 `total_tokens`。為什麼？在典型用法下，96% 以上的 `total_tokens` 是
`cache_read`，會隨 turn 數量與 system prompt 長度膨脹 —— 不是工作量的
穩定訊號。Output tokens 在月度間維持可比性。

## Roadmap

- [x] v0.1 —— 時間 + tokens + per-session 敘事 + 4 bucket 預設
- [x] v0.1.1 —— Per-bucket 顏色、日期區間標題、★ Top focus highlight
- [x] v0.1.2 —— vs-previous-window 比較 + `ccstory trend` sparkline
- [x] v0.1.3 —— `ccstory init` 自動分類 + trend 的 quota burn %
- [x] v0.2 —— 預設讀取 jsonl 中的 `aiTitle`（秒開，不呼 `claude -p`）；
      `--rich` opt-in 取得 outcome-focused 敘事
- [ ] v0.3 —— Per-category 聚合敘事（2-3 句「整個 bucket 這期在做什麼」）
- [ ] v0.3 —— Session 層級內容感知分類（用 `claude -p` 覆蓋資料夾 bucket）
- [ ] v0.4 —— Claude Code plugin 形式（`/ccstory` slash command）
- [ ] v0.5 —— 選用 PNG 卡片匯出

## 授權

MIT —— 見 [LICENSE](LICENSE)。
