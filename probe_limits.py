import json, urllib.request, urllib.parse, urllib.error

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DS = "fd06f5ae-8a4f-4120-b275-8a514ad23499"
T = "append_taxes_nadlan_full_f41fb496_fd06f5ae"


def sql(q, to=300):
    u = f"https://www.over.org.il/api/append/{DS}/datastore_search_sql?" + urllib.parse.urlencode({"sql": q})
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=to).read())
        return d["result"]
    except urllib.error.HTTPError as e:
        return {"ERR": e.code, "body": e.read().decode("utf-8", "replace")[:200]}


for lim in (200, 500, 1000, 2000, 5000):
    r = sql(f"SELECT deal_date, deal_amount FROM {T} WHERE settlement_code='2630' LIMIT {lim}")
    if "ERR" in r:
        print(f"LIMIT {lim}: ERR {r['ERR']} {r['body'][:70]}")
    else:
        print(f"LIMIT {lim}: получено {len(r['records'])} строк, truncated={r.get('truncated')}")

r = sql(f"SELECT deal_date, deal_amount FROM {T} WHERE settlement_code='2630' LIMIT 200 OFFSET 200")
print("OFFSET работает:", "ERR" not in r and len(r["records"]))

print("\n=== /api/deals/search лимиты ===")
for lim in (50, 200, 500, 1000):
    u = f"https://www.over.org.il/api/deals/search?settlement_code=2630&limit={lim}"
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=180).read())
        print(f"  limit={lim}: отдано {len(d.get('data', []))} | total={d.get('total')} | capped={d.get('total_capped')}")
    except Exception as e:
        print(f"  limit={lim}: ERR {e}")
