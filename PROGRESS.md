# Tickets Hunter — 個人開發進度

> 這個檔案是 Rebecca 個人的工作紀錄，用來提醒自己現在做到哪、下一步做什麼。
> 每次完成一個里程碑，記得回來更新這份檔案。

最後更新：2026-04-23

---

## 🎯 當前主線：NOL World（놀티켓）平台

### 為什麼選這個

NOL World 是韓國的票務平台（놀티켓 = Nol-Ticket），原專案沒有支援，是 Rebecca 主責開發的新模組。

### 📍 目前進度：Onestop 系統座位選取尚未完成

**已完成的 commit**：

| 日期 | Commit | 內容 |
|------|--------|------|
| 2026-04-17 01:02 | `e41b9c1` feat(nol) | NOL World GPO booking 完整自動化 — 第一個穩定版 |
| 2026-04-17 01:13 | `480c59d` perf(nol) | 速度 + CAPTCHA 準確率優化 |
| 2026-04-20 | `e5339ab` fix(nol) | 關鍵字解析修正 + 區域地圖載入 + 區域輪替修正 |
| 2026-04-22~23 | `d6591ba`~`305469c` fix(nol) | Onestop：日期選擇、驗證碼、dialog 修正、CDP 座位圓圈機制 |

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
| **區域輪替** | 依照 `area_keyword` 設定的順序循環找票；全部無票時呼叫 `fnSeatUpdate()` 刷新座位圖並從第一個區域重新開始，**不觸發 page reload** |

### 速度優化數據（v2 perf commit）

| 項目 | 改動前 | 改動後 |
|------|--------|--------|
| 非驗證碼等待 | 預設 | **0.1–0.2 秒**（加快日期 / 座位 / 票價流程） |
| 驗證碼提交後等待 | 1.5 秒 | **0.8 秒** |
| 驗證碼重試間隔 | 1.0 秒 | **0.3 秒** |
| 驗證碼圖片刷新 | 1.0 秒 | **0.5 秒** |

### Bug 修復紀錄

| 問題 | 根本原因 | 修正方式 |
|------|---------|---------|
| `priceReached` 誤判重試 | 只搜尋 main document，漏掉 iframe | 改為搜尋 iframes |
| 關鍵字引號殘留 | 直接用 `json.loads` 解析含引號字串 | 改用 `util.parse_keyword_string_to_array()` |
| Phase 3 座位圖載入時機 | 同時呼叫 `fnCheck()` + `fnSeatUpdate()` 衝突 | Phase 3 只呼叫 `fnSeatUpdate()`，`fnCheck()` 由驗證碼處理器統一呼叫 |
| 區域輪替卡在 001 | `<area>` alt 為空時 `getSeatCount()` 回傳 `-1`，`-1 != 0` 導致永遠點 001 | 改為點擊後確認 `ifrmSeatDetail` 內有無 `SelectSeat` 元素；無票時才遞增 `_gpo_kw_idx` |
| 全部區域無票後卡死 | 呼叫 `tab.reload()` 導致 `no_iframe` 空白頁無限迴圈 | 改用 `fnSeatUpdate()` 刷新座位圖（不 reload 頁面），重置 `_gpo_kw_idx = 0` |

---

## 🔧 關鍵實作細節（給未來的自己看）

### 區域輪替流程（`_nol_handle_gpo_booking`）

```
主迴圈每次進入 → 讀取 _gpo_kw_idx → 選出當前關鍵字
→ Phase 3 點擊對應 <area>
→ 等待 ifrmSeatDetail 載入（最多 7 秒）
→ 確認有無 SelectSeat 元素
  ├── 有 → 繼續訂票流程
  └── 無（無票）→ _gpo_kw_idx += 1
       ├── 還有下一個關鍵字 → 直接 return True（下次迴圈用新關鍵字）
       └── 全部輪完 → _gpo_kw_idx = 0 + fnSeatUpdate() 刷新 → return True
```

### iframe 存取路徑

```
main document
  └── #ifrmSeat（第一層 iframe）
        └── #ifrmSeatDetail（第二層 iframe）← 個別座位在這裡
```

存取方式：
```js
document.getElementById('ifrmSeat')
  .contentDocument
  .getElementById('ifrmSeatDetail')
```

### `fnSeatUpdate()` vs `tab.reload()`

- `fnSeatUpdate()`：只刷新座位圖 iframe，頁面狀態保留，速度快
- `tab.reload()`：整頁重載，會觸發 `no_iframe` 空白頁問題 → **禁止在 Phase 3 使用**

---

## 🔜 下一步（明天繼續）

### ⚠️ 最優先：Onestop 座位選取（未解決）

**系統**：`tickets.interpark.com/onestop/seat`（NOL World onestop 流程）

**已確認可運作的機制**：
- 日期選擇 ✅（date_keyword 精確比對）
- 驗證碼 ✅（ddddocr 雙模型共識）
- 等級面板開啟 + 點擊有色等級項目 ✅
- CDP 點擊座位地圖 → 1993 個 `circle.js-seat` 成功載入 ✅

**待解決的問題**：
- CDP 點擊地圖後，所有 1993 個圓圈都是 `disabled`（目前測試時不在開賣時間）
- 尚未驗證開賣時段可用座位能否正確被點選
- 點擊座位圓圈後「完成選擇」按鈕出現 → `_nol_click_next_step` Priority 1 應可處理

**關鍵技術細節**：
- 座位地圖：內嵌 SVG 的 `<circle class="js-seat">` 元素（不是外部 img）
- 可用座位：有 `js-seat` class、**沒有** `disabled` 字樣
- 座標轉換：`screenX = svgRect.left + cx * (svgRect.width / viewBox.width)`
- 確認按鈕：「完成選擇」（Priority 1 in `_nol_click_next_step`）

### A. GPO 實戰驗證

- [ ] 在真實搶票場景下測試區域輪替穩定性（已初步驗證可運作）
- [ ] 記錄實際搶票成功率 / 失敗原因

### B. 程式碼清理

- [ ] 移除 Phase 1.5 的診斷用 `seat_detail_dump` 程式碼（確認無 bug 後可移除）
- [ ] `src/platforms/nol.py` 已 5000+ 行，考慮模組拆分（CAPTCHA / 流程 / 工具）

### C. 功能強化

- [ ] Telegram Bot 通知整合（專案已有這個基礎設施，見 commit `e6a7ed8`）
- [ ] 失敗重試策略優化（例如：CAPTCHA 連續錯 N 次的 fallback）

---

## 🧠 接續工作時該怎麼做

### 開工標準流程

```bash
# 1. 進專案資料夾
cd ~/Projects/TICKET/tickets_hunter

# 2. 啟用虛擬環境
source venv/bin/activate

# 3. 開啟 Claude Code，接續上次對話
claude -c        # 接續最近對話
# 或
claude -r        # 列清單選一個
```

### ⚠️ 注意：venv 需要重建（若是第一次）

```bash
cd ~/Projects/TICKET/tickets_hunter
python3 -m venv venv
source venv/bin/activate
pip install -r requirement.txt
```

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
- 專案主 README：[`./README.md`](./README.md)
- 使用者手冊：[`./guide/README.md`](./guide/README.md)
- 更新紀錄：[`./CHANGELOG.md`](./CHANGELOG.md)
