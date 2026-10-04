"""
«Сторож» — следит за изменениями в официальных датасетах Израиля.

Как работает:
  1. Снимает метаданные датасета (номер версии, дату последнего изменения).
  2. Выкачивает все строки и считает отпечаток схемы и содержимого.
  3. Сравнивает с предыдущим снимком и выдаёт список того, что реально поменялось.

Запуск:
    python3 watch.py                 # снять снимок и сравнить с прошлым
    python3 watch.py --init          # первый запуск, просто записать базовый снимок
    python3 watch.py --city "קרית גת"  # дополнительно следить за конкретным городом

Снапшоты кладутся в snapshots/ как <dataset>__<timestamp>.json.
Код возврата 1 = изменения найдены (удобно для cron).
Telegram: задать WATCH_TELEGRAM_TOKEN и WATCH_TELEGRAM_CHAT.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request

import common

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "snapshots")
REPORT_DIR = os.path.join(HERE, "reports")


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stable_hash(obj) -> str:
    """Отпечаток, не зависящий от порядка ключей и переносов строк.

    Сначала прогоняем объект через JSON туда-обратно: это приводит кортежи к спискам.
    Без этого свежий снимок в памяти никогда не совпадёт с тем же снимком,
    прочитанным из файла, и сторож будет кричать о несуществующих изменениях.
    """
    obj = json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    payload = " ".join(payload.split())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def take_snapshot(key: str, meta_cfg: dict, city: str | None) -> dict:
    """Снять полный снимок одного датасета."""
    pkg = common.package_meta(meta_cfg["package"])
    resource = next(
        (r for r in pkg["resources"] if r["id"] == meta_cfg["resource_id"]), None
    )
    if resource is None:
        # Ресурс могли заменить — ищем по имени, это само по себе событие
        resource = next(
            (r for r in pkg["resources"] if r.get("name") == meta_cfg["resource_name"]),
            pkg["resources"][0] if pkg["resources"] else {},
        )

    rows = common.datastore_rows(resource.get("id", meta_cfg["resource_id"]))

    # Схема = имя колонки + тип значения в первой строке. Отслеживаем и то и другое:
    # CKAN иногда молча меняет тип (число превращается в строку), и это ломает расчёты.
    schema = sorted(
        (str(k), type(v).__name__) for k, v in (rows[0].items() if rows else [])
    )
    fields = [name for name, _ in schema]

    city_rows = []
    if city:
        city_rows = [r for r in rows if common.is_city(r, key, city)]

    # Агрегаты по городу. Правила:
    #   * только JSON-стабильные значения (числа, строки, словари строк) — никаких кортежей;
    #   * длинные списки сворачиваем в отпечаток + скаляры, иначе отчёт становится нечитаемым.
    city_stats: dict = {}
    if city:
        city_stats["строк"] = len(city_rows)
        city_stats["отпечаток"] = stable_hash(city_rows)

        if key == "mechir_lamishtaken":
            prices = sorted(
                {
                    (common.num(r.get("PriceForMeter")), str(r.get("LotteryExecutionDate"))[:10])
                    for r in city_rows
                    if common.num(r.get("PriceForMeter")) > 0
                }
            )
            city_stats["цен_за_метр"] = len(prices)
            city_stats["отпечаток_цен"] = stable_hash(prices)
            city_stats["макс_цена_за_метр"] = prices[-1][0] if prices else None
            city_stats["квартир_в_лотереях"] = int(
                sum(common.num(r.get("LotteryHousingUnits")) for r in city_rows)
            )
            city_stats["районов"] = len({" ".join(str(r.get("Neighborhood") or "").split()) for r in city_rows})
        elif key == "urban_renewal":
            city_stats["комплексов"] = len(city_rows)
            city_stats["существующих_единиц"] = int(
                sum(common.num(r.get("YachadKayam")) for r in city_rows)
            )
            city_stats["планируемых_единиц"] = int(
                sum(common.num(r.get("YachadMutza")) for r in city_rows)
            )
            # словарь «комплекс → статус»: JSON-стабильный, и из него видно, что именно сменилось
            city_stats["статусы"] = {
                " ".join(str(r.get("ShemMitcham")).split()): " ".join(str(r.get("Status")).split())
                for r in city_rows
            }
        elif key == "planning_inventory":
            city_stats["планов"] = len(city_rows)
            city_stats["потенциальных_единиц"] = int(
                sum(common.num(r.get("יחד פוטנציאל לשיווק")) for r in city_rows)
            )
            city_stats["отпечаток_планов"] = stable_hash(
                sorted(
                    (
                        str(r.get("מספר תוכנית") or "").strip(),
                        str(r.get("שלב תכנוני") or "").strip(),
                        common.num(r.get("יחד פוטנציאל לשיווק")),
                    )
                    for r in city_rows
                )
            )
        elif key == "construction_progress":
            city_stats["объектов"] = len(city_rows)
            city_stats["квартир"] = int(
                sum(common.num(r.get("YEHIDOT_BINYAN")) for r in city_rows)
            )

    return {
        "dataset": key,
        "ru": meta_cfg["ru"],
        "package_id": pkg.get("id"),
        "package_title": pkg.get("title"),
        "version": pkg.get("version"),
        "metadata_modified": pkg.get("metadata_modified"),
        "metadata_created": pkg.get("metadata_created"),
        "resource_id": resource.get("id"),
        "resource_name": resource.get("name"),
        "resource_last_modified": resource.get("last_modified"),
        "resource_created": resource.get("created"),
        "total_rows": len(rows),
        "fields": fields,
        "schema": schema,
        "schema_hash": stable_hash(schema),
        "rows_hash": stable_hash(rows),
        "city": city,
        "city_stats": city_stats,
        "taken_at": now_iso(),
    }


def latest_snapshot(key: str) -> dict | None:
    if not os.path.isdir(SNAP_DIR):
        return None
    files = sorted(
        f for f in os.listdir(SNAP_DIR) if f.startswith(f"{key}__") and f.endswith(".json")
    )
    if not files:
        return None
    with open(os.path.join(SNAP_DIR, files[-1]), encoding="utf-8") as fh:
        return json.load(fh)


def diff(old: dict, new: dict) -> list[str]:
    """Список человеческих сообщений о том, что изменилось."""
    out: list[str] = []
    if old is None:
        return out

    if old.get("resource_id") != new.get("resource_id"):
        out.append(
            "⚠️ заменили сам файл данных (resource id изменился) — "
            f"{old.get('resource_id')} → {new.get('resource_id')}"
        )
    if old.get("metadata_modified") != new.get("metadata_modified"):
        out.append(
            f"📅 датасет обновлён: {old.get('metadata_modified')} → {new.get('metadata_modified')}"
        )
    if old.get("schema_hash") != new.get("schema_hash"):
        old_schema = {k: v for k, v in (old.get("schema") or [])}
        new_schema = {k: v for k, v in (new.get("schema") or [])}
        added = sorted(set(new_schema) - set(old_schema))
        removed = sorted(set(old_schema) - set(new_schema))
        retyped = sorted(k for k in set(old_schema) & set(new_schema) if old_schema[k] != new_schema[k])
        parts = []
        if added:
            parts.append(f"добавились колонки: {', '.join(added)}")
        if removed:
            parts.append(f"убрали колонки: {', '.join(removed)}")
        if retyped:
            parts.append(
                "сменился тип значений: "
                + ", ".join(f"{k} ({old_schema[k]} → {new_schema[k]})" for k in retyped)
            )
        if parts:
            out.append("🧩 изменилась структура таблицы — " + "; ".join(parts))
    if old.get("total_rows") != new.get("total_rows"):
        d = new["total_rows"] - old["total_rows"]
        sign = "+" if d > 0 else ""
        out.append(
            f"🔢 строк в датасете: {old.get('total_rows')} → {new.get('total_rows')} ({sign}{d})"
        )
    elif old.get("rows_hash") != new.get("rows_hash"):
        out.append("✏️ количество строк то же, но содержимое изменилось (кто-то правил значения)")

    cs_old, cs_new = old.get("city_stats") or {}, new.get("city_stats") or {}
    if cs_old and cs_new:
        city = new.get("city")
        # Поля, которые либо служебные, либо обрабатываются отдельно ниже
        skip = {"отпечаток", "отпечаток_цен", "отпечаток_планов", "статусы"}

        for k in cs_new:
            if k in skip or cs_old.get(k) == cs_new.get(k):
                continue
            o, n = cs_old.get(k), cs_new.get(k)
            fmt = (lambda v: f"{v:,.0f}".replace(",", " ")) if isinstance(n, (int, float)) and not isinstance(n, bool) else str
            out.append(f"🏙️ {city}: {k}: {fmt(o) if o is not None else '—'} → {fmt(n)}")

        # Смена состава цен: хэш изменился, но количество то же — значит правили значения
        if (
            cs_old.get("отпечаток_цен") != cs_new.get("отпечаток_цен")
            and cs_old.get("цен_за_метр") == cs_new.get("цен_за_метр")
        ):
            out.append(f"🏙️ {city}: набор цен тот же по количеству, но значения изменились")

        if cs_old.get("отпечаток_планов") != cs_new.get("отпечаток_планов"):
            out.append(f"🏙️ {city}: изменился состав или этапы планов застройки")

        st_old, st_new = cs_old.get("статусы") or {}, cs_new.get("статусы") or {}
        for name in sorted(set(st_old) | set(st_new)):
            a, b = st_old.get(name), st_new.get(name)
            if a is None:
                out.append(f"🆕 {city}: новый комплекс реновации «{name}» — {b}")
            elif b is None:
                out.append(f"❌ {city}: комплекс «{name}» исчез из списка (был статус {a})")
            elif a != b:
                out.append(f"🔄 {city}: «{name}» сменил статус: {a} → {b}")
    return out


def send_telegram(text: str) -> bool:
    token = os.environ.get("WATCH_TELEGRAM_TOKEN")
    chat = os.environ.get("WATCH_TELEGRAM_CHAT")
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}
    ).encode()
    try:
        req = urllib.request.Request(url, data=body, headers=common.UA)
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  (Telegram не отправлен: {e})", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="первый запуск: только записать снимки")
    ap.add_argument("--city", default="קרית גת", help="город под прицелом")
    ap.add_argument("--only", nargs="*", help="только эти датасеты")
    args = ap.parse_args()

    os.makedirs(SNAP_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    keys = args.only or list(common.DATASETS)
    all_changes: dict[str, list[str]] = {}
    snapshots = {}

    for key in keys:
        cfg = common.DATASETS[key]
        try:
            snap = take_snapshot(key, cfg, args.city)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {key}: {e}")
            continue
        snapshots[key] = snap

        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(SNAP_DIR, f"{key}__{ts}.json"), "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1)

        prev = None
        if not args.init:
            files = sorted(
                f for f in os.listdir(SNAP_DIR) if f.startswith(f"{key}__") and f.endswith(".json")
            )
            if len(files) >= 2:
                with open(os.path.join(SNAP_DIR, files[-2]), encoding="utf-8") as fh:
                    prev = json.load(fh)

        changes = diff(prev, snap)
        all_changes[key] = changes
        flag = "🔴" if changes else "🟢"
        print(f"{flag} {snap['ru']}: строк {snap['total_rows']}, изменений {len(changes)}")
        for c in changes:
            print(f"     {c}")

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    total = sum(len(v) for v in all_changes.values())
    lines = [f"Отчёт сторожа — {stamp}", f"Город: {args.city}", f"Изменений: {total}", ""]
    for key, ch in all_changes.items():
        if ch:
            lines.append(f"▸ {common.DATASETS[key]['ru']}")
            lines.extend(f"   {c}" for c in ch)
            lines.append("")
    if total == 0 and not args.init:
        lines.append("Ничего не изменилось.")

    report_path = os.path.join(REPORT_DIR, f"report-{dt.datetime.now():%Y%m%d-%H%M%S}.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open(os.path.join(REPORT_DIR, "last.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"generated_at": now_iso(), "city": args.city, "total_changes": total,
             "changes": all_changes, "snapshots": snapshots},
            fh, ensure_ascii=False, indent=1,
        )
    print(f"\nотчёт: {report_path}")

    if total and not args.init:
        sent = send_telegram("\n".join(lines))
        print(f"telegram: {'отправлено' if sent else 'не настроен (WATCH_TELEGRAM_TOKEN/CHAT)'}")

    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
