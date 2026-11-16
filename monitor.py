#!/usr/bin/env python3
"""
Мониторинг инвертора LuxPower (портал eu.luxpowertek.com) с уведомлениями в Telegram.

Запускается периодически (например, GitHub Actions каждые 5 минут). Каждый запуск:
  1. логинится в портал и читает текущие данные инвертора и батареи;
  2. сравнивает их с прошлым состоянием (state.json);
  3. шлёт в Telegram только события: свет пропал/появился, низкий заряд,
     перегрузка, инвертор не на связи, ошибки, температура батареи,
     и раз в день — вечернюю сводку.

Настройки — через переменные окружения (см. README.md).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


LUX_BASE = env("LUX_BASE", "https://eu.luxpowertek.com").rstrip("/")
LUX_ACCOUNT = env("LUX_ACCOUNT")
LUX_PASSWORD = env("LUX_PASSWORD")
LUX_SERIAL = env("LUX_SERIAL")

TG_TOKEN = env("TG_BOT_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")

TZ = ZoneInfo(env("TIMEZONE", "Europe/Kyiv"))
SITE_NAME = env("SITE_NAME", "Квартира")

# Пороги
SOC_ALERTS = sorted({int(x) for x in env("SOC_ALERTS", "50,30,20,10").split(",") if x.strip()}, reverse=True)
CUTOFF_SOC = int(env("CUTOFF_SOC", "10"))            # ниже этого инвертор прекращает разряд
OVERLOAD_W = int(env("OVERLOAD_W", "4500"))          # предупреждать о нагрузке выше (инвертор 6 кВт)
OFFLINE_MIN = int(env("OFFLINE_MIN", "20"))          # данные старше N минут = нет связи
SUMMARY_HOUR = int(env("SUMMARY_HOUR", "21"))        # час вечерней сводки (местное время), -1 = выкл
BAT_TEMP_LOW = float(env("BAT_TEMP_LOW", "5"))       # °C, LiFePO4 плохо заряжается на холоде
BAT_TEMP_HIGH = float(env("BAT_TEMP_HIGH", "45"))
NOMINAL_V = float(env("BATTERY_NOMINAL_V", "51.2"))
PORTAL_FAIL_ALERT = int(env("PORTAL_FAIL_ALERT", "3"))  # сколько неудачных запусков подряд до тревоги

GRID_MIN_V = 170.0   # ниже — считаем, что сети нет
GRID_MIN_HZ = 45.0

# Сервер портала хранит время в UTC+8
SERVER_TZ = timezone(timedelta(hours=8))

STATE_FILE = Path(env("STATE_FILE", "state.json"))


# ----------------------------------------------------------------------------
# Портал LuxPower
# ----------------------------------------------------------------------------


class PortalError(Exception):
    pass


class LuxPortal:
    def __init__(self, base: str, account: str, password: str, serial: str):
        self.base, self.account, self.password, self.serial = base, account, password, serial
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (inverter-monitor)"
        self.logged_in = False

    def login(self) -> None:
        r = self.s.post(
            f"{self.base}/WManage/web/login",
            data={"account": self.account, "password": self.password},
            timeout=30,
            allow_redirects=True,
        )
        r.raise_for_status()
        if r.url.rstrip("/").endswith("/web/login"):
            raise PortalError("Не удалось войти в портал LuxPower: проверьте логин и пароль")

    def _api(self, path: str, **data) -> dict:
        data.setdefault("serialNum", self.serial)
        r = self.s.post(f"{self.base}/WManage/api/{path}", data=data, timeout=30)
        r.raise_for_status()
        try:
            j = r.json()
        except ValueError as e:
            raise PortalError(f"Портал вернул не JSON на {path} (сессия?)") from e
        if not j.get("success"):
            # ответ целиком не печатаем: журнал публичного репозитория виден всем
            raise PortalError(f"Портал вернул ошибку на {path}: {str(j.get('msg') or 'success=false')[:200]}")
        return j

    def fetch(self) -> dict:
        """Читает данные; сессию переиспользует, при сбое входит заново и повторяет один раз."""
        if self.logged_in:
            try:
                return self._fetch_all()
            except (PortalError, requests.RequestException):
                self.logged_in = False  # сессия могла истечь
        self.login()
        self.logged_in = True
        return self._fetch_all()

    def _fetch_all(self) -> dict:
        return {
            "runtime": self._api("inverter/getInverterRuntime"),
            "battery": self._api("battery/getBatteryInfo"),
            "energy": self._api("inverter/getInverterEnergyInfo"),
        }


# ----------------------------------------------------------------------------
# Разбор данных
# ----------------------------------------------------------------------------


def parse(raw: dict) -> dict:
    rt, bat, en = raw["runtime"], raw.get("battery", {}), raw.get("energy", {})
    cells = (bat.get("batteryArray") or [{}])[0]

    server_time = None
    if rt.get("serverTime"):
        server_time = datetime.strptime(rt["serverTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SERVER_TZ)

    vac = (rt.get("vacr") or 0) / 10
    fac = (rt.get("fac") or 0) / 100
    full_ah = bat.get("fullCapacity") or cells.get("currentFullCapacity") or rt.get("batCapacity") or 105
    tmin = bat.get("globalMinCellTemp", cells.get("batMinCellTemp"))
    tmax = bat.get("globalMaxCellTemp", cells.get("batMaxCellTemp"))

    return {
        "data_time": server_time,
        "lost": bool(rt.get("lost")),
        "grid_on": vac >= GRID_MIN_V and fac >= GRID_MIN_HZ,
        "grid_v": vac,
        "grid_hz": fac,
        "soc": int(rt.get("soc") or 0),
        "bat_v": (rt.get("vBat") or 0) / 10,
        "p_charge": int(rt.get("pCharge") or 0),
        "p_discharge": int(rt.get("pDisCharge") or 0),
        "p_eps": int(rt.get("peps") or 0),          # нагрузка на резервном выходе
        "p_from_grid": int(rt.get("pToUser") or 0),
        "fault": int(rt.get("faultCode") or 0),
        "warning": int(rt.get("warningCode") or 0),
        "status_text": rt.get("statusText", ""),
        "full_kwh": float(full_ah) * NOMINAL_V / 1000,
        "soh": cells.get("soh"),
        "cycles": bat.get("bmsCycleCnt", cells.get("cycleCnt")),
        "t_min": tmin / 10 if tmin is not None else None,
        "t_max": tmax / 10 if tmax is not None else None,
        "today_usage": (en.get("todayUsage") or 0) / 10,
        "today_import": (en.get("todayImport") or 0) / 10,
        "today_discharge": (en.get("todayDischarging") or 0) / 10,
        "today_charge": (en.get("todayCharging") or 0) / 10,
        "power_rating": rt.get("powerRatingText", ""),
    }


# ----------------------------------------------------------------------------
# Форматирование
# ----------------------------------------------------------------------------


def fmt_dur(td: timedelta) -> str:
    mins = max(0, int(td.total_seconds() // 60))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def fmt_hhmm(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def runtime_left(d: dict, load_w: float) -> timedelta:
    usable_kwh = d["full_kwh"] * max(d["soc"] - CUTOFF_SOC, 0) / 100
    return timedelta(hours=usable_kwh * 1000 / max(load_w, 60))


def battery_line(d: dict) -> str:
    if d["p_charge"] > 30:
        return f"🔋 Батарея {d['soc']}%, заряжается {d['p_charge']} W"
    if d["p_discharge"] > 30:
        return f"🔋 Батарея {d['soc']}%, отдаёт {d['p_discharge']} W"
    return f"🔋 Батарея {d['soc']}%"


def status_text(d: dict, st: dict, now: datetime) -> str:
    lines = [f"<b>{SITE_NAME}: текущий статус</b>"]
    if d["grid_on"]:
        lines.append(f"💡 Свет есть ({d['grid_v']:.0f} V, {d['grid_hz']:.2f} Hz)")
    else:
        since = st.get("grid_changed_at")
        extra = f", уже {fmt_dur(now - datetime.fromisoformat(since))}" if since else ""
        lines.append(f"🌑 Света нет{extra}")
    lines.append(battery_line(d))
    if not d["grid_on"]:
        load = st.get("load_ema") or d["p_discharge"]
        lines.append(f"🏠 Нагрузка {d['p_eps']} W, батареи хватит ещё ≈ {fmt_dur(runtime_left(d, load))}")
    if d["t_min"] is not None:
        lines.append(f"🌡 Батарея {d['t_min']:.0f}–{d['t_max']:.0f} °C")
    if d["fault"] or d["warning"]:
        lines.append(f"❗ Код ошибки {d['fault']}, предупреждение {d['warning']}")
    lines.append(f"⚡ Сегодня: потребление {d['today_usage']:.1f} kWh, из сети {d['today_import']:.1f} kWh")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Логика событий
# ----------------------------------------------------------------------------


def new_day(st: dict, today: str, soc: int) -> None:
    if st.get("day") != today:
        st["day"] = today
        st["day_outages"] = []          # [[start_iso, end_iso], ...] завершённые за день
        st["day_min_soc"] = soc


def evaluate(d: dict | None, st: dict, now: datetime, portal_error: str | None = None) -> list[str]:
    """Сравнивает новые данные с состоянием, обновляет состояние, возвращает сообщения."""
    msgs: list[str] = []

    # --- Портал недоступен -------------------------------------------------
    if d is None:
        st["portal_fails"] = st.get("portal_fails", 0) + 1
        if st["portal_fails"] == PORTAL_FAIL_ALERT:
            msgs.append(f"⚠️ Не получается получить данные с портала LuxPower ({st['portal_fails']} попытки подряд).\n"
                        f"<i>{html.escape(str(portal_error))}</i>")
        return msgs
    if st.get("portal_fails", 0) >= PORTAL_FAIL_ALERT:
        msgs.append("✅ Портал LuxPower снова отвечает.")
    st["portal_fails"] = 0

    today = now.astimezone(TZ).date().isoformat()
    new_day(st, today, d["soc"])

    # --- Связь с инвертором ------------------------------------------------
    stale = d["lost"] or d["data_time"] is None or (now - d["data_time"]) > timedelta(minutes=OFFLINE_MIN)
    if stale:
        if not st.get("offline"):
            st["offline"] = True
            last = d["data_time"]
            last_txt = f"Последние данные в {fmt_hhmm(last)}" if last else "Данных нет"
            last_grid = "свет был" if st.get("grid") == "on" else "света не было" if st.get("grid") == "off" else ""
            msgs.append(
                "📡 <b>Инвертор не на связи</b>\n"
                f"{last_txt}{' (' + last_grid + ', батарея ' + str(st.get('last_soc')) + '%)' if last_grid else ''}.\n"
                "Возможные причины: пропал интернет, роутер не подключён к резервному питанию "
                "или батарея села и инвертор выключился."
            )
        return msgs  # по устаревшим данным событий не считаем
    if st.get("offline"):
        st["offline"] = False
        msgs.append("📡 Инвертор снова на связи.")

    st["last_soc"] = d["soc"]
    st["day_min_soc"] = min(st.get("day_min_soc", d["soc"]), d["soc"])

    # --- Сеть -------------------------------------------------------------
    prev = st.get("grid")
    cur = "on" if d["grid_on"] else "off"

    if cur == "off":
        # сглаженная нагрузка для оценки времени работы
        prev_ema = st.get("load_ema")
        st["load_ema"] = d["p_discharge"] if prev_ema is None or prev != "off" else 0.5 * prev_ema + 0.5 * d["p_discharge"]

    if prev is None:
        st["grid"] = cur
        st["grid_changed_at"] = now.isoformat()
        st["outage_start_soc"] = d["soc"]
        st["outage_min_soc"] = d["soc"]
        st["soc_alerts_sent"] = []
        if SUMMARY_HOUR >= 0 and now.astimezone(TZ).hour >= SUMMARY_HOUR:
            st["last_summary"] = today  # наблюдений за этот день ещё нет, сводку не шлём
        msgs.append("🤖 Мониторинг инвертора запущен.\n\n" + status_text(d, st, now))
    elif prev == "on" and cur == "off":
        st.update(grid="off", grid_changed_at=now.isoformat(), outage_start_soc=d["soc"],
                  outage_min_soc=d["soc"], soc_alerts_sent=[], overload_alerted=False)
        msgs.append(
            f"🌑 <b>Свет пропал</b> около {fmt_hhmm(now)}\n"
            f"{battery_line(d)}\n"
            f"🏠 Нагрузка {d['p_eps']} W — батареи хватит ≈ {fmt_dur(runtime_left(d, st['load_ema']))}"
        )
    elif prev == "off" and cur == "on":
        started = datetime.fromisoformat(st["grid_changed_at"])
        st.setdefault("day_outages", []).append([started.isoformat(), now.isoformat()])
        min_soc = min(st.get("outage_min_soc", d["soc"]), d["soc"])
        msgs.append(
            f"💡 <b>Свет появился</b> около {fmt_hhmm(now)}\n"
            f"⏱ Не было {fmt_dur(now - started)} (с {fmt_hhmm(started)})\n"
            f"🔋 Батарея опустилась с {st.get('outage_start_soc', '?')}% до {min_soc}%, начинает заряжаться"
        )
        st.update(grid="on", grid_changed_at=now.isoformat(), soc_alerts_sent=[], load_ema=None)

    # --- Во время отключения ------------------------------------------------
    if cur == "off":
        st["outage_min_soc"] = min(st.get("outage_min_soc", d["soc"]), d["soc"])
        sent = set(st.get("soc_alerts_sent", []))
        crossed = [t for t in SOC_ALERTS if d["soc"] <= t and t not in sent]
        if crossed:
            level = min(crossed)
            sent.update(t for t in SOC_ALERTS if t >= level)
            st["soc_alerts_sent"] = sorted(sent)
            icon = "🪫" if level <= 20 else "🔋"
            if d["soc"] <= CUTOFF_SOC:
                msgs.append(f"{icon} <b>Батарея {d['soc']}%</b> — почти разряжена, "
                            "инвертор вот-вот отключит питание до появления света.")
            else:
                left = fmt_dur(runtime_left(d, st["load_ema"]))
                advice = "\nВыключите всё лишнее, оставьте самое необходимое." if level <= 20 else ""
                msgs.append(f"{icon} <b>Батарея {d['soc']}%</b> — при нагрузке ~{int(st['load_ema'])} W "
                            f"хватит ≈ {left}{advice}")

        if d["p_eps"] >= OVERLOAD_W and not st.get("overload_alerted"):
            st["overload_alerted"] = True
            msgs.append(f"⚠️ <b>Большая нагрузка на батарею: {d['p_eps']} W</b> (инвертор {d['power_rating']}).\n"
                        "Отключите мощные приборы (бойлер, обогреватель, чайник), иначе инвертор может уйти в перегрузку.")
        elif d["p_eps"] < OVERLOAD_W * 0.7:
            st["overload_alerted"] = False

    # --- Ошибки инвертора --------------------------------------------------
    code = (d["fault"], d["warning"])
    if code != tuple(st.get("codes", (0, 0))):
        if code != (0, 0):
            msgs.append(f"❗ <b>Инвертор сообщает о проблеме</b>: код ошибки {d['fault']}, предупреждение {d['warning']}.\n"
                        "Подробности — в портале LuxPower или приложении LuxCloud.")
        else:
            msgs.append("✅ Ошибки инвертора сброшены.")
        st["codes"] = list(code)

    # --- Температура батареи -----------------------------------------------
    if d["t_min"] is not None:
        t_state = "low" if d["t_min"] < BAT_TEMP_LOW else "high" if d["t_max"] > BAT_TEMP_HIGH else "ok"
        prev_t = st.get("temp_state", "ok")
        if t_state != prev_t:
            if t_state == "low":
                msgs.append(f"🥶 Батарея остыла до {d['t_min']:.0f} °C. Ниже ~0 °C литиевая батарея не будет заряжаться.")
            elif t_state == "high":
                msgs.append(f"🔥 Батарея нагрелась до {d['t_max']:.0f} °C. Проверьте вентиляцию.")
            elif prev_t in ("low", "high"):
                msgs.append(f"🌡 Температура батареи снова в норме ({d['t_min']:.0f}–{d['t_max']:.0f} °C).")
            st["temp_state"] = t_state

    # --- Вечерняя сводка ----------------------------------------------------
    local = now.astimezone(TZ)
    if SUMMARY_HOUR >= 0 and local.hour >= SUMMARY_HOUR and st.get("last_summary") != today:
        st["last_summary"] = today
        msgs.append(daily_summary(d, st, now))

    st["grid"] = cur
    return msgs


def daily_summary(d: dict, st: dict, now: datetime) -> str:
    outs = [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in st.get("day_outages", [])]
    # отключение, начавшееся вчера, в сумму за день считаем с полуночи
    midnight = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    ongoing = st.get("grid") == "off" and not d["grid_on"]
    ongoing_start = datetime.fromisoformat(st["grid_changed_at"]) if ongoing else None
    total = sum((b - max(a, midnight) for a, b in outs), timedelta())
    if ongoing:
        total += now - max(ongoing_start, midnight)

    lines = [f"📊 <b>Итоги дня, {now.astimezone(TZ).strftime('%d.%m')}</b>"]
    n = len(outs) + (1 if ongoing else 0)
    if n == 0:
        lines.append("💡 Отключений не было")
    else:
        spans = [f"{fmt_hhmm(a)}–{fmt_hhmm(b)}" for a, b in outs]
        if ongoing:
            spans.append(f"{fmt_hhmm(ongoing_start)}–сейчас")
        lines.append(f"🌑 Отключений: {n}, всего без света {fmt_dur(total)}")
        lines.append("    " + ", ".join(spans))
    lines.append(f"🔋 Сейчас {d['soc']}%, минимум за день {st.get('day_min_soc', d['soc'])}%")
    lines.append(f"⚡ Потребление {d['today_usage']:.1f} kWh, из сети {d['today_import']:.1f} kWh, "
                 f"от батареи {d['today_discharge']:.1f} kWh")
    if d.get("soh") is not None:
        lines.append(f"❤️ Здоровье батареи {d['soh']}%, циклов {d.get('cycles', '?')}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------


def send_telegram(text: str) -> None:
    """Отправляет сообщение, до 3 попыток. Токен бота в текст ошибок не попадает."""
    last = ""
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=30,
            )
            if r.status_code == 200:
                return
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (400, 401, 403, 404):
                break  # повтор не поможет: неверный токен, chat_id или текст
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Telegram: сообщение не отправлено ({last})")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="прислать текущий статус в любом случае")
    ap.add_argument("--dry-run", action="store_true", help="печатать сообщения вместо отправки")
    ap.add_argument("--fixture", help="JSON с ответами портала вместо реального запроса (для тестов)")
    ap.add_argument("--now", help="подменить текущее время, ISO 8601 с часовым поясом (для тестов)")
    ap.add_argument("--loop", type=float, default=0, metavar="МИНУТ",
                    help="работать столько минут, проверяя инвертор каждые --interval секунд")
    ap.add_argument("--interval", type=float, default=120, metavar="СЕКУНД", help="пауза между проверками в цикле")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not args.fixture and not all([LUX_ACCOUNT, LUX_PASSWORD, LUX_SERIAL]):
        print("Нужны переменные LUX_ACCOUNT, LUX_PASSWORD, LUX_SERIAL", file=sys.stderr)
        return 2
    if not args.dry_run and not all([TG_TOKEN, TG_CHAT_ID]):
        print("Нужны переменные TG_BOT_TOKEN и TG_CHAT_ID", file=sys.stderr)
        return 2

    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except ValueError:
        print("state.json повреждён, начинаю с чистого состояния", file=sys.stderr)
        st = {}
    portal = None if args.fixture else LuxPortal(LUX_BASE, LUX_ACCOUNT, LUX_PASSWORD, LUX_SERIAL)
    deadline = time.monotonic() + args.loop * 60
    want_status = args.status
    failed = 0

    while True:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ)

        d, err = None, None
        try:
            raw = json.loads(Path(args.fixture).read_text(encoding="utf-8")) if args.fixture else portal.fetch()
            d = parse(raw)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            print(err, file=sys.stderr, flush=True)

        msgs = evaluate(d, st, now, err)
        if want_status and d is not None:
            want_status = False  # статус по запросу — один раз за запуск
            if not any("Мониторинг инвертора запущен" in m for m in msgs):
                msgs.append(status_text(d, st, now))

        for m in msgs:
            if args.dry_run:
                print("----\n" + m)
                continue
            try:
                send_telegram(m)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(e, file=sys.stderr, flush=True)

        # Состояние сохраняем в любом случае, иначе уже отправленные события повторятся.
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        if d is not None:
            # Показаний в журнале нет: в публичном репозитории его видят все.
            print(f"OK: проверка выполнена, сообщений: {len(msgs)}", flush=True)

        if time.monotonic() + args.interval > deadline:
            break
        time.sleep(args.interval)

    if args.loop:
        return 0  # в цикле сбой отправки не должен обрывать цепочку запусков
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
