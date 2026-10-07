# Тестово качване на STORA в СуперХостинг (СуперПро план)

Експеримент: STORA в облака, отваря се от браузър отвсякъде. Касовият
апарат НЕ работи в тази версия. Остави `FISCAL_ENABLED=False`; мостът към
касата е следваща стъпка. За реалните обекти виж `DEPLOYMENT.md`, този файл
не ги засяга.

Всичко тук става **само през cPanel, без терминал** (в нашия акаунт няма
Terminal, а SSH трябва да се пуска отделно). Тестовият адрес е
`https://stora.petarstoychev.com`.

**Какво вече е проверено за плана:**
- Python 3.12.14 има в „Setup Python App“.
- `pg_trgm` е инсталиран от поддръжката за базата `vn8ao5it_stora_test`.
- PostgreSQL на хостинга е **10.23**. Django 6 официално иска 14+, затова
  тук ползваме `DB_ENGINE=STORA.db_backends.postgresql_legacy`, който
  пропуска само проверката за версия. Тествано: всички миграции и всички
  тестове минават срещу PostgreSQL 10.23. Преди всяко обновяване на Django
  тестовете трябва пак да се пуснат срещу PostgreSQL 10.

## 1. Поддомейн

Домейнът трябва да има DNS при СуперХостинг. `spartaksport.com` например е
на Wix и поддомейн, създаден в cPanel, там просто не съществува („Server Not
Found“). `petarstoychev.com` е при СуперХостинг, затова ползваме него.

cPanel → **Domains** → „Добавяне на нов домейн“ → `stora.petarstoychev.com`.
Основната директория остави на предложената (`stora.petarstoychev.com`),
а **не** `stora` и **не** `public_html`. В `stora` е кодът заедно с `.env`,
който не бива да е достъпен от уеб, а в `public_html` е WordPress сайтът.
После **SSL/TLS Status** → AutoSSL за поддомейна.

## 2. Код

cPanel → **Git Version Control** → Create:
- Clone URL: `https://github.com/Dimitrandir/django_exam_stora.git`
- Repository Path: `stora`
- После в Manage → Pull/Deploy избери клона `claude/project-thread-811g76`.

**Права:** File Manager → домашната папка → маркирай `stora` → Permissions →
**755** (само на папката). Иначе уеб сървърът дава „Passenger error #2 ...
Permission denied (errno=13)“.

## 3. Python приложение

cPanel → **Setup Python App** → **Create Application**:

| Поле | Стойност |
|---|---|
| Python version | 3.12.14 |
| Application root | `stora` |
| Application URL | `stora.petarstoychev.com` (празно след него) |
| Application startup file | `STORA/wsgi.py` |
| Application Entry point | `application` |
| Passenger log file | `/home/vn8ao5it/stora/logs/passenger.log` |

Натисни **Create**.

**Провери след Create (и след всяка смяна на адреса):** cPanel понякога
презаписва startup файла със своя тестов код и сайтът показва „It works!
Python v3.12.14“. Ако е така, отвори `stora/STORA/wsgi.py` и върни
оригинала:

```python
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'STORA.settings')

application = get_wsgi_application()
```

## 4. Библиотеки

В приложението: „Add another file“ → `requirements.txt` → Add, после
**Run Pip Install**. Ако даде „Web application is inaccessible by its
address“, адресът от стъпка 1 още не отговаря (DNS).

## 5. Настройки (`.env`)

File Manager → Settings → **Show Hidden Files**. В папката `stora` (до
`manage.py`) създай файл `.env`:

```
SECRET_KEY=<дълъг случаен низ, поне 50 знака>
DEBUG=False
ALLOWED_HOSTS=stora.petarstoychev.com
CSRF_TRUSTED_ORIGINS=https://stora.petarstoychev.com

DB_ENGINE=STORA.db_backends.postgresql_legacy
DB_NAME=vn8ao5it_stora_test
DB_USER=vn8ao5it_usr
DB_PASSWORD=<паролата на потребителя на базата>
DB_HOST=127.0.0.200
DB_PORT=5432

FISCAL_ENABLED=False

DJANGO_SUPERUSER_USERNAME=<потребителско име>
DJANGO_SUPERUSER_EMAIL=<имейл>
DJANGO_SUPERUSER_PASSWORD=<парола за вход в STORA>
```

- `DB_HOST` е адресът, който phpPgAdmin показва горе („running on
  127.0.0.200:5432“), не обичайното `127.0.0.1`.
- В паролите ползвай само букви и цифри (`#`, кавички и интервали
  объркват `.env`).
- После Permissions на `.env` → **600**.

## 6. Помощен файл `run.py`

„Execute python script“ в cPanel казва само „exit code 1“ и не показва
грешката. Затова в `stora` създай `run.py`, който пуска `manage.py` и
записва всичко в `stora/logs/run_output.txt`:

```python
import os, sys, subprocess
here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
os.makedirs('logs', exist_ok=True)
r = subprocess.run([sys.executable, 'manage.py'] + sys.argv[1:], capture_output=True, text=True)
with open('logs/run_output.txt', 'w') as f:
    f.write(r.stdout + '\n----- ERRORS -----\n' + r.stderr)
sys.exit(r.returncode)
```

## 7. База и статични файлове

Setup Python App → приложението → **Execute python script**, една по една
(след всяка: Run Script, после погледни `logs/run_output.txt`):

1. `run.py migrate`
2. `run.py collectstatic --noinput`
3. `run.py createsuperuser --noinput`

После изтрий трите реда `DJANGO_SUPERUSER_...` от `.env`.

## 8. `.htaccess` на поддомейна

Отвори `.htaccess` в **папката на поддомейна** (`stora.petarstoychev.com`,
не `public_html`). Блокът `# CLOUDLINUX PASSENGER CONFIGURATION` остава.
Ако има блок `# BEGIN WordPress ... # END WordPress`, изтрий го: той
пренасочва всеки адрес към `index.php` и STORA дава 404 на всяка страница,
включително без стилове.

## 9. Старт

**Setup Python App** → **RESTART**. Отвори
`https://stora.petarstoychev.com` и влез с потребителя от стъпка 7.

## Обновяване след промени в кода

1. Git Version Control → Manage → **Update from Remote**.
2. Setup Python App → **Run Pip Install** (ако `requirements.txt` е сменен).
3. Execute python script: `run.py migrate`, после
   `run.py collectstatic --noinput`.
4. **RESTART**.

## Ако нещо не тръгне

- „exit code 1“ при Run Script: виж `stora/logs/run_output.txt`.
- „password authentication failed“: грешна `DB_PASSWORD` в `.env`.
- „PostgreSQL 14 or later is required“: в `.env` липсва редът `DB_ENGINE`.
- „Forbidden“ или „Passenger error #2 ... Permission denied“: правата на
  папката `stora` трябва да са 755.
- „It works! Python ...“: виж стъпка 3, startup файлът е презаписан.
- 404 на всяка страница: временно `DEBUG=True`, RESTART и виж „Request
  URL“. Ако пише `/index.php`, виж стъпка 8. После пак `DEBUG=False`.
- „DisallowedHost“: адресът в браузъра не съвпада с `ALLOWED_HOSTS`.
- „CSRF verification failed“ при вход: провери `CSRF_TRUSTED_ORIGINS`
  (с `https://` отпред).
- За повече подробности: `stora/logs/passenger.log` и `stora/logs/stora.log`.
- Бавно първо зареждане сутрин е нормално. Хостингът приспива
  приложението, когато не се ползва.
