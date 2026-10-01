# file-finder-tui 弱點掃描

掃描日期：2026-10-01（Asia/Taipei）。掃描基準：`83ed05acc35ccda5a7ec45e26248b1cead9f9ead`，包含 PR #3 的索引與清單更新。PR #3 已於掃描期間合併；本文的修正提交接續 main 的 `fa6271b`，尚需合併新的安全修正 PR 才會進入 main。

## 範圍與方法

- 檢查 `main.py`、`backend.py`、`requirements.txt`、設定／收藏處理、路徑枚舉、背景工作及開啟檔案的流程。
- `pip-audit 2.10.1` 解析 requirements 與間接相依，使用預設 PyPI／PyPA 已知漏洞資料比對；9 項皆成功比對，沒有略過套件。
- `Bandit 1.9.4` 掃描兩個程式；修正前後均無警告。另人工檢查程式執行、外部文字輸出、連結與 JSON 信任邊界，透過暫存資料及 headless Textual 重現。
- 執行環境：Linux、Python 3.12.14、Textual 8.2.8。未取得使用者 Windows 虛擬環境，也未操作真實網路磁碟、啟動真實檔案關聯程式或執行不可信檔案。
- 對目前追蹤的程式做基本憑證字串檢查，未發現明顯硬編碼憑證；未使用專用秘密掃描器檢查完整 Git 歷史。

## 結果與修正

未發現本次解析之相依套件的已知漏洞。人工驗證發現 3 項可重現的程式風險，已加入防護。等級為本專案威脅情境的人工評估，並非 CVE 或 CVSS 評分。

| 編號 | 等級 | 修正前的問題與條件 | 修正及驗證 |
| --- | --- | --- | --- |
| FF-01 | 中 | 外部文字中的終端 ESC、控制字元或 Unicode 方向控制字元未轉義。即使 `markup=False`，設定根目錄名稱中的 `hello\x1b[2J.txt` 仍可原樣進入終端輸出，可能改變畫面或誤導檔名辨識。攻擊者需能影響設定名稱或讀取範圍內的名稱／路徑；未驗證程式碼執行或終端特定攻擊。 | 外部名稱、路徑、錯誤、確認視窗、狀態與 `--check` 輸出使用 `display_text()`，將控制字元顯示成文字。實際路徑保持原值。以 headless compositor 的最終輸出確認惡意名稱中的原始序列不再出現。 |
| FF-02 | 低 | 子項目枚舉會排除連結，但直接指定的根目錄／收藏／檔案開啟使用跟隨連結的檢查，排除規則不一致。直接瀏覽測試用根目錄 symlink 可列出目標檔名；直接開啟檔案 symlink 會呼叫 opener 替身。未驗證 Windows 對捷徑／連結的實際執行效果。 | 新讀取的根目錄與直接操作先用 `lstat()` 檢查 symlink／Windows reparse 屬性；瀏覽也在讀取快取前重新檢查直接目標。被替換成一般檔案的快取資料夾回報錯誤。POSIX 實際連結與模擬 Windows reparse 屬性均確認不枚舉／不開啟；父路徑及競態限制見下節。 |
| FF-03 | 低 | 收藏 JSON 接受含 NUL 的絕對路徑，後續檔案系統呼叫可拋出未處理的 `ValueError`；深度過大的 JSON 可拋出 `RecursionError`。整份 JSON 無大小上限，可能消耗過多記憶體。需要能更改本機設定／收藏資料。 | 驗證路徑、拒絕 NUL／孤立 surrogate；JSON 最多讀取 2 MiB + 1 位元組，超限或過深提供可處理的錯誤。收藏保存也套用大小限制，失敗保留原檔與記憶體資料。介面測試確認無效收藏不會令程式退出。 |

## 相依套件清單

這是掃描當日以 Linux／Python 3.12.14 解析的安裝版本；間接相依未固定在 requirements，其他日期或 Python 版本的解析結果可能不同。

| 套件 | 版本 | 已知漏洞數 |
| --- | --- | --- |
| textual | 8.2.8 | 0 |
| platformdirs | 4.12.2 | 0 |
| pygments | 2.21.0 | 0 |
| typing-extensions | 4.16.0 | 0 |
| markdown-it-py | 4.2.0 | 0 |
| mdurl | 0.1.2 | 0 |
| linkify-it-py | 2.2.0 | 0 |
| rich | 15.0.0 | 0 |
| mdit-py-plugins | 0.6.1 | 0 |

## 剩餘限制

- `os.startfile()` 交由 Windows 檔案關聯處理；副檔名確認清單不是程式執行安全邊界，也無法涵蓋自訂關聯或所有可執行類型。一般文件及其關聯程式仍可能有巨集或漏洞。更嚴格的策略需要另行決定是否全部開啟都確認或限制可開啟類型；本次保留既有操作流程。
- 路徑檢查與 `scandir()`／`startfile()` 之間仍存在 TOCTOU 競態；未以 Windows handle 鎖定物件，也未完整拒絕所有父路徑的 reparse point。直接目標檢查降低已存在連結的繞過，不能保證對抗持續改寫路徑的攻擊者。快取的搜尋只比對記憶體名稱，不新增磁碟讀取；F5 或重新進入資料夾才讀取新內容。
- 大量資料夾子項目、記憶體索引與背景檔案系統呼叫仍可能消耗時間／記憶體；本次沒有新增瀏覽筆數限制，以保留完整資料夾清單。網路呼叫等待仍取決於 Windows 逾時。
- 本次沒有發現使用 `eval`、shell 命令拼接、pickle 載入或網路服務入口；這不等於所有外部關聯程式都安全。
- 套件掃描只涵蓋當時資料庫已公開的漏洞，靜態工具也可能漏報。尚未實測使用者的 Windows Terminal、ACL、網路磁碟或實際 junction 操作；跨平台模擬不能取代 Windows 驗收。

## 重跑方式

在開發用虛擬環境安裝工具後執行；不需要改動 `requirements.txt`：

```powershell
python -m pip install pip-audit==2.10.1 bandit==1.9.4 pytest
python -m pip_audit -r requirements.txt --strict
python -m bandit main.py backend.py
python -m pytest -q
python -m compileall -q main.py backend.py tests
```

最後一次全套測試：110 通過、1 略過（Windows junction）；編譯檢查及 `git diff --check` 通過。安全測試使用暫存檔案與 opener 替身，不執行實際不可信程式。

## 參考

- [PyPA pip-audit 的能力及安全模型](https://github.com/pypa/pip-audit)
- [Python JSON：處理不可信資料的資源風險](https://docs.python.org/3.12/library/json.html)
- [Python os.startfile 的檔案關聯行為](https://docs.python.org/3/library/os.html#os.startfile)
- [Microsoft：符號連結對檔案系統操作的影響](https://learn.microsoft.com/en-us/windows/win32/fileio/symbolic-link-effects-on-file-systems-functions)
