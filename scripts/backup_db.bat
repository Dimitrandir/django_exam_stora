@echo off
rem STORA database backup -- run by Windows Task Scheduler every 15 minutes
rem (see DEPLOYMENT.md, section 8). It only starts manage.py backup_db with the
rem project's own Python; that command decides by itself whether a backup is
rem due (BACKUP_* settings in .env) -- change the frequency there, not here.
cd /d "%~dp0.."
".venv\Scripts\python.exe" manage.py backup_db >> "logs\backup_task.log" 2>&1
