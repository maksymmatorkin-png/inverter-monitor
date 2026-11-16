# Задача: мониторинг инвертора LuxPower → Telegram-группа (GitHub Actions)

Вставьте этот файл в Claude Code вместе с `monitor.py` и попросите продолжить.

## Цель
Скрипт запускается в облаке (GitHub Actions по cron каждые 5 мин), читает портал
LuxPower и шлёт в Telegram-группу полезные для владельца квартиры события.

## Что уже сделано
`monitor.py` — черновик готов, синтаксис проверен, **не тестировался**:
- логин в портал (`POST /WManage/web/login`, поля формы `account`, `password`,
  сессия в cookie JSESSIONID; при неудаче редирект обратно на `/web/login`);
- чтение данных, логика событий, сохранение состояния в `state.json`, отправка в Telegram (HTML);
- флаги: `--dry-run`, `--status`, `--fixture file.json`.

События: свет пропал / появился (с длительностью и падением заряда), заряд 50/30/20/10%
с прогнозом времени работы, перегрузка EPS > 4500 W, инвертор не на связи и снова на связи,
коды ошибок, температура батареи < 5 °C или > 45 °C, вечерняя сводка в 21:00,
портал недоступен 3 запуска подряд.

## Что осталось
1. `.github/workflows/monitor.yml`:
   - cron `*/5 * * * *` и `workflow_dispatch` с входом `send_status`;
   - `concurrency` без отмены запусков;
   - состояние через `actions/cache` (restore с префиксом `inverter-state-`, save с ключом `inverter-state-${run_id}-${run_attempt}`), старые кэши удалять через `gh cache delete` (`permissions: actions: write`);
   - keepalive: пустой коммит, если репо не менялся больше 45 дней (`contents: write`). GitHub отключает cron в неактивных публичных репо через 60 дней.
   - Репозиторий публичный, иначе бесплатных минут не хватит. Пароль хранить только в Secrets.
2. Тесты сценариев через `--fixture` + `--dry-run`: первый запуск, отключение, разряд до порогов, возврат света, нет связи, сводка.
   Проверить, что в отключение `vacr` действительно падает ниже 170 V. Реального снимка во время отключения пока нет.
3. `README.md` на русском: создать бота через @BotFather, добавить его в группу, узнать chat_id
   (`getUpdates`), создать секреты, включить Actions.
4. `requirements.txt` (`requests`).

## Данные портала (eu.luxpowertek.com)
Все API — `POST`, `application/x-www-form-urlencoded`, тело `serialNum=<SN>`:
- `/WManage/api/inverter/getInverterRuntime` — `vacr` (V×10), `fac` (Hz×100), `soc`, `vBat` (×10),
  `pCharge`, `pDisCharge`, `peps` (нагрузка EPS, W), `pToUser` (из сети, W), `faultCode`, `warningCode`,
  `lost`, `deviceTime` (местное), `serverTime` (UTC+8, время получения данных — для проверки связи), `powerRatingText` "6kW".
- `/WManage/api/battery/getBatteryInfo` — `fullCapacity` (Ah, 106), `remainCapacity`, `bmsCycleCnt`,
  `globalMin/MaxCellTemp` (°C×10), `batteryArray[0].soh`.
- `/WManage/api/inverter/getInverterEnergyInfo` — `todayUsage`, `todayImport`, `todayCharging`, `todayDischarging` (kWh×10).
- `/WManage/api/analyze/chart/dayMultiLine` (`dateText=YYYY-MM-DD`) — точки каждые 5 мин: `gridPower`, `batteryDischarging`, `consumption`, `soc`.
- `/WManage/api/inverterChart/monthColumn` — по дням (kWh×10).

## Об установке
Инвертор 6 kW (серийный номер — в настройках портала), литиевая батарея PowerGem примерно на 5,4 kWh, панели не подключены:
это резервное питание от сети. Нагрузка без света около 120 W, батарея разряжается
примерно на 2,9% в час, полной батареи хватает примерно на 27–31 ч. В простое инвертор сам потребляет около 58 W.
Часовой пояс Europe/Kyiv. Сообщения на русском.
