# Деплойване на реален обект (1 компютър, Windows 10)

Стъпка по стъпка за качване на STORA на реална касова машина в малък обект.
Достъпът е само от тази машина (`http://localhost:<порт>`) — няма нужда от
firewall правила или HTTPS.

Redis/Celery **не са включени в тоя пилот** (решено съзнателно — виж
CLAUDE.md) — `dispatch_task` поглъща грешката тихо, приложението работи
нормално, само еднократният backfill при маркиране на продукт като рецепта
няма да се изпълни. **Не маркирай продукти като рецепти на тази машина**,
докато не се добави Celery worker.

## 1. Python

Инсталирай Python 3.12 (същата версия като в dev, https://www.python.org/downloads/)
— при инсталацията чекни "Add python.exe to PATH".

## 2. PostgreSQL

Инсталирай PostgreSQL 16+ (https://www.postgresql.org/download/windows/).
При инсталацията ще зададеш парола на `postgres` супер-потребителя — запази я,
но не е паролата, която ползва самото приложение.

След инсталация, отвори "SQL Shell (psql)" от Start менюто (или `psql` от
command prompt) и създай базата + отделен потребител за приложението:

```sql
CREATE DATABASE stora_db;
CREATE USER stora_usr WITH PASSWORD 'сложи-тук-истинска-парола';
GRANT ALL PRIVILEGES ON DATABASE stora_db TO stora_usr;
ALTER DATABASE stora_db OWNER TO stora_usr;
```

### 2.1 AI режим на справките — само-за-четене роля

Reports app-ът има "AI режим" (справки от свободен текст) — вика Anthropic API,
което съставя и изпълнява SQL заявки срещу базата. За да е това истински
безопасно (не само "казахме му да не пипа"), заявките минават през отделна
Postgres роля, която физически няма право да пише нищо. Докато си още в
"SQL Shell (psql)" от стъпка 2, пусни и:

```sql
\i db_create_ai_readonly_role.sql
```

(файлът е в root на проекта, до `manage.py`) — от "SQL Shell (psql)" вече си
логнат като `postgres` с паролата от стъпка 2, така че `\i` просто го
изпълнява директно, без нова връзка/парола.

По подразбиране скриптът задава на новата роля същата парола като на
`stora_usr` (`stora_psswd` в dev пробите) — смени я в самия `.sql` файл
преди да го пуснеш, ако искаш различна. Тази парола отива в `.env` като
`DB_READONLY_PASSWORD` в стъпка 4.

## 3. Проектът

Копирай папката на проекта на машината (или `git clone`, ако има интернет и
достъп до repo-то). После в command prompt, в root на проекта:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 4. `.env`

Копирай `.env.example` като `.env` (същата папка, до `manage.py`) и попълни:

- `SECRET_KEY` — генерирай с:
  ```bash
  python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
  ```
- `DEBUG=False`
- `ALLOWED_HOSTS=localhost,127.0.0.1`
- `DB_PASSWORD` — паролата, която зададе на `stora_usr` в стъпка 2.
- `DB_READONLY_PASSWORD` — паролата, която зададе на `stora_ai_readonly` в стъпка 2.1.
- `ANTHROPIC_API_KEY` — за AI режима на справките.

`.env` НЕ се комитва в git — остава само на тази машина.

## 5. База данни + статични файлове

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

`migrate` автоматично създава групите Managers/Cashiers/Warehouse с правилните
права (виж `STORA/accounts/signals.py`) — не е нужна отделна стъпка. Първия
служител влиза с `createsuperuser`, после останалите се създават от Employees
екрана с избрана роля.

## 6. Пробно пускане

```bash
waitress-serve --host=127.0.0.1 --port=8000 STORA.wsgi:application
```

Отвори `http://localhost:8000` в браузър на същата машина — трябва да излезе
login екрана. Ctrl+C да спреш пробното пускане, преди да минеш на стъпка 7.

Ако нещо гръмне, browser вече не показва traceback (`DEBUG=False`) — грешката
е в `logs\stora.log` в root на проекта.

## 7. Автоматично стартиране + рестарт при срив (NSSM)

Касата трябва да работи и без разработчик до нея — ако компютърът се
рестартира или waitress гръмне, услугата трябва сама да се вдигне пак.

1. Свали NSSM (https://nssm.cc/download), разархивирай, копирай `nssm.exe`
   някъде постоянно (напр. `C:\nssm\nssm.exe`).
2. От command prompt (Administrator):
   ```bash
   C:\nssm\nssm.exe install STORA
   ```
3. В прозореца, който се отваря:
   - **Path**: пълния път до `.venv\Scripts\waitress-serve.exe`
   - **Startup directory**: root на проекта (където е `manage.py`)
   - **Arguments**: `--host=127.0.0.1 --port=8000 STORA.wsgi:application`
   - Табче **Details** → Startup type: `Automatic`
   - Табче **Exit actions** → Action: `Restart application` (рестартира сам
     при срив)
4. `C:\nssm\nssm.exe start STORA` — или просто рестартирай компютъра, услугата
   тръгва сама.

Проверка/спиране по-нататък: `nssm status STORA`, `nssm stop STORA`,
`nssm restart STORA`.

## 7.1 Обновяване на приложението по-нататък (`git pull`)

```bash
git pull
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
nssm restart STORA
```

**Последната стъпка (`nssm restart STORA`) е задължителна при всяка промяна**
**на статичен файл (CSS/JS) — не е достатъчно само `collectstatic`.**
WhiteNoise (`DEBUG=False`) чете статичните файлове в паметта само веднъж,
при стартиране на процеса — не следи промени на диска след това. Ако само
пуснеш `collectstatic` без рестарт, файлът на диска е коректен (`grep` през
`staticfiles/` ще го покаже), но работещият процес продължава да сервира
старото копие от паметта си, докато не се рестартира. Хванато на живо:
бутон в интерфейса се появи (HTML-ът се рендира на живо от Django, не е
статичен файл — вижда се веднага), но си остана в стар/дефолтен цвят чак
до `nssm restart STORA`.

## 8. Дневен бекъп на базата

Създай `backup.bat` (напр. в `C:\stora-backup\backup.bat`):

```bat
@echo off
set PGPASSWORD=паролата-на-stora_usr
"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -U stora_usr -h 127.0.0.1 -F c stora_db > "C:\stora-backup\stora_%date:~-4%-%date:~3,2%-%date:~0,2%.dump"
```

После в Windows **Task Scheduler** → Create Task → Trigger: Daily (напр. 03:00
през нощта, извън работно време) → Action: стартира `backup.bat`.

Възстановяване, ако някога потрябва:
```bash
pg_restore -U stora_usr -h 127.0.0.1 -d stora_db --clean stora_2026-09-13.dump
```

Пази бекъпите на второ място (USB диск, друг компютър) — само на самия
компютър не пази при хардуерен проблем.

## 9. Фискален принтер (Daisy Perfect S01)

Целият протокол/настройка е документирана в `kasov_aparat.md` и
`kasov_aparat_ECRCommApp_Guide.pdf` в корена на проекта — прочети ги, преди
да пипаш това. Накратко, за да заработи реалната фискализация на CASH
продажби:

1. Драйверът (STM32 Virtual COM Port) и `ecrcommapp_win_x64.exe` трябва вече
   да са налични на машината (виж `kasov_aparat.md`) — STORA не ги
   инсталира, само стартира/спира самия `.exe` около всяка продажба.
2. Провери COM порта, на който виси апаратът (Device Manager), и реалния
   сериен номер на устройството + номера на ФП (и двете в Daisy Manager →
   Настройки — "Номер на устройството" и "Номер на ФП" са ДВЕ различни
   числа, номерът на ФП трябва за `FISCAL_DEVICE_FM_NUMBER`, нужен само за
   сторно операции, не за обикновена продажба).
3. Провери операторската парола НА САМИЯ апарат (Програмиране → Оператори
   → оператор 1) — НЕ паролата за PC режим (тези са различни неща, лесно се
   бъркат).
4. Попълни `FISCAL_*` стойностите в `.env` (виж `.env.example`) с тези
   реални стойности, и чак накрая сложи `FISCAL_ENABLED=True`.
5. В Products → Tax Groups, за всяка активна данъчна група попълни
   "Fiscal device letter" (буквата, с която е програмирана на апарата —
   потвърдено: Б=20%, Г=9% на пилотния апарат) — продажба на продукт с
   непопълнена буква ще се провали с ясна грешка, вместо да фискализира с
   грешна ставка.
6. **Важно за RACS (системата за контрол на достъп, ако е на същата
   машина):** тя също иска периодично връзка със същия COM порт. STORA
   пуска/спира `ecrcommapp_win_x64.exe` само около всяка продажба (не
   постоянна услуга), точно за да ѝ остави порта свободен между продажбите
   — потвърдено на живо, че моделът работи. Ако все пак видиш чести грешки
   тип "Open (SetCommState)" в `fiscal_error` на продажба, първо провери
   дали RACS не държи порта в момента.
   **Капан, случил се на живо:** COM номерът на апарата може да се смени
   сам (напр. COM4 → COM7) при прекъсване на USB връзката/рестарт на
   апарата или машината — грешката тогава е `"Invalid COM Port"` в
   `ecrcommapp`-логовете, докато нашият код изчерпи 10-те си секунди чакане
   и маркира продажбата `FAILED` с "ECRCommApp did not become ready in
   time." Провери Device Manager → Ports (COM & LPT) за реалния номер и
   поправи `FISCAL_COM_PORT` в `.env` (+ рестарт на Django процеса).
7. Тествай с една истинска продажба за минимална сума (0.01) и провери
   физически, че излиза бон — само тогава се минава на реални продажби.

Ако фискализацията гръмне при истинска продажба, самата продажба **не се
губи** — записва се нормално в базата, само `fiscal_status` става `FAILED`
с текст на грешката, видим на страницата на продажбата, откъдето може да се
натисне "Retry Fiscal Print" след като проблемът е отстранен.

## Известни отложени неща за тоя пилот

- Celery/Redis не са вкарани — рецептурните продукти не се ползват на тая
  машина засега (виж бележката горе).
- Достъп само от тая машина — ако по-късно потрябва каса на второ устройство
  в мрежата, `ALLOWED_HOSTS`/CSRF настройките и firewall правилото за порта
  трябват преразглеждане, не е направено сега.
- Фискализация засега само за CASH продажби — CARD/MIXED се пропускат
  (никакъв фискален Payment type все още не е програмиран/потвърден за
  карта на апарата). Виж раздел 9 по-горе.
