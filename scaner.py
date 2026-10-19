#!/usr/bin/env python3
"""Сканер виртуальных машин и Linux-систем.

Работает на Windows, macOS и Linux. Ищет ВМ в VirtualBox, VMware,
Parallels (macOS), UTM (macOS), WSL (Windows) и libvirt/KVM (Linux),
определяет, какие из них с Linux, и проверяет, не запущен ли сам скрипт
внутри виртуальной машины.

Запуск:
    python3 scaner.py                     # обычный отчёт
    python3 scaner.py --json              # отчёт в формате JSON
    python3 scaner.py --out report.json   # сохранить JSON-отчёт в файл
    python3 scaner.py --fast              # быстрый режим: без деталей каждой ВМ
    python3 scaner.py --timeout 5         # короче таймаут внешних команд

Тесты: python3 -m unittest -v
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

try:
    import psutil
except ImportError:  # psutil необязателен: без него просто не будет списка процессов
    psutil = None

SYSTEM = platform.system()  # 'Windows', 'Darwin' или 'Linux'
CMD_TIMEOUT = 15
CREATE_NO_WINDOW = 0x08000000  # флаг Windows: не мигать консолью
ENCODING = locale.getpreferredencoding(False) or "utf-8"
VERSION = "2.0"

# Быстрый режим (--fast): не опрашивать каждую ВМ отдельно, чтобы не ждать
# N запусков VBoxManage. Тогда у ВМ не будет памяти, CPU и IP.
FAST = False

# Состояния ВМ. Строки попадают и в JSON, и в консольный отчёт.
STATE_RUNNING = "запущена"
STATE_STOPPED = "остановлена"
STATE_UNKNOWN = "остановлена или не определено"

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _try_decode(data: bytes, encoding: str) -> str | None:
    """Строгое декодирование; None, если кодировка не подошла."""
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_output(data: bytes) -> str:
    """Декодирует вывод команды.

    wsl.exe отдаёт UTF-16, часть программ Windows — системную кодировку
    (cp1251/cp866), остальные — UTF-8. Определяем по содержимому.
    """
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:64]:
        candidates = ("utf-16", "utf-8")
    else:
        candidates = ("utf-8", ENCODING)

    text = None
    for encoding in dict.fromkeys(candidates):  # без повторов, порядок сохранён
        text = _try_decode(data, encoding)
        if text is not None:
            break
    if text is None:  # совсем битые или обрезанные данные
        text = data.decode("utf-8", errors="replace")
    return text.replace("\ufeff", "").replace("\x00", "")


def executable(cmd0: str) -> str | None:
    """Возвращает путь к программе или None, если её нет/нельзя запустить."""
    if not cmd0:
        return None
    if os.path.dirname(cmd0):  # абсолютный или относительный путь
        return cmd0 if os.path.isfile(cmd0) else None
    return shutil.which(cmd0)


def run(cmd: Iterable[str], timeout: int | None = None, ok_codes=(0,)) -> str | None:
    """Запускает команду. Возвращает текст вывода или None, если не получилось.

    Если программы нет в системе или она упала, команда молча пропускается.
    timeout по умолчанию берётся из CMD_TIMEOUT в момент вызова, чтобы
    учитывать значение из --timeout.
    """
    cmd = list(cmd)
    if not cmd:
        return None
    exe = executable(cmd[0])
    if exe is None:
        return None

    if timeout is None:
        timeout = CMD_TIMEOUT
    kwargs = {"capture_output": True, "timeout": timeout, "stdin": subprocess.DEVNULL}
    if SYSTEM == "Windows":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        res = subprocess.run([exe, *cmd[1:]], **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode not in ok_codes:
        return None
    return decode_output(res.stdout)


def find_tool(name: str, extra_paths: Iterable[str] = ()) -> str | None:
    """Ищет программу в PATH, затем в типичных местах установки."""
    found = shutil.which(name)
    if found:
        return found
    for path in extra_paths:
        if executable(path):
            return path
    return None


def parse_kv(text: str) -> dict[str, str]:
    """Разбирает строки вида key="value" в словарь (ключи в нижнем регистре)."""
    result: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip().lower()] = value.strip().strip('"')
    return result


# Дистрибутивы Linux: (название, ключевые слова). Совпадение ищется по целому
# слову, чтобы "arch" не находился внутри "search", а "mint" внутри "minter".
# Порядок важен: производные дистрибутивы идут раньше базовых.
LINUX_DISTROS = [
    ("Linux Mint", ("mint",)),
    ("Pop!_OS", ("pop", "pop_os")),
    ("Kali Linux", ("kali",)),
    ("Ubuntu", ("ubuntu", "xubuntu", "kubuntu", "lubuntu")),
    ("elementary OS", ("elementary",)),
    ("Zorin OS", ("zorin",)),
    ("Debian", ("debian", "devuan", "raspbian")),
    ("Fedora", ("fedora",)),
    ("CentOS", ("centos",)),
    ("Rocky/Alma Linux", ("rocky", "almalinux", "alma")),
    ("Red Hat", ("rhel", "redhat", "red hat")),
    ("Oracle Linux", ("oracle",)),
    ("Amazon Linux", ("amzn", "amazon")),
    ("Arch Linux", ("archlinux", "arch")),
    ("Manjaro", ("manjaro",)),
    ("openSUSE/SUSE", ("opensuse", "suse")),
    ("Gentoo", ("gentoo",)),
    ("Alpine", ("alpine",)),
    ("NixOS", ("nixos",)),
    ("Void Linux", ("void",)),
    ("Slackware", ("slackware",)),
    ("Deepin", ("deepin",)),
]

# Дистрибутивы, которые гипервизоры не умеют указывать в типе ОС: их выбирают
# как ближайшего родителя (Mint → Ubuntu, Kali → Debian, Manjaro → Arch).
# Только для них имеет смысл уточнять дистрибутив по имени ВМ.
DERIVED_DISTROS = frozenset({
    "Linux Mint", "Pop!_OS", "Kali Linux", "elementary OS", "Zorin OS",
    "Manjaro", "Rocky/Alma Linux", "Oracle Linux", "Devuan",
})


def guess_linux(*texts: str | None) -> tuple[bool, str | None]:
    """Возвращает (это_Linux, дистрибутив) по набору строк."""
    haystack = " ".join(t for t in texts if t).lower()
    for distro, keys in LINUX_DISTROS:
        for key in keys:
            if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", haystack):
                return True, distro
    # Слово "linux" может быть склеено с другим (VMware guestOS
    # "otherlinux-64", VirtualBox "Linux26_64"), поэтому тут ищем подстроку.
    if "linux" in haystack:
        return True, "Linux"
    return False, None


def classify_vm(os_type: str | None, name: str) -> tuple[bool, str | None]:
    """Определяет, Linux ли гостевая система.

    Если гипервизор сообщил конкретный тип ОС, доверяем только ему, а не
    имени ВМ. Имя используется, когда тип неизвестен/общий или когда оно
    называет более конкретный (производный) дистрибутив.
    """
    if os_type and not re.search(r"other|unknown|generic", os_type, re.I):
        is_linux, distro = guess_linux(os_type)
        if is_linux:
            _, named = guess_linux(name)
            if named in DERIVED_DISTROS:
                distro = named
        return is_linux, distro
    return guess_linux(os_type, name)


def make_vm(hypervisor: str, name: str, state: str, os_type: str = "", **details) -> dict:
    is_linux, distro = classify_vm(os_type, name)
    return {
        "hypervisor": hypervisor,
        "name": name,
        "state": state,
        "os_type": os_type or "",
        "is_linux": is_linux,
        "distro": distro,
        "details": {k: v for k, v in details.items() if v not in (None, "")},
    }


def running_process_cmdlines() -> list[tuple[str, str]]:
    """Список (имя_процесса_в_нижнем_регистре, командная_строка_в_нижнем_регистре).

    Через psutil, а если его нет — через системные утилиты, чтобы проверка
    запущенных ВМ работала и без установки зависимостей.
    """
    if psutil is not None:
        return psutil_processes()
    return system_processes()


def psutil_processes() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        result.append((name, cmdline))
    return result


def system_processes() -> list[tuple[str, str]]:
    """Процессы без psutil: tasklist на Windows, ps на macOS и Linux."""
    result: list[tuple[str, str]] = []
    if SYSTEM == "Windows":
        out = run(["tasklist", "/fo", "csv", "/nh"]) or ""
        for row in out.splitlines():
            match = re.match(r'^\s*"([^"]+)"', row)
            if match:
                name = match.group(1).lower()
                result.append((name, name))
        return result

    # args содержит и путь к программе, и её аргументы; имя процесса берём
    # из первого слова, а всю строку используем для поиска подстроки.
    out = run(["ps", "-A", "-ww", "-o", "args="]) or ""
    for row in out.splitlines():
        row = row.strip()
        if not row:
            continue
        exe = row.split(" ", 1)[0]
        result.append((os.path.basename(exe).lower(), row.lower()))
    return result


def normalize_path(path: str) -> str:
    """Единый вид пути для сравнения (важно на регистронезависимом Windows)."""
    return os.path.normcase(os.path.normpath(str(path)))


def bytes_to_mb(value) -> int | None:
    """Приводит размер памяти к мегабайтам.

    Разные гипервизоры отдают память либо сразу в МБ, либо в байтах
    (так делает UTM). Числа, которые явно больше любого разумного значения
    в МБ, считаем байтами.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number // (1024 * 1024) if number >= 1024 * 1024 else number


# ---------------------------------------------------------------------------
# Информация о хосте
# ---------------------------------------------------------------------------


def read_os_release() -> dict[str, str]:
    try:
        text = Path("/etc/os-release").read_text(errors="ignore")
    except OSError:
        return {}
    return parse_kv(text)


def get_system_info() -> dict:
    """Собирает общую информацию о системе, на которой запущен скрипт."""
    info: dict = {
        "machine": platform.machine(),  # arm64 на Apple Silicon, x86_64 на Intel
        "hostname": platform.node(),
        "python": platform.python_version(),
        "cpu_threads": os.cpu_count(),
    }

    if SYSTEM == "Darwin":
        info["os"] = f"macOS {platform.mac_ver()[0]}"
        cpu = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        info["cpu"] = (cpu or "").strip() or platform.processor()
    elif SYSTEM == "Windows":
        info["os"] = f"Windows {platform.release()} ({platform.version()})"
        info["cpu"] = platform.processor()
    else:
        release = read_os_release()
        info["os"] = release.get("pretty_name") or f"Linux {platform.release()}"
        info["cpu"] = read_cpu_model() or platform.processor() or platform.machine()

    if psutil is not None:
        info["ram_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    return info


def read_cpu_model() -> str | None:
    """Название процессора из /proc/cpuinfo (только Linux)."""
    try:
        lines = Path("/proc/cpuinfo").read_text(errors="ignore").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def detect_self_virtualization() -> dict:
    """Проверяет, не запущен ли сам скрипт внутри виртуальной машины."""
    keywords = ("virtualbox", "vmware", "kvm", "qemu", "parallels", "xen",
                "hyper-v", "virtual machine", "bochs", "innotek")

    if SYSTEM == "Linux":
        out = run(["systemd-detect-virt", "--vm"], ok_codes=(0, 1))
        if out is not None:
            value = out.strip()
            if value and value != "none":
                return {"is_vm": True, "detail": value}
            return {"is_vm": False, "detail": "физическая машина"}
        parts = []
        for name in ("sys_vendor", "product_name"):
            try:
                parts.append(Path("/sys/class/dmi/id/" + name).read_text().strip())
            except OSError:
                pass
        joined = " ".join(parts).lower()
        if joined:
            return {"is_vm": any(k in joined for k in keywords),
                    "detail": " ".join(parts)}

    elif SYSTEM == "Darwin":
        model = (run(["sysctl", "-n", "hw.model"]) or "").strip()
        if model and re.search(r"virtualmac|vmware|virtualbox|parallels|qemu", model, re.I):
            return {"is_vm": True, "detail": f"виртуальная машина ({model})"}
        out = run(["sysctl", "-n", "kern.hv_vmm_present"])
        if out is not None:
            is_vm = out.strip() == "1"
            detail = "виртуальная машина" if is_vm else "физический Mac"
            if model:
                detail += f" ({model})"
            return {"is_vm": is_vm, "detail": detail}

    elif SYSTEM == "Windows":
        # Принудительный UTF-8, иначе PowerShell отвечает в системной кодировке.
        command = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                   "Get-CimInstance Win32_ComputerSystem | "
                   "Select-Object Manufacturer,Model | ConvertTo-Json")
        out = run(["powershell", "-NoProfile", "-Command", command])
        if out:
            try:
                data = json.loads(out)
            except ValueError:
                data = None
            if isinstance(data, list):  # на всякий случай, если вернулся массив
                data = data[0] if data else {}
            if isinstance(data, dict):
                text = f"{data.get('Manufacturer', '')} {data.get('Model', '')}".strip()
                return {"is_vm": any(k in text.lower() for k in keywords),
                        "detail": text}

    return {"is_vm": None, "detail": "определить не удалось"}


# ---------------------------------------------------------------------------
# Сканирование гипервизоров. Каждая функция возвращает (установлен, список_ВМ)
# ---------------------------------------------------------------------------


def scan_virtualbox() -> tuple[bool, list[dict]]:
    tool = find_tool("VBoxManage", (
        r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
        "/Applications/VirtualBox.app/Contents/MacOS/VBoxManage",
        "/usr/local/bin/VBoxManage",
        "/usr/bin/VBoxManage",
    ))
    if not tool:
        return False, []

    listing = run([tool, "list", "vms"])
    if listing is None:
        return True, []
    running_out = run([tool, "list", "runningvms"]) or ""
    running = set(re.findall(r"\{([0-9a-fA-F-]+)\}", running_out))

    entries: list[tuple[str, str, bool]] = []
    for line in listing.splitlines():
        match = re.match(r'^"(.*)"\s+\{([0-9a-fA-F-]+)\}\s*$', line.strip())
        if match:
            name, uuid = match.groups()
            entries.append((name, uuid, uuid in running))

    def details(entry: tuple[str, str, bool]) -> tuple[dict[str, str], str | None]:
        """Детали одной ВМ: VBoxManage опрашивает каждую ВМ отдельно."""
        _, uuid, is_running = entry
        if FAST:
            return {}, None
        info = parse_kv(run([tool, "showvminfo", uuid, "--machinereadable"]) or "")
        ip = None
        if is_running:
            prop = run([tool, "guestproperty", "get", uuid,
                        "/VirtualBox/GuestInfo/Net/0/V4/IP"])
            if prop and "Value:" in prop:
                ip = prop.split("Value:", 1)[1].strip() or None
                if ip and ip.upper() == "N/A":  # VirtualBox так пишет «адреса нет»
                    ip = None
        return info, ip

    # Опрос каждой ВМ — это отдельный запуск процесса, поэтому распараллеливаем.
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(entries)))) as pool:
        infos = list(pool.map(details, entries))

    vms = []
    for (name, _, is_running), (info, ip) in zip(entries, infos):
        vms.append(make_vm(
            "VirtualBox", name,
            STATE_RUNNING if is_running else STATE_STOPPED,
            info.get("ostype", ""),
            memory_mb=info.get("memory"), cpus=info.get("cpus"), ip=ip,
        ))
    return True, vms


def parse_vmx(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return data
    for line in text.splitlines():
        match = re.match(r'^\s*([\w.:]+)\s*=\s*"(.*)"\s*$', line)
        if match:
            data[match.group(1).lower()] = match.group(2)
    return data


def scan_vmware() -> tuple[bool, list[dict]]:
    tool = find_tool("vmrun", (
        "/Applications/VMware Fusion.app/Contents/Public/vmrun",
        r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
        r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
    ))

    home = Path.home()
    search_dirs = [
        home / "Virtual Machines.localized",
        home / "Virtual Machines",
        home / "Documents" / "Virtual Machines",
        home / "Documents" / "Virtual Machines.localized",
        home / "vmware",
    ]
    existing = [d for d in search_dirs if d.is_dir()]
    if not tool and not existing:
        return False, []

    running: set[str] = set()
    if tool:
        product = "fusion" if SYSTEM == "Darwin" else "ws"
        out = run([tool, "-T", product, "list"]) or ""
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line:
                running.add(normalize_path(line))

    vms, seen = [], set()
    for directory in existing:
        for vmx in list(directory.glob("*.vmx")) + list(directory.glob("*/*.vmx")):
            key = normalize_path(vmx)
            if key in seen:
                continue
            seen.add(key)
            data = parse_vmx(vmx)
            vms.append(make_vm(
                "VMware", data.get("displayname") or vmx.stem,
                STATE_RUNNING if key in running else STATE_STOPPED,
                data.get("guestos", ""),
                memory_mb=data.get("memsize"), cpus=data.get("numvcpus"),
                path=str(vmx),
            ))
    return True, vms


def scan_parallels() -> tuple[bool, list[dict]]:
    tool = find_tool("prlctl", ("/usr/local/bin/prlctl",
                                "/Applications/Parallels Desktop.app/Contents/MacOS/prlctl"))
    if not tool:
        return False, []
    out = run([tool, "list", "-a", "-i", "-j"])
    if out is None:
        return True, []
    try:
        data = json.loads(out)
    except ValueError:
        return True, []
    if isinstance(data, dict):
        data = [data]

    vms = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("Name") or item.get("name") or "?"
        status = str(item.get("State") or item.get("status") or "").lower()
        hardware = item.get("Hardware") if isinstance(item.get("Hardware"), dict) else {}
        cpu = hardware.get("cpu") if isinstance(hardware.get("cpu"), dict) else {}
        memory = hardware.get("memory") if isinstance(hardware.get("memory"), dict) else {}
        vms.append(make_vm(
            "Parallels", name,
            STATE_RUNNING if status == "running" else STATE_STOPPED,
            item.get("OS") or item.get("os") or "",
            cpus=cpu.get("cpus"),
            memory_mb=memory.get("size"),
            ip=item.get("ip_configured") or item.get("IP Address"),
        ))
    return True, vms


def scan_utm() -> tuple[bool, list[dict]]:
    docs = Path.home() / "Library/Containers/com.utmapp.UTM/Data/Documents"
    installed = docs.is_dir() or Path("/Applications/UTM.app").exists()
    if not installed:
        return False, []

    procs = [cmdline for name, cmdline in running_process_cmdlines()
             if "qemu" in name or name.startswith("com.apple.virtualization.virtualmachine")]

    vms = []
    for bundle in (sorted(docs.glob("*.utm")) if docs.is_dir() else []):
        try:
            with open(bundle / "config.plist", "rb") as fh:
                config = plistlib.load(fh)
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        information = config.get("Information") or {}
        system = config.get("System") or {}
        name = information.get("Name") or bundle.stem
        is_running = any(name.lower() in cmdline for cmdline in procs)
        vms.append(make_vm(
            "UTM", name,
            STATE_RUNNING if is_running else STATE_UNKNOWN,
            # в UTM нет поля с типом гостевой ОС, лучшая подсказка это значок и имя
            str(information.get("Icon") or ""),
            arch=system.get("Architecture"),
            cpus=system.get("CPUCount"),
            memory_mb=bytes_to_mb(system.get("MemorySize")),
        ))
    return True, vms


# WSL отдаёт таблицу вида: "  NAME   STATE   VERSION", имя (запущенного)
# дистрибутива помечено "*" и может содержать пробелы ("Ubuntu 22.04").
WSL_ROW = re.compile(
    r"^\*?\s*(?P<name>.+?)\s+"
    r"(?P<state>Running|Stopped|Installing|Uninstalling|Converting|Disabled)\s*"
    r"(?P<version>\d+)?\s*$",
    re.I,
)


def scan_wsl() -> tuple[bool, list[dict]]:
    out = run(["wsl.exe", "--list", "--verbose"])
    if out is None:
        return False, []
    vms = []
    for row in out.splitlines()[1:]:
        match = WSL_ROW.match(row.strip())
        if not match:
            continue
        name = match.group("name").strip()
        state = match.group("state")
        if name.lower().startswith("docker-desktop"):
            continue  # служебные дистрибутивы Docker, это не гостевые системы
        vms.append(make_vm(
            "WSL", name,
            STATE_RUNNING if state.lower() == "running" else STATE_STOPPED,
            "Linux",  # все дистрибутивы WSL это Linux
            wsl_version=match.group("version"),
        ))
    return True, vms


def scan_libvirt() -> tuple[bool, list[dict]]:
    tool = find_tool("virsh", ("/usr/bin/virsh", "/usr/local/bin/virsh"))
    if not tool:
        return False, []
    out = run([tool, "list", "--all"])
    if out is None:
        return True, []
    vms = []
    for row in out.splitlines():
        row = row.strip()
        if not row or set(row) <= {"-"}:
            continue
        match = re.match(r"^(\S+)\s+(\S+)\s+(.+?)$", row)
        if not match:
            continue
        vm_id, name, state = match.groups()
        if vm_id.lower() == "id":  # строка-заголовок
            continue
        vms.append(make_vm(
            "libvirt/KVM", name,
            STATE_RUNNING if state.lower().startswith("running") else STATE_STOPPED,
            running_id=vm_id if vm_id != "-" else None,
        ))
    return True, vms


def hypervisor_scanners() -> list[tuple[str, Callable[[], tuple[bool, list[dict]]]]]:
    scanners: list[tuple[str, Callable[[], tuple[bool, list[dict]]]]] = [
        ("VirtualBox", scan_virtualbox),
        ("VMware", scan_vmware),
    ]
    if SYSTEM == "Darwin":
        scanners += [("Parallels", scan_parallels), ("UTM", scan_utm)]
    elif SYSTEM == "Windows":
        scanners += [("WSL", scan_wsl)]
    elif SYSTEM == "Linux":
        scanners += [("libvirt/KVM", scan_libvirt)]
    return scanners


# Процессы гипервизоров. Сравниваем имя целиком или по началу, а не по
# вхождению подстроки, чтобы не ловить посторонние программы.
HYPERVISOR_PROCESS_NAMES = {
    "virtualboxvm", "vboxheadless", "vboxsvc", "vmware-vmx", "vmware",
    "vmware-vmx-debug", "prl_vm_app", "prl_client_app", "utm", "vmmem",
    "vmmemwsl", "vmwp",
}
HYPERVISOR_PROCESS_PREFIXES = ("qemu-system", "com.apple.virtualization.virtualmachine")


def scan_hypervisor_processes() -> dict[str, int] | None:
    """Считает запущенные процессы гипервизоров. None — список получить не удалось."""
    procs = running_process_cmdlines()
    if not procs:
        return None
    found: dict[str, int] = {}
    for name, _ in procs:
        base = name[:-4] if name.endswith(".exe") else name
        if base in HYPERVISOR_PROCESS_NAMES or base.startswith(HYPERVISOR_PROCESS_PREFIXES):
            found[base] = found.get(base, 0) + 1
    return found


# ---------------------------------------------------------------------------
# Сборка отчёта и вывод
# ---------------------------------------------------------------------------


def collect_report() -> dict:
    hypervisors = []
    for name, scanner in hypervisor_scanners():
        try:
            installed, vms = scanner()
        except Exception as exc:  # noqa: BLE001 — один сломанный гипервизор не рушит отчёт
            hypervisors.append({"name": name, "installed": False, "vms": [],
                                "error": f"{type(exc).__name__}: {exc}"})
            continue
        vms.sort(key=lambda vm: (vm["state"] != STATE_RUNNING, vm["name"].lower()))
        hypervisors.append({"name": name, "installed": bool(installed), "vms": vms})

    all_vms = [vm for h in hypervisors for vm in h["vms"]]
    return {
        "system": get_system_info(),
        "inside_vm": detect_self_virtualization(),
        "hypervisors": hypervisors,
        "hypervisor_processes": scan_hypervisor_processes(),
        "fast_mode": FAST,
        "psutil_available": psutil is not None,
        "summary": {
            "total_vms": len(all_vms),
            "running_vms": sum(1 for vm in all_vms if vm["state"] == STATE_RUNNING),
            "linux_vms": sum(1 for vm in all_vms if vm["is_linux"]),
        },
    }


def line(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def print_report(report: dict) -> None:
    line("🔍 СКАНЕР ВИРТУАЛЬНЫХ МАШИН И LINUX-СИСТЕМ")

    s = report["system"]
    print("\n--- Информация о системе ---")
    print(f"💻 Система: {s['os']}")
    print(f"🏗️  Архитектура: {s['machine']}")
    print(f"🏷️  Имя компьютера: {s['hostname']}")
    threads = s.get("cpu_threads") or "?"
    print(f"⚙️  Процессор: {s.get('cpu') or 'неизвестно'} ({threads} потоков)")
    if "ram_gb" in s:
        print(f"🧠 Память: {s['ram_gb']} ГБ")
    print(f"🐍 Python: {s['python']}")

    inside = report["inside_vm"]
    print("\n--- Запущен ли сам скрипт внутри ВМ ---")
    if inside["is_vm"] is None:
        print(f"❔ {inside['detail']}")
    elif inside["is_vm"]:
        print(f"🖥️  Да, это виртуальная машина: {inside['detail']}")
    else:
        print(f"✅ Нет: {inside['detail']}")

    line("🖥️  ГИПЕРВИЗОРЫ И ВИРТУАЛЬНЫЕ МАШИНЫ")
    for hyper in report["hypervisors"]:
        if hyper.get("error"):
            print(f"\n{hyper['name']}: ошибка сканирования — {hyper['error']}")
        elif not hyper["installed"]:
            print(f"\n{hyper['name']}: не найден")
        elif not hyper["vms"]:
            print(f"\n{hyper['name']}: установлен, ВМ не найдено")
        else:
            print(f"\n{hyper['name']}: найдено ВМ: {len(hyper['vms'])}")
            for vm in hyper["vms"]:
                icon = "🐧" if vm["is_linux"] else "🖥️"
                kind = vm["distro"] if vm["is_linux"] else (vm["os_type"] or "ОС не определена")
                print(f"   {icon} {vm['name']}  [{kind}]  {vm['state']}")
                for key, label in (("cpus", "процессоры"), ("memory_mb", "память, МБ"),
                                   ("ip", "IP"), ("arch", "архитектура"),
                                   ("path", "файл")):
                    if key in vm["details"]:
                        print(f"        {label}: {vm['details'][key]}")

    procs = report["hypervisor_processes"]
    print("\n--- Запущенные процессы гипервизоров ---")
    if procs is None:
        print("список процессов получить не удалось "
              "(для надёжности поставь psutil: pip install psutil)")
    elif not procs:
        print("не найдено")
    else:
        for name, count in sorted(procs.items()):
            print(f"   ▶ {name} (процессов: {count})")

    line("📋 ИТОГ")
    summary = report["summary"]
    total = summary["total_vms"]
    if total == 0:
        print("❌ Виртуальные машины не найдены")
    else:
        print(f"Всего ВМ: {total}, запущено: {summary['running_vms']}, "
              f"из них с Linux: {summary['linux_vms']}")
        for hyper in report["hypervisors"]:
            if hyper["vms"]:
                print(f"   {hyper['name']}: {len(hyper['vms'])}")
    if FAST:
        print("ℹ️  Быстрый режим (--fast): детали ВМ (память, CPU, IP) не опрашивались")
    print("✅ СКАНИРОВАНИЕ ЗАВЕРШЕНО")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сканер виртуальных машин и Linux-систем")
    parser.add_argument("--json", action="store_true", help="вывести отчёт в формате JSON")
    parser.add_argument("--out", metavar="ФАЙЛ", help="сохранить JSON-отчёт в файл")
    parser.add_argument("--timeout", type=int, default=CMD_TIMEOUT, metavar="СЕК",
                        help=f"таймаут одной внешней команды (по умолчанию {CMD_TIMEOUT})")
    parser.add_argument("--fast", action="store_true",
                        help="быстрый режим: не опрашивать детали каждой ВМ (память, CPU, IP)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    global CMD_TIMEOUT, FAST
    if args.timeout > 0:
        CMD_TIMEOUT = args.timeout
    FAST = args.fast

    # Чтобы эмодзи и кириллица не ломались в консоли Windows
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    report = collect_report()

    try:
        if args.out:
            path = Path(args.out)
            if path.parent and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            print(f"Отчёт сохранён: {path}")
        elif args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_report(report)
    except BrokenPipeError:  # например, `python3 scaner.py | head`
        try:
            sys.stdout.close()
        finally:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
