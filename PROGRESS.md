# Tickets Hunter — 個人開發進度

> 這個檔案是 Rebecca 個人的工作紀錄，用來提醒自己現在做到哪、下一步做什麼。
> 每次完成一個里程碑，記得回來更新這份檔案。

最後更新：2026-04-17

---

## 🎯 當前主線：NOL World（놀티켓）平台

### 為什麼選這個

NOL World 是韓國的票務平台（놀티켓 = Nol-Ticket），原專案沒有支援，是 Rebecca 主責開發的新模組。

### 📍 目前進度：穩定版 + 速度/準確率優化

**已完成的兩個 commit**：

| 日期 | Commit | 內容 |
|------|--------|------|
| 2026-04-17 01:02 | `e41b9c1` feat(nol) | **NOL World GPO booking 完整自動化** — 第一個穩定版 |
| 2026-04-17 01:13 | `480c59d` perf(nol) | **速度 + CAPTCHA 準確率優化** |

---

## ✅ 已實作功能

### 完整 GPO 訂票流程

**流程**：選日期 → 驗證碼 → 選位圖 → 票價/折扣 → 配送方式 → 付款

### 核心技術點

| 功能 | 實作細節 |
|------|---------|
| **CAPTCHA 辨識** | `ddddocr` 雙模型（default + beta）共識評分；快取 OCR 實例減少初始化時間 |
| **CAPTCHA 預處理** | 對角噪音線條移除（diagonal noise line removal） |
| **選位** | 通用區域 / 個別座位點擊，支援可設定的區域關鍵字 |
| **票價 / 折扣** | 自動數量選擇，iframe-aware 偵測 |
| **Cloudflare Turnstile** | 商品頁使用 CDP mouse events 處理 |
| **步驟偵測** | 所有步驟搜尋 main document + 巢狀 iframes |

### 速度優化數據（v2 perf commit）

| 項目 | 改動前 | 改動後 |
|------|--------|--------|
| 非驗證碼等待 | 預設 | **0.1–0.2 秒**（加快日期 / 座位 / 票價流程） |
| 驗證碼提交後等待 | 1.5 秒 | **0.8 秒** |
| 驗證碼重試間隔 | 1.0 秒 | **0.3 秒** |
| 驗證碼圖片刷新 | 1.0 秒 | **0.5 秒** |

### Bug 修復

- ✅ `priceReached` 偵測修正為搜尋 iframes（防止誤判重試）

---

## 🔜 下一步可能的方向

*（以下是「若要繼續深入」的建議方向，還沒開始做）*

### A. 實戰驗證（優先）

- [ ] 在真實搶票場景下測試穩定性
- [ ] 記錄實際搶票成功率 / 失敗原因
- [ ] 收集 edge cases（特殊活動頁面結構、異常流程）

### B. 功能強化

- [ ] 支援更多 NOL 商品類型（目前聚焦 GPO，可能還有 general ticket、package 等）
- [ ] Telegram Bot 通知整合（專案已有這個基礎設施，見 commit `e6a7ed8`）
- [ ] 失敗重試策略優化（例如：CAPTCHA 連續錯 N 次的 fallback）

### C. 程式碼品質

- [ ] `src/platforms/nol.py` 已 5000+ 行，考慮模組拆分（CAPTCHA / 流程 / 工具）
- [ ] 加入更詳細的 `DebugLogger` 記錄（參考 `util.py` 中現有機制）
- [ ] 錯誤訊息本地化（中文 / 韓文）

### D. 文件補完

- [ ] `docs/` 下補一份 NOL 平台專屬說明
- [ ] `guide/` 下加入 NOL 使用教學（區域關鍵字設定、票種選擇等）

---

## 🧠 接續工作時該怎麼做

### 開工標準流程

```bash
# 1. 進專案資料夾
cd ~/Projects/TICKET/tickets_hunter

# 2. 啟用虛擬環境（第一次需先建）
source venv/bin/activate

# 3. 開啟 Claude Code，接續上次對話
claude -c        # 接續最近對話
# 或
claude -r        # 列清單選一個
```

### ⚠️ 注意：venv 需要重建

因為專案資料夾從 `~/Desktop/ticket/tickets_hunter` 搬到 `~/Projects/TICKET/tickets_hunter`，
原本的 venv 寫死了舊路徑、**已刪除**。第一次來要重建：

```bash
cd ~/Projects/TICKET/tickets_hunter
python3 -m venv venv                    # 建新 venv
source venv/bin/activate                 # 啟用
pip install -r requirement.txt           # 裝依賴（約 2–5 分鐘）
```

之後就只要 `source venv/bin/activate` 即可。

---

## 📚 相關對話紀錄

- 舊對話存於 `~/.claude/projects/-Users-rebeccaying-Projects-TICKET-tickets_hunter/`
- 用 `claude -r` 可接續（在這個資料夾內執行才看得到）
- **保存期 30 天**，超過自動清除 — 重要決策要及早寫進 CLAUDE.md 或 PROGRESS.md

---

## 📝 更新這份檔案的時機

- ✅ 完成一個功能或 bug 修復 → 勾選 TODO、新增到「已實作」
- ✅ 決定了新方向 → 更新「下一步可能的方向」
- ✅ 發現重要技術細節 → 寫進 CLAUDE.md（給 Claude）或這裡（給自己）
- ❌ **不需要**：每次關機前強制更新（日常進度靠對話紀錄 `/resume` 就好）

---

## 🔗 快速連結

- 原始 GitHub：https://github.com/bouob/tickets_hunter
- 自己的 fork：（確認一下有沒有自己的 remote，目前只看到 origin 是 bouob 的）
- 專案主 README：[`./README.md`](./README.md)
- 使用者手冊：[`./guide/README.md`](./guide/README.md)
- 更新紀錄：[`./CHANGELOG.md`](./CHANGELOG.md)
