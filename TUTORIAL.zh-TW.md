# ccstory 新手教學

[English](TUTORIAL.md) · [繁體中文](TUTORIAL.zh-TW.md) · [简体中文](TUTORIAL.zh-CN.md)

從安裝到看見第一份 recap，5 分鐘走完。

> **太長不看版**: `pipx install ccstory && ccstory init && ccstory week`

---

## 開始之前

- Python 3.11+
- 你至少用過幾次 Claude Code（CLI）—— ccstory 讀取
  `~/.claude/projects/**/*.jsonl`，這是 Claude Code 自動建立的紀錄檔。

就這樣。不用 API key，預設模式不用 `claude` CLI（除非之後你要用 `--rich`）。

---

## Step 1 —— 安裝

```bash
pipx install ccstory
```

（如果你不用 pipx，`pip install ccstory` 也行）

確認:

```bash
ccstory --version
# → ccstory 0.2.0
```

---

## Step 2 —— 把你的 projects 分類

```bash
ccstory init
```

它會做的事:

1. 掃描 `~/.claude/projects/` 過去 30 天的 sessions。
2. 列出所有出現過的 project 資料夾（例如 `my-portfolio-app`、
   `ondc-research`、`personal-blog`）。
3. 發一次 `claude -p` 請求，請 Claude 為每個 project 建議分類 bucket ——
   `coding`、`writing`、`investment` 或 `other`。
4. 把提案寫入 `~/.ccstory/config.toml`。你隨時可以編輯這個檔案。

如果你想先預覽，加上 `--dry-run`:

```bash
ccstory init --dry-run
```

跳過確認:

```bash
ccstory init -y
```

> 沒裝 `claude` CLI？`init` 會 fallback 到對資料夾名稱做關鍵字配對。
> 較不準但仍可用 —— 之後手動調 config 即可。

---

## Step 3 —— 你的第一份 recap

```bash
ccstory week
```

你會看到類似這樣的 panel:

```
╭──── Claude Code Recap · May 5 – 12 ────╮
│  ★ Top focus  coding  10.9h  (53%)     │
│    ↳ 重構 auth middleware…              │
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

怎麼讀:

| 欄位 | 意義 |
|---|---|
| **★ Top focus** | 最大的 bucket，加上其中最長 session 的一句敘事 |
| **Active** | 連續訊息間隔 ≤ 5 分鐘的累計時數（5 分鐘 gap 啟發式）。間隔超過就視為「離開」 |
| **Sessions** | 該時段內 session jsonl 檔的數量（只算有實際互動的，自動排程任務不算） |
| **Output / Cost** | Output tokens 與 API-equivalent 成本。*要精準帳單請搭配 [ccusage](https://github.com/ryoppippi/ccusage)。* |
| **Time by category** | 每個 bucket 的時數。Wall-clock 去重確保平行 sessions 不會雙計 |
| **vs previous window** | 每個 bucket vs. 前一個同長度時段的 ▲/▼ delta。跨期比較用 **output tokens**（不是 `total_tokens`） |

完整的 markdown 報告在 `~/.ccstory/reports/recap-2026-W19.md` —— 包含每個
session 的敘事行，貼進週報文件正合適。

---

## Step 4 —— 自訂分類

打開 `~/.ccstory/config.toml`:

```toml
default_bucket = "coding"
monthly_quota_usd = 3500    # Max 20× 方案，用於計算 "burn %"

[categories]
"work"      = ["company-repo", "internal-tool", "infra"]
"writing"   = ["blog", "newsletter", "essay"]
"learning"  = ["leetcode", "tutorial", "scratch"]
```

規則:

- 分類是對 project 資料夾 leaf 以 `-` 切分後的 tokens 配對
  （worktree 後綴與路徑前綴會先剝掉）。
- 第一個匹配勝出，大小寫不敏感。
- 你的規則**永遠**優先於內建預設。
- 對不上的 project 掉到 `default_bucket`（沒設則掉到 `coding`）。

重跑 `ccstory week` 就能看到新分類。分類在報告時計算 —— 不用 rebuild。

---

## Step 5 —— 看更長的弧線

```bash
ccstory trend           # 過去 8 週
ccstory trend --weeks 12
ccstory trend --months 6
```

Sparkline 顯示每個 bucket 隨時間的形狀:

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

`burn %` 是你的 API-equivalent 成本佔按比例分攤的月 quota 百分比。
在 config 設 `monthly_quota_usd = 0` 可隱藏此行。

---

## 常用 flags

```bash
ccstory                  # 本月迄今
ccstory month            # 同上
ccstory week             # 過去 7 天 + vs previous
ccstory 2026-04          # 特定月份
ccstory all              # 整個歷史

ccstory --rich           # 用 `claude -p` 產 outcome-focused 敘事
                         # （較慢；會花掉真實的 Claude Code turn）
ccstory --no-summary     # 完全跳過 per-session 敘事
ccstory --no-compare     # 跳過 vs-previous 區塊
```

---

## FAQ

**Q. ccstory 顯示「No engaged sessions in this window」—— 但我有用 Claude Code 啊。**

Engagement 過濾要求 ≥ 2 條真實使用者訊息，或 1 條訊息 + ≥ 60 秒的活動。
非常短或自動觸發的 session 會被排除，免得污染報告。如果你覺得有 session
被誤排除，歡迎開 issue。

**Q. 為什麼有些 session 敘事很短 / 很籠統？**

預設敘事來源是 Claude Code 寫入每個 session jsonl 的 `aiTitle`。剛開始的
session 可能還沒有 —— 那些會 fallback 到第一則使用者訊息。要更豐富、
outcome-focused 的句型，加上 `--rich`。

**Q. `--rich` 會花真錢 / 真 quota 嗎？**

會 —— `--rich` 會呼叫你本機的 `claude -p`，用的是你的 Claude Code session。
每個沒有快取敘事的 session 會花一個短 turn。50 個 session 的一週大約是
背景跑 5–10 分鐘。預設（不加 flag）是免費且秒開。

**Q. 可以改 5 分鐘 gap 門檻嗎？**

目前沒有 flag —— 它是 `ccstory/time_tracking.py` 裡的 `GAP_CAP_SEC`。如果
你有調整需求，開 issue 告訴我們。

**Q. ccstory 會上傳任何東西嗎？**

不會。零網路呼叫。可在
[ccstory/session_summarizer.py](ccstory/session_summarizer.py) 自行驗證 ——
唯一的子程序是你本機的 `claude -p`（而且只有 `--rich` 才會用到）。

**Q. 這跟 `ccusage` 差在哪？**

[ccusage](https://github.com/ryoppippi/ccusage) 是**成本 / token 精度**的
標準工具 —— 跟 ccstory 一起用。README 表格有完整比較。簡單版: ccusage
答「花了多少」，ccstory 答「花在什麼上」。

---

## 接下來

- **Issues / 想法**: <https://github.com/atomchung/ccstory/issues>
- **Roadmap**: 見 README
- **搭配 ccusage**: `ccusage monthly && ccstory month`
