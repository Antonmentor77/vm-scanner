# vm-scanner

[![Тесты](https://github.com/Antonmentor77/vm-scanner/actions/workflows/tests.yml/badge.svg)](https://github.com/Antonmentor77/vm-scanner/actions/workflows/tests.yml)
[![Лицензия: MIT](https://img.shields.io/badge/лицензия-MIT-blue.svg)](LICENSE)

Сканер виртуальных машин и Linux-систем: один скрипт на Python, без обязательных
зависимостей. Работает на **Windows**, **macOS** и **Linux**.

Скрипт находит ВМ в установленных гипервизорах, определяет, какие из них с Linux
(и какой дистрибутив), а также проверяет, не запущен ли сам скрипт внутри
виртуальной машины.

## Что поддерживается

| Гипервизор | ОС хоста | Как находит ВМ |
|---|---|---|
| VirtualBox | Windows / macOS / Linux | `VBoxManage` |
| VMware Fusion / Workstation | macOS / Windows | `vmrun` + поиск `*.vmx` |
| Parallels Desktop | macOS | `prlctl` |
| UTM | macOS | `config.plist` из контейнера + `qemu`-процессы |
| WSL | Windows | `wsl.exe --list --verbose` |
| libvirt / KVM | Linux | `virsh` |

## Запуск

```bash
python3 scaner.py                 # обычный отчёт
python3 scaner.py --json          # отчёт в формате JSON
python3 scaner.py --out report.json
python3 scaner.py --fast          # быстрый режим: без опроса деталей каждой ВМ
python3 scaner.py --timeout 5     # таймаут одной внешней команды (сек)
python3 scaner.py --version
```

Зависимости не нужны. Для надёжного списка процессов гипервизоров рекомендуется
поставить `psutil`, но без него всё тоже работает — сканер использует
`ps` / `tasklist`.

## Пример вывода

```
============================================================
🔍 СКАНЕР ВИРТУАЛЬНЫХ МАШИН И LINUX-СИСТЕМ
============================================================

--- Информация о системе ---
💻 Система: macOS 26.6.2
🏗️  Архитектура: arm64
🏷️  Имя компьютера: MacBook-Air.local
⚙️  Процессор: Apple M4 (10 потоков)
🐍 Python: 3.13.5

--- Запущен ли сам скрипт внутри ВМ ---
✅ Нет: физический Mac (Mac16,12)

============================================================
🖥️  ГИПЕРВИЗОРЫ И ВИРТУАЛЬНЫЕ МАШИНЫ
============================================================

VirtualBox: найдено ВМ: 2
   🐧 Mint dev  [Linux Mint]  запущена
        память, МБ: 4096
        процессоры: 4
   🐧 Server  [Debian]  запущена
        память, МБ: 2048
        процессоры: 2
        IP: 10.0.2.15

============================================================
📋 ИТОГ
Всего ВМ: 2, запущено: 2, из них с Linux: 2
   VirtualBox: 2
✅ СКАНИРОВАНИЕ ЗАВЕРШЕНО
```

## Как определяется дистрибутив

Гипервизоры почти не умеют указывать точный дистрибутив (VirtualBox выбирает
Mint как «Ubuntu»), поэтому:

* если гипервизор сообщил конкретный тип ОС — ему доверяют, а не имени ВМ;
* имя ВМ уточняет дистрибутив только когда это производный дистрибутив
  (Mint, Pop!_OS, Kali, Manjaro, elementary, Zorin, Rocky/Alma, Oracle Linux);
* названия ищутся по целому слову, чтобы `arch` не находился внутри
  «arch-search», а `mint` внутри «minter»;
* склеенные написания вроде `otherlinux-64` (VMware) и `Linux26_64` (VirtualBox)
  тоже распознаются как Linux.

## JSON-отчёт

```json
{
  "system": {"os": "...", "machine": "arm64", "hostname": "...",
             "python": "...", "cpu_threads": 10, "cpu": "...", "ram_gb": 16.0},
  "inside_vm": {"is_vm": false, "detail": "физический Mac (Mac16,12)"},
  "hypervisors": [
    {"name": "VirtualBox", "installed": true, "vms": [
      {"hypervisor": "VirtualBox", "name": "Mint dev", "state": "запущена",
       "os_type": "Ubuntu_64", "is_linux": true, "distro": "Linux Mint",
       "details": {"memory_mb": "4096", "cpus": "4", "ip": "10.0.2.15"}}
    ]}
  ],
  "hypervisor_processes": {"virtualboxvm": 1},
  "fast_mode": false,
  "psutil_available": true,
  "summary": {"total_vms": 1, "running_vms": 1, "linux_vms": 1}
}
```

Поля `details` у ВМ зависят от гипервизора: `memory_mb`, `cpus`, `ip`,
`arch`, `wsl_version`, `path`, `running_id`. Упавший сканер не роняет отчёт —
у гипервизора появляется поле `error`.

## Тесты

```bash
python3 -m unittest -v
```

Тесты не трогают реальные гипервизоры: вывод команд подменяется заглушками,
поэтому они проходят на любой машине.

## Структура

* `scaner.py` — сам сканер, один файл, без зависимостей;
* `test_scaner.py` — тесты на `unittest`.

## Лицензия

Проект распространяется под лицензией MIT — см. [LICENSE](LICENSE).
