"""
Реестр сделок с недвижимостью Израиля — доступ через over.org.il.

Источник первоначальный: nadlan.taxes.gov.il (система «Карман» налогового управления).
Сам сайт закрыт reCAPTCHA Enterprise, но over.org.il держит полную копию — 3,84 млн сделок
с 1998 года по сентябрь 2026 — и отдаёт её через SQL-движок без ключей.

Три вещи, которые надо знать, прежде чем верить цифрам (оговорки отдаёт сам источник):

1. Все колонки в SQL-движке — ТЕКСТ. Любой агрегат требует явного приведения типа:
   avg(deal_amount::numeric), а не avg(deal_amount). Среднего по тексту не существует.
2. Колонки _id нет, поэтому курсорная пагинация невозможна — только LIMIT/OFFSET.
   Потолок ответа — 1000 строк, дальше ставится truncated=true и строки молча обрезаются.
3. Сумма сделки — это заявленная налоговому управлению величина, а не проверенная рыночная
   цена. Одна строка может быть и квартирой, и целым зданием. Поэтому везде медиана, не среднее.

Ключевая содержательная деталь: year_built — это год постройки ДЛЯ ГОТОВОГО жилья
и ПЛАНОВЫЙ ГОД СДАЧИ для покупки на этапе котлована. Поэтому значения бывают 2029 и 2030.
Новая квартира = year_built >= год сделки (включая ещё не построенные),
а не «построена недавно». Кто этого не понимает, посчитает новые квартиры вторичкой.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
    "Accept": "application/json",
}

OVER = "https://www.over.org.il"
DATASET_ID = "fd06f5ae-8a4f-4120-b275-8a514ad23499"
TABLE = "append_taxes_nadlan_full_f41fb496_fd06f5ae"
PAGE = 1000  # жёсткий потолок движка; больше — тихо обрежется

# Жильё. «מגורים» сюда НЕ входит: по данным источника, в 68% таких строк нет площади
# и в 99% нет числа комнат — это земля и права, а не квартиры.
APT_NATURES = [
    "דירה בבית קומות",   # квартира в многоквартирном доме — основной тип
    "דירת גן",           # квартира с садом
    "דירת גג",           # пентхаус
    "קוטג' חד משפחתי",   # коттедж на одну семью
    "קוטג' דו משפחתי",   # на две семьи
    "קוטג' טורי",        # таунхаус
    "בית בודד",          # отдельный дом
]

# Коды населённых пунктов берём из самого реестра, а не из головы.
# Первая версия этого файла содержала четыре неверных кода из двенадцати —
# ровно та ошибка, из-за которой в выборку по Кирьят-Гату однажды подмешался Эйлат.
CITY_NAME_ALIASES = {
    "קרית גת": "קריית גת",   # в реестре сделок город пишется с йудом
    "קרית מלאכי": "קריית מלאכי",
    "באר שבע": "באר שבע",
}

_SETTLEMENT_CACHE: list[dict] | None = None
_SETTLEMENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "settlements.json")
# Справочник кодов населённых пунктов меняется редко, а без него не работает ничего:
# resolve_city, geo, market, renewal_geo. Один неудачный сетевой запрос раньше ронял
# весь запуск с сообщением «населённый пункт не найден», которое выглядело как
# отсутствие города в реестре. Поэтому справочник кэшируется на диск и живёт 14 дней.
_SETTLEMENTS_TTL_DAYS = 14


def settlements(max_age_days: int = _SETTLEMENTS_TTL_DAYS) -> list[dict]:
    """Справочник населённых пунктов реестра: название, код, число сделок, последняя сделка."""
    global _SETTLEMENT_CACHE
    if _SETTLEMENT_CACHE is not None:
        return _SETTLEMENT_CACHE

    fresh = None
    if os.path.exists(_SETTLEMENTS_FILE):
        age = dt.datetime.now(dt.timezone.utc).timestamp() - os.path.getmtime(_SETTLEMENTS_FILE)
        if age < max_age_days * 86400:
            try:
                with open(_SETTLEMENTS_FILE, encoding="utf-8") as fh:
                    fresh = json.load(fh)
            except Exception:  # noqa: BLE001
                fresh = None
    if fresh:
        _SETTLEMENT_CACHE = fresh
        return fresh

    data = None
    try:
        data = _get(f"{OVER}/api/deals/settlements")["data"]
    except Exception:  # noqa: BLE001
        data = None
    # Ответ обязан быть непустым списком с кодами. Раньше сюда попадал и пустой
    # список, и тело ошибки с HTTP 200 — и тогда resolve_city сообщал
    # «населённый пункт не найден» для города, который в реестре есть.
    if not (isinstance(data, list) and len(data) > 500
            and all(isinstance(x, dict) and x.get("settlement") for x in data[:5])):
        data = None
    if data:
        os.makedirs(os.path.dirname(_SETTLEMENTS_FILE), exist_ok=True)
        with open(_SETTLEMENTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        _SETTLEMENT_CACHE = data
        return data
    # сеть не ответила — берём что есть на диске, даже просроченное: коды городов
    # не меняются, а «город не найден» из-за недоступности сети хуже устаревшего кэша.
    if os.path.exists(_SETTLEMENTS_FILE):
        with open(_SETTLEMENTS_FILE, encoding="utf-8") as fh:
            _SETTLEMENT_CACHE = json.load(fh)
        return _SETTLEMENT_CACHE
    raise RuntimeError("справочник населённых пунктов недоступен и кэша нет")


def _norm(value) -> str:
    """Привести название к сравниваемой форме: без дефисов по краям и лишних пробелов."""
    return " ".join(str(value or "").replace("-", " ").split())


def resolve_city(name: str) -> tuple[str, int, int]:
    """Вернуть (имя в реестре, код ЛАМАС, число сделок).

    Названия в реестре сделок и в датасетах минстроя пишутся по-разному
    («קריית גת» против «קרית גת»), поэтому сначала пробуем точное совпадение
    по нормализованному имени и по алиасам, потом — поиск по подстроке.
    Если совпадений несколько, берём то, где сделок больше: так отсевается
    одноимённый кибуц или малый посёлок.
    """
    key = _norm(name)
    variants = {key}
    alias = CITY_NAME_ALIASES.get(key)
    if alias:
        variants.add(_norm(alias))
    for v in list(variants):
        variants.add(_norm(CITY_NAME_ALIASES.get(v, v)))

    rows = settlements()
    exact = [s for s in rows if _norm(s.get("settlement")) in variants]
    hits = exact or [
        s for s in rows if any(v and v in _norm(s.get("settlement")) for v in variants)
    ]
    if not hits:
        raise KeyError(
            f"населённый пункт «{name}» не найден в реестре сделок "
            f"(в справочнике {len(rows)} населённых пунктов; "
            "если список пуст или короткий — это сбой загрузки справочника, а не отсутствие города)"
        )

    best = max(hits, key=lambda s: s.get("deals") or 0)
    code = best.get("resolved_code") or best.get("settlement_code")
    if code is None:
        raise KeyError(f"у «{name}» в реестре нет кода населённого пункта")
    return str(best.get("settlement") or "").strip(), int(code), int(best.get("deals") or 0)


def _get(url: str, timeout: int = 300, retries: int = 3) -> dict:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {e.code} на {url[:120]}: {body}") from None
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"сбой после {retries} попыток для {url[:120]}: {last}")


def sql(query: str, timeout: int = 300) -> list[dict]:
    """Выполнить SQL и вернуть записи. Следит за флагом truncated — это молчаливая обрезка."""
    url = f"{OVER}/api/append/{DATASET_ID}/datastore_search_sql?" + urllib.parse.urlencode({"sql": query})
    res = _get(url, timeout=timeout)["result"]
    if res.get("truncated"):
        raise RuntimeError(
            f"ответ обрезан на {len(res.get('records', []))} строках — уменьши LIMIT или добавь OFFSET"
        )
    return res.get("records", [])


def stats() -> dict:
    """Сводка по всему реестру: число сделок, период, сколько населённых пунктов."""
    return _get(f"{OVER}/api/deals/stats")


def natures(code: int | str) -> list[dict]:
    return sql(
        f"SELECT deal_nature, sum(1) AS n FROM {TABLE} "
        f"WHERE settlement_code='{code}' GROUP BY deal_nature ORDER BY n DESC"
    )


def in_list(values: list[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def fetch_apartment_deals(code: int | str, since_year: int = 1998) -> list[dict]:
    """Вытащить ВСЕ жилищные сделки города постранично.

    Пагинация через LIMIT/OFFSET, потому что _id в таблице нет. Сортировка обязательна —
    иначе порядок между запросами не гарантирован и страницы будут пересекаться.
    """
    base = (
        f"SELECT deal_date, deal_amount, declared_amount, deal_nature, asset_area, room_num, "
        f"year_built, portion, gush, chelka, sub_chelka "
        f"FROM {TABLE} "
        f"WHERE settlement_code='{code}' "
        f"AND deal_nature IN ({in_list(APT_NATURES)}) "
        f"AND substr(deal_date,7,4) >= '{since_year}' "
    )
    order = "ORDER BY deal_date, gush, chelka, sub_chelka, deal_amount"

    total = sql(f"SELECT count(*) AS n FROM ({base}) AS q")[0]["n"]
    rows: list[dict] = []
    offset = 0
    while offset < total:
        batch = sql(f"{base} {order} LIMIT {PAGE} OFFSET {offset}")
        if not batch:
            break
        rows.extend(batch)
        offset += PAGE
    return rows


def year_of(deal_date: str) -> int | None:
    """Даты в реестре хранятся текстом в формате DD/MM/YYYY."""
    s = str(deal_date or "").strip()
    if len(s) < 10 or s[2] != "/" or s[5] != "/":
        return None
    try:
        return int(s[6:10])
    except ValueError:
        return None


def to_int(value) -> int | None:
    s = str(value or "").strip().replace(",", "")
    if not s or s in ("-", "0"):
        return None if s == "-" else (0 if s == "0" else None)
    try:
        return int(float(s))
    except ValueError:
        return None


def to_float(value) -> float | None:
    s = str(value or "").strip().replace(",", "")
    if not s or s in ("-",):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def median(values) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
