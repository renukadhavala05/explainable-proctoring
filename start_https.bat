@echo off
REM ---- Real trial with mobile secondary camera (HTTPS required for phone camera) ----
cd /d %~dp0
set HTTPS_PORT=8443

echo Generating self-signed certificate...
venv\Scripts\python.exe make_cert.py
echo.

REM Try to open the firewall for the phone (needs admin; ignored if it fails)
netsh advfirewall firewall add rule name="Proctoring 8443" dir=in action=allow protocol=TCP localport=8443 >nul 2>&1

echo Starting HTTPS server on port 8443 (reachable from your phone on the same Wi-Fi).
echo   Laptop proctor : https://localhost:8443/proctor?session=exam-001
echo   Laptop exam    : https://localhost:8443/candidate?session=exam-001
echo   Landing + QR   : https://localhost:8443
echo.
echo On the phone, scan the QR on the proctor/landing page and accept the security warning once.
echo.
venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8443 --ssl-keyfile certs\key.pem --ssl-certfile certs\cert.pem
pause
