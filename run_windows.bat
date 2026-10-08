@echo off
rem Launch the pump-down + PL monitor from the venv created with:
rem   python -m venv .venv
rem   .venv\Scripts\python -m pip install -r requirements.txt
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py" %*
