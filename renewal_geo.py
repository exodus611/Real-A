"""
Границы комплексов реновации: какие конкретно участки (и значит — какие улицы) попадают
в каждый из них.

Зачем. Датасет минстроя «מתחמי התחדשות עירונית» (тот, что уже используется в market.py)
даёт по комплексу только имя, число квартир, статус и ссылку на план в mavat. Где именно
проходит граница — в таблице нет. Поэтому «все районы реновации» без геометрии остаются
списком названий: человек не может проверить, его дом в комплексе или нет.

Откуда берутся границы. У минстроя есть отдельный GIS-датасет
`gis_urban_renewal` («שכבות מידע גיאוגרפי — התחדשות עירונית»), ресурс
«מתחמי התחדשות עירונית מוכרזים» — шейпфайл с полигонами всех объявленных
комплексов страны (976 штук, обновлён 31.07.2026). В нём те же MisparProj,
что и в табличном датасете, поэтому слои стыкуются по номеру проекта.

Проекция. Шейпфайл лежит в Israel TM Grid (EPSG:2039, метры). Своими руками
пере投影тировать его в градусы здесь не нужно: over.org.il держит те же слои в PostGIS
уже в EPSG:4326 и отдаёт их как GeoJSON через GET /api/tables/{table}/features?bbox=...
То есть координаты приходят готовыми lon/lat, и пересекать их с точками участков
из реестра можно напрямую.

Как считается попадание. Участок — это полигон, а не точка: большой участок башни
может пересекать границу комплекса centroid-ом наружу. Поэтому здесь настоящее
отсечение полигонов (Сазерленд—Ходжман) и площадь пересечения в локальной
равновеликой проекции (метрический x = lon·111320·cos(lat), y = lat·110540).
Порог «в комплексе» — 50% площади участка. Между 5% и 50% участок помечается
как «частично», потому что граница комплекса режет его по живому.

Ограничение, которое надо держать в голове: сделки реестра привязаны к гуш и хелька
без под-гуша, поэтому на участках, где номер хелька повторяется в разных под-гушах,
одна и та же сделка может лечь в два места. Такие участки помечаются separately.
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
import geo

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "analysis")
BASE = "https://www.over.org.il"

# Слои over.org.il (PostGIS, EPSG:4326) — те же данные, что в шейпфайлах минстроя.
LAYER_RENEWAL = "append_gis_urban_renewal_fbc765da_a225ac29"   # объявленные комплексы
LAYER_MASTERPLAN = "append_gis_urban_renewal_fbc765da_c7587560"  # проекты комплексного развития
LAYER_PARCELS = "append_shape_ff3176b1"                        # кадастровые участки

RENEWAL_COLS = ("MisparProj,ShemMitcha,Yeshuv,plan_name,Kishur,TeurMaslul,TeurSugMas,"
                "codeStatus,taarichTok,yachadKaya,yachadMutz,YachadTosa,heara,Source")
PARCEL_COLS = "GUSH_NUM,GUSH_SUFFI,PARCEL,LEGAL_AREA,STATUS_TEX,LOCALITY_N,REG_MUN_NA"

# Насколько расширить габарит комплекса при отборе участков: ~450 м.
# Большие участки (промышленность, поля) бывают длиннее комплекса, и без запаса
# участок, который явно пересекает границу, не попал бы в выборку.
BUFFER_DEG = 0.004


# --------------------------------------------------------------------------- #
#  Геометрия: отсечение полигонов и площадь
# --------------------------------------------------------------------------- #

def _rings(geom) -> list[list[tuple[float, float]]]:
    """Все внешние и внутренние кольца полигона/мультиполигона одним списком."""
    t = geom.get("type")
    c = geom.get("coordinates") or []
    if t == "Polygon":
        return [[(p[0], p[1]) for p in ring] for ring in c]
    if t == "MultiPolygon":
        return [[(p[0], p[1]) for p in ring] for poly in c for ring in poly]
    return []


def _outer_rings(geom) -> list[list[tuple[float, float]]]:
    """Только внешние кольца — дыры (дворы) при пересечении учитывать не нужно:
    площадь двора мала относительно застроенного участка, а учёт дыр ломает
    отсечение Сазерленда—Ходжмана, которое рассчитано на выпуклый клиппер."""
    t = geom.get("type")
    c = geom.get("coordinates") or []
    if t == "Polygon":
        return [[(p[0], p[1]) for p in c[0]]] if c else []
    if t == "MultiPolygon":
        return [[(p[0], p[1]) for p in poly[0]] for poly in c if poly]
    return []


def _to_m(ring, lat0: float) -> list[tuple[float, float]]:
    """Градусы → метры в локальной равновеликой проекции вокруг lat0."""
    kx = 111320.0 * math.cos(math.radians(lat0))
    return [(lon * kx, lat * 110540.0) for lon, lat in ring]


def _signed_area(ring: list[tuple[float, float]]) -> float:
    """Знак нужен, чтобы отличить обход по часовой от обхода против."""
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _area(ring: list[tuple[float, float]]) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _clip(subject: list[tuple[float, float]], clip: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Сазерленд—Ходжман:subject (любой полигон) ∩ clip (выпуклое окно)."""
    out = list(subject)
    n = len(clip)
    for i in range(n):
        if not out:
            return []
        a, b = clip[i], clip[(i + 1) % n]
        inside, res = [], []
        ex, ey = b[0] - a[0], b[1] - a[1]

        def side(p):
            return (p[0] - a[0]) * ey - (p[1] - a[1]) * ex

        for j, p in enumerate(out):
            q = out[j - 1]
            sp, sq = side(p), side(q)
            if sp >= 0:
                if sq < 0:
                    t = sq / (sq - sp)
                    res.append((q[0] + t * (p[0] - q[0]), q[1] + t * (p[1] - q[1])))
                res.append(p)
            elif sq >= 0:
                t = sq / (sq - sp)
                res.append((q[0] + t * (p[0] - q[0]), q[1] + t * (p[1] - q[1])))
        out = res
    return out


def intersect_area_m2(subject_geom, clipper_geom) -> float:
    """Площадь пересечения двух полигонов в м². Клиппер разбивается на выпуклые
    окна по вершинам — приближение, но для кадастровых полигонов с сотнями точек
    ошибка на доли процента."""
    subs = _outer_rings(subject_geom)
    clips = _outer_rings(clipper_geom)
    if not subs or not clips:
        return 0.0
    lat0 = sum(p[1] for r in subs for p in r) / max(1, sum(len(r) for r in subs))
    total = 0.0
    for s in subs:
        sm = _to_m(s, lat0)
        if len(sm) < 3 or _area(sm) <= 0:
            continue
        for c in clips:
            cm = _to_m(c, lat0)
            if len(cm) < 3:
                continue
            # выпуклая оболочка клиппера как окно отсечения
            hull = _convex_hull(cm)
            if len(hull) < 3:
                continue
            # Сазерленд—Ходжман здесь считает «внутри» как side() >= 0 при обходе
            # клиппера ПО ЧАСОВОЙ, а _convex_hull возвращает обход против часовой.
            # Без разворота любое пересечение выходило пустым — проверено на двух
            # квадратах с заведомым перекрытием.
            if _signed_area(hull) > 0:
                hull = hull[::-1]
            inter = _clip(sm, hull)
            if len(inter) >= 3:
                total += _area(inter)
    return total


def _convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    p = sorted(set(pts))
    if len(p) < 3:
        return p
    def half(build):
        h = []
        for q in build:
            while len(h) >= 2 and ((h[-1][0]-h[-2][0])*(q[1]-h[-2][1]) - (h[-1][1]-h[-2][1])*(q[0]-h[-2][0])) <= 0:
                h.pop()
            h.append(q)
        return h
    lo = half(p)
    up = half(reversed(p))
    return lo[:-1] + up[:-1]


def ring_area_m2(geom) -> float:
    subs = _outer_rings(geom)
    if not subs:
        return 0.0
    lat0 = sum(p[1] for r in subs for p in r) / max(1, sum(len(r) for r in subs))
    return sum(_area(_to_m(s, lat0)) for s in subs if len(s) >= 3)


def bbox(geoms) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for g in geoms:
        for r in _rings(g):
            for lon, lat in r:
                xs.append(lon); ys.append(lat)
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- #
#  Загрузка слоёв
# --------------------------------------------------------------------------- #

def _features(table: str, bb: str, cols: str, limit: int = 5000, gap: float = 0.9) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        q = urllib.parse.urlencode({"bbox": bb, "columns": cols, "limit": limit, "offset": offset})
        url = f"{BASE}/api/tables/{table}/features?{q}"
        data = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=300) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(25 + 8 * attempt)
                else:
                    raise RuntimeError(f"{table}: HTTP {e.code}") from None
            except Exception:  # noqa: BLE001
                time.sleep(4 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"{table}: не удалось получить слой")
        batch = data.get("features") or []
        out.extend(batch)
        if not data.get("exceededTransferLimit") or not batch:
            return out
        offset += limit
        time.sleep(gap)


def renewal_complexes(city: str, bbox_pad: float = 0.06) -> list[dict]:
    """Объявленные комплексы реновации города — с полигонами в WGS84.

    Габарит берётся с запасом вокруг города, потому что слой не фильтруется по имени:
    bbox надёжнее, чем строковое сравнение «קרית гת» / «קריית גת».
    """
    names, code, _ = deals.resolve_city(city)
    # точка города — из уже накопленного кэша участков; если кэша нет, берём шире
    lat = lon = None
    cache = geo.load_cache(code)
    pts = [(v.get("lat"), v.get("lon")) for v in cache.get("участки", {}).values() if v.get("lat")]
    if pts:
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
    if lat is None:
        raise RuntimeError("нет ни одной координаты участка — сначала запустите geo.py, он копит кэш")
    bb = f"{lon-bbox_pad},{lat-bbox_pad},{lon+bbox_pad},{lat+bbox_pad}"
    feats = _features(LAYER_RENEWAL, bb, RENEWAL_COLS)
    want = {deals._norm(n) for n in names} | {deals._norm(city)}
    out = []
    for f in feats:
        p = f.get("properties") or {}
        yeshuv = deals._norm(str(p.get("Yeshuv") or ""))
        if yeshuv not in want and deals._norm(str(p.get("Yeshuv") or "")) not in want:
            continue
        out.append({
            "номер_проекта": p.get("MisparProj"),
            "комплекс": " ".join(str(p.get("ShemMitcha") or "").split()),
            "план": " ".join(str(p.get("plan_name") or "").split()),
            "трек": " ".join(str(p.get("TeurMaslul") or "").split()),
            "подтрек": " ".join(str(p.get("TeurSugMas") or "").split()),
            "статус_код": p.get("codeStatus"),
            "дата_вступления": str(p.get("taarichTok") or "")[:10],
            "сейчас_единиц": p.get("yachadKaya"),
            "станет_единиц": p.get("yachadMutz"),
            "добавят": p.get("YachadTosa"),
            "mavat": p.get("Kishur") or "",
            "примечание": " ".join(str(p.get("heara") or "").split()),
            "источник": p.get("Source"),
            "geom": f.get("geometry"),
        })
    return out


def parcels_in_bbox(bb: str) -> list[dict]:
    feats = _features(LAYER_PARCELS, bb, PARCEL_COLS)
    out = []
    for f in feats:
        p = f.get("properties") or {}
        out.append({
            "гуш": str(p.get("GUSH_NUM") or "").split(".")[0],
            "подгуш": str(p.get("GUSH_SUFFI") or "").split(".")[0],
            "хелька": str(p.get("PARCEL") or "").split(".")[0],
            "площадь_м2": p.get("LEGAL_AREA"),
            "статус": " ".join(str(p.get("STATUS_TEX") or "").split()),
            "населённый_пункт": " ".join(str(p.get("LOCALITY_N") or "").split()),
            "geom": f.get("geometry"),
        })
    return out


# --------------------------------------------------------------------------- #
#  Пересечение комплексов с участками, по которым были сделки
# --------------------------------------------------------------------------- #

def match(city: str, since: int = 2012, share_in: float = 0.5, share_part: float = 0.05) -> dict:
    names, code, total = deals.resolve_city(city)
    complexes = renewal_complexes(city)
    if not complexes:
        return {"ошибка": "в GIS-слое минстроя нет объявленных комплексов этого города"}

    # Габарит всех комплексов + запас: тянем только те участки, которые вообще могут
    # пересекаться. Для Кирьят-Гата это сотни участков вместо 15 тысяч в bbox города.
    x0, y0, x1, y1 = bbox([c["geom"] for c in complexes if c.get("geom")])
    bb = f"{x0-BUFFER_DEG},{y0-BUFFER_DEG},{x1+BUFFER_DEG},{y1+BUFFER_DEG}"
    parcels = parcels_in_bbox(bb)

    by_key: dict[str, list[dict]] = collections.defaultdict(list)
    for p in parcels:
        if p["гуш"] and p["хелька"]:
            by_key[f"{p['гуш']}/{p['хелька']}"].append(p)

    deal_parcels = geo.q_parcels(code, since)
    street_cache = geo.load_cache(code).get("участки", {})

    results = []
    for c in complexes:
        geom = c.get("geom")
        if not geom:
            continue
        c_area = ring_area_m2(geom)
        inside, partial = [], []
        for dp in deal_parcels:
            key = f"{dp['гуш']}/{dp['хелька']}"
            cands = by_key.get(key)
            if not cands:
                continue
            best = 0.0
            variants = len(cands)
            for cand in cands:
                if not cand.get("geom"):
                    continue
                inter = intersect_area_m2(cand["geom"], geom)
                own = ring_area_m2(cand["geom"]) or 1.0
                best = max(best, inter / own)
            info = street_cache.get(key) or {}
            row = {
                "участок": key,
                "сделок": int(dp["сделок"]),
                "новых": int(dp["новых"]),
                "м2_все": dp["м2_все"],
                "м2_новые": dp["м2_новые"],
                "медиана_цены": dp["медиана_цены"],
                "медиана_площади": dp["медиана_площади"],
                "последняя_сделка": dp["последняя_сделка"],
                "улицы": info.get("улицы") or [],
                "доля_в_комплексе_pct": round(100 * best, 1),
                "двойной_участок": variants > 1,
            }
            if best >= share_in:
                inside.append(row)
            elif best >= share_part:
                partial.append(row)
        inside.sort(key=lambda r: -r["сделок"])
        partial.sort(key=lambda r: -r["сделок"])
        # Только участки, которые действительно внутри комплекса. Раньше сюда
        # добавлялись и «частичные», из-за чего на улице оказывалось больше сделок,
        # чем во всём комплексе, — выглядело как арифметическая ошибка.
        streets = collections.Counter()
        street_parcels = collections.Counter()
        for r in inside:
            for st in r["улицы"]:
                streets[st] += r["сделок"]
                street_parcels[st] += 1
        results.append({
            **{k: v for k, v in c.items() if k != "geom"},
            "площадь_га": round(c_area / 10000, 2),
            "участков_внутри": len(inside),
            "участков_частично": len(partial),
            "сделок_внутри": sum(r["сделок"] for r in inside),
            "новых_сделок_внутри": sum(r["новых"] for r in inside),
            "м2_внутри_взвеш": geo._med_weighted([(r["м2_все"], r["сделок"]) for r in inside]),
            "м2_новых_внутри": geo._med_weighted([(r["м2_новые"], r["новых"]) for r in inside]),
            "улицы": [{"улица": st, "сделок": n, "участков": street_parcels[st]}
                      for st, n in streets.most_common()],
            "улицы_частично": sorted({st for r in partial for st in r["улицы"]}),
            "участки_внутри": inside,
            "участки_частично": partial[:40],
            "центроид": _centroid(geom),
        })
    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    for r in results:
        for k in ("сейчас_единиц", "станет_единиц", "добавят"):
            r[k] = int(_n(r.get(k)))
    results.sort(key=lambda r: -r["станет_единиц"])
    return {
        "город": {"в_реестре": names, "код": code, "всего_сделок": total},
        "слои": {
            "комплексы": LAYER_RENEWAL,
            "участки": LAYER_PARCELS,
            "габарит_выборки": bb,
            "участков_в_габарите": len(parcels),
            "участков_со_сделками_в_городе": len(deal_parcels),
        },
        "комплексы": results,
        "собрано": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "оговорки": [
            "Границы — шейпфайл минстроя «מתחמי התחדשות עירונית מוכרזים», версия 31.07.2026, "
            "проекция Israel TM Grid, отдан сервером в WGS84.",
            "Попадание считается пересечением полигонов, а не центроидом: порог «в комплексе» — "
            f"{int(share_in*100)}% площади участка, «частично» — от {int(share_part*100)}%.",
            "Реестр сделок не публикует под-гуш, поэтому участок с повторяющимся номером хелька "
            "может соответствовать нескольким кадастровым полигонам; берётся наилучшее совпадение "
            "и помечается флагом «двойной_участок».",
            "Дыры в полигонах (дворы) при пересечении не учитываются.",
            "Улица участка берётся из газового реестра недвижимости: гуш-хелька связывается с улицей, "
            "но не с номером дома.",
        ],
    }


def _centroid(geom):
    rs = _rings(geom)
    pts = [p for r in rs for p in r]
    if not pts:
        return None
    return {"lat": sum(p[1] for p in pts) / len(pts), "lon": sum(p[0] for p in pts) / len(pts)}


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="קרית גת")
    ap.add_argument("--since", type=int, default=2012)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    names, code, _ = deals.resolve_city(args.city)
    print(f"город: {names} · код {code}")
    res = match(args.city, args.since)
    path = args.out or os.path.join(OUT_DIR, f"renewal_geo_{code}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    print(f"участков в габарите: {res['слои']['участков_в_габарите']}\n")
    for c in res["комплексы"]:
        print(f"■ {c['комплекс']}  (проект {c['номер_проекта']}, {c['площадь_га']} га)")
        print(f"   {c['сейчас_единиц']} → {c['станет_единиц']} квартир · {c['трек']}/{c['подтрек']} · "
              f"в силе с {c['дата_вступления']}")
        print(f"   участков внутри: {c['участков_внутри']} (+{c['участков_частично']} частично) · "
              f"сделок: {c['сделок_внутри']} · ₪{c['м2_внутри_взвеш'] or '—'}/м² · "
              f"новых ₪{c['м2_новых_внутри'] or '—'}/м²")
        streets = ", ".join(f"{s['улица']} ({s['сделок']})" for s in c["улицы"][:8]) or "улицы не определились"
        print(f"   улицы: {streets}")
        print(f"   mavat: {c['mavat']}\n")
    print(f"сохранено: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
