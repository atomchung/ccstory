---
description: 把當前雲端對話整理成 canonical frontmatter 任務記錄，push 進 private inbox repo（session-records），供本地 reconcile 回流 personal_os / session-board。用戶說 /record、記錄這次對話、存成任務時用。
argument-hint: "[slug，如 onds-deep-dive] 或留空自動從對話推斷"
---

# /record（雲端版 · 寫入 private inbox）

把當前對話整理成結構化任務記錄，push 進 inbox repo（預設 `atomchung/session-records`）的 `records/{slug}.md`。

> 為什麼寫 inbox 而非當前 repo / personal_os：雲端碰不到本機 personal_os；寫當前 repo 會洩漏 public、污染 git 史。改 push 一個專用 private inbox 最安全。本地 importer 會把 inbox 併進 personal_os/tasks/，/session-board 才看得到。

## ⚠️ 兩個核心
1. frontmatter 必須是 canonical YAML（下方 schema）。/session-board parser 只認 frontmatter，舊式 `> Status:` 引言區塊會被靜默漏掉。
2. status 語意精確：`paused` 只給「等外部、自己推不動」；卡點欄位只能叫 `blocked_by`。

## 步驟
### 1. 決定 slug
有給 $ARGUMENTS 就用它（kebab-case）；否則從對話主題推斷。

### 2. 掃描對話整理
做了什麼 / 關鍵發現（2–5）/ 產出檔案 / 待辦 / 下一步。去重、去 noise。

### 3. 組 record（canonical frontmatter）
status 決策樹：① 用戶說做完 → `done` ② 等外部事件（回信/審核/財報/他人交付）→ `paused` + `blocked_by` 必填 ③ 其他 → `in_progress`。
內容：`# Title` / `## Context` / `## Sessions` → `### {date} Session`（關鍵發現 / 檔案清單 / 待辦 / 後續行動）。

### 4. 立即 push 到 inbox（不要等 session 結束——雲端 crash 會丟失）
```bash
INBOX="${RECORD_INBOX_REPO:-atomchung/session-records}"
cd /tmp && rm -rf inbox && git clone --depth 1 "https://github.com/$INBOX" inbox && cd inbox && mkdir -p records
# 寫 records/<slug>.md；若已存在 → 在其 ## Sessions 底下追加新 session 區塊，不覆蓋
git add records/<slug>.md && git commit -m "record: <slug>"
git pull --rebase && git push
```
若 git push 認證失敗 → 改用 GitHub Contents API（需環境變數 `GH_TOKEN`，一把只授權 inbox repo、`contents:write` 的 fine-grained PAT）：先 GET 取舊 sha（檔已存在才有），再 PUT base64 內容到 `repos/$INBOX/contents/records/<slug>.md`。

### 5. 回報
inbox 路徑（`$INBOX/records/<slug>.md`）/ status / next_action，提醒「本地 importer 會把它併進 personal_os，/session-board 就看得到」。

## frontmatter schema（與 session-board 共用，逐字一致）
| 欄位 | 必填 | 值 |
|---|---|---|
| slug | ✅ | 同檔名 |
| status | ✅ | open / in_progress / paused / done / archived |
| created / last_session | ✅ | YYYY-MM-DD |
| next_action | 推薦 | ≤30 字 |
| blocked_by | paused 必填 | 卡住原因 |
| origin | ✅ | cloud |
| source_repo | ✅ | 當前 repo 名 |
| synced | ✅ | false → 本地 reconcile 後改 true |

## 排除：week-fit-* / week-review-* / recap-* 不套（那些是週報，走對應 skill）。
