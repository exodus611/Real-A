"""
Проверка двух подозрительных результатов по Кирьят-Гату:

1. «Новые дешевле вторички» (₪13 879 против ₪16 038 за м²) — так почти не бывает.
   Гипотеза: это эффект смеси. Медиана «новых» считается по всем новым сделкам сразу,
   а в 2026 их 73% от всех, и среди них много льготных/котлованных.
   Проверка — взять ОДИН гомогенный сегмент (4 комнаты, квартира в многоквартирном доме)
   и посмотреть, что там. Если и в нём новые дешевле вторички — значит дело не в смеси.

2. «−25% за год» в 2026. Реестр пополняется с задержкой: последняя сделка 2026-08-04,
   а выгрузка сделана 2026-09-19. Плюс 2026-й неполный. Значит сравнивать его с 2025
   в лоб нельзя. Проверка — сравнить ОДИНАКОВЫЕ месячные окна (январь–август) разных лет.

Запуск: python3 validate.py --city "קרית גת" --since 2015
"""

from __future__ import annotations

import argparse
import json
import os

import deals
import deals_agg as da

T = deals.TABLE
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis")


def q_rooms_mix(code: int, since: int) -> list[dict]:
    """4 комнаты, квартира в многоквартирном доме: новые против вторички по годам."""
    seg = da.SEGMENT_CASE
    return da.sql_slow(
        f"""
SELECT substr(deal_date,7,4) AS год,
  count(*) FILTER (WHERE {seg} <> 'вторичка') AS новых,
  count(*) FILTER (WHERE {seg} = 'вторичка') AS вторичка,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM})
    FILTER (WHERE {seg} <> 'вторичка')::int AS м2_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)
    FILTER (WHERE {seg} <> 'вторичка')::int AS цена_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM})
    FILTER (WHERE {seg} = 'вторичка')::int AS м2_вторичка,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)
    FILTER (WHERE {seg} = 'вторичка')::int AS цена_вторичка,
  count(*) FILTER (WHERE {seg} = 'новая_котлован') AS котлован
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
  AND deal_nature = 'דירה בבית קומות' AND room_num = '4'
GROUP BY substr(deal_date,7,4) ORDER BY год"""
    )


def q_same_window(code: int, years: list[int], months: tuple[int, int]) -> list[dict]:
    """Одинаковое месячное окно в разных годах — снимает неполноту последнего года."""
    seg = da.SEGMENT_CASE
    ys = ", ".join(f"'{y}'" for y in years)
    return da.sql_slow(
        f"""
SELECT substr(deal_date,7,4) AS год, count(*) AS сделок,
  count(*) FILTER (WHERE {seg} <> 'вторичка') AS новых,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM})::int AS м2_все,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY {da.PPSQM})
    FILTER (WHERE {seg} <> 'вторичка')::int AS м2_новые,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric)
    FILTER (WHERE {seg} <> 'вторичка')::int AS цена_новые
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)}
  AND substr(deal_date,7,4) IN ({ys})
  AND substr(deal_date,4,2)::int BETWEEN {months[0]} AND {months[1]}
GROUP BY substr(deal_date,7,4) ORDER BY год"""
    )


def q_pit_share(code: int, since: int) -> list[dict]:
    """Доля котлована среди новых — объясняет, откуда медианный год сдачи 2029."""
    seg = da.SEGMENT_CASE
    return da.sql_slow(
        f"""
SELECT substr(deal_date,7,4) AS год,
  count(*) FILTER (WHERE {seg} <> 'вторичка') AS новых,
  count(*) FILTER (WHERE {seg} = 'новая_котлован') AS котлован,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY year_built::numeric)
    FILTER (WHERE {seg} <> 'вторичка')::int AS медиана_года_сдачи,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric)
    FILTER (WHERE {seg} <> 'вторичка')::int AS медиана_площади_новых,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY asset_area::numeric)
    FILTER (WHERE {seg} = 'вторичка')::int AS медиана_площади_вторичка
FROM {T}
WHERE {da.BASE_WHERE.format(code=code)} AND substr(deal_date,7,4) >= '{since}'
GROUP BY substr(deal_date,7,4) ORDER BY год"""
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="קרית גת")
    ap.add_argument("--since", type=int, default=2015)
    args = ap.parse_args()

    canon, code, total = deals.resolve_city(args.city)
    st = deals.stats()
    print(f"{canon} (код {code}). Реестр: {st['deals']:,} сделок, выгрузка {st['scraped_at'][:10]}".replace(",", " "))

    print("\n=== 1. Гомогенный сегмент: 4 комнаты, квартира в многоквартирном доме ===")
    r1 = q_rooms_mix(code, args.since)
    print(f"{'год':>5} {'новых':>6} {'2чка':>6} {'м2 новые':>9} {'м2 втор':>8} {'цена нов':>10} {'цена втор':>10} {'котлован':>8}")
    for r in r1:
        print(f"{r['год']:>5} {r['новых'] or 0:>6} {r['вторичка'] or 0:>6} "
              f"{r['м2_новые'] or 0:>9} {r['м2_вторичка'] or 0:>8} "
              f"{r['цена_новые'] or 0:>10} {r['цена_вторичка'] or 0:>10} {r['котлован'] or 0:>8}")

    print("\n=== 2. Одинаковое окно январь–август (снимает неполноту 2026) ===")
    years = [int(r["год"]) for r in r1][-6:]
    r2 = q_same_window(code, years, (1, 8))
    print(f"{'год':>5} {'сделок':>7} {'новых':>6} {'м2 все':>8} {'м2 новые':>9} {'цена новые':>11}")
    prev = None
    for r in r2:
        chg = ""
        if prev and r["м2_новые"] and prev:
            chg = f"  ({(r['м2_новые'] / prev - 1) * 100:+.1f}% к пред. году)"
        print(f"{r['год']:>5} {r['сделок']:>7} {r['новых'] or 0:>6} {r['м2_все'] or 0:>8} "
              f"{r['м2_новые'] or 0:>9} {r['цена_новые'] or 0:>11}{chg}")
        if r["м2_новые"]:
            prev = r["м2_новые"]

    print("\n=== 3. Доля котлована среди новых ===")
    r3 = q_pit_share(code, args.since)
    print(f"{'год':>5} {'новых':>6} {'котлован':>8} {'доля %':>7} {'год сдачи':>9} {'площ. нов':>9} {'площ. втор':>10}")
    for r in r3:
        n, p = r["новых"] or 0, r["котлован"] or 0
        share = round(p / n * 100, 1) if n else 0
        print(f"{r['год']:>5} {n:>6} {p:>8} {share:>7} {r['медиана_года_сдачи'] or 0:>9} "
              f"{r['медиана_площади_новых'] or 0:>9} {r['медиана_площади_вторичка'] or 0:>10}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"validate_{code}.json")
    json.dump(
        {"город": canon, "код": code, "одинаковый_сегмент_4комн": r1,
         "окно_январь_август": r2, "доля_котлована": r3},
        open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1,
    )
    print(f"\nсохранено: {out}")


if __name__ == "__main__":
    main()
