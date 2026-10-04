"""
Веб-панель: цены на квартиры (по умолчанию Кирьят-Гат) + сторож изменений.

Только стандартная библиотека, без внешних зависимостей: приложение открывается
в изолированном просмотрщике, где CDN и внешние шрифты не грузятся.

Запуск:
    python3 server.py --port 8000 --city "קרית גת"
Затем открыть http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common
import market as market_mod

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYSIS_DIR = os.path.join(HERE, "analysis")
REPORTS_DIR = os.path.join(HERE, "reports")

STATE = {"city": "קרית גат", "lock": threading.Lock()}


def city_filename(city: str) -> str:
    return common.norm_city(city).replace(" ", "_") + ".json"


def market_filename(city: str) -> str | None:
    """Отчёт по сделкам лежит под кодом города — код узнаём из реестра, не из головы."""
    try:
        import deals

        _, code, _ = deals.resolve_city(city)
        return f"market_{code}.json"
    except Exception:  # noqa: BLE001
        return None


def run_analysis(city: str) -> dict:
    """Запустить analyze.py для города и вернуть результат."""
    out = os.path.join(ANALYSIS_DIR, city_filename(city))
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "analyze.py"), "--city", city, "--out", out],
        cwd=HERE, capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:] or proc.stdout[-2000:])
    with open(out, encoding="utf-8") as fh:
        return json.load(fh)


def run_watch(city: str) -> dict:
    """Запустить сторожа (не блокирующий первую загрузку — вызывается по кнопке)."""
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "watch.py"), "--city", city],
        cwd=HERE, capture_output=True, text=True, timeout=900,
    )
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr[-2000:]}


def load_analysis(city: str) -> dict | None:
    path = os.path.join(ANALYSIS_DIR, city_filename(city))
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_watch() -> dict:
    last = os.path.join(REPORTS_DIR, "last.json")
    if not os.path.exists(last):
        return {"configured": False, "snapshots": [], "changes": {}, "total_changes": None}
    with open(last, encoding="utf-8") as fh:
        data = json.load(fh)
    snaps = []
    for key, s in (data.get("snapshots") or {}).items():
        snaps.append(
            {
                "key": key,
                "ru": s.get("ru"),
                "title": s.get("package_title"),
                "rows": s.get("total_rows"),
                "metadata_modified": s.get("metadata_modified"),
                "resource_last_modified": s.get("resource_last_modified"),
                "fields_count": len(s.get("fields") or []),
                "taken_at": s.get("taken_at"),
                "city_stats": s.get("city_stats"),
            }
        )
    return {
        "configured": True,
        "generated_at": data.get("generated_at"),
        "city": data.get("city"),
        "total_changes": data.get("total_changes"),
        "changes": data.get("changes") or {},
        "snapshots": snaps,
        "telegram_configured": bool(
            os.environ.get("WATCH_TELEGRAM_TOKEN") and os.environ.get("WATCH_TELEGRAM_CHAT")
        ),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "IsraelWatch/1.0"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, fmt, *args) -> None:  # тише в логах
        sys.stderr.write("  " + (fmt % args) + "\n")

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as fh:
                html = fh.read().replace("__CITY__", STATE["city"])
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if route == "/api/market":
            city = (qs.get("city") or [STATE["city"]])[0]
            schedule = (qs.get("schedule") or ["quarter"])[0]
            fname = market_filename(city)
            if not fname:
                self._json({"error": f"не нашёл «{city}» в реестре сделок"}, 404)
                return
            path = os.path.join(ANALYSIS_DIR, fname)
            refresh = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            if not os.path.exists(path) or refresh:
                with STATE["lock"]:
                    try:
                        cmd = [sys.executable, os.path.join(HERE, "market.py"), "--city", city, "--since", "2012"]
                        if refresh:
                            cmd.append("--rebuild")
                        subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=1500)
                    except Exception as e:  # noqa: BLE001
                        self._json({"error": f"не удалось собрать отчёт: {e}"}, 500)
                        return
            if not os.path.exists(path):
                self._json({"error": "отчёт не создался"}, 500)
                return
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            data["telegram_text"] = market_mod.telegram_text(data)
            data["расписание"] = schedule
            self._json(data)
            return

        if route == "/api/state":
            city = (qs.get("city") or [STATE["city"]])[0]
            with STATE["lock"]:
                analysis = load_analysis(city)
                if analysis is None:
                    try:
                        analysis = run_analysis(city)
                    except Exception as e:  # noqa: BLE001
                        self._json({"error": str(e)}, 500)
                        return
                STATE["city"] = city
            self._json(
                {
                    "city": city,
                    "analysis": analysis,
                    "watch": load_watch(),
                    "datasets": {k: v["ru"] for k, v in common.DATASETS.items()},
                    "known_cities_hint": "Любой город из реестра ЛАМАС, например: קרית גת, באר שבע, אשקלון, אשדוד, נתיבות, אופקים, שדרות, ירוחם, דימונה, ערד",
                }
            )
            return

        if route == "/api/watch/run":
            city = (qs.get("city") or [STATE["city"]])[0]
            try:
                res = run_watch(city)
                self._json({"ok": res["returncode"] in (0, 1), **res, "watch": load_watch()})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if route == "/api/reload":
            city = (qs.get("city") or [STATE["city"]])[0]
            try:
                self._json({"ok": True, "analysis": run_analysis(city)})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 500)
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--city", default="קרית גת")
    args = ap.parse_args()
    STATE["city"] = args.city

    print(f"подготовка отчёта по сделкам для «{args.city}»…", flush=True)
    try:
        subprocess.run(
            [sys.executable, os.path.join(HERE, "market.py"), "--city", args.city, "--since", "2012"],
            cwd=HERE, capture_output=True, text=True, timeout=1500,
        )
        print("отчёт готов", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"не удалось подготовить отчёт: {e}", flush=True)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"панель запущена: http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
