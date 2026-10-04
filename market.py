"""
Рынок жилья города по РЕАЛЬНЫМ сделкам + все районы реновации + что построят.

Собирает три государственных источника в один отчёт и одно сообщение в Telegram:

  1. Реестр сделок налогового управления (система «Карман», 3,84 млн сделок с 1998) —
     через SQL-движок over.org.il. Отсюда цены на новые квартиры и их изменение.
  2. Реестр комплексов городской реновации минстроя (data.gov.il) — все районы
     пинуй-бинуй и ТАМА 38 города, со статусами и ссылками на план и на карту.
  3. Планировочный запас РМАИ + ход строительства минстроя — сколько ещё построят.

Ключевые решения, которые тут приняты осознанно:

* Новые квартиры разделены на «готовые» и «котлован». Это разные продукты: у котлована
  year_built хранит плановый год сдачи (в Кирьят-Гате есть 2029 и 2030), деньги замораживаются
  на годы, а цена за м² другая. Смешанная медиана по ним обеим непригодна для решений.
* Последний год всегда помечается как неполный. Реестр пополняется с задержкой
  (выгрузка 19.09.2026, последняя сделка 04.08.2026), поэтому свежий год выглядит
  провалом объёма, а не рынка.
* Везде медиана, не среднее: одна строка реестра может быть и квартирой, и зданием.

Запуск:
    python3 market.py --city "קרית גת"
    python3 market.py --city "קרית גת" --send          # отправить в Telegram
    python3 market.py --city "קרית גת" --schedule quarter
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sys

import common
import deals
import deals_agg as da
import geo
import renewal_geo

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "analysis")
T = deals.TABLE


# --------------------------------------------------------------------------- #
#  Часть 1. Реестр сделок
# --------------------------------------------------------------------------- #

def q_year_split(code: int, since: int) -> list[dict]:
    """Годовой ряд: готовые новые / котлован / вторичка раздельно."""
    seg = da.SEGMENT_CASE
    return da.sql_slow(f"""
SELECT substr(deal_date,7,4) AS год,
  count(*) AS сделок,
  count(*) FILTER (WHERE {seg}='новая_свежая') AS готовых_новых,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}='новая_свежая')::int AS м2_готовые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) FILTER (WHERE {seg}='новая_свежая')::int AS цена_готовые,
  count(*) FILTER (WHERE {seg}='новая_котлован') AS котлован,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}='новая_котлован')::int AS м2_котлован,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) FILTER (WHERE {seg}='новая_котлован')::int AS цена_котлован,
  count(*) FILTER (WHERE {seg}='вторичка') AS вторичка,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}='вторичка')::int AS м2_вторичка,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) FILTER (WHERE {seg}='вторичка')::int AS цена_вторичка,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric)::int AS медиана_площади
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
GROUP BY substr(deal_date,7,4) ORDER BY год""")


def q_quarters(code: int, since: int) -> list[dict]:
    seg = da.SEGMENT_CASE
    return da.sql_slow(f"""
SELECT substr(deal_date,7,4) || '-Q' || ((substr(deal_date,4,2)::int - 1)/3 + 1)::text AS период,
  count(*) FILTER (WHERE {seg}<>'вторичка') AS новых,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}<>'вторичка')::int AS м2_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) FILTER (WHERE {seg}<>'вторичка')::int AS цена_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}='вторичка')::int AS м2_вторичка
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
GROUP BY 1 ORDER BY 1""")


def q_bands(code: int, year: int, segment_new: bool = True) -> list[dict]:
    """Распределение сделок по полосам цены за м² — показывает расслоение рынка."""
    seg = da.SEGMENT_CASE
    filt = f"AND {seg}<>'вторичка'" if segment_new else f"AND {seg}='вторичка'"
    return da.sql_slow(f"""
SELECT (({da.PPSQM})/1000)::int*1000 AS полоса, count(*) AS сделок,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)::int AS медиана_цены,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric)::int AS медиана_площади
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) = '{year}' {filt}
  AND ({da.PPSQM}) < 100000
GROUP BY 1 HAVING count(*) >= 3 ORDER BY 1""")


def q_rooms(code: int, since: int) -> list[dict]:
    seg = da.SEGMENT_CASE
    return da.sql_slow(f"""
SELECT room_num::int AS комнат,
  count(*) FILTER (WHERE {seg}<>'вторичка') AS новых,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) FILTER (WHERE {seg}<>'вторичка')::int AS цена_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}<>'вторичка')::int AS м2_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric) FILTER (WHERE {seg}<>'вторичка')::int AS площадь_новые,
  count(*) FILTER (WHERE {seg}='вторичка') AS вторичка,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) FILTER (WHERE {seg}='вторичка')::int AS цена_вторичка
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
  AND room_num ~ '^[0-9]+$' AND room_num::int BETWEEN 1 AND 7
GROUP BY room_num::int ORDER BY комнат""")


def q_areas(code: int, since: int, min_deals: int = 20) -> list[dict]:
    seg = da.SEGMENT_CASE
    return da.sql_slow(f"""
SELECT gush || '/' || chelka AS участок, count(*) AS сделок,
  count(*) FILTER (WHERE {seg}<>'вторичка') AS новых,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM})::int AS м2_медиана,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM}) FILTER (WHERE {seg}<>'вторичка')::int AS м2_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)::int AS медиана_цены,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY year_built::numeric)::int AS медиана_года_постройки,
  max(substr(deal_date,7,4)) AS последняя_сделка
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
GROUP BY gush || '/' || chelka HAVING count(*) >= {min_deals}
ORDER BY м2_медиана DESC NULLS LAST""")


def q_recent(code: int, limit: int = 30) -> list[dict]:
    seg = da.SEGMENT_CASE
    return da.sql_slow(f"""
SELECT deal_date AS дата, deal_amount::int AS цена, asset_area::numeric::int AS площадь,
  room_num AS комнат, year_built AS год_постройки, deal_nature AS тип,
  gush || '/' || chelka AS участок, ({da.PPSQM})::int AS цена_за_м2, {seg} AS сегмент
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)}
ORDER BY substr(deal_date,7,4) || substr(deal_date,4,2) || substr(deal_date,1,2) DESC
LIMIT {limit}""")


# --------------------------------------------------------------------------- #
#  Часть 2. Реновация и будущее предложение
# --------------------------------------------------------------------------- #

def city_matches(dataset_key: str, code: int, names: list[str]) -> list[dict]:
    """Строки датасета минстроя, относящиеся к городу. Фильтр и по коду, и по написанию."""
    rows = common.datastore_rows(common.DATASETS[dataset_key]["resource_id"])
    field = common.CITY_FIELD.get(dataset_key)
    out = []
    for r in rows:
        if str(r.get("SemelYeshuv") or "").strip() == str(code):
            out.append(r)
            continue
        if field and common.norm_city(r.get(field)) in [common.norm_city(n) for n in names]:
            out.append(r)
    return out


def renewal_districts(code: int, names: list[str]) -> dict:
    """Все районы реновации города: статус, трек, было/станет, ссылки на план и карту."""
    rows = city_matches("urban_renewal", code, names)
    items = []
    for r in rows:
        items.append(
            {
                "комплекс": " ".join(str(r.get("ShemMitcham") or "").split()),
                "номер": r.get("MisparMitham"),
                "номер_плана": " ".join(str(r.get("MisparTochnit") or "").split()),
                "сейчас_единиц": int(common.num(r.get("YachadKayam"))),
                "добавят": int(common.num(r.get("YachadTosafti"))),
                "станет": int(common.num(r.get("YachadMutza"))),
                "объявлен": " ".join(str(r.get("TaarichHachraza") or "").split()),
                "трек": " ".join(str(r.get("Maslul") or "").split()),
                "статус": " ".join(str(r.get("Status") or "").split()),
                "в_работе": " ".join(str(r.get("Bebitzua") or "").split()),
                "год_вступления_в_силу": r.get("ShnatMatanTokef"),
                "план_mavat": r.get("KishurLatar") or "",
                "карта_govmap": r.get("KishurLaMapa") or "",
            }
        )
    items.sort(key=lambda x: -x["станет"])
    return {
        "комплексы": items,
        "всего_комплексов": len(items),
        "сейчас_единиц": sum(x["сейчас_единиц"] for x in items),
        "добавят_единиц": sum(x["добавят"] for x in items),
        "станет_единиц": sum(x["станет"] for x in items),
        "в_работе": sum(1 for x in items if x["в_работе"].startswith("כן") or "במימוש" in x["статус"]),
    }


def planning_pipeline(code: int, names: list[str]) -> dict:
    rows = city_matches("planning_inventory", code, names)
    plans = []
    for r in rows:
        plans.append(
            {
                "номер": " ".join(str(r.get("מספר תוכנית") or "").split()),
                "название": " ".join(str(r.get("שם תוכנית") or "").split()),
                "этап": " ".join(str(r.get("שלב תכנוני") or "").split()),
                "девелопер": " ".join(str(r.get("יזם תכנון") or "").split()),
                "единиц": int(common.num(r.get("יחד פוטנציאל לשיווק"))),
                "порог": " ".join(str(r.get("תאריך קיום תנאי סף") or "").split()),
                "утверждён": " ".join(str(r.get("תאריך פרסום לאישור ברשומות") or "").split()),
                "рмаи": r.get("קישור לאתר רשות מקרקעי ישראל") or "",
                "мават": r.get("קישור לאתר מנהל תכנון") or "",
            }
        )
    plans.sort(key=lambda x: -x["единиц"])
    by_stage = collections.defaultdict(int)
    for p in plans:
        by_stage[p["этап"]] += p["единиц"]
    return {"планы": plans, "всего_единиц": sum(p["единиц"] for p in plans),
            "по_этапам": dict(sorted(by_stage.items(), key=lambda kv: -kv[1]))}


# --------------------------------------------------------------------------- #
#  Сборка
# --------------------------------------------------------------------------- #

def m(v) -> str:
    return f"{v:,}".replace(",", " ") if isinstance(v, (int, float)) and v is not None else "—"


def build(city: str, since: int) -> dict:
    canon, code, total = deals.resolve_city(city)
    st = deals.stats()
    names = [canon] + [k for k, v in deals.CITY_NAME_ALIASES.items() if v == canon] + [canon]
    print(f"{canon} (код ЛАМАС {code}), всего сделок в реестре: {m(total)}", flush=True)

    print("  → годовой ряд по сегментам", flush=True)
    years = q_year_split(code, since)
    print("  → кварталы", flush=True)
    quarters = q_quarters(code, max(since, 2022))
    print("  → комнаты", flush=True)
    rooms = q_rooms(code, since)
    print("  → участки гуш/хелька", flush=True)
    areas = q_areas(code, since)

    # Адреса: реестр сделок публикует только гуш/хелька, поэтому без отдельного слоя
    # «цены по районам» выглядели как «2812/349 — ₪22 744/м²». Здесь гуш/хелька
    # превращается в улицу, почтовый индекс, статистический район ЦСБ и точку на карте.
    geo_cache = geo.load_cache(code)
    if not geo_cache.get("участки"):
        print("  → улицы и районы (первый запуск, ~6 минут)", flush=True)
        geo_cache = geo.resolve_city(code, since)
    else:
        print(f"  → улицы и районы (кэш: {len(geo_cache['участки'])} участков)", flush=True)
        # добираем только участки, которых в кэше ещё нет
        geo.resolve_city(code, since)
    addresses = geo.summarize(code, since)
    print("  → границы комплексов реновации (GIS-слой минстроя)", flush=True)
    try:
        renewal_bounds = renewal_geo.match(city, since)
    except Exception as e:  # noqa: BLE001
        renewal_bounds = {"ошибка": str(e)[:200]}
    print("  → последние сделки", flush=True)
    recent = q_recent(code)

    # Последний год в реестре неполный: сделки заявляют с задержкой.
    last_year = max(int(r["год"]) for r in years)
    registry_cut = str(st.get("scraped_at", ""))[:10]
    last_deal_date = max((r["дата"] for r in recent if r.get("дата")), default="")
    partial_year = last_year if dt.date.today().year == last_year else None

    # Полосы цен: последний полный год против текущего — видно расслоение рынка
    full_years = [int(r["год"]) for r in years if r["год"] != str(partial_year)]
    bands_now = q_bands(code, partial_year or full_years[-1]) if (partial_year or full_years) else []
    ref_year = full_years[-2] if len(full_years) >= 2 else None
    bands_ref = q_bands(code, ref_year) if ref_year else []

    # Заголовок: берём последний ПОЛНЫЙ год для честной динамики, а текущий показываем отдельно
    def row_of(y):
        return next((r for r in years if int(r["год"]) == y), None)

    base_year = full_years[-1] if full_years else last_year
    base, prev, back5 = row_of(base_year), row_of(base_year - 1), row_of(base_year - 5)
    cur = row_of(partial_year) if partial_year else None

    headline = {
        "опорный_год": base_year,
        "опорный_год_полный": True,
        "м2_готовые_новые": base.get("м2_готовые") if base else None,
        "цена_готовые_новые": base.get("цена_готовые") if base else None,
        "м2_котлован": base.get("м2_котлован") if base else None,
        "цена_котлован": base.get("цена_котлован") if base else None,
        "м2_вторичка": base.get("м2_вторичка") if base else None,
        "готовых_новых_сделок": base.get("готовых_новых") if base else None,
        "котлован_сделок": base.get("котлован") if base else None,
        "за_год_pct": da.pct(base.get("м2_готовые"), prev.get("м2_готовые")) if base and prev else None,
        "за_5_лет_pct": da.pct(base.get("м2_готовые"), back5.get("м2_готовые")) if base and back5 else None,
    }
    if cur:
        headline["текущий_год_неполный"] = {
            "год": partial_year,
            "м2_готовые_новые": cur.get("м2_готовые"),
            "цена_готовые_новые": cur.get("цена_готовые"),
            "м2_котлован": cur.get("м2_котлован"),
            "готовых_новых_сделок": cur.get("готовых_новых"),
            "котлован_сделок": cur.get("котлован"),
            "м2_вторичка": cur.get("м2_вторичка"),
            "изменение_к_опорному_pct": da.pct(cur.get("м2_готовые"), base.get("м2_готовые")),
        }

    print("  → районы реновации и планы", flush=True)
    ren = renewal_districts(code, names)
    plan = planning_pipeline(code, names)

    return {
        "создано": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "источники": {
            "сделки": {
                "название": 'עסקאות נדל"ן — רשות המסים (מערכת כרמ"ן)',
                "первоисточник": "https://nadlan.taxes.gov.il/svinfonadlan2010/startpage.aspx",
                "доступ": "SQL-движок over.org.il, без ключей",
                "всего_сделок_в_реестре": st.get("deals"),
                "реестр_покрывает": [st.get("first_deal"), st.get("last_deal")],
                "выгрузка_снята": registry_cut,
                "последняя_сделка_города": last_deal_date,
            },
            "реновация": "התחדשות עירונית — משרד הבינוי והשיכון (data.gov.il)",
            "планы": "מלאי תכנוני למגורים — רשות מקרקעי ישראל (data.gov.il)",
        },
        "город": {"запрос": city, "в_реестре": canon, "код": code, "всего_сделок": total},
        "заголовок": headline,
        "по_годам": years,
        "поквартально": quarters,
        "полосы_цен_текущий": {"год": (partial_year or base_year), "полосы": bands_now},
        "полосы_цен_опорный": {"год": ref_year, "полосы": bands_ref},
        "по_комнатам": rooms,
        "участки": areas,
        "адреса": addresses,
        "реновация_границы": renewal_bounds,
        "последние_сделки": recent,
        "реновация": ren,
        "планы": plan,
        "неполный_год": partial_year,
        "оговорки": [
            "Сумма сделки — заявленная налоговому управлению, а не проверенная рыночная цена.",
            "Одна строка реестра может быть и квартирой, и целым зданием, поэтому везде медиана.",
            "Цена за м² = сумма / (площадь × проданная доля). Строки с ценой за м² вне ₪2 000–150 000 отброшены как битые.",
            "В самом реестре сделок адресов нет — только гуш и хелька. Улицы, индексы, "
            "статистические районы ЦСБ и координаты добавлены из открытого слоя «נדל״ן לעם» "
            f"(газовый реестр недвижимости + кадастр): покрыто "
            f"{addresses['покрытие']['доля_сделок_pct']}% сделок города. "
            "Точность — улица: газовый реестр связывает гуш-хелька с улицей, но не с номером дома.",
            'Тип «מגורים» исключён: в 68% таких строк нет площади и в 99% нет комнат — это земля и права.',
            f"Последний год ({partial_year}) неполный: реестр пополняется с задержкой, "
            f"выгрузка снята {registry_cut}, последняя сделка города {last_deal_date}.",
        ],
    }


# --------------------------------------------------------------------------- #
#  Сообщение в Telegram
# --------------------------------------------------------------------------- #

LIMIT = 4000  # запас к лимиту Telegram в 4096


def telegram_text(res: dict) -> str:
    c, h = res["город"], res["заголовок"]
    L = [
        f"🏠 {c['в_реестре']} — жильё",
        f"реестр сделок налогового управления · обновлено {res['создано'][:10]}",
        "",
        f"НОВЫЕ ГОТОВЫЕ, {h['опорный_год']}:",
        f"  ₪{m(h['м2_готовые_новые'])}/м² · медиана ₪{m(h['цена_готовые_новые'])}",
        f"  сделок: {m(h['готовых_новых_сделок'])}",
    ]
    if h.get("за_год_pct") is not None:
        L.append(f"  за год: {h['за_год_pct']:+}%   за 5 лет: {h.get('за_5_лет_pct'):+}%")
    if h.get("м2_котлован"):
        L += ["", f"КОТЛОВАН, {h['опорный_год']}: ₪{m(h['м2_котлован'])}/м² · медиана ₪{m(h['цена_котлован'])} · {m(h['котлован_сделок'])} сделок"]
    L += ["", f"ВТОРИЧКА, {h['опорный_год']}: ₪{m(h['м2_вторичка'])}/м²"]

    cur = h.get("текущий_год_неполный")
    if cur:
        L += [
            "",
            f"⚠️ {cur['год']} год неполный (реестр пополняется с задержкой):",
            f"  готовые новые: ₪{m(cur['м2_готовые_новые'])}/м² · {m(cur['готовых_новых_сделок'])} сделок",
            f"  котлован: ₪{m(cur['м2_котлован'])}/м² · {m(cur['котлован_сделок'])} сделок",
            f"  вторичка: ₪{m(cur['м2_вторичка'])}/м²",
        ]
        if cur.get("изменение_к_опорному_pct") is not None:
            L.append(f"  к {h['опорный_год']}: {cur['изменение_к_опорному_pct']:+}%")

    L += ["", "₪/м², готовые новые, по годам:"]
    for r in res["по_годам"][-9:]:
        v = r.get("м2_готовые")
        if not v:
            continue
        mark = " ⚠️неполный" if str(r["год"]) == str(res.get("неполный_год")) else ""
        L.append(f"  {r['год']}: ₪{m(v)} · {r.get('готовых_новых') or 0} сделок{mark}")

    qs = [q for q in res["поквартально"] if q.get("м2_новые")][-6:]
    if qs:
        L += ["", "Поквартально, все новые:"]
        for q in qs:
            L.append(f"  {q['период']}: ₪{m(q['м2_новые'])}/м² · {q['новых']} сделок")

    rn = res["реновация"]
    if rn["всего_комплексов"]:
        L += [
            "",
            f"🏗 РАЙОНЫ РЕНОВАЦИИ — {rn['всего_комплексов']} комплексов",
            f"  {m(rn['сейчас_единиц'])} квартир сейчас → {m(rn['станет_единиц'])} станет (+{m(rn['добавят_единиц'])})",
            f"  в работе: {rn['в_работе']}",
            "",
        ]
        bounds = {(b.get("номер_проекта"), b.get("комплекс")): b
                  for b in (res.get("реновация_границы") or {}).get("комплексы", [])}
        by_name = {b.get("комплекс"): b for b in bounds.values()}
        for x in rn["комплексы"][:10]:
            L.append(f"  • {x['комплекс']}: {m(x['сейчас_единиц'])} → {m(x['станет'])} · {x['статус'] or '—'} · трек {x['трек'] or '—'}")
            b = by_name.get(x["комплекс"])
            if b and b.get("улицы"):
                streets = ", ".join(st["улица"] for st in b["улицы"][:4])
                more = f" +{len(b['улицы']) - 4}" if len(b["улицы"]) > 4 else ""
                L.append(f"      где: {streets}{more} · {b['площадь_га']} га · "
                         f"{b['участков_внутри']} участков · {b['сделок_внутри']} сделок")
        if rn["всего_комплексов"] > 10:
            L.append(f"  …и ещё {rn['всего_комплексов'] - 10}")

    pl = res["планы"]
    if pl["всего_единиц"]:
        L += ["", f"📋 ЧТО ЕЩЁ ПОСТРОЯТ: {m(pl['всего_единиц'])} единиц в {len(pl['планы'])} планах"]
        for p in pl["планы"][:5]:
            L.append(f"  • {p['название'][:44]}: {m(p['единиц'])} · {p['этап']}")

    ad = res.get("адреса") or {}
    streets = [x for x in ad.get("улицы", []) if (x.get("сделок") or 0) >= 40 and x.get("м2_все")]
    if streets:
        cov = ad.get("покрытие", {})
        L += ["", f"📍 ЦЕНЫ ПО УЛИЦАМ (привязано {cov.get('доля_сделок_pct')}% сделок)"]
        top = sorted(streets, key=lambda x: -x["м2_все"])[:6]
        L.append("  дороже всего:")
        for x in top:
            nz = f" · новых ₪{m(x['м2_новые'])}" if x.get("м2_новые") else ""
            L.append(f"    {x['улица']}: ₪{m(x['м2_все'])}/м² · {x['сделок']} сделок{nz}")
        L.append("  дешевле всего:")
        for x in sorted(streets, key=lambda x: x["м2_все"])[:5]:
            L.append(f"    {x['улица']}: ₪{m(x['м2_все'])}/м² · {x['сделок']} сделок · "
                     f"медиана ₪{m(x['медиана_цены'])}")
        areas = [a for a in ad.get("статрайоны", []) if (a.get("сделок") or 0) >= 100]
        if areas:
            L.append("  районы ЦСБ (статистические):")
            for a in sorted(areas, key=lambda x: -(x.get("м2_все") or 0))[:5]:
                L.append(f"    район {a['статрайон']}: ₪{m(a['м2_все'])}/м² · {m(a['сделок'])} сделок · "
                         f"население {m(a['население'])} · кластер {a['кластер_соцэк'] or '—'}")

    rooms = [r for r in res["по_комнатам"] if (r.get("новых") or 0) >= 15]
    if rooms:
        L += ["", "Новые по комнатам (медиана цены):"]
        for r in rooms:
            L.append(f"  {r['комнат']} комн: ₪{m(r['цена_новые'])} · ₪{m(r['м2_новые'])}/м² · {m(r['площадь_новые'])} м² · {r['новых']} сделок")

    txt = "\n".join(L)
    if len(txt) <= LIMIT:
        return txt
    # Лимит Telegram — 4096 символа. Обрезать хвост значит потерять именно то,
    # что добавили последним, поэтому хвостовые блоки выбрасываются по одному,
    # от наименее важного, пока сообщение не влезет.
    blocks = txt.split("\n\n")
    while len("\n\n".join(blocks)) > LIMIT and len(blocks) > 4:
        blocks.pop()
    txt = "\n\n".join(blocks)
    return txt if len(txt) <= LIMIT else txt[:LIMIT - 20] + "\n…(обрезано)"


def send_telegram(text: str) -> str:
    import urllib.parse
    import urllib.request

    token, chat = os.environ.get("WATCH_TELEGRAM_TOKEN"), os.environ.get("WATCH_TELEGRAM_CHAT")
    if not token or not chat:
        return "не настроен: задайте WATCH_TELEGRAM_TOKEN и WATCH_TELEGRAM_CHAT"
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers=deals.UA)
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return "отправлено"
    except Exception as e:  # noqa: BLE001
        return f"ошибка отправки: {e}"


SCHEDULE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "quarter": 91}
STATE_FILE = os.path.join(HERE, "last_digest.json")


def due(schedule: str, city_code: int) -> tuple[bool, str]:
    """Пора ли слать дайджест. Состояние хранится в last_digest.json по коду города.

    Смысл: скрипт можно дёргать из cron хоть каждый день, а сообщение уйдёт только
    когда истёк интервал расписания. Так «раз в квартал» не требует отдельного планировщика.
    """
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    last = state.get(str(city_code))
    days = SCHEDULE_DAYS.get(schedule, 91)
    today = dt.date.today()
    if not last:
        return True, "первая отправка"
    try:
        last_d = dt.date.fromisoformat(last)
    except ValueError:
        return True, "не разобрал дату прошлой отправки"
    passed = (today - last_d).days
    if passed >= days:
        return True, f"прошло {passed} дн. при расписании «{schedule}» ({days} дн.)"
    return False, f"до следующей отправки {days - passed} дн. (расписание «{schedule}»)"


def mark_sent(city_code: int) -> None:
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    state[str(city_code)] = dt.date.today().isoformat()
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="קרית גת")
    ap.add_argument("--since", type=int, default=2012)
    ap.add_argument("--send", action="store_true", help="отправить в Telegram")
    ap.add_argument("--schedule", choices=["daily", "weekly", "monthly", "quarter"], default="quarter",
                    help="как часто планировщик должен слать это сообщение")
    ap.add_argument("--out", default=None)
    ap.add_argument("--force", action="store_true", help="слать даже если срок ещё не наступил")
    ap.add_argument("--rebuild", action="store_true",
                    help="не переиспользовать готовый analysis/market_<код>.json (флаг читает server.py)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    res = build(args.city, args.since)
    path = args.out or os.path.join(OUT_DIR, f"market_{res['город']['код']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    txt = telegram_text(res)
    print("\n" + "═" * 66)
    print(txt)
    print("═" * 66)
    print(f"\nсимволов в сообщении: {len(txt)}")

    is_due, why = due(args.schedule, res["город"]["код"])
    print(f"расписание «{args.schedule}»: {'пора' if is_due else 'ещё не пора'} — {why}")
    if args.send:
        if is_due or args.force:
            print("telegram:", send_telegram(txt))
            mark_sent(res["город"]["код"])
        else:
            print("telegram: пропустил, срок не наступил (--force отправит принудительно)")
    else:
        print("telegram: не отправлял (добавьте --send)")
    print("сохранено:", path)
    print("расписание:", args.schedule)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
