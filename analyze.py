"""
Анализ рынка жилья конкретного города (по умолчанию Кирьят-Гат) по открытым данным.

Что считаем:
  1. Цена за м² по программе «Дира ба-анаха» — динамика по годам и по районам.
  2. Место города среди всех 112 городов, где эта цена известна.
  3. Конвейер будущего предложения: планировочный запас РМАИ + реновация + стройка.

Честное ограничение (важно, не забыть):
  * Реестр всех сделок (nadlan.gov.il) закрыт reCAPTCHA Enterprise → свободных рыночных
    цен по конкретным квартирам тут нет.
  * ЦСБ (индекс цен на жильё) из песочницы не отдаёт JSON.
  * «Дира ба-анаха» — это льготная цена, она ниже рынка. И ряд «по городу» составной:
    в разные годы разыгрывались разные районы, поэтому рост общего ряда частично
    отражает смену района, а не только рост цен. Ниже считаем отдельно динамику
    ВНУТРИ одного района — вот она честная.

Запуск: python3 analyze.py --city "קרית גат"
Результат: analysis/<city>.json
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os

import common

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "analysis")


def parse_price(value) -> float | None:
    v = common.num(value, default=-1)
    return v if v > 0 else None


def parse_date(value) -> dt.date | None:
    """Даты в датасетах приходят в трёх форматах — и это типичная израильская боль."""
    s = str(value or "").strip()
    if not s or s == "-":
        return None
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10), ("%d/%m/%Y", 10)):
        try:
            return dt.datetime.strptime(s[:length], fmt).date()
        except ValueError:
            continue
    return None


def money(x) -> str:
    return f"{x:,.0f}".replace(",", " ")


def analyze_prices(city: str) -> dict:
    rows = common.datastore_rows(common.DATASETS["mechir_lamishtaken"]["resource_id"])

    recs = []
    for r in rows:
        p = parse_price(r.get("PriceForMeter"))
        d = parse_date(r.get("LotteryExecutionDate"))
        if p is None or d is None:
            continue
        recs.append(
            {
                "price_m2": p,
                "date": d,
                "city": common.norm_city(r.get("LamasName")),
                "code": str(r.get("LamasCode") or "").strip(),
                "neighborhood": " ".join(str(r.get("Neighborhood") or "-").split()),
                "project": " ".join(str(r.get("ProjectName") or "").split()),
                "units": int(common.num(r.get("LotteryHousingUnits"))),
                "winners": int(common.num(r.get("Winners"))),
                "subscribers": int(common.num(r.get("Subscribers"))),
                "status": " ".join(str(r.get("ProjectStatus") or "").split()),
                "provider": " ".join(str(r.get("ProviderName") or "").split()),
            }
        )

    mine = [r for r in recs if r["city"] == common.norm_city(city)]

    # Самопроверка: код города из нашей карты должен соответствовать ровно одному названию.
    # Именно на этом ловится подмешивание чужого города в выборку.
    code = common.CITY_LAMAS_CODE.get(common.norm_city(city))
    code_check = None
    if code is not None:
        names = {r["city"] for r in recs if r["code"] == str(code)}
        code_check = {
            "код_ламас": code,
            "города_под_этим_кодом": sorted(names),
            "корректно": names == {common.norm_city(city)},
        }
        wrong = [r for r in mine if r["code"] and r["code"] != str(code)]
        if wrong:
            code_check["строк_с_чужим_кодом"] = len(wrong)

    # Общий ряд по городу: медиана цены за м² в пределах года (медиана устойчивее к выбросам)
    by_year: dict[int, list[float]] = collections.defaultdict(list)
    by_year_units: dict[int, int] = collections.defaultdict(int)
    for r in mine:
        by_year[r["date"].year].append(r["price_m2"])
        by_year_units[r["date"].year] += r["units"]

    def median(v):
        v = sorted(v)
        n = len(v)
        return (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2) if v else None

    timeline = [
        {
            "year": y,
            "price_m2_median": round(median(v), 1),
            "price_m2_min": round(min(v), 1),
            "price_m2_max": round(max(v), 1),
            "lotteries": len(v),
            "units": by_year_units[y],
        }
        for y, v in sorted(by_year.items())
    ]

    # Честная динамика: только районы, где есть цены минимум в двух разных годах
    hood_years: dict[str, dict[int, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for r in mine:
        hood_years[r["neighborhood"]][r["date"].year].append(r["price_m2"])

    neighborhoods = []
    for hood, years in hood_years.items():
        ys = sorted(years)
        series = [
            {"year": y, "price_m2": round(median(years[y]), 1), "lots": len(years[y])}
            for y in ys
        ]
        cagr = None
        if len(ys) >= 2 and ys[-1] > ys[0]:
            first, last = median(years[ys[0]]), median(years[ys[-1]])
            span = ys[-1] - ys[0]
            if first and last and span:
                cagr = round(((last / first) ** (1 / span) - 1) * 100, 1)
        neighborhoods.append(
            {
                "name": hood,
                "years": len(ys),
                "first_year": ys[0],
                "last_year": ys[-1],
                "first_price": series[0]["price_m2"],
                "last_price": series[-1]["price_m2"],
                "cagr_pct": cagr,
                "series": series,
            }
        )
    neighborhoods.sort(key=lambda h: (h["cagr_pct"] is None, -(h["cagr_pct"] or 0)))

    # Честный ценовой индекс — цепной (Laspeyres-подобный).
    #
    # Почему нельзя просто построить график «цена по городу»: в разные годы государство
    # разыгрывало разные районы, и общий ряд растёт уже потому, что новый район дороже.
    # Цепной индекс сравнивает ТОЛЬКО те районы, которые встречаются в обоих соседних годах,
    # и перемножает такие год-к-году отношения. Там, где пересечения нет, связь рвётся —
    # и мы это показываем явно, вместо того чтобы рисовать красивую ложную кривую.
    index = {"построен": False, "база": None, "ряд": [], "разрывы": [], "покрытие": None}
    if len(timeline) >= 2:
        hood_year: dict[str, dict[int, float]] = collections.defaultdict(dict)
        for r in mine:
            y = r["date"].year
            hood_year[r["neighborhood"]].setdefault(y, []).append(r["price_m2"])
        med = {}
        for hood, years in hood_year.items():
            for y, vals in years.items():
                vs = sorted(vals)
                med[(hood, y)] = vs[len(vs) // 2] if len(vs) % 2 else (vs[len(vs) // 2 - 1] + vs[len(vs) // 2]) / 2

        years = sorted({r["date"].year for r in mine})
        base_year = years[0]
        index["база"] = base_year
        level = 100.0
        for i, y in enumerate(years):
            if i == 0:
                index["ряд"].append({"year": y, "index": 100.0, "районов_в_сравнении": None, "связь": "база"})
                continue
            prev = years[i - 1]
            shared = sorted(h for h in hood_year if (h, prev) in med and (h, y) in med)
            if not shared:
                index["разрывы"].append(
                    {
                        "между_годами": [prev, y],
                        "причина": "ни один район не встречается в обоих годах",
                        "районы_раньше": sorted(h for h in hood_year if (h, prev) in med),
                        "районы_позже": sorted(h for h in hood_year if (h, y) in med),
                    }
                )
                index["ряд"].append(
                    {"year": y, "index": None, "районов_в_сравнении": 0, "связь": "разрыв"}
                )
                continue
            ratio = sum(med[(h, y)] / med[(h, prev)] for h in shared) / len(shared)
            level *= ratio
            index["ряд"].append(
                {
                    "year": y,
                    "index": round(level, 2),
                    "районов_в_сравнении": len(shared),
                    "связь": "прямая",
                    "изменение_к_пред_году_pct": round((ratio - 1) * 100, 2),
                }
            )
        linked = [p for p in index["ряд"] if p["связь"] in ("база", "прямая")]
        index["построен"] = len(linked) >= 2 and not index["разрывы"]
        index["частично"] = len(linked) >= 2
        index["покрытие"] = f"{len(linked)} из {len(years)} годов связаны напрямую"
        if linked:
            index["итог_pct"] = round(linked[-1]["index"] - 100, 1)
            first_y, last_y = linked[0]["year"], linked[-1]["year"]
            if last_y > first_y:
                index["cagr_pct"] = round(
                    ((linked[-1]["index"] / 100) ** (1 / (last_y - first_y)) - 1) * 100, 2
                )
            index["период"] = [first_y, last_y]

    # Место города среди всех: берём последнюю по дате цену каждого города
    last_seen: dict[str, tuple[dt.date, float]] = {}
    for r in recs:
        c = r["city"]
        if c not in last_seen or r["date"] > last_seen[c][0]:
            last_seen[c] = (r["date"], r["price_m2"])
    ranking = sorted(
        ({"city": c, "price_m2": v[1], "as_of": v[0].isoformat()} for c, v in last_seen.items()),
        key=lambda x: -x["price_m2"],
    )
    my_rank = next((i + 1 for i, x in enumerate(ranking) if x["city"] == common.norm_city(city)), None)

    # Сравнение с ближайшими по цене городами
    my_price = last_seen.get(common.norm_city(city), (None, None))[1]
    peers = []
    if my_price:
        peers = sorted(
            (x for x in ranking if x["city"] != common.norm_city(city)),
            key=lambda x: abs(x["price_m2"] - my_price),
        )[:8]

    return {
        "city": city,
        "code_check": code_check,
        "records": len(mine),
        "all_records": len(recs),
        "cities_covered": len(ranking),
        "timeline": timeline,
        "price_index": index,
        "neighborhoods": neighborhoods,
        "ranking": ranking,
        "my_rank": my_rank,
        "my_latest_price": my_price,
        "peers": peers,
        "total_units_lotteries": sum(r["units"] for r in mine),
        "total_winners": sum(r["winners"] for r in mine),
        "total_subscribers": sum(r["subscribers"] for r in mine),
        "projects": sorted(
            (
                {
                    "project": r["project"],
                    "neighborhood": r["neighborhood"],
                    "price_m2": r["price_m2"],
                    "date": r["date"].isoformat(),
                    "units": r["units"],
                    "winners": r["winners"],
                    "subscribers": r["subscribers"],
                    "status": r["status"],
                    "provider": r["provider"],
                }
                for r in mine
            ),
            key=lambda x: x["date"],
            reverse=True,
        ),
    }


def analyze_supply(city: str) -> dict:
    # Планировочный запас
    plan_rows = common.datastore_rows(common.DATASETS["planning_inventory"]["resource_id"])
    plans = [r for r in plan_rows if common.is_city(r, "planning_inventory", city)]
    by_stage: dict[str, int] = collections.defaultdict(int)
    plan_list = []
    for r in plans:
        stage = " ".join(str(r.get("שלב תכנוני") or "?").split())
        units = int(common.num(r.get("יחד פוטנציאל לשיווק")))
        by_stage[stage] += units
        plan_list.append(
            {
                "plan_number": " ".join(str(r.get("מספר תוכנית") or "").split()),
                "name": " ".join(str(r.get("שם תוכנית") or "").split()),
                "stage": stage,
                "developer": " ".join(str(r.get("יזם תכנון") or "").split()),
                "units": units,
                "threshold_date": " ".join(str(r.get("תאריך קיום תנאי סף") or "").split()),
                "approved_date": " ".join(str(r.get("תאריך פרסום לאישור ברשומות") or "").split()),
                "rmi_link": r.get("קישור לאתר רשות מקרקעי ישראל") or "",
                "mavat_link": r.get("קישור לאתר מנהל תכנון") or "",
            }
        )
    plan_list.sort(key=lambda x: -x["units"])

    # Реновация
    ur_rows = common.datastore_rows(common.DATASETS["urban_renewal"]["resource_id"])
    ur = [r for r in ur_rows if common.is_city(r, "urban_renewal", city)]
    renewal = []
    for r in ur:
        renewal.append(
            {
                "complex": " ".join(str(r.get("ShemMitcham") or "").split()),
                "number": r.get("MisparMitham"),
                "existing_units": int(common.num(r.get("YachadKayam"))),
                "added_units": int(common.num(r.get("YachadTosafti"))),
                "planned_units": int(common.num(r.get("YachadMutza"))),
                "declared": " ".join(str(r.get("TaarichHachraza") or "").split()),
                "track": " ".join(str(r.get("Maslul") or "").split()),
                "status": " ".join(str(r.get("Status") or "").split()),
                "permits_total": r.get("SachHeterim"),
                "in_progress": r.get("Bebitzua"),
                "plan_number": " ".join(str(r.get("MisparTochnit") or "").split()),
                "map_link": r.get("KishurLaMapa") or "",
                "year_effective": r.get("ShnatMatanTokef"),
            }
        )
    renewal.sort(key=lambda x: -x["planned_units"])

    # Ход строительства
    con_rows = common.datastore_rows(common.DATASETS["construction_progress"]["resource_id"])
    con = [r for r in con_rows if common.is_city(r, "construction_progress", city)]
    sites: dict[str, dict] = collections.defaultdict(lambda: {"units": 0, "projects": 0, "last_contract": ""})
    for r in con:
        site = " ".join(str(r.get("ATAR") or "?").split())
        sites[site]["units"] += int(common.num(r.get("YEHIDOT_BINYAN")))
        sites[site]["projects"] += 1
        d = " ".join(str(r.get("TAARICH_KOBEA") or "").split())
        sites[site]["last_contract"] = max(sites[site]["last_contract"], d)
    construction = sorted(
        ({"site": k, **v} for k, v in sites.items()), key=lambda x: -x["units"]
    )

    return {
        "planning": {
            "plans": plan_list,
            "total_units": sum(p["units"] for p in plan_list),
            "by_stage": dict(sorted(by_stage.items(), key=lambda kv: -kv[1])),
        },
        "renewal": {
            "complexes": renewal,
            "existing_units": sum(x["existing_units"] for x in renewal),
            "added_units": sum(x["added_units"] for x in renewal),
            "planned_units": sum(x["planned_units"] for x in renewal),
            "approved_and_active": sum(
                1 for x in renewal if "מאושרת" in x["status"]
            ),
        },
        "construction": {
            "sites": construction,
            "total_units": sum(x["units"] for x in construction),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="קרית גת")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    prices = analyze_prices(args.city)
    supply = analyze_supply(args.city)

    caveats = [
        "Цены — по льготной программе «Дира ба-анаха», они ниже свободного рынка.",
        "Реестр всех сделок nadlan.gov.il закрыт reCAPTCHA Enterprise — свободные рыночные цены по конкретным квартирам недоступны.",
        "Индекс цен на жильё ЦСБ из песочницы не отдаёт JSON, поэтому общерыночный тренд по Южному округу не приведён.",
        "Общий ряд «по городу» составной: в разные годы разыгрывались разные районы, поэтому его рост частично отражает смену района, а не рост цен. Смотрите динамику внутри одного района.",
    ]
    idx = prices.get("price_index") or {}
    if idx.get("разрывы"):
        for g in idx["разрывы"]:
            caveats.append(
                f"Цепной индекс рвётся между {g['между_годами'][0]} и {g['между_годами'][1]}: "
                f"раньше разыгрывались {' / '.join(g['районы_раньше'])}, "
                f"позже — {' / '.join(g['районы_позже'])}. Сравнивать эти годы напрямую нельзя."
            )

    cc = prices.get("code_check") or {}
    if not cc.get("корректно", True):
        caveats.insert(
            0,
            f"ОШИБКА ДАННЫХ: под кодом ЛАМАС {cc.get('код_ламас')} найдены города {cc.get('города_под_этим_кодом')} — выборка загрязнена.",
        )

    result = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "city": args.city,
        "prices": prices,
        "supply": supply,
        "caveats": caveats,
    }

    path = args.out or os.path.join(OUT_DIR, f"{common.norm_city(args.city).replace(' ', '_')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    # Краткая сводка в консоль
    print(f"=== {args.city} ===")
    print(f"записей по городу: {prices['records']} из {prices['all_records']} ({prices['cities_covered']} городов)")
    if prices["my_rank"]:
        print(f"место среди городов по последней цене за м²: {prices['my_rank']} из {prices['cities_covered']}")
        print(f"последняя цена за м²: ₪{money(prices['my_latest_price'])}")
    print("\nдинамика по годам (медиана ₪/м²):")
    for t in prices["timeline"]:
        print(f"   {t['year']}: ₪{money(t['price_m2_median'])}  (лотерей {t['lotteries']}, квартир {t['units']})")
    print("\nчестная динамика внутри районов:")
    for h in prices["neighborhoods"]:
        if h["cagr_pct"] is not None:
            print(f"   {h['name']}: {h['first_year']} ₪{money(h['first_price'])} → {h['last_year']} ₪{money(h['last_price'])}  = {h['cagr_pct']:+.1f}% в год")
        else:
            print(f"   {h['name']}: только {h['years']} год(а), тренд не посчитать — ₪{money(h['last_price'])}")
    idx = prices.get("price_index") or {}
    if idx:
        print("\nцепной ценовой индекс (сравниваются только общие районы соседних лет):")
        print(f"   покрытие: {idx.get('покрытие')}")
        for pnt in idx.get("ряд", []):
            if pnt.get("index") is None:
                print(f"   {pnt['year']}: РАЗРЫВ — нет общих районов с прошлым годом")
            else:
                d = pnt.get("изменение_к_пред_году_pct")
                print(f"   {pnt['year']}: {pnt['index']:.1f}"
                      + (f"  ({d:+.2f}% к прошлому году" if d is not None else "  (база")
                      + f", районов в сравнении: {pnt.get('районов_в_сравнении') if d is not None else '—'})")
        if idx.get("итог_pct") is not None:
            print(f"   итог за {idx.get('период')}: {idx['итог_pct']:+.1f}%"
                  + (f"  = {idx['cagr_pct']:+.2f}% в год" if idx.get('cagr_pct') is not None else ""))
        for g in idx.get("разрывы", []):
            print(f"   ⚠️ разрыв {g['между_годами'][0]}→{g['между_годами'][1]}: "
                  f"{' / '.join(g['районы_раньше'])} сменились на {' / '.join(g['районы_позже'])}")
    if prices.get("code_check"):
        cc = prices["code_check"]
        print(f"\nсамопроверка кода города: ЛАМАС {cc['код_ламас']} → {cc['города_под_этим_кодом']} "
              f"({'OK' if cc['корректно'] else 'ОШИБКА, в выборке чужой город!'})")
    print("\nбудущее предложение:")
    print(f"   планов РМАИ: {len(supply['planning']['plans'])}, потенциальных квартир: {money(supply['planning']['total_units'])}")
    for stage, u in supply["planning"]["by_stage"].items():
        print(f"      {stage}: {money(u)}")
    print(f"   комплексов реновации: {len(supply['renewal']['complexes'])}, "
          f"{money(supply['renewal']['existing_units'])} существующих → {money(supply['renewal']['planned_units'])} планируемых")
    print(f"   строек минстроя: {len(supply['construction']['sites'])} площадок, {money(supply['construction']['total_units'])} квартир")
    print(f"\nсохранено: {path}")


if __name__ == "__main__":
    main()
