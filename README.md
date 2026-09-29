# file_finder_tui

極簡 Windows 檔名搜尋與資料夾瀏覽工具。使用 Python 3.11 以上與 Textual，執行時只有 Textual 一項直接第三方依賴。搜尋固定三個根目錄，不讀取檔案內容，不建立資料庫或永久索引。收藏只寫入專案的 `favorites.json`。

## 實作方式與範圍

主程式集中於 `main.py`，設定、名稱搜尋、最愛保存、瀏覽歷史及 TUI 以小型函式／類別分工，避免一次性個人工具擴張成框架。沒有新增檔案預覽、複製、刪除、移動或重新命名功能。

介面預設使用低調霧藍主題：深黑背景、柔和藍色選取列與清楚的鍵盤焦點邊框。搜尋欄與清單使用黑灰底，首頁、搜尋、資料夾瀏覽和確認／錯誤對話框使用一致配色。字型與字級由 Windows Terminal 控制；顏色需終端支援真彩色才能完整呈現。

- 搜尋文字停止變更 250ms 後，使用 Textual 執行緒 Worker 搭配 `os.scandir()` 遞迴搜尋。
- 搜尋期間約每 200ms 回報已掃描項目、目前找到至少幾筆、最近回報的掃描資料夾、讀取錯誤與排除連結數；底部持續更新耗時。輸入新條件或返回首頁後，舊進度不能覆蓋畫面。
- 文字一改變，立即設定上一個取消旗標並更新請求序號；只有最新序號可更新畫面。清除、返回與退出也會使舊結果失效。
- 比對名稱時使用 `casefold()`，先連續子字串，再依序字元。例如 `rpt` 可命中 `report.xlsx`；不讀取其內容。
- 排序依序為連續子字串、資料夾、名稱長度、名稱。相同名稱以完整路徑穩定排序。
- 最多保留排序最佳的 200 筆搜尋結果，但在掃描完成後顯示實際命中總數，並非找到 200 筆就停止。
- 達上限、取消或出現讀取錯誤時，標示「結果不完整」與「已找到至少 N 筆」。所有錯誤保留在本次記憶體報告中，底部顯示錯誤數量與第一項資訊；按 `Ctrl+E` 可捲動查看目前畫面的全部讀取錯誤。
- 資料夾即時讀取直接子項目，不遞迴展開；瀏覽清單不套用 200 筆搜尋結果上限。
- 最愛以同目錄暫存檔、`flush`、`fsync`、`os.replace` 原子保存；寫入失敗時保留原 JSON 與記憶體收藏。

## 已知限制與風險

- 每次新搜尋都重新掃描。大型目錄／慢速網路可能較久；預設最多檢查 250,000 個子項目，達上限會明確標示不完整。三個設定根目錄本身也參與名稱比對。
- 搜尋前不知道全部項目數量，因此進度不顯示百分比或預估剩餘時間；掃描上限不是檔案總數。搜尋中顯示的筆數標示「尚未完成」，結束後才套用完整／不完整結果的說明。短時間完成的搜尋可能直接顯示最終結果。
- 為避免跨出掃描範圍與循環，搜尋與瀏覽都排除符號連結及 Windows reparse point，包含 junction、部分雲端佔位檔。狀態會顯示排除數量；「完整」指依此排除規則完成的掃描。
- Windows 路徑比較不分大小寫，並正規化 `.`、`..`、尾端斜線與分隔符；不同磁碟映射／UNC 別名並不會自動合併。此工具不適用刻意啟用大小寫敏感的特殊目錄。
- Python 無法強制中斷正在等待 Windows 回應的單次檔案系統呼叫。UI 可以繼續操作，舊結果會被丟棄；若網路磁碟呼叫卡住，背景執行緒及程式結束可能仍需等待 Windows 逾時。
- 等待檔案系統回應時，耗時仍會增加，但項目計數及最近回報的資料夾可能暫時停住；這不代表該目錄已完成掃描。讀取錯誤的完整清單在本次搜尋結束後可用 `Ctrl+E` 查看。
- 資料夾包含極大量直接子項目時，清單排序與 UI 更新可能較慢；未加入額外分頁功能。
- 執行檔、腳本、安裝程式及捷徑會要求確認，預設焦點在「取消」。常見確認副檔名包含 `.exe`、`.bat`、`.cmd`、`.ps1`、`.vbs`、`.js`、`.msi`、`.reg`、`.lnk`、`.url`、`.py` 等。副檔名清單不是完整的程式安全沙箱。
- 一般檔案透過 Windows 預設關聯程式開啟；後續外部程式的行為由該程式負責。自動測試使用開啟函式替身，沒有執行真實腳本或啟動真實關聯程式。
- `favorites.json` 損壞／無法讀取時會停用收藏寫入，避免覆蓋舊資料；修正檔案後請重啟。不支援同時執行多個實例修改收藏，以免最後一次保存覆蓋其他實例的變更。
- 映射磁碟必須在目前使用者的 PowerShell 工作階段已連線。不要求管理員權限；實際 ACL 拒絕與網路斷線以可重現的例外注入測試驗證，尚未實際斷開使用者的網路磁碟。

## 安裝與設定

前置條件：Windows 10／11、PowerShell、Python 3.11 以上；建議使用 Windows Terminal。無需 Docker、管理員權限或啟用 PowerShell 執行腳本權限。

從 GitHub 下載專案後，在專案目錄建立虛擬環境並安裝 Textual：

```powershell
Set-Location 'C:\path\to\file-finder-tui'
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .\config.json)) {
    Copy-Item .\config.example.json .\config.json
}
notepad .\config.json
```

如果 `py -3.11` 無法執行，請將 `py -3.11` 換成已安裝的 Python 3.11+ 執行檔完整路徑。直接呼叫虛擬環境中的 Python 即可，無需啟用 `Activate.ps1`。

Git 僅追蹤設定範本 `config.example.json`。實際使用的 `config.json` 與個人收藏 `favorites.json` 已加入 `.gitignore`，本次遷移保留本機原有內容並取消 Git 追蹤。首次執行請以上述指令建立設定；缺少設定時程式會顯示建立提示。首次收藏時會建立 `favorites.json`。

輸入設定預設為程式旁的 `config.json`，首頁會顯示實際讀取的設定檔完整路徑。`roots` 必須剛好三筆，且 `path` 必須為絕對路徑。JSON 中反斜線需寫成 `\\`：

```json
{
  "roots": [
    {"name": "Project A", "path": "C:\\Projects\\Alpha"},
    {"name": "Project B", "path": "C:\\Projects\\Beta"},
    {"name": "Project C", "path": "U:\\SQL"}
  ],
  "max_scan_entries": 250000
}
```

範例中的路徑是示範值。執行前請改為你要搜尋的三個實際資料夾；例如可以使用 `U:\SQL` 或 UNC 路徑。無效路徑在首頁顯示「失效／無法讀取」，其餘根目錄仍可搜尋；設定格式錯誤會顯示提示並停用搜尋，修正後重啟。

最愛保存為絕對路徑的 JSON 陣列：

```json
[
  "U:\\SQL",
  "D:\\Shared"
]
```

最愛可以指向三個根目錄以外的資料夾供直接瀏覽，但不會加入搜尋範圍。失效收藏保留紀錄，可在首頁選取後按 `F` 明確移除。

## 執行與快捷鍵

```powershell
Set-Location 'C:\path\to\file-finder-tui'
.\.venv\Scripts\python.exe .\main.py
```

首頁在搜尋欄位為空時顯示最愛與三個根目錄，根目錄項目會顯示完整設定路徑。搜尋時隱藏獨立的最愛區域，已收藏的資料夾仍會顯示 `★`。搜尋與資料夾瀏覽結果會顯示完整父目錄路徑，以區分同名項目。

| 操作 | 行為 |
| --- | --- |
| `↑`／`↓` | 移動清單選取；搜尋欄位取得焦點時，切換到清單首／末項 |
| `Tab`／`Shift+Tab` | 切換搜尋欄位與清單焦點 |
| `Enter` | 清單中開啟檔案或進入資料夾 |
| `F` | 清單取得焦點時加入／取消資料夾最愛；輸入欄位內仍是正常文字 |
| `Ctrl+L` | 聚焦搜尋欄位 |
| `Backspace` | 清單中返回前一個瀏覽畫面；輸入欄位中正常刪字 |
| `Alt+←`／`Alt+→` | 搜尋欄位或清單取得焦點時返回／前進；對話框內不切換瀏覽歷史 |
| `Ctrl+E` | 查看目前畫面的全部讀取錯誤；清單可用方向鍵或 PageUp／PageDown 捲動 |
| `Esc` | 清除搜尋並返回首頁；確認對話框中取消開啟，錯誤詳情中關閉對話框 |
| `Ctrl+Q` | 結束程式 |
| 滑鼠單擊 | 選取並開啟項目；執行檔仍需確認 |

從搜尋結果或最愛直接進入深層資料夾，第一次返回會回到原搜尋結果或首頁，並保留選取位置。只有實際進入過的資料夾才形成返回歷史，不會直接跳到未瀏覽過的父目錄。`Alt+←`／`Alt+→` 可在已走過的瀏覽、搜尋與首頁畫面之間前後切換；新的搜尋、Esc 回首頁或進入其他資料夾會清除可前進歷史。返回或前進至資料夾時會重新讀取直接子項目；原搜尋結果則還原到進入前的畫面。

## 驗證

只檢查設定與三個根目錄是否可讀，不遞迴搜尋或修改收藏：

```powershell
Set-Location 'C:\path\to\file-finder-tui'
.\.venv\Scripts\python.exe .\main.py --check
$LASTEXITCODE
```

全部根目錄可讀時退出碼為 `0`；設定或任一根目錄有問題時為 `1`。檢查網路路徑仍需等待 Windows 的檔案系統回應。

指定其他設定檔時，最愛預設保存於該設定檔旁：

```powershell
.\.venv\Scripts\python.exe .\main.py --config 'C:\Example\config.json'
```

pytest 僅供測試，不是程式執行依賴：

```powershell
Set-Location 'C:\path\to\file-finder-tui'
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q main.py tests
```

測試以 pytest 的 `tmp_path`／`tempfile` 建立三個根目錄與多層假資料，測試名稱搜尋、同名路徑、排序與 200 筆限制、掃描上限、取消與過期結果、歷史、即時子項目、收藏重載／原子寫入失敗、無效設定、權限與網路例外，以及 Textual 鍵盤／滑鼠操作和執行確認。Windows junction 測試只在暫存資料夾建立迴圈，不需符號連結管理員權限。

最近一次驗證在原始碼的 Python 3.11 與安裝資料夾的 Python 3.12 環境分別執行 pytest，兩者皆為 `72 passed`；Python 3.11 的 `compileall` 也成功。測試涵蓋設定檔絕對路徑、輸入欄位中的前後導航、全部錯誤顯示及對話框不影響主畫面操作。搜尋進度另驗證三個根目錄的計數與錯誤、回報頻率限制、搜尋取消、等待背景工作時耗時持續更新，以及過期／完成後的進度不能覆蓋畫面。測試只建立暫存假資料。

## 檔案與後續使用

```text
file-finder-tui/
├── main.py
├── config.example.json
├── config.json         # 本機設定，Git 忽略
├── favorites.json      # 本機收藏，Git 忽略
├── requirements.txt
├── README.md
├── .gitignore
└── tests/
    ├── conftest.py
    ├── test_search.py
    ├── test_favorites.py
    └── test_navigation.py
```

先調整三個根目錄並執行 `--check`，再啟動 TUI。實際 Windows Terminal 顯示、使用者網路磁碟與 Windows 關聯程式開啟行為可依快捷鍵表進行人工驗收；自動測試不會修改你的原始搜尋檔案。
