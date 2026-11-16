#!/usr/bin/env python3
"""
Сценарные тесты monitor.py: запускают скрипт с --fixture, --dry-run и --now
и проверяют, какие сообщения он бы отправил. Сеть и секреты не нужны.

Запуск:  python tests/test_scenarios.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_TZ = timezone(timedelta(hours=8))  # портал отдаёт serverTime в UTC+8


def snapshot(now: str, *, grid: bool = True, soc: int = 100, load: int = 120, age_min: int = 0,
             fault: int = 0, warning: int = 0, tmin: int = 180, tmax: int = 200) -> dict:
    """Ответы портала, как их возвращает LuxPortal.fetch()."""
    data_time = datetime.fromisoformat(now) - timedelta(minutes=age_min)
    return {
        "runtime": {
            "success": True,
            "serverTime": data_time.astimezone(SERVER_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "lost": False,
            "vacr": 2310 if grid else 0,
            "fac": 5001 if grid else 0,
            "soc": soc,
            "vBat": 532,
            "pCharge": 900 if grid and soc < 100 else 0,
            "pDisCharge": 0 if grid else load,
            "peps": load,
            "pToUser": load + 58 if grid else 0,
            "faultCode": fault,
            "warningCode": warning,
            "statusText": "normal" if grid else "offGrid",
            "powerRatingText": "6kW",
        },
        "battery": {
            "success": True,
            "fullCapacity": 106,
            "remainCapacity": 106 * soc // 100,
            "bmsCycleCnt": 12,
            "globalMinCellTemp": tmin,
            "globalMaxCellTemp": tmax,
            "batteryArray": [{"soh": 100}],
        },
        "energy": {"success": True, "todayUsage": 31, "todayImport": 35,
                   "todayCharging": 12, "todayDischarging": 9},
    }


class Home:
    """Один «дом» со своим state.json во временной папке."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="inverter-test-"))

    def run(self, now: str, *, portal_down: bool = False, status: bool = False,
            extra: tuple[str, ...] = (), **snap) -> list[str]:
        fixture = self.dir / "fixture.json"
        if portal_down:
            fixture = self.dir / "missing.json"
        else:
            fixture.write_text(json.dumps(snapshot(now, **snap)), encoding="utf-8")
        cmd = [sys.executable, str(ROOT / "monitor.py"), "--dry-run", "--fixture", str(fixture), "--now", now]
        if status:
            cmd.append("--status")
        cmd += extra
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("LUX_", "TG_")) and k not in ("SOC_ALERTS", "SUMMARY_HOUR", "TIMEZONE")}
        env["STATE_FILE"] = str(self.dir / "state.json")
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8")
        assert p.returncode == 0, f"код {p.returncode}\n{p.stdout}\n{p.stderr}"
        self.checks = p.stdout.count("OK: проверка выполнена")
        for line in p.stdout.splitlines():  # показаний инвертора в строках журнала быть не должно
            assert not line.startswith("OK:") or "=" not in line, f"показания попали в журнал: {line}"
        # отрезаем строки «OK: ...» после сообщений
        return [m.split("\nOK: ")[0].strip() for m in p.stdout.split("----\n")[1:]]


def expect(msgs: list[str], *needles: str, count: int | None = None) -> None:
    text = "\n".join(msgs)
    if count is not None:
        assert len(msgs) == count, f"ожидалось сообщений: {count}, получено {len(msgs)}:\n{text}"
    for n in needles:
        assert n in text, f"нет «{n}» в:\n{text}"


def T(hhmm: str, day: int = 15) -> str:
    return f"2026-01-{day:02d}T{hhmm}:00+02:00"  # зима, Киев = UTC+2


def test_outage_day() -> None:
    h = Home()
    m = h.run(T("10:00"))
    expect(m, "Мониторинг инвертора запущен", "Свет есть", count=1)
    assert "Итоги дня" not in m[0]

    expect(h.run(T("10:05")), count=0)                                   # всё спокойно — молчим

    m = h.run(T("10:10"), grid=False, soc=100)
    expect(m, "Свет пропал", "около 10:10", "отдаёт 120 W", "батареи хватит", count=1)

    expect(h.run(T("12:00"), grid=False, soc=95), count=0)               # выше порогов — молчим

    m = h.run(T("17:00"), grid=False, soc=50)
    expect(m, "Батарея 50%", "~120 W", count=1)
    expect(h.run(T("17:05"), grid=False, soc=50), count=0)               # порог не повторяется

    m = h.run(T("20:00"), grid=False, soc=28)
    expect(m, "Батарея 28%", count=1)
    assert "Выключите" not in m[0]

    m = h.run(T("21:00"), grid=False, soc=19)                            # порог 20% + вечерняя сводка
    expect(m, "Батарея 19%", "Выключите всё лишнее", "Итоги дня, 15.01",
           "Отключений: 1", "10 ч 50 мин", "10:10–сейчас", "минимум за день 19%", count=2)

    m = h.run(T("21:05"), grid=False, soc=10)
    expect(m, "Батарея 10%", "почти разряжена", count=1)

    m = h.run(T("21:30"), grid=True, soc=10)
    expect(m, "Свет появился", "около 21:30", "Не было 11 ч 20 мин", "с 10:10", "с 100% до 10%", count=1)

    expect(h.run(T("21:35"), grid=True, soc=12), count=0)

    # следующее отключение: пороги снова работают
    m = h.run(T("23:00"), grid=False, soc=45)
    expect(m, "Свет пропал", "Батарея 45%", count=2)


def test_offline_and_next_day_summary() -> None:
    h = Home()
    h.run(T("10:00"), soc=97)
    m = h.run(T("10:40"), age_min=35, soc=97)                            # данные старше 20 минут
    expect(m, "Инвертор не на связи", "Последние данные в 10:05", "свет был, батарея 97%", count=1)
    expect(h.run(T("10:45"), age_min=40), count=0)
    expect(h.run(T("10:50")), "Инвертор снова на связи", count=1)

    m = h.run(T("21:00"))
    expect(m, "Итоги дня, 15.01", "Отключений не было", "Здоровье батареи 100%", count=1)
    expect(h.run(T("21:05")), count=0)                                   # сводка один раз в день

    m = h.run(T("21:02", day=16))
    expect(m, "Итоги дня, 16.01", "Отключений не было", count=1)


def test_outage_over_midnight() -> None:
    h = Home()
    h.run(T("20:00"))
    h.run(T("23:00"), grid=False, soc=100)
    m = h.run(T("21:00", day=16), grid=False, soc=40)
    # в сумме за день — только время после полуночи
    expect(m, "Итоги дня, 16.01", "Отключений: 1", "всего без света 21 ч", "23:00–сейчас")


def test_portal_down() -> None:
    h = Home()
    h.run(T("10:00"))
    expect(h.run(T("10:05"), portal_down=True), count=0)
    expect(h.run(T("10:10"), portal_down=True), count=0)
    expect(h.run(T("10:15"), portal_down=True), "Не получается получить данные", "3 попытки", count=1)
    expect(h.run(T("10:20"), portal_down=True), count=0)                 # тревога один раз
    expect(h.run(T("10:25")), "Портал LuxPower снова отвечает", count=1)


def test_overload_faults_temperature_status() -> None:
    h = Home()
    h.run(T("10:00"))
    m = h.run(T("10:05"), grid=False, load=5000)
    expect(m, "Свет пропал", "Большая нагрузка на батарею: 5000 W", "6kW", count=2)
    expect(h.run(T("10:10"), grid=False, load=5200), count=0)            # не повторяем
    expect(h.run(T("10:15"), grid=False, load=300), count=0)
    expect(h.run(T("10:20"), grid=False, load=4800), "Большая нагрузка", count=1)

    h = Home()
    h.run(T("10:00"))
    expect(h.run(T("10:05"), fault=4), "Инвертор сообщает о проблеме", "код ошибки 4", count=1)
    expect(h.run(T("10:10"), fault=4), count=0)
    expect(h.run(T("10:15")), "Ошибки инвертора сброшены", count=1)

    expect(h.run(T("10:20"), tmin=30, tmax=40), "Батарея остыла до 3 °C", count=1)
    expect(h.run(T("10:25"), tmin=30, tmax=40), count=0)
    expect(h.run(T("10:30"), tmin=120, tmax=140), "снова в норме (12–14 °C)", count=1)
    expect(h.run(T("10:35"), tmin=440, tmax=460), "Батарея нагрелась до 46 °C", count=1)

    expect(h.run(T("10:40")), "снова в норме", count=1)

    expect(h.run(T("10:45"), status=True), "текущий статус", "Свет есть (231 V, 50.01 Hz)", count=1)


def test_first_run_in_the_evening() -> None:
    h = Home()
    m = h.run(T("22:00"), status=True)
    expect(m, "Мониторинг инвертора запущен", count=1)                   # без сводки и без дубля статуса
    assert "Итоги дня" not in m[0]


def test_loop_mode() -> None:
    h = Home()
    h.run(T("10:00"))
    # ~3 секунды, проверка каждую секунду: событие и статус приходят один раз, а не на каждой проверке
    m = h.run(T("10:05"), grid=False, status=True, extra=("--loop", "0.05", "--interval", "1"))
    assert h.checks >= 3, f"проверок в цикле: {h.checks}"
    expect(m, "Свет пропал", "текущий статус", count=2)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok    {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}\n{e}\n")
    print(f"\n{len(tests) - failed} из {len(tests)} сценариев прошли")
    sys.exit(1 if failed else 0)
