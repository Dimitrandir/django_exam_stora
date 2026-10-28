@echo off
rem STORA daily database backup (see DEPLOYMENT.md, section 8).
rem Runs pg_dump into BACKUP_DIR, logs the result, keeps the last KEEP_DAYS
rem days of dumps, and optionally copies each new dump to a second place.
rem Needs no password in this file: pg_dump reads it from PGPASSFILE below.
setlocal

rem ---- settings: change these to match this machine ----
set "PGBIN=C:\Program Files\PostgreSQL\18\bin"
set "DB_NAME=stora_db"
set "DB_USER=stora_usr"
set "BACKUP_DIR=C:\stora-backup"
set "KEEP_DAYS=30"
rem Optional second copy (USB drive / network share), e.g. set "COPY_TO=E:\stora-backup". Empty = skip.
set "COPY_TO="
rem ------------------------------------------------------

set "PGPASSFILE=%BACKUP_DIR%\pgpass.conf"
set "LOG=%BACKUP_DIR%\backup.log"

if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

rem Locale-independent timestamp (the old %%date%% trick broke on other regional settings).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm"') do set "STAMP=%%i"
set "TARGET=%BACKUP_DIR%\stora_%STAMP%.dump"

rem -w = never ask for a password (an unattended run must fail, not hang).
"%PGBIN%\pg_dump.exe" -U %DB_USER% -h 127.0.0.1 -w -F c -f "%TARGET%" %DB_NAME% 2>>"%LOG%"
if errorlevel 1 (
    echo [%STAMP%] FAILED - see the lines above>>"%LOG%"
    if exist "%TARGET%" del "%TARGET%"
    exit /b 1
)
echo [%STAMP%] OK %TARGET%>>"%LOG%"

if defined COPY_TO (
    copy "%TARGET%" "%COPY_TO%\" >nul
    if errorlevel 1 echo [%STAMP%] COPY TO %COPY_TO% FAILED>>"%LOG%"
)

rem Rotation: delete dumps older than KEEP_DAYS (forfiles exits 1 when nothing matches - that is fine).
forfiles /p "%BACKUP_DIR%" /m stora_*.dump /d -%KEEP_DAYS% /c "cmd /c del @path" >nul 2>&1
exit /b 0
