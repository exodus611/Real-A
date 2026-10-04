"""
Общий слой доступа к открытым данным Израиля.

Источники:
  * data.gov.il — портал на CKAN. Метаданные через package_show, сами строки через
    datastore_search. Прямые CSV-ссылки на aws-e.data.gov.il отдают 403 из песочницы,
    поэтому ходим только через datastore API.
  * nadlan.gov.il (реестр сделок налоговой) — закрыт reCAPTCHA Enterprise, НЕ используется.
  * data.cbs.gov.il (ЦСБ) — из песочницы не отдаёт JSON, НЕ используется.

Важная деталь: название города пишется по-разному в разных датасетах —
«קרית גת» и «קריית גת». Нормализация обязательна, иначе получим ноль строк.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

CKAN = "https://data.gov.il/api/3/action"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json",
}

# Датасеты, за которыми следим. id ресурса стабильнее, чем имя датасета.
DATASETS = {
    "mechir_lamishtaken": {
        "ru": "Цена за метр по программе «Дира ба-анаха»",
        "package": "mechir-lamishtaken",
        "resource_id": "7c8255d0-49ef-49db-8904-4cf917586031",
        "resource_name": "מעקב אחר הגרלות דירה בהנחה",
    },
    "urban_renewal": {
        "ru": "Комплексы городской реновации (пинуй-бинуй / ТАМА 38)",
        "package": "urban_renewal",
        "resource_id": "f65a0daf-f737-49c5-9424-d378d52104f5",
        "resource_name": "מתחמי התחדשות עירונית",
    },
    "construction_progress": {
        "ru": "Ход строительства (проекты минстроя)",
        "package": "hitkadmuthabnia",
        "resource_id": "1ec45809-5927-430a-9b30-77f77f528ce3",
        "resource_name": "דיווחי התקדמות הבניה - בניה רוויה",
    },
    "planning_inventory": {
        "ru": "Планировочный запас под жильё (РМАИ)",
        "package": "housingunits-planning-inventory",
        "resource_id": "99aad98f-2b54-4eea-834d-650b56389bf3",
        "resource_name": "מלאי תכנוני",
    },
    "public_housing_vacant": {
        "ru": "Свободные квартиры социального жилья",
        "package": "diurtziburi",
        "resource_id": "c3a68837-9b7a-4ee7-bd92-130678dc8ae3",
        "resource_name": "דירות פנויות בדיור הציבורי",
    },
}

# Как в каждом датасете называется поле города
CITY_FIELD = {
    "mechir_lamishtaken": "LamasName",
    "urban_renewal": "Yeshuv",
    "construction_progress": "YESHUV_LAMAS",
    "planning_inventory": "יישוב",
    "public_housing_vacant": None,
}

# Все известные варианты написания — ключом нормализованная форма
CITY_ALIASES = {
    "קריית גת": "קרית גת",
    "קרית-גת": "קרית גת",
    "קריית-גת": "קרית גת",
    "קרייתאת": "קרית גת",
}

# Символьный код города по реестру ЛАМАС (надёжнее названия).
# ВНИМАНИЕ, это грабли: 2600 — это Эйлат, а Кирьят-Гат = 2630.
# Ошибка здесь молча подмешивает в выборку чужой город (так и вышло при первой попытке:
# «91 записей по Кирьят-Гату» на самом деле были 52 строки Кирьят-Гата + 39 строк Эйлата).
CITY_LAMAS_CODE = {"קרית גת": 2630}


def verify_city_codes(resource_id: str, city_field: str, code_field: str = "LamasCode") -> dict:
    """Самопроверка: для каждого кода собрать множество названий городов.

    Если код соответствует нескольким названиям — карта кодов неверна, и это надо
    увидеть сразу, а не через месяц в красивом отчёте.
    """
    rows = datastore_rows(resource_id)
    mapping: dict[str, set] = {}
    for r in rows:
        code = str(r.get(code_field, "")).strip()
        name = norm_city(r.get(city_field))
        if code:
            mapping.setdefault(code, set()).add(name)
    return {k: sorted(v) for k, v in mapping.items()}


def _http(url: str, retries: int = 3, timeout: int = 120) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"HTTP сбой для {url}: {last}")


def ckan_action(action: str, **params) -> dict:
    """Вызов произвольного метода CKAN API."""
    url = f"{CKAN}/{action}?" + urllib.parse.urlencode(params)
    return json.loads(_http(url).decode("utf-8"))


def package_meta(package_id: str) -> dict:
    """Метаданные датасета: заголовок, версия, время изменения, список ресурсов."""
    return ckan_action("package_show", id=package_id)["result"]


def datastore_rows(resource_id: str, page: int = 1000, max_rows: int | None = None) -> list[dict]:
    """Постранично выкачать все строки ресурса из datastore."""
    rows: list[dict] = []
    offset = 0
    while True:
        res = ckan_action(
            "datastore_search", resource_id=resource_id, limit=page, offset=offset
        )["result"]
        batch = res.get("records", [])
        if not batch:
            break
        rows.extend(batch)
        total = res.get("total") or 0
        if max_rows and len(rows) >= max_rows:
            return rows[:max_rows]
        if len(rows) >= total:
            break
        offset += page
    return rows


def norm_city(value) -> str:
    """Привести название города к одной форме (обрезка пробелов + алиасы)."""
    if value is None:
        return ""
    v = " ".join(str(value).split())
    return CITY_ALIASES.get(v, v)


def is_city(row: dict, dataset_key: str, city: str) -> bool:
    """Проверить, относится ли строка к городу. Сначала по коду ЛАМАС, потом по названию."""
    code = CITY_LAMAS_CODE.get(city)
    if code is not None and str(row.get("LamasCode", "")).strip() == str(code):
        return True
    field = CITY_FIELD.get(dataset_key)
    if not field:
        return False
    return norm_city(row.get(field)) == norm_city(city)


def num(value, default=0.0):
    """Аккуратный парсинг чисел: '12,206.00', '1260', '-', None."""
    if value is None:
        return default
    s = str(value).strip().replace(",", "")
    if s in ("", "-", "None"):
        return default
    try:
        return float(s)
    except ValueError:
        return default
