"""
Адреса: превращает гуш/хелька из реестра сделок в улицу, район и точку на карте.

Зачем это нужно. Реестр сделок налогового управления не публикует адреса — только
гуш и хелька (номер земельного участка). Поэтому «цены по районам» без дополнительного
слоя выглядят как «2812/349 — ₪22 744/м²», что человеку ни о чём не говорит.

Откуда берутся адреса. У over.org.il есть отдельный открытый слой «נדל״ן לעם»,
который скрещивает три источника:

  * кадастровые участки (1 097 775 шт.) — гуш/хелька → координаты центра и статус;
  * газовый реестр недвижимости (gazetteer, 758 864 участка) — гуш/хелька → УЛИЦА;
  * список адресов почты (728 548 шт.) — улица + дом → точка, индекс, 76% с координатами.

Плюс слой статистических районов ЦСБ (division 2022) с населением и социо-экономическим
кластером, и почтовые индексы.

Честное ограничение, которое отдаёт сам источник:
«Газовый реестр связывает гуш-хелька только с улицей, не с номером дома».
То есть точность — улица (и часто одна улица на участок), но не «дом 8, квартира 12».
И второе: «сделки привязаны только по гуш и хелька, под-гуш реестр не публикует,
поэтому на участках с одинаковым номером одна и та же сделка может показаться дважды».

Доступ. POST /api/tables/sql и /api/tables/export.csv закрыты авторизацией,
поэтому здесь используется публичный GET /api/nadlan/parcel/{gush}/{helka} —
по одному запросу на участок. Участков в городе сотни, запрос дешёвый,
результат кэшируется в analysis/geo_<код>.json и добирается при повторном запуске.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import deals
import deals_agg as da

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "analysis")
BASE = "https://www.over.org.il"

# Поля ответа, которые реально нужны. deals=false отсекает самый тяжёлый блок:
# на участке башни может быть до 1850 сделок, и тянуть их ради улицы незачем.
FIELDS = "identity,streets,point,zip,stat_area"

# Слой /api/nadlan не троттлит так жёстко, как SQL-движок реестра сделок
# (там 20 запросов в минуту): 12 запросов подряд прошли по 0,5 с без единого 429.
# Держим небольшой запас и обработку 429 на всякий случай.
GAP = 0.13


# --------------------------------------------------------------------------- #
#  1. Участки города из реестра сделок
# --------------------------------------------------------------------------- #

def q_parcels(code: int, since: int, page: int = 1000) -> list[dict]:
    """Все гуш/хелька города, по которым были сделки с `since`, со своей статистикой.

    Медианы считаются на сервере, наружу приходят строки участков, а не сделки.
    Но участков в городе больше тысячи (в Кирьят-Гате 1627 с 2012 года), а движок
    молча обрезает ответ на 1000 строк, — поэтому здесь настоящая постраничность
    с детерминированным ORDER BY. Без OFFSET часть участков просто потерялась бы.
    """
    seg = da.SEGMENT_CASE
    out: list[dict] = []
    offset = 0
    while True:
        rows = da.sql_slow(f"""
SELECT gush AS гуш, chelka AS хелька, count(*) AS сделок,
  count(*) FILTER (WHERE {seg}<>'вторичка') AS новых,
  count(*) FILTER (WHERE {seg}<>'вторичка' AND substr(deal_date,7,4)>='{max(since, 2024)}') AS новых_свежих,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM})::int AS м2_все,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}<>'вторичка')::int AS м2_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)::int AS медиана_цены,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric)::int AS медиана_площади,
  max(substr(deal_date,7,4)) AS последняя_сделка
FROM {deals.TABLE}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
  AND gush ~ '^[0-9]+$' AND chelka ~ '^[0-9]+$'
GROUP BY gush, chelka
ORDER BY count(*) DESC, gush, chelka
LIMIT {page} OFFSET {offset}""")
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


# --------------------------------------------------------------------------- #
#  2. Участок → улица, точка, индекс, статрайон
# --------------------------------------------------------------------------- #

def parcel_info(gush, helka, tries: int = 3) -> dict | None:
    """Один участок. Возвращает плоский словарь или None, если участка нет в слое."""
    q = urllib.parse.urlencode({"fields": FIELDS, "deals": "false"})
    url = f"{BASE}/api/nadlan/parcel/{gush}/{helka}?{q}"
    last = None
    for i in range(tries):
        time.sleep(GAP)
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:  # «נסו שוב בעוד N שניות» — ждём дольше обычного
                time.sleep(25 + 8 * i)
                continue
            if e.code == 404:
                return None
            time.sleep(4 * (i + 1))
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]
            time.sleep(4 * (i + 1))
    else:
        return {"ошибка": last or "не удалось"}

    data = (payload.get("data") or [None])[0]
    if not data:
        return None
    idn = data.get("identity") or {}
    point = idn.get("point") or {}
    stat = data.get("stat_area") or {}
    socio = stat.get("socio") or {}
    return {
        "улицы": [s for s in (idn.get("streets") or []) if s],
        "lat": point.get("lat"),
        "lon": point.get("lon"),
        "zip5": [z for z in (idn.get("zip5") or []) if z],
        "регион": (idn.get("region") or {}).get("region"),
        "округ_минвн": (idn.get("region") or {}).get("county"),
        "статрайон": stat.get("code"),
        "население_района": stat.get("population"),
        "год_населения": stat.get("population_year"),
        "функция_района": stat.get("main_function"),
        "кластер_соцэк": socio.get("eshkol"),
        "год_кластера": socio.get("index_year"),
    }


def cache_path(code: int) -> str:
    return os.path.join(OUT_DIR, f"geo_{code}.json")


def load_cache(code: int) -> dict:
    p = cache_path(code)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return {"код": code, "участки": {}, "обновлено": None}


def resolve_city(code: int, since: int, limit: int | None = None, verbose: bool = True) -> dict:
    """Добирает улицы для всех участков города. Кэш возобновляемый: прерванный
    запуск продолжает с того же места, уже полученные участки не перезапрашиваются."""
    os.makedirs(OUT_DIR, exist_ok=True)
    cache = load_cache(code)
    parcels = q_parcels(code, since)
    if limit:
        parcels = parcels[:limit]
    todo = [p for p in parcels if f"{p['гуш']}/{p['хелька']}" not in cache["участки"]]
    if verbose:
        print(f"участков со сделками: {len(parcels)}, в кэше: {len(parcels) - len(todo)}, "
              f"добрать: {len(todo)}", flush=True)

    for n, p in enumerate(todo, 1):
        key = f"{p['гуш']}/{p['хелька']}"
        info = parcel_info(p["гуш"], p["хелька"])
        cache["участки"][key] = info if info is not None else {"улицы": [], "нет_в_слое": True}
        if verbose and (n % 25 == 0 or n == len(todo)):
            cache["обновлено"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            with open(cache_path(code), "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False, indent=1)
            с_улицей = sum(1 for v in cache["участки"].values() if v.get("улицы"))
            print(f"  {n}/{len(todo)} · улиц найдено у {с_улицей} участков", flush=True)

    cache["обновлено"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(cache_path(code), "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1)
    return cache


# --------------------------------------------------------------------------- #
#  3. Сводка: улицы и статистические районы
# --------------------------------------------------------------------------- #

def _med_weighted(items: list[tuple[float, int]]) -> int | None:
    """Медиана, взвешенная по числу сделок. Точную медиану улицы из медиан участков
    не восстановить — для этого пришлось бы тянуть все сделки города построчно,
    а это десятки запросов против лимита частоты. Взвешенная медиана отличается
    от точной на единицы процентов при однородном участке и честно помечается."""
    pts = sorted((v, w) for v, w in items if v is not None and w > 0)
    if not pts:
        return None
    total = sum(w for _, w in pts)
    half = total / 2
    acc = 0
    for v, w in pts:
        acc += w
        if acc >= half:
            return int(round(v))
    return int(round(pts[-1][0]))


def _center(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    pts = [(la, lo) for la, lo in points if la and lo]
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние по поверхности Земли, км. Нужно, чтобы сказать
    «эта улица в 2,4 км от центра города», а не просто показать координаты."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def summarize(code: int, since: int, min_street_deals: int = 15) -> dict:
    """Цены по улицам и по статистическим районам ЦСБ."""
    parcels = q_parcels(code, since)
    cache = load_cache(code)
    lookup = cache.get("участки", {})

    city_point = _center([(v.get("lat"), v.get("lon")) for v in lookup.values()])

    by_street: dict[str, list[dict]] = collections.defaultdict(list)
    by_area: dict[str, list[dict]] = collections.defaultdict(list)
    unknown = []
    for p in parcels:
        key = f"{p['гуш']}/{p['хелька']}"
        info = lookup.get(key) or {}
        n = int(p["сделок"])
        streets = info.get("улицы") or []
        if not streets:
            unknown.append({**p, "точка": bool(info.get("lat"))})
        # участок может стоять на двух улицах (угловой, или комплекс на две стороны):
        # сделка приписывается обеим, поэтому в сумме по улицам сделок больше, чем всего.
        for s in streets:
            by_street[s].append({**p, "_n": n})
        area = info.get("статрайон")
        if area:
            by_area[str(area)].append({**p, "_n": n, "_info": info})

    streets = []
    for name, rows in by_street.items():
        deals_all = sum(r["_n"] for r in rows)
        deals_new = sum(int(r["новых"]) for r in rows)
        # Спорные участки: реестр сделок не публикует под-гуш, и один кадастровый
        # участок может стоять на двух-трёх улицах (угловой, или комплекс на две стороны).
        # Один такой участок на 275 сделок способен задать медиану всей улице — так
        # «נחל ירקון» из четырёх участков получила ровно ту же медиану, что и
        # «שד לכיש» из двадцати пяти. Поэтому цена улицы считается дважды:
        # со спорными участками и без них.
        clean = [r for r in rows if len((lookup.get(f"{r['гуш']}/{r['хелька']}") or {}).get("улицы") or []) < 2]
        deals_clean = sum(r["_n"] for r in clean)
        pts = [((lookup.get(f"{r['гуш']}/{r['хелька']}") or {}).get("lat"),
                (lookup.get(f"{r['гуш']}/{r['хелька']}") or {}).get("lon")) for r in rows]
        c = _center(pts)
        streets.append({
            "улица": name,
            "сделок": deals_all,
            "новых_сделок": deals_new,
            "участков": len({f"{r['гуш']}/{r['хелька']}" for r in rows}),
            "м2_все": _med_weighted([(r["м2_все"], r["_n"]) for r in rows]),
            "м2_новые": _med_weighted([(r["м2_новые"], int(r["новых"])) for r in rows]),
            "сделок_чистых": deals_clean,
            "участков_спорных": len(rows) - len(clean),
            "м2_чистое": _med_weighted([(r["м2_все"], r["_n"]) for r in clean]),
            "м2_новые_чистое": _med_weighted([(r["м2_новые"], int(r["новых"])) for r in clean]),
            "медиана_цены": _med_weighted([(r["медиана_цены"], r["_n"]) for r in rows]),
            "медиана_цены_чистая": _med_weighted([(r["медиана_цены"], r["_n"]) for r in clean]),
            "медиана_площади": _med_weighted([(r["медиана_площади"], r["_n"]) for r in rows]),
            "последняя_сделка": max((r["последняя_сделка"] for r in rows), default=None),
            # «00000» — это не индекс, а заглушка слоя (394 участка города из 1627).
            # Настоящие индексы Кирьят-Гата начинаются с 82.
            "индекс": sorted({z for r in rows
                              for z in ((lookup.get(f"{r['гуш']}/{r['хелька']}") or {}).get("zip5") or [])
                              if z.isdigit() and not set(z) == {"0"}}),
            # Статрайон ЦСБ берём по большинству участков улицы: у улицы, лежащей
            # на границе двух районов, участки могут относиться к разным.
            "статрайон": collections.Counter(
                (lookup.get(f"{r['гуш']}/{r['хелька']}") or {}).get("статрайон")
                for r in rows if (lookup.get(f"{r['гуш']}/{r['хелька']}") or {}).get("статрайон")
            ).most_common(1)[0][0] if any((lookup.get(f"{r['гуш']}/{r['хелька']}") or {}).get("статрайон") for r in rows) else None,
            "lat": c[0] if c else None,
            "lon": c[1] if c else None,
            "км_от_центра": round(_km(c, city_point), 2) if (c and city_point) else None,
        })
    streets.sort(key=lambda r: (-r["сделок_чистых"], -r["сделок"]))

    areas = []
    for area, rows in by_area.items():
        info = rows[0]["_info"]
        areas.append({
            "статрайон": area,
            "сделок": sum(r["_n"] for r in rows),
            "новых_сделок": sum(int(r["новых"]) for r in rows),
            "м2_все": _med_weighted([(r["м2_все"], r["_n"]) for r in rows]),
            "м2_новые": _med_weighted([(r["м2_новые"], int(r["новых"])) for r in rows]),
            "население": info.get("население_района"),
            "функция": info.get("функция_района"),
            "кластер_соцэк": info.get("кластер_соцэк"),
            "улиц": sorted({r["улица"] for row in rows
                            for r in [{"улица": u} for u in
                                      ((lookup.get(f"{row['гуш']}/{row['хелька']}") or {}).get("улицы") or [])]}),
        })
    areas.sort(key=lambda r: -r["сделок"])

    with_street = sum(int(p["сделок"]) for p in parcels
                      if (lookup.get(f"{p['гуш']}/{p['хелька']}") or {}).get("улицы"))
    total = sum(int(p["сделок"]) for p in parcels)
    return {
        "центр_города": {"lat": city_point[0], "lon": city_point[1]} if city_point else None,
        "покрытие": {
            "участков_всего": len(parcels),
            "участков_с_улицей": sum(1 for p in parcels
                                    if (lookup.get(f"{p['гуш']}/{p['хелька']}") or {}).get("улицы")),
            "сделок_всего": total,
            "сделок_с_улицей": with_street,
            "доля_сделок_pct": round(100 * with_street / total, 1) if total else 0.0,
        },
        "улицы": [s for s in streets if s["сделок_чистых"] >= min_street_deals],
        "улицы_мелкие": [s for s in streets if s["сделок_чистых"] < min_street_deals],
        "статрайоны": areas,
        "без_улицы": unknown[:60],
        "оговорки": [
            "Реестр сделок публикует только гуш и хелька — номер квартиры и номер дома в нём отсутствуют.",
            "Газовый реестр недвижимости связывает гуш-хелька с улицей, но не с номером дома, "
            "поэтому точность здесь — улица.",
            "Угловой участок может стоять на двух-трёх улицах: сделка приписывается каждой, "
            "поэтому сумма сделок по улицам больше общего числа. Один такой участок способен "
            "задать медиану всей улице, поэтому для каждой улицы есть и «чистая» цена — "
            "только по участкам, стоящим на одной улице.",
            "Медиана улицы собрана из медиан её участков, взвешенных по числу сделок, — "
            "это не точная медиана всех сделок улицы.",
            "Под-гуш (תת-חלקה) реестр сделок не публикует, поэтому на участках с одинаковым номером "
            "одна и та же сделка может учитываться дважды.",
        ],
    }


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="קרית גת")
    ap.add_argument("--since", type=int, default=2012)
    ap.add_argument("--limit", type=int, default=None, help="сколько участков обработать (отладка)")
    ap.add_argument("--parcel", help="один участок, например 2812/349")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.parcel:
        gush, helka = args.parcel.split("/")
        print(json.dumps(parcel_info(gush, helka), ensure_ascii=False, indent=1))
        return 0

    name, code, total = deals.resolve_city(args.city)
    print(f"город: {name} · код {code} · сделок в реестре {total}")
    resolve_city(code, args.since, limit=args.limit)
    res = summarize(code, args.since)
    path = args.out or os.path.join(OUT_DIR, f"geo_summary_{code}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    cov = res["покрытие"]
    print(f"\nпокрытие: {cov['участков_с_улицей']}/{cov['участков_всего']} участков, "
          f"{cov['доля_сделок_pct']}% сделок")
    print(f"\n{'улица':<28} {'сделок':>7} {'новых':>6} {'₪/м² все':>9} {'₪/м² новые':>10} "
          f"{'медиана':>10} {'площадь':>7} {'км':>5} {'индекс'}")
    for s in res["улицы"]:
        print(f"{s['улица']:<28} {s['сделок']:>7} {s['новых_сделок']:>6} "
              f"{s['м2_все'] or '—':>9} {s['м2_новые'] or '—':>10} {s['медиана_цены'] or '—':>10} "
              f"{s['медиана_площади'] or '—':>7} {s['км_от_центра'] or '—':>5} "
              f"{','.join(s['индекс']) or '—'}")
    print(f"\nстатистические районы ЦСБ:")
    for a in res["статрайоны"]:
        print(f"  район {a['статрайон']}: {a['сделок']} сделок · ₪{a['м2_все']}/м² · "
              f"новых ₪{a['м2_новые']}/м² · население {a['население']} · кластер {a['кластер_соцэк']}")
    print(f"\nсохранено: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
