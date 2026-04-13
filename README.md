# Orthanc PACS Cleanup

Автоматизоване видалення застарілих DICOM-досліджень з Orthanc PACS з погодженням через GLPI.

## Як працює

```
python cleanup.py gather          # знайти старі дослідження → створити тікет у GLPI
         ↓
  [відповідальний погоджує тікет у GLPI: статус Solved або Closed]
         ↓
python cleanup.py check           # перевірити статус → видалити якщо погоджено
```

`check` безпечно завершується без дій якщо тікет ще не погоджено — зручно запускати за розкладом (cron).

## Встановлення

```bash
git clone <repo>
cd orthanc-retention

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example server.назва.env
# відредагувати — вказати URL, credentials, шляхи до файлів стану
```

## Налаштування

Всі параметри у файлі `server.<назва>.env` (див. `.env.example`):

| Змінна | Опис |
|---|---|
| `ORTHANC_URL` | URL Orthanc REST API |
| `ORTHANC_USER` / `ORTHANC_PASSWORD` | Облікові дані Orthanc |
| `ORTHANC_VERIFY_SSL` | SSL-перевірка (див. нижче) |
| `RETENTION_YEARS` | Вік досліджень для видалення (років, мін. 1) |
| `GLPI_URL` | URL GLPI |
| `GLPI_APP_TOKEN` | App-Token GLPI API |
| `GLPI_USER_TOKEN` | User-Token GLPI API |
| `GLPI_VERIFY_SSL` | SSL-перевірка для GLPI (див. нижче) |
| `GLPI_CATEGORY_ID` | ID категорії тікету |
| `GLPI_ENTITY_ID` | ID організації GLPI (0 = root) |
| `GLPI_ASSIGN_USER_ID` | ID відповідального у GLPI |
| `STUDIES_FILE` | Шлях до JSON зі списком досліджень |
| `STATE_FILE` | Шлях до файлу стану (ticket_id) |

### SSL-перевірка

`ORTHANC_VERIFY_SSL` та `GLPI_VERIFY_SSL` підтримують три варіанти:

| Значення | Поведінка |
|---|---|
| `false` | Вимкнути перевірку (dev/self-signed без CA) |
| `true` | Перевіряти через системні CA (production) |
| `/etc/ssl/certs/my-ca.crt` | Перевіряти через вказаний CA-файл (самопідписний сертифікат) |

```env
# Приклади:
ORTHANC_VERIFY_SSL=false
ORTHANC_VERIFY_SSL=true
ORTHANC_VERIFY_SSL=/etc/ssl/certs/orthanc-ca.crt
```

### Отримати GLPI токени та ID

| Змінна | Де знайти |
|---|---|
| `GLPI_APP_TOKEN` | Налаштування → Загальні → API → створити App-Token |
| `GLPI_USER_TOKEN` | Профіль користувача → Налаштування → Згенерувати зовнішній токен API |
| `GLPI_CATEGORY_ID` | Служба підтримки → Категорії тікетів → відкрити → ID в URL (`itilcategory.form.php?id=X`) |
| `GLPI_ENTITY_ID` | Налаштування → Організації → відкрити → ID в URL (`entity.form.php?id=X`); Root = `0` |
| `GLPI_ASSIGN_USER_ID` | Адміністрування → Користувачі → відкрити → ID в URL (`user.form.php?id=X`) |

## Команди

```bash
# Знайти старі дослідження та створити тікет у GLPI
python cleanup.py gather --env server.xr.env

# Перевірити статус тікету — видалити якщо Solved/Closed
python cleanup.py check --env server.xr.env

# Ручне видалення з інтерактивним підтвердженням (без GLPI)
python cleanup.py delete --env server.xr.env
```

## Кілька серверів

Конвенція імен файлів: `server.<назва>.env`

```bash
cp .env.example server.xr.env    # рентген
cp .env.example server.ct.env    # КТ
cp .env.example server.mri.env   # МРТ
```

> **Важливо:** `STATE_FILE` та `STUDIES_FILE` мають бути унікальними для кожного сервера.

Назва сервера (`xr`, `ct` тощо) автоматично підставляється у тікет GLPI з імені файлу.

## Статуси тікетів GLPI

`check` запускає видалення при статусах **Solved (5)** або **Closed (6)**.

| Код | Статус | Дія |
|---|---|---|
| 1 | New | очікування |
| 2–4 | Processing / Pending | очікування |
| **5** | **Solved** | **запускає видалення** |
| **6** | **Closed** | **запускає видалення** |

## Автозапуск (cron)

Скрипт `cron.sh` запускає вказану команду для всіх `server.*.env` по черзі та пише лог у `/var/log/orthanc-cleanup/`.

### Налаштування

```bash
# 1. Переконайтесь що venv існує та залежності встановлено
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate

# 2. Зробіть скрипт виконуваним
chmod +x cron.sh

# 3. Створіть директорію для логів
sudo mkdir -p /var/log/orthanc-cleanup
sudo chown "$USER" /var/log/orthanc-cleanup

# 4. Додайте задачі в crontab
crontab -e
```

Вставте рядки (замінити шлях на свій):

```cron
# Orthanc cleanup — gather (1-го числа кожного місяця о 08:00)
0 8 1 * * /path/to/orthanc-retention/cron.sh gather

# Orthanc cleanup — check (щодня о 22:00)
0 22 * * * /path/to/orthanc-retention/cron.sh check
```

### Логи

```
/var/log/orthanc-cleanup/server.xr_gather.log
/var/log/orthanc-cleanup/server.xr_check.log
/var/log/orthanc-cleanup/server.ct_gather.log
...
```

### Ручний запуск для перевірки

```bash
./cron.sh gather
./cron.sh check
```

## Тести

```bash
venv/bin/pytest tests/ -v
```

## Залежності

- Python 3.7+
- `requests` — HTTP-запити до Orthanc та GLPI
- `python-dotenv` — завантаження конфігурації з `.env`
- `pytest`, `requests-mock` — тести (dev)
