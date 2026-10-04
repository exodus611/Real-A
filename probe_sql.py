"""Разведка: какие агрегатные функции пропускает SQL-движок over.org.il.

Зачем: постраничная выгрузка 26 тысяч строк упирается в лимит 20 запросов в минуту.
Если медиану можно посчитать на сервере, весь анализ по городу — это 3-4 запроса.
"""

import sys
import time

sys.path.insert(0, ".")
import deals  # noqa: E402

T = deals.TABLE
BASE = (
    f"FROM {T} WHERE settlement_code='2630' "
    f"AND deal_nature IN ({deals.in_list(deals.APT_NATURES)}) "
    f"AND deal_amount ~ '^[0-9]+$' AND deal_amount::numeric >= 150000 "
    f"AND asset_area ~ '^[0-9]+([.][0-9]+)?$' AND asset_area::numeric BETWEEN 20 AND 400"
)

TESTS = [
    ("count", f"SELECT count(*) AS n {BASE}"),
    (
        "avg с приведением",
        f"SELECT avg(deal_amount::numeric)::int AS a {BASE}",
    ),
    (
        "percentile_disc",
        f"SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) AS m {BASE}",
    ),
    (
        "percentile_cont",
        f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY deal_amount::numeric) AS m {BASE}",
    ),
    (
        "median()",
        f"SELECT median(deal_amount::numeric) AS m {BASE}",
    ),
    (
        "mode() within group",
        f"SELECT mode() WITHIN GROUP (ORDER BY deal_amount::numeric) AS m {BASE}",
    ),
    (
        "array_agg + индекс",
        f"SELECT (array_agg(deal_amount::numeric ORDER BY deal_amount::numeric))[ceil(count(*)/2.0)::int] AS m {BASE}",
    ),
    (
        "width_bucket гистограмма",
        f"""SELECT width_bucket(deal_amount::numeric / (asset_area::numeric * greatest(portion::numeric,0.001)),
             0, 40000, 40) AS b, count(*) AS n {BASE}
             AND portion ~ '^[0-9]+([.][0-9]+)?$' AND year_built ~ '^[0-9]{{4}}$'
             GROUP BY b ORDER BY b""",
    ),
    (
        "group by год + avg",
        f"SELECT substr(deal_date,7,4) AS y, count(*) AS n, avg(deal_amount::numeric)::int AS a {BASE} GROUP BY y ORDER BY y",
    ),
]

for name, q in TESTS:
    try:
        rec = deals.sql(q)
        head = rec[:3]
        print(f"✓ {name:26} строк={len(rec)}  {head}")
    except Exception as e:
        print(f"✗ {name:26} {str(e)[:150]}")
    time.sleep(3.2)  # лимит 20 запросов в минуту — держим запас
