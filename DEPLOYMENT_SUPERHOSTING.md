# Тестово качване на STORA в СуперХостинг (СуперПро план)

Експеримент: STORA в облака, отваря се от браузър отвсякъде. Касовият
апарат НЕ работи в тази версия. Остави `FISCAL_ENABLED=False`; мостът към
касата е следваща стъпка. За реалните обекти виж `DEPLOYMENT.md`, този файл
не ги засяга.

**Какво вече е проверено за плана:**
- Python 3.12.14 има в „Setup Python App“.
- `pg_trgm` е инсталиран от поддръжката за базата `vn8ao5it_stora_test`.
- PostgreSQL на хостинга е **10.23**. Django 6 официално иска 14+, затова
  тук ползваме `DB_ENGINE=STORA.db_backends.postgresql_legacy`, който
  пропуска само проверката за версия. Тествано: всички миграции и всички
  тестове минават срещу PostgreSQL 10.23. Преди всяко обновяване на Django
  тестовете трябва пак да се пуснат срещу PostgreSQL 10.

## 1. Поддомейн

cPanel → **Domains** → „Добавяне на нов домейн“ → Домейн
`stora.petarstoychev.com`. Чекбоксът „Използвай основната директория“ остава
**празен**. Основната директория остави на предложената
(`stora.petarstoychev.com`), а **не** `stora`, защото там ще е кодът заедно с
`.env`, който не бива да е достъпен от уеб.
После cPanel → **SSL/TLS Status** → пусни AutoSSL за него, за да тръгне по
HTTPS.

## 2. Код

cPanel → **Terminal**:

```bash
cd ~
git clone -b claude/project-thread-811g76 https://github.com/Dimitrandir/django_exam_stora.git stora
```

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

Натисни **Create**. Горе на страницата ще излезе команда от типа
`source /home/vn8ao5it/virtualenv/stora/3.12/bin/activate && cd /home/vn8ao5it/stora`.
Копирай я, трябва ти в следващите стъпки.

## 4. Библиотеки

В Terminal пусни командата от стъпка 3, после:

```bash
pip install -r requirements.txt
```

## 5. Настройки (`.env`)

Създай файла с `nano ~/stora/.env`. Генерирай SECRET_KEY с:
`python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`

```
SECRET_KEY=<генерирания ключ>
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
```

`DB_HOST` е адресът, който phpPgAdmin показва горе („running on
127.0.0.200:5432“), не обичайното `127.0.0.1`.

Запис в nano: Ctrl+O, Enter, Ctrl+X.

## 6. База и статични файлове

Пак в Terminal, с активираната среда от стъпка 3:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

## 7. Старт

**Setup Python App** → при приложението натисни **Restart**. Отвори
`https://stora.petarstoychev.com` и влез с потребителя от `createsuperuser`.

## Обновяване след промени в кода

```bash
<командата от стъпка 3>
git pull
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```
После **Restart** в Setup Python App.

## Ако нещо не тръгне

- Страница „Something went wrong“ или 500: виж
  `~/stora/logs/passenger.log` и `~/stora/logs/stora.log`.
- „PostgreSQL 14 or later is required“: в `.env` липсва реда `DB_ENGINE`.
- „DisallowedHost“: адресът в браузъра не съвпада с `ALLOWED_HOSTS`.
- „CSRF verification failed“ при вход: провери `CSRF_TRUSTED_ORIGINS`
  (с `https://` отпред).
- Бавно първо зареждане сутрин е нормално. Хостингът приспива
  приложението, когато не се ползва.
