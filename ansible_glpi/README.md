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
cd orthanc-cleanup

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# відредагувати .env — вказати URL та credentials
```

## Налаштування

Всі параметри у файлі `.env` (див. `.env.example`):

| Змінна | Опис |
|---|---|
| `ORTHANC_URL` | URL Orthanc REST API |
| `ORTHANC_USER` / `ORTHANC_PASSWORD` | Облікові дані Orthanc |
| `ORTHANC_VERIFY_SSL` | Перевірка TLS (`false` для dev, `true` для prod) |
| `RETENTION_YEARS` | Вік досліджень для видалення (років) |
| `GLPI_URL` | URL GLPI |
| `GLPI_APP_TOKEN` | App-Token GLPI API |
| `GLPI_USER_TOKEN` | User-Token GLPI API |
| `GLPI_CATEGORY_ID` | ID категорії тікету |
| `GLPI_ENTITY_ID` | ID організації GLPI (0 = root) |
| `GLPI_ASSIGN_USER_ID` | ID відповідального у GLPI |
| `STUDIES_FILE` | Шлях до JSON зі списком досліджень |
| `STATE_FILE` | Шлях до файлу стану (ticket_id) |

### Отримати GLPI токени та ID

| Змінна | Де знайти |
|---|---|
| `GLPI_APP_TOKEN` | Налаштування → Загальні → API → створити App-Token |
| `GLPI_USER_TOKEN` | Профіль користувача → Налаштування → Згенерувати зовнішній токен API |
| `GLPI_CATEGORY_ID` | Служба підтримки → Категорії тікетів → відкрити категорію → ID в URL (`itilcategory.form.php?id=X`) |
| `GLPI_ENTITY_ID` | Налаштування → Організації → відкрити організацію → ID в URL (`entity.form.php?id=X`); Root entity = `0` |
| `GLPI_ASSIGN_USER_ID` | Адміністрування → Користувачі → відкрити користувача → ID в URL (`user.form.php?id=X`) |

## Команди

```bash
# Знайти старі дослідження та створити тікет у GLPI
python cleanup.py gather

# Перевірити статус тікету — видалити якщо Solved/Closed
python cleanup.py check

# Ручне видалення з інтерактивним підтвердженням (без GLPI)
python cleanup.py delete
```

## Кілька серверів

Для кожного сервера — окремий `.env` файл:

```bash
cp .env.example server1.env   # відредагувати ORTHANC_URL, credentials, STATE_FILE
cp .env.example server2.env
cp .env.example server3.env
```

> **Важливо:** `STATE_FILE` та `STUDIES_FILE` мають бути унікальними для кожного сервера.

```bash
python cleanup.py gather --env server1.env
python cleanup.py gather --env server2.env
python cleanup.py gather --env server3.env

python cleanup.py check  --env server1.env
python cleanup.py check  --env server2.env
python cleanup.py check  --env server3.env
```

## Статуси тікетів GLPI

`check` запускає видалення при статусах **Solved (5)** або **Closed (6)**.

| Код | Статус | Дія |
|---|---|---|
| 1 | New | очікування |
| 2–4 | Processing / Pending | очікування |
| **5** | **Solved** | **запускає видалення** |
| **6** | **Closed** | **запускає видалення** |

## Залежності

- Python 3.8+
- `requests` — HTTP-запити до Orthanc та GLPI
- `python-dotenv` — завантаження конфігурації з `.env`
