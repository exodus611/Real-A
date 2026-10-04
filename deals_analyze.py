"""
Цены на квартиры в конкретном городе по РЕАЛЬНЫМ сделкам налогового управления.

Классификация «новая / вторичка» — содержательная часть, а не формальность:
в реестре year_built для готового жилья означает год постройки, а для покупки
на этапе котлована — ПЛАНОВЫЙ ГОД СДАЧИ. Поэтому в данных встречаются 2029 и 2030.
Новая квартира = year_built >= год сделки. Кто сравнивает year_built с «недавно построен»,
тот записывает весь котлован во вторичку и получает вдвое заниженную долю новых сделок.

Запуск:
    python3 deals_analyze.py --city "קרית גת"
    python3 deals_analyze.py --city "קרית גת" --since 2015 --telegram-dry
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os

import deals

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "analysis")

# Здравый фильтр цены: отсекает передачи долей, паркинги, оформленные как квартира,
# и прочий мусор, который ломает медиану снизу.
MIN_AMOUNT = 150_000
MIN_AREA = 20
MAX_AREA = 400


def classify(year_built, deal_year) -> str:
    if year_built is None or deal_year is None:
        return "неизвестно"
    if year_built > deal_year:
        return "новая (котлован)"      # дом ещё не сдан, указан плановый год
    if year_built >= deal_year - 2:
        return "новая (свежая)"        # сдан в год сделки или двумя годами ранее
    return "вторичка"


def clean(rows: list[dict]) -> tuple[list[dict], collections.Counter]:
    """Привести текстовые колонки реестра к числам и отбросить заведомый мусор."""
    out = []
    dropped = collections.Counter()
    for r in rows:
        dy = deals.year_of(r.get("deal_date"))
        amount = deals.to_int(r.get("deal_amount"))
        area = deals.to_float(r.get("asset_area"))
        yb = deals.to_int(r.get("year_built"))
        portion = deals.to_float(r.get("portion")) or 1.0

        if dy is None:
            dropped["нет даты"] += 1
            continue
        if amount is None or amount < MIN_AMOUNT:
            dropped["цена ниже порога или пустая"] += 1
            continue
        if area is None or not (MIN_AREA <= area <= MAX_AREA):
            dropped["площадь вне диапазона"] += 1
            continue
        if portion <= 0:
            dropped["нулевая доля"] += 1
            continue

        seg = classify(yb, dy)
        out.append(
            {
                "year": dy,
                "date": r.get("deal_date"),
                "amount": amount,
                "area": area,
                "rooms": deals.to_int(r.get("room_num")),
                "year_built": yb,
                "portion": portion,
                "ppsqm": amount / (area * portion),   # цена за м² с учётом проданной доли
                "nature": (r.get("deal_nature") or "").strip(),
                "gush": str(r.get("gush") or "").strip(),
                "helka": str(r.get("chelka") or "").strip(),
                "segment": seg,
            }
        )
    return out, dropped


def agg(rows: list[dict]) -> dict:
    if not rows:
        return {"deals": 0}
    return {
        "deals": len(rows),
        "median_price": round(deals.median([r["amount"] for r in rows]) or 0),
        "median_ppsqm": round(deals.median([r["ppsqm"] for r in rows]) or 0),
        "median_area": round(deals.median([r["area"] for r in rows]) or 0),
        "median_rooms": deals.median([r["rooms"] for r in rows]),
        "p25_price": round(deals.median(sorted([r["amount"] for r in rows])[: len(rows) // 2] or [0])),
        "p75_price": round(deals.median(sorted([r["amount"] for r in rows])[len(rows) // 2 :] or [0])),
    }


def by_key(rows: list[dict], key) -> list[dict]:
    groups = collections.defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for k, v in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if k is None:
            continue
        out.append({"key": k, **agg(v)})
    return out


def pct(new, old):
    if not old:
        return None
    return round((new / old - 1) * 100, 1)


def analyze(city: str, since: int) -> dict:
    canon, code, total_deals = deals.resolve_city(city)
    rows_raw = deals.fetch_apartment_deals(code, since_year=since)
    rows, dropped = clean(rows_raw)

    new_all = [r for r in rows if r["segment"].startswith("новая")]
    second = [r for r in rows if r["segment"] == "вторичка"]
    pit = [r for r in rows if r["segment"] == "новая (котлован)"]

    years_all = by_key(rows, lambda r: r["year"])
    years_new = by_key(new_all, lambda r: r["year"])
    years_second = by_key(second, lambda r: r["year"])

    # Сопоставление по годам: новые против вторички
    yn = {y["key"]: y for y in years_new}
    ys = {y["key"]: y for y in years_second}
    ya = {y["key"]: y for y in years_all}
    compare = []
    for y in sorted(ya):
        compare.append(
            {
                "year": y,
                "всего_сделок": ya[y]["deals"],
                "новых_сделок": yn.get(y, {}).get("deals", 0),
                "доля_новых_pct": round(yn.get(y, {}).get("deals", 0) / ya[y]["deals"] * 100, 1),
                "медиана_все": ya[y]["median_price"],
                "медиана_новые": yn.get(y, {}).get("median_price"),
                "медиана_вторичка": ys.get(y, {}).get("median_price"),
                "м2_все": ya[y]["median_ppsqm"],
                "м2_новые": yn.get(y, {}).get("median_ppsqm"),
                "м2_вторичка": ys.get(y, {}).get("median_ppsqm"),
                "премия_новых_к_вторичке_pct": (
                    pct(yn[y]["median_ppsqm"], ys[y]["median_ppsqm"]) if y in yn and y in ys else None
                ),
                "изменение_м2_новых_к_пред_году_pct": None,
            }
        )
    for i in range(1, len(compare)):
        prev, cur = compare[i - 1], compare[i]
        if prev.get("м2_новые") and cur.get("м2_новые"):
            cur["изменение_м2_новых_к_пред_году_pct"] = pct(cur["м2_новые"], prev["м2_новые"])

    # Последние полные годы для заголовка
    full_years = [c for c in compare if c.get("м2_новые")]
    latest = full_years[-1] if full_years else None
    prev = full_years[-2] if len(full_years) > 1 else None
    five_back = next((c for c in full_years if c["year"] == (latest["year"] - 5)), None) if latest else None

    # Районы = гуш/хелька. Названий улиц в реестре нет, привязка к адресу есть
    # далеко не у всех участков, поэтому группируем по кадастровому делению.
    areas = by_key(new_all or rows, lambda r: f"{r['gush']}/{r['helka']}" if r["gush"] else None)
    areas = [a for a in areas if a["deals"] >= 8]
    areas.sort(key=lambda a: -a["median_ppsqm"])

    # Последние сделки — чтобы было видно живые примеры.
    # Даты в реестре текстом в формате DD/MM/YYYY, поэтому сортировать строкой нельзя.
    def iso(d):
        s = str(d or "")
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}" if len(s) == 10 else ""

    recent = sorted(rows, key=lambda r: iso(r["date"]), reverse=True)[:25]

    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "источник": {
            "название": "עסקאות נדל\"ן — רשות המסים (מיסוי מקרקעין)",
            "первоисточник": "https://nadlan.taxes.gov.il/svinfonadlan2010/startpage.aspx",
            "доступ": "https://www.over.org.il/api/append/.../datastore_search_sql",
            "всего_сделок_в_реестре": deals.stats().get("deals"),
            "период_реестра": [deals.stats().get("first_deal"), deals.stats().get("last_deal")],
        },
        "город": {"запрос": city, "в_реестре": canon, "код": code, "всего_сделок_в_реестре": total_deals},
        "выборка": {
            "после_фильтра_типов_жилья": len(rows_raw),
            "после_очистки": len(rows),
            "отброшено": dict(dropped),
            "порог_цены": MIN_AMOUNT,
            "диапазон_площади": [MIN_AREA, MAX_AREA],
            "с_начала_года": since,
        },
        "сводка": {
            "всего": agg(rows),
            "новые": agg(new_all),
            "вторичка": agg(second),
            "котлован": agg(pit),
        },
        "по_годам": compare,
        "заголовок": {
            "год": latest["year"] if latest else None,
            "м2_новые": latest.get("м2_новые") if latest else None,
            "медиана_новые": latest.get("медиана_новые") if latest else None,
            "изменение_за_год_pct": latest.get("изменение_м2_новых_к_пред_году_pct") if latest else None,
            "изменение_за_5_лет_pct": (
                pct(latest["м2_новые"], five_back["м2_новые"]) if latest and five_back and five_back.get("м2_новые") else None
            ),
            "премия_новых_к_вторичке_pct": latest.get("премия_новых_к_вторичке_pct") if latest else None,
            "доля_новых_pct": latest.get("доля_новых_pct") if latest else None,
        },
        "районы_гуш_хелька": areas,
        "последние_сделки": [
            {
                "дата": r["date"],
                "цена": r["amount"],
                "м2": round(r["area"]),
                "комнат": r["rooms"],
                "год_постройки": r["year_built"],
                "цена_за_м2": round(r["ppsqm"]),
                "сегмент": r["segment"],
                "тип": r["nature"],
                "гуш_хелька": f"{r['gush']}/{r['helka']}",
            }
            for r in recent
        ],
        "оговорки": [
            "Сумма сделки — заявленная налоговому управлению, а не проверенная рыночная цена.",
            "Одна строка может быть и квартирой, и целым зданием, поэтому везде медиана, не среднее.",
            "Цена за м² = сумма / (площадь × проданная доля); при мелких долях точность падает.",
            "Адресов в реестре нет — только гуш и хелька. Названия улиц добавляет over.org.il "
            "через привязку точки к участку, и для малых городов она покрыта слабо "
            "(в последних 200 сделках Кирьят-Гата адрес нашёлся у 9%).",
            "Тип «מגורים» в выборку не входит: в 68% таких строк нет площади и в 99% нет комнат — "
            "это земля и права, а не квартиры.",
        ],
    }


def telegram_text(res: dict) -> str:
    h = res["заголовок"]
    c = res["город"]
    s = res["сводка"]
    lines = [
        f"🏠 {c['в_реестре']} — новые квартиры",
        f"по данным реестра сделок налогового управления",
        "",
    ]
    if h.get("м2_новые"):
        lines.append(f"₪/м² (новые, {h['год']}): {h['м2_новые']:,}".replace(",", " "))
    if h.get("медиана_новые"):
        lines.append(f"Медианная цена новой: ₪{h['медиана_новые']:,}".replace(",", " "))
    if h.get("изменение_за_год_pct") is not None:
        lines.append(f"За год: {h['изменение_за_год_pct']:+}%")
    if h.get("изменение_за_5_лет_pct") is not None:
        lines.append(f"За 5 лет: {h['изменение_за_5_лет_pct']:+}%")
    if h.get("премия_новых_к_вторичке_pct") is not None:
        lines.append(f"Новые дороже вторички на {h['премия_новых_к_вторичке_pct']:+}% за м²")
    if h.get("доля_новых_pct") is not None:
        lines.append(f"Доля новых в сделках: {h['доля_новых_pct']}%")
    lines += [
        "",
        f"Всего сделок в выборке: {s['всего'].get('deals', 0):,}".replace(",", " "),
        f"  новых: {s['новых'].get('deals', 0):,}  вторичка: {s['вторичка'].get('deals', 0):,}".replace(",", " "),
        "",
        "Динамика ₪/м², новые:",
    ]
    for y in res["по_годам"][-8:]:
        if y.get("м2_новые"):
            chg = y.get("изменение_м2_новых_к_пред_году_pct")
            tail = f"  ({chg:+}%)" if chg is not None else ""
            lines.append(f"  {y['year']}: ₪{y['м2_новые']:,} / {y['новых_сделок']} сделок{tail}".replace(",", " "))
    top = res["районы_гуш_хелька"][:5]
    if top:
        lines += ["", "Дороже всего за м² (гуш/хелька):"]
        for a in top:
            lines.append(f"  {a['key']}: ₪{a['median_ppsqm']:,} / {a['deals']} сделок".replace(",", " "))
    lines += ["", f"обновлено {res['generated_at'][:16]}"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="קרית גת")
    ap.add_argument("--since", type=int, default=2010)
    ap.add_argument("--telegram-dry", action="store_true", help="напечатать текст сообщения, не отправляя")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    res = analyze(args.city, args.since)

    path = os.path.join(OUT_DIR, f"deals_{res['город']['код']}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    txt = telegram_text(res)
    if args.telegram_dry:
        print(txt)
    else:
        sent = deals_notify(txt)
        print(txt)
        print("\n---")
        print("telegram:", sent)
    print(f"\nсохранено: {path}")


def deals_notify(text: str) -> str:
    token, chat = os.environ.get("WATCH_TELEGRAM_TOKEN"), os.environ.get("WATCH_TELEGRAM_CHAT")
    if not token or not chat:
        return "не настроен (нужны WATCH_TELEGRAM_TOKEN и WATCH_TELEGRAM_CHAT)"
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode({"chat_id": chat, "text": text[:4096]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers=deals.UA
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return "отправлено"
    except Exception as e:  # noqa: BLE001
        return f"ошибка отправки: {e}"


if __name__ == "__main__":
    main()
