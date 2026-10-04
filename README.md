# One More Thing v1

One More Thing 是學生的個人學習行動助手：將零散的校園資訊結合個人行事曆與待辦脈絡，整理成可檢查、可修改、由使用者確認後才套用的行動。

## 核心流程

Capture → Context → Interpret → Proposal → User review → Apply

AI 產生的是待審核提案；使用者確認前，不會寫入日常 Event 或 Task。

## 專案結構

- ackend/：FastAPI 後端
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

## 版本與範圍

此原始碼快照標示為 v1。正式 AI 服務、端對端加密、WebDAV、原生行動端與學校平台整合不在此版本已驗證範圍內。
