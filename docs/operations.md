# Alpine 3.22 / Raspberry Pi 5: безопасная миграция

Эти команды выполняет оператор по SSH. Они **не выполнялись** на вашей Pi
автоматически. Нужен второй SSH-сеанс и физический доступ для возврата к старому
релизу. Сначала проверка новой сборки, затем переключение службы. Ни старое
`/root/ddt`, ни его `scripts/.venv` не изменять во время миграции.

## 1. Зафиксировать восстановленную среду

```sh
DDT_BACKUP=$(mktemp -d /root/ddt-backup.XXXXXX)
echo "$DDT_BACKUP"
cp -p /etc/init.d/sweep "$DDT_BACKUP/sweep"
cp -p /usr/local/bin/start_sweep.sh "$DDT_BACKUP/start_sweep.sh"
if [ -f /etc/conf.d/sweep ]; then cp -p /etc/conf.d/sweep "$DDT_BACKUP/sweep.conf"; fi
if [ -f /etc/ddt/detector.json ]; then cp -p /etc/ddt/detector.json "$DDT_BACKUP/detector.json"; fi
/root/ddt/scripts/.venv/bin/python3 -m pip freeze --all > "$DDT_BACKUP/pip-freeze.txt"
apk info -v > "$DDT_BACKUP/apk-versions.txt"
apk audit --system > "$DDT_BACKUP/apk-audit.txt"
cp -p /var/log/sweep.log "$DDT_BACKUP/sweep.log"
```

Скопировать этот каталог на рабочую машину (например `scp -r`). Значение
`DDT_BACKUP` сохранить вне SSH-сеанса. Если снова изменены libpython, .pyc,
компиляторы без намеренной установки — остановить миграцию и исследовать носитель,
память и питание. Повторная переустановка пакетов не устанавливает причину порчи.
`throttled=0x0` не доказывает исправность всей цепи питания/USB.

## 2. Подготовить отдельный релиз, не останавливая старый

Сначала изменения этого PR должны попасть в Git, а нужный commit — в `/root/ddt`
через fetch. В примере заменить SHA на **полный идентификатор проверяемого коммита**.
Не разворачивать незакоммиченный рабочий каталог и не копировать venv между путями.

```sh
DDT_REV='ВСТАВИТЬ_ПОЛНЫЙ_COMMIT_SHA'
DDT_RELEASE="/opt/ddt/releases/$DDT_REV"
mkdir -p /opt/ddt/releases /opt/ddt/manifests
git clone --no-hardlinks /root/ddt "$DDT_RELEASE"
cd "$DDT_RELEASE"
git checkout --detach "$DDT_REV"
```

Установка инструментов сборки изменяет системные пакеты; предварительно сохраните
список выше. Не выполнять попутно `apk upgrade` и обновление firmware.

```sh
apk add build-base cmake pkgconf libusb-dev fftw-dev python3 python3-dev py3-pip swig linux-headers zlib-dev jpeg-dev
python3 -m venv .venv
.venv/bin/python3 -m pip install 'pip==25.2' 'setuptools==80.9.0' 'wheel==0.45.1'
mkdir -p dist/wheels
PYPI=1 .venv/bin/python3 -m pip wheel --no-build-isolation --wheel-dir dist/wheels -r requirements.txt
.venv/bin/python3 -m pip install --no-index --find-links dist/wheels -r requirements.txt
.venv/bin/python3 -m pip check
.venv/bin/python3 -c 'import lgpio; from luma.oled.device import ssd1306'
make build
DDT_SWEEP="$PWD/build/bin/sweep" make PYTHON=.venv/bin/python3 check
make PYTHON=.venv/bin/python3 replay
PYTHON=.venv/bin/python3 sh scripts/manifest.sh > "/opt/ddt/manifests/$DDT_REV.txt"
```

`PYPI=1` нужен исходной сборке lgpio с включённой C-библиотекой;
[upstream setup.py](https://github.com/joan2937/lg/blob/master/PY_LGPIO/setup.py)
различает этот режим и линковку с внешней liblgpio. Готовый manylinux wheel не
равен Alpine/musl wheel. Если сборка зависимости/импорт не прошли, **не переключать
сервис**; сохранить полный вывод. Проверка на ARM64 остаётся обязательной.

Сохранить `dist/wheels`, manifest и контрольные суммы wheels в хранилище релиза:

```sh
sha256sum dist/wheels/*.whl > dist/wheels.sha256
.venv/bin/python3 -m pip freeze --all > dist/python-freeze.txt
```

Версии в requirements закреплены, но это не полная побитовая воспроизводимость
ОС/компилятора. Для повторения того же релиза использовать сохранённые wheels
после проверки SHA256, тот же Python ABI/архитектуру/musl, manifest системных
пакетов и сохранённые APK/эталонный образ стенда. Не пересобирать релиз с тем же
именем на обновлённых зависимостях. Установка должна работать из wheelhouse
без интернета; самому устройству интернет для запуска не нужен.

## 3. Проверить новый runtime вручную

Этот этап прерывает детектирование. Сначала штатно остановить старую службу:

```sh
rc-service sweep stop
ps -ef | grep -E 'start_sweep|bin/sweep|driver.py|reset_hackrf_prompt'
```

Старый launcher может зависнуть на wait или оставить reset prompt. Если stop не
завершается — во втором SSH-сеансе проверить конкретные PID/команды и остановить
только эти процессы через TERM, затем при необходимости KILL. Не использовать
`killall python3`, не запускать второй экземпляр поверх первого.

Из каталога нового релиза:

```sh
.venv/bin/python3 -m ddt --config config/detector.json
```

Проверить OLED, изменение порога кнопками, статус в другом SSH-сеансе,
восстановление после отключения/подключения USB. Ctrl+C должен освободить SDR.
Без GPIO/OLED можно предварительно проверить `--headless`, но это не приёмка UI.
Если не работает — остановить новый процесс и `rc-service sweep start` для
возврата старого запуска; его файлы пока не изменены.

## 4. Переключить OpenRC

Новый ручной процесс должен быть остановлен. Проверить версию и наличие
supervise-daemon: `apk info -v | grep '^openrc-'`, `supervise-daemon --help`.
Механизм описан в [OpenRC supervisor guide](https://github.com/OpenRC/openrc/blob/master/supervise-daemon-guide.md).

```sh
mkdir -p /etc/ddt
# На первой установке. При существующем detector.json сначала сравнить его,
# сохранить резервную копию и перенести нужные значения, не перезаписывать вслепую.
test -f /etc/ddt/detector.json || install -m 0640 config/detector.json /etc/ddt/detector.json
install -m 0755 deploy/openrc/sweep /etc/init.d/sweep
install -m 0644 deploy/openrc/sweep.conf /etc/conf.d/sweep
cp -p /etc/ddt/detector.json "/opt/ddt/manifests/$DDT_REV.config.json"
```

Для короткой команды `ddt status` установить wrapper. Если `/usr/local/bin/ddt`
уже существует, сначала проверить, что это предыдущий wrapper проекта, и сохранить
его в backup; чужую команду не перезаписывать. Можно обойтись без установки,
вызывая `.venv/bin/python3 -m ddt status` из каталога релиза.

```sh
install -m 0755 deploy/ddt /usr/local/bin/ddt
```

В `/opt/ddt/current` допустима только ссылка, не настоящий каталог. Проверить
`ls -ld /opt/ddt/current` (при первой установке пути ещё нет). Если там каталог —
остановиться, не заменять его. Временная ссылка тоже не должна существовать:

```sh
ln -s "$DDT_RELEASE" /opt/ddt/current.next
mv -Tf /opt/ddt/current.next /opt/ddt/current
rc-update add sweep default
rc-service sweep start
sleep 5
rc-service sweep status
cat /run/ddt/status.json
tail -n 100 /var/log/ddt.log
```

В релизе с диагностикой schema 2 дополнительно выполнить `ddt status` и
`ddt status --json`. Код 1 означает прочитанный статус неготового устройства,
код 2 — недоступный/неподдерживаемый статус. При первом запуске дождаться нового
snapshot: старый файл без schema 2 отвергается. Подробности и приёмка изменений —
[diagnostics.md](diagnostics.md). При откате на старый релиз использовать его
прежние команды (`cat /run/ddt/status.json`), не новый интерфейс status.

OpenRC принудительно использует `/run/ddt/status.json`, независимо от JSON.
Стандартный бюджет штатной остановки приёмника — до 6 секунд (TERM, затем KILL),
службы — TERM/12/KILL/3. Не увеличивать `stop_timeout` без согласования этих
бюджетов и healthcheck. Зависший системный I/O может потребовать ручного
восстановления. Не менять live-каталог релиза, его venv или бинарник.

## 5. Откат первой миграции

Сначала остановить новый сервис и убедиться, что sweep больше не держит USB:

```sh
rc-service sweep stop
cp -p "$DDT_BACKUP/sweep" /etc/init.d/sweep
if [ -f "$DDT_BACKUP/sweep.conf" ]; then
    cp -p "$DDT_BACKUP/sweep.conf" /etc/conf.d/sweep
else
    # Удаляется только созданный на шаге 4 новый конфиг службы.
    rm /etc/conf.d/sweep
fi
rc-service sweep start
```

Старые `/usr/local/bin/start_sweep.sh`, `/root/ddt` и venv не изменялись.
Новый релиз/логи оставить для анализа. При будущих откатах между новыми релизами
после stop атомарно вернуть `/opt/ddt/current` на предыдущий каталог, восстановить
совместимый JSON из backup и start. Схему/содержимое конфигурации версионировать
вместе с релизом; никакого `git pull` в работающем релизе.

## Сбор диагностики

С рабочей машины, заменить hostname и путь релиза:

```sh
ssh root@alp-pi 'sh /opt/ddt/current/scripts/diagnostics.sh' > pi-diagnostics.txt 2>&1
```

Скрипт не сбрасывает USB и не запускает `hackrf_info` параллельно активному sweep.
Перед передачей просмотреть файл: присутствуют серийные номера, процессы и логи.
Не публиковать ключи SSH/Tailscale, Wi-Fi и дамп всего `/etc`.
