# One More Thing v1

One More Thing 是學生的個人學習行動助手：將零散的校園資訊結合個人行事曆與待辦脈絡，整理成可檢查、可修改、由使用者確認後才套用的行動。

## 核心流程

輸入資訊 → AI 理解 → 整理成行程／待辦 → 使用者確認 → 加入個人安排

AI 會先整理出建議的行程與待辦，只有在使用者確認後，才會正式加入行事曆或待辦清單。

## 專案結構

- backend/：FastAPI 後端
- web/：Vue 3 + Vite 前端
- LICENSE：本專案自有程式碼採 WTFPL 第 2 版；第三方依賴依其各自授權條款

## 本機啟動

後端（Windows）：

`powershell
cd backend
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
`

前端（另一個終端機）：

`powershell
cd web
npm install
npm run dev
`
