"""
Агрегатный доступ к реестру сделок: все медианы считаются на сервере.

Почему так. Постраничная выгрузка всех строк города — это десятки запросов, а движок
over.org.il ограничивает частоту: 20 запросов в минуту на действие, дальше HTTP 429
с текстом «נסו שוב בעוד N שניות». При этом percentile_cont(0.5) WITHIN GROUP там работает
(обязательно с приведением типа — все колонки текстовые), поэтому годовой разрез
по городу со всеми медианами получается ОДНИМ запросом на ~30 строк результата.

Важная деталь про percentile_cont: без приведения к int результат приходит
в экспоненциальной записи текстом («9.5E+5»). Поэтому везде ::int.

Сегментация «новая / вторичка» основана на том, что year_built для покупки на этапе
котлована хранит ПЛАНОВЫЙ год сдачи (в данных Кирьят-Гата есть 2029 и 2030).
Новая = year_built >= год сделки.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

import deals

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "analysis")

MIN_AMOUNT = 150_000
MIN_AREA = 20
MAX_AREA = 400

T = deals.TABLE

# Цена за м² с учётом проданной доли. greatest(...,0.001) защищает от деления на ноль:
# доля округлена до трёх знаков, и у части строк это 0.000.
PPSQM = "deal_amount::numeric / (asset_area::numeric * greatest(portion::numeric, 0.001))"

# Шаблон, а не f-строка: {code} подставляется позже через .format(),
# поэтому все фигурные скобки регулярных выражений удвоены.
BASE_WHERE = (
    "settlement_code = '{code}' AND "
    f"deal_nature IN ({deals.in_list(deals.APT_NATURES)}) AND "
    f"deal_amount ~ '^[0-9]+$' AND deal_amount::numeric >= {MIN_AMOUNT} AND "
    "asset_area ~ '^[0-9]+([.][0-9]+)?$' AND "
    f"asset_area::numeric BETWEEN {MIN_AREA} AND {MAX_AREA} AND "
    "portion ~ '^[0-9]+([.][0-9]+)?$' AND "
    "year_built ~ '^[0-9]{{4}}$' AND "
    f"({PPSQM}) BETWEEN 2000 AND 150000"
)

SEGMENT_CASE = (
    "CASE WHEN year_built::int > substr(deal_date,7,4)::int THEN 'новая_котлован' "
    "WHEN year_built::int >= substr(deal_date,7,4)::int - 2 THEN 'новая_свежая' "
    "ELSE 'вторичка' END"
)

MEDIAN_COLS = """
  count(*) AS сделок,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)::int AS медиана_цены,
  percentile_cont(0.25) WITHIN GROUP (ORDER BY deal_amount::numeric)::int AS p25_цены,
  percentile_cont(0.75) WITHIN GROUP (ORDER BY deal_amount::numeric)::int AS p75_цены,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {ppsqm})::int AS медиана_м2,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric)::int AS медиана_площади,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY room_num::numeric)::numeric AS медиана_комнат
"""


class RateLimited(RuntimeError):
    pass


def sql_slow(query: str, min_gap: float = 3.4, tries: int = 4) -> list[dict]:
    """SQL с соблюдением лимита 20 запросов в минуту и честным ожиданием после 429."""
    last = getattr(sql_slow, "_last", 0.0)
    wait = min_gap - (time.time() - last)
    if wait > 0:
        time.sleep(wait)

    for attempt in range(tries):
        sql_slow._last = time.time()
        try:
            return deals.sql(query, timeout=300)
        except RuntimeError as e:
            msg = str(e)
            m = re.search(r"HTTP 429.*?בעוד (\d+) שניות", msg, re.S)
            if m is None and "429" in msg:
                m = re.search(r"(\d+) per 1 minute", msg)
                wait_s = 25
            elif m:
                wait_s = int(m.group(1)) + 2
            else:
                raise
            wait_s = min(max(wait_s, 5), 90)
            print(f"    ⏳ лимит частоты, жду {wait_s} с (попытка {attempt + 1}/{tries})", flush=True)
            time.sleep(wait_s)
    raise RateLimited("лимит частоты исчерпан, попробуйте позже")


def _median_block(ppsqm: str = PPSQM) -> str:
    return MEDIAN_COLS.format(ppsqm=ppsqm)


def year_series(code: int | str, since: int) -> list[dict]:
    """Один запрос → годовой разрез со всеми медианами и раскладкой по сегментам."""
    seg = SEGMENT_CASE
    q = f"""
SELECT substr(deal_date,7,4) AS год,
  {_median_block()},
  count(*) FILTER (WHERE {seg} IN ('новая_котлован','новая_свежая')) AS новых_сделок,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY {PPSQM}) FILTER (WHERE {seg} IN ('новая_котлован','новая_свежая'))::int AS м2_новые,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY deal_amount::numeric) FILTER (WHERE {seg} IN ('новая_котлован','новая_свежая'))::int AS цена_новые,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY {PPSQM}) FILTER (WHERE {seg} = 'новая_котлован')::int AS м2_котлован,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY {PPSQM}) FILTER (WHERE {seg} = 'вторичка')::int AS м2_вторичка,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY deal_amount::numeric) FILTER (WHERE {seg} = 'вторичка')::int AS цена_вторичка,
  percentile_cont(0.5) WITHIN GROUP (
    ORDER BY year_built::numeric) FILTER (WHERE {seg} IN ('новая_котлован','новая_свежая'))::int AS медиана_года_сдачи
FROM {T}
WHERE {BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
GROUP BY substr(deal_date,7,4)
ORDER BY год"""
    return sql_slow(q)


def totals(code: int | str, since: int) -> dict:
    """Итоговые цифры по сегментам за весь период — один запрос."""
    seg = SEGMENT_CASE
    q = f"""
SELECT {seg} AS сегмент, {_median_block()}
FROM {T}
WHERE {BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
GROUP BY {seg} ORDER BY сделок DESC"""
    return sql_slow(q)


def by_rooms(code: int | str, since: int, segment_new: bool = True) -> list[dict]:
    """Срез по числу комнат — то, как люди реально сравнивают квартиры."""
    filt = (
        f"AND {SEGMENT_CASE} IN ('новая_котлован','новая_свежая')" if segment_new else ""
    )
    q = f"""
SELECT room_num::int AS комнат, {_median_block()}
FROM {T}
WHERE {BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
  AND room_num ~ '^[0-9]+$' AND room_num::int BETWEEN 1 AND 8 {filt}
GROUP BY room_num::int ORDER BY комнат"""
    return sql_slow(q)


def by_area(code: int | str, since: int, min_deals: int = 25) -> list[dict]:
    """Разрез по кадастровому делению (гуш/хелька) — замена районам, которых в реестре нет."""
    q = f"""
SELECT gush || '/' || chelka AS участок, count(*) AS сделок,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)::int AS медиана_цены,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {PPSQM})::int AS медиана_м2,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric)::int AS медиана_площади,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY year_built::numeric)::int AS медиана_года_постройки,
  max(substr(deal_date,7,4)) AS последняя_сделка,
  count(*) FILTER (WHERE {SEGMENT_CASE} IN ('новая_котлован','новая_свежая')) AS новых
FROM {T}
WHERE {BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
GROUP BY gush || '/' || chelka
HAVING count(*) >= {min_deals}
ORDER BY медиана_м2 DESC NULLS LAST"""
    return sql_slow(q)


def recent_deals(code: int | str, limit: int = 40) -> list[dict]:
    """Последние сделки по дате — живые примеры."""
    q = f"""
SELECT deal_date AS дата, deal_amount::int AS цена, asset_area::numeric::int AS площадь,
  room_num AS комнат, year_built AS год_постройки, deal_nature AS тип,
  gush || '/' || chelka AS участок,
  ({PPSQM})::int AS цена_за_м2,
  {SEGMENT_CASE} AS сегмент
FROM {T}
WHERE {BASE_WHERE.format(code=code)}
ORDER BY substr(deal_date,7,4) || substr(deal_date,4,2) || substr(deal_date,1,2) DESC
LIMIT {limit}"""
    return sql_slow(q)


def pct(new, old):
    try:
        new, old = float(new), float(old)
    except (TypeError, ValueError):
        return None
    if not old:
        return None
    return round((new / old - 1) * 100, 1)


def analyze(city: str, since: int = 2010) -> dict:
    canon, code, total_in_registry = deals.resolve_city(city)
    print(f"{canon} (код {code}), сделок в реестре всего: {total_in_registry:,}".replace(",", " "), flush=True)

    print("  → годовой ряд", flush=True)
    series = year_series(code, since)
    print("  → итоги по сегментам", flush=True)
    tot = totals(code, since)
    print("  → разрез по комнатам (новые)", flush=True)
    rooms_new = by_rooms(code, since, True)
    print("  → разрез по комнатам (вторичка)", flush=True)
    rooms_old = by_rooms(code, since, False)
    print("  → участки гуш/хелька", flush=True)
    areas = by_area(code, since)
    print("  → последние сделки", flush=True)
    recent = recent_deals(code)

    # Производные показатели по годам
    years = []
    prev_new_m2 = None
    for r in series:
        y = int(r["год"])
        row = {
            "год": y,
            "сделок": r["сделок"],
            "новых_сделок": r["новых_сделок"] or 0,
            "доля_новых_pct": round((r["новых_сделок"] or 0) / r["сделок"] * 100, 1) if r["сделок"] else None,
            "медиана_цены": r["медиана_цены"],
            "медиана_м2": r["медиана_м2"],
            "м2_новые": r["м2_новые"],
            "цена_новые": r["цена_новые"],
            "м2_котлован": r["м2_котлован"],
            "м2_вторичка": r["м2_вторичка"],
            "цена_вторичка": r["цена_вторичка"],
            "медиана_площади": r["медиана_площади"],
            "медиана_года_сдачи": r["медиана_года_сдачи"],
            "p25": r["p25_цены"],
            "p75": r["p75_цены"],
        }
        row["премия_новых_pct"] = pct(row["м2_новые"], row["м2_вторичка"])
        row["м2_новые_к_пред_году_pct"] = pct(row["м2_новые"], prev_new_m2) if prev_new_m2 else None
        if row["м2_новые"]:
            prev_new_m2 = row["м2_новые"]
        years.append(row)

    with_new = [y for y in years if y.get("м2_новые")]
    latest = with_new[-1] if with_new else None
    back5 = next((y for y in with_new if y["год"] == latest["год"] - 5), None) if latest else None
    back10 = next((y for y in with_new if y["год"] == latest["год"] - 10), None) if latest else None

    headline = {}
    if latest:
        headline = {
            "год": latest["год"],
            "м2_новые": latest["м2_новые"],
            "цена_новые": latest["цена_новые"],
            "за_год_pct": latest["м2_новые_к_пред_году_pct"],
            "за_5_лет_pct": pct(latest["м2_новые"], back5["м2_новые"]) if back5 else None,
            "за_10_лет_pct": pct(latest["м2_новые"], back10["м2_новые"]) if back10 else None,
            "премия_новых_pct": latest["премия_новых_pct"],
            "доля_новых_pct": latest["доля_новых_pct"],
            "м2_вторичка": latest["м2_вторичка"],
            "медиана_площади": latest["медиана_площади"],
            "медиана_года_сдачи": latest["медиана_года_сдачи"],
        }

    return {
        "создано": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "источник": {
            "название": 'עסקאות נדל"ן — רשות המסים (מיסוי מקרקעין)',
            "первоисточник": "https://nadlan.taxes.gov.il/svinfonadlan2010/startpage.aspx",
            "доступ": "SQL-движок over.org.il, без ключей",
            "всего_сделок_в_реестре": deals.stats().get("deals"),
            "реестр_покрывает": [deals.stats().get("first_deal"), deals.stats().get("last_deal")],
        },
        "город": {"запрос": city, "в_реестре": canon, "код": code, "всего_сделок": total_in_registry},
        "фильтры": {
            "с_года": since,
            "типы_жилья": deals.APT_NATURES,
            "порог_цены": MIN_AMOUNT,
            "площадь": [MIN_AREA, MAX_AREA],
            "новые": "year_built >= год сделки (включая котлован с плановым годом сдачи)",
        },
        "заголовок": headline,
        "итоги_по_сегментам": tot,
        "по_годам": years,
        "по_комнатам_новые": rooms_new,
        "по_комнатам_вторичка": rooms_old,
        "участки": areas,
        "последние_сделки": recent,
        "оговорки": [
            "Сумма сделки — заявленная налоговому управлению, а не проверенная рыночная цена.",
            "Одна строка может быть и квартирой, и целым зданием, поэтому везде медиана, не среднее.",
            "Цена за м² = сумма / (площадь × проданная доля); при мелких долях точность падает.",
            "Адресов и названий районов в реестре нет — только гуш и хелька. Привязка к адресу "
            "появляется через надстройку over.org.il и для малых городов покрыта слабо "
            "(в последних 200 сделках Кирьят-Гата адрес нашёлся у 9%).",
            'Тип «מגורים» исключён: в 68% таких строк нет площади и в 99% нет комнат — это земля и права.',
            "Последние 2-3 периода в реестре неполные: сделки заявляются с задержкой, "
            "поэтому свежий год выглядит провалом объёма, а не рынка.",
        ],
    }


def telegram_text(res: dict) -> str:
    h = res.get("заголовок") or {}
    c = res["город"]
    m = lambda v: f"{v:,}".replace(",", " ") if isinstance(v, (int, float)) else "—"  # noqa: E731

    L = [f"🏠 {c['в_реестре']} — квартиры", "по реестру сделок налогового управления", ""]
    if h:
        L += [
            f"НОВЫЕ, {h.get('год')}: ₪{m(h.get('м2_новые'))}/м² · медиана ₪{m(h.get('цена_новые'))}",
            f"ВТОРИЧКА, {h.get('год')}: ₪{m(h.get('м2_вторичка'))}/м²",
        ]
        if h.get("за_год_pct") is not None:
            L.append(f"За год (новые, ₪/м²): {h['за_год_pct']:+}%")
        if h.get("за_5_лет_pct") is not None:
            L.append(f"За 5 лет: {h['за_5_лет_pct']:+}%")
        if h.get("за_10_лет_pct") is not None:
            L.append(f"За 10 лет: {h['за_10_лет_pct']:+}%")
        if h.get("премия_новых_pct") is not None:
            L.append(f"Новые дороже вторички на {h['премия_новых_pct']:+}% за м²")
        L.append(f"Доля новых в сделках: {h.get('доля_новых_pct')}%")
        L.append(f"Медианная площадь: {m(h.get('медиана_площади'))} м² · медианный год сдачи: {h.get('медиана_года_сдачи')}")

    L += ["", "₪/м² новые по годам:"]
    for y in res["по_годам"][-10:]:
        if y.get("м2_новые"):
            chg = y.get("м2_новые_к_пред_году_pct")
            L.append(f"  {y['год']}: ₪{m(y['м2_новые'])} · {y['новых_сделок']} сделок" + (f" · {chg:+}%" if chg is not None else ""))

    rooms = res.get("по_комнатам_новые") or []
    if rooms:
        L += ["", "Новые по комнатам (медиана):"]
        for r in rooms:
            L.append(f"  {r['комнат']} комн: ₪{m(r['медиана_цены'])} · ₪{m(r['медиана_м2'])}/м² · {r['сделок']} сделок")

    top = (res.get("участки") or [])[:6]
    if top:
        L += ["", "Дороже всего за м² (гуш/хелька):"]
        for a in top:
            L.append(f"  {a['участок']}: ₪{m(a['медиана_м2'])} · {a['сделок']} сделок · новых {a['новых']}")

    L += ["", f"обновлено {res['создано'][:16]}"]
    txt = "\n".join(L)
    return txt[:4000]


def notify(text: str) -> str:
    import urllib.parse
    import urllib.request

    token, chat = os.environ.get("WATCH_TELEGRAM_TOKEN"), os.environ.get("WATCH_TELEGRAM_CHAT")
    if not token or not chat:
        return "не настроен: нужны WATCH_TELEGRAM_TOKEN и WATCH_TELEGRAM_CHAT"
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers=deals.UA
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return "отправлено"
    except Exception as e:  # noqa: BLE001
        return f"ошибка отправки: {e}"


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="קרית גת")
    ap.add_argument("--since", type=int, default=2010)
    ap.add_argument("--send", action="store_true", help="реально отправить в Telegram")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    res = analyze(args.city, args.since)
    path = args.out or os.path.join(OUT_DIR, f"deals_{res['город']['код']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    txt = telegram_text(res)
    print("\n" + "=" * 62)
    print(txt)
    print("=" * 62)
    if args.send:
        print("telegram:", notify(txt))
    print("сохранено:", path)


if __name__ == "__main__":
    main()
