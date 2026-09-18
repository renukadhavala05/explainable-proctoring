@echo off
REM ---- Tabs demo (candidate + proctor on this laptop) ----
cd /d %~dp0
echo Starting Explainable Proctoring demo...
echo Open these in your browser:
echo    Landing : http://localhost:8000
echo    Candidate: http://localhost:8000/candidate?session=exam-001
echo    Proctor  : http://localhost:8000/proctor?session=exam-001
echo.
venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
pause
