@echo off
rem ============================================================
rem  Restart Windows Bluetooth Support Service (bthserv)
rem
rem  Use this when WorkBuddy / any BLE app reports "device not
rem  found" while the Bluetooth icon still looks normal. Symptom:
rem  the BLE advertisement scan channel stops delivering results
rem  and even address-based connection fails.
rem
rem  Right-click this file -> Run as administrator.
rem ============================================================
net stop bthserv
timeout /t 2 /nobreak >nul
net start bthserv
echo.
echo Bluetooth support service restarted.
echo Now retry connecting from the WorkBuddy console.
echo.
pause
