@echo off
set NSSM="C:\Users\satvi\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"

%NSSM% set TS_Worker AppDirectory "C:\Users\satvi\Downloads\ts-tours\backend"
%NSSM% set TS_Worker AppStdout "C:\Users\satvi\Downloads\ts-tours\backend\ts_worker.log"
%NSSM% set TS_Worker AppStderr "C:\Users\satvi\Downloads\ts-tours\backend\ts_worker.log"
%NSSM% set TS_Worker AppRestartDelay 5000
%NSSM% set TS_Worker Start SERVICE_AUTO_START
%NSSM% start TS_Worker
