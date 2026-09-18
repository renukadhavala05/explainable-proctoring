@echo off
REM ---- Free PUBLIC demo via Cloudflare Tunnel (valid HTTPS, works on any network) ----
cd /d %~dp0

echo Starting the proctoring server on http://localhost:8000 ...
start "Proctoring Server" cmd /k venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
timeout /t 4 >nul

echo.
echo ============================================================
echo  Starting Cloudflare Tunnel.
echo  Look below for your PUBLIC URL:  https://XXXX.trycloudflare.com
echo.
echo  Open that URL on ANY device (laptop + phone, any network):
echo     Landing / QR :  https://XXXX.trycloudflare.com
echo     Candidate    :  https://XXXX.trycloudflare.com/candidate?session=exam-001
echo     Proctor      :  https://XXXX.trycloudflare.com/proctor?session=exam-001
echo  On the phone, just scan the QR shown on the landing/proctor page.
echo ============================================================
echo.
cloudflared.exe tunnel --url http://localhost:8000
pause
