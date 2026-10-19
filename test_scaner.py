#!/usr/bin/env python3
"""Тесты для scaner.py.

Запуск:
    python3 -m unittest -v

Внешние команды и файловая система не используются: вывод гипервизоров
подменяется заглушками, поэтому тесты работают на любой ОС.
"""

import io
import unittest
from unittest import mock

import scaner


class DistroTest(unittest.TestCase):
    def test_classify_uses_os_type(self):
        self.assertEqual(scaner.classify_vm("Ubuntu_64", "prod-db"), (True, "Ubuntu"))
        self.assertEqual(scaner.classify_vm("Debian12_64", "Trixie"), (True, "Debian"))

    def test_vmware_glued_otherlinux(self):
        # guestOS в VMware называется otherlinux-64: "linux" склеено с "other",
        # но тип всё равно должен распознаваться как Linux.
        self.assertEqual(scaner.classify_vm("otherlinux-64", "web-1"), (True, "Linux"))

    def test_name_refines_only_derived_distro(self):
        # Mint гипервизоры указывают как Ubuntu — имя уточняет дистрибутив.
        self.assertEqual(scaner.classify_vm("Ubuntu_64", "Mint test"), (True, "Linux Mint"))
        # Debian не является производным от Ubuntu, поэтому имя игнорируется.
        self.assertEqual(scaner.classify_vm("Ubuntu_64", "Debian box"), (True, "Ubuntu"))

    def test_word_boundaries(self):
        # "arch" не должен находиться внутри "search", "mint" внутри "minter".
        self.assertEqual(scaner.classify_vm("Windows11_64", "arch-search"), (False, None))
        self.assertEqual(scaner.classify_vm("other", "Kali Linux"), (True, "Kali Linux"))

    def test_non_linux_is_not_linux(self):
        self.assertEqual(scaner.classify_vm("Windows11_64", "win-vm"), (False, None))
        self.assertEqual(scaner.classify_vm("", ""), (False, None))


class DecodeTest(unittest.TestCase):
    def test_utf16_and_utf8(self):
        self.assertEqual(scaner.decode_output("Привет".encode("utf-16")), "Привет")
        self.assertEqual(scaner.decode_output("Привет".encode("utf-8")), "Привет")

    def test_broken_and_empty_data(self):
        self.assertEqual(scaner.decode_output(b""), "")
        # Обрезанный UTF-16 не должен приводить к исключению.
        self.assertIsInstance(scaner.decode_output(b"\xff\xfe\x00"), str)
        self.assertIsInstance(scaner.decode_output(b"\xd2\xe8\xec"), str)

    def test_bom_is_stripped(self):
        self.assertEqual(scaner.decode_output(b"\xef\xbb\xbfVBoxManage"), "VBoxManage")


class HelperTest(unittest.TestCase):
    def test_bytes_to_mb(self):
        self.assertEqual(scaner.bytes_to_mb(4294967296), 4096)  # байты (UTM)
        self.assertEqual(scaner.bytes_to_mb(2048), 2048)        # уже МБ
        self.assertIsNone(scaner.bytes_to_mb(None))

    def test_parse_kv(self):
        data = scaner.parse_kv('ostype="Ubuntu_64"\nMemory = "4096"\n')
        self.assertEqual(data, {"ostype": "Ubuntu_64", "memory": "4096"})

    def test_make_vm_drops_empty_details(self):
        vm = scaner.make_vm("Test", "vm", scaner.STATE_STOPPED, "", ip=None, cpus="2")
        self.assertEqual(vm["details"], {"cpus": "2"})

    def test_run_skips_missing_tool(self):
        self.assertIsNone(scaner.run(["этой-программы-точно-нет", "arg"]))


class FakeRun:
    """Заглушка scaner.run: возвращает заранее заданный вывод по аргументам."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def __call__(self, cmd, timeout=None, ok_codes=(0,)):
        cmd = list(cmd)
        self.calls.append(tuple(cmd))
        return self.outputs.get(tuple(cmd[1:]))


UUID_MINT = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_SERVER = "bbbbbbbb-1111-2222-3333-444444444444"


class VirtualBoxTest(unittest.TestCase):
    outputs = {
        ("list", "vms"): (
            f'"Mint test" {{{UUID_MINT}}}\n'
            f'"Server" {{{UUID_SERVER}}}\n'
        ),
        ("list", "runningvms"): f'"Server" {{{UUID_SERVER}}}\n',
        ("showvminfo", UUID_MINT, "--machinereadable"): (
            'ostype="Ubuntu_64"\nmemory="4096"\ncpus="2"\n'
        ),
        ("showvminfo", UUID_SERVER, "--machinereadable"): (
            'ostype="Debian_64"\nmemory="2048"\ncpus="1"\n'
        ),
        ("guestproperty", "get", UUID_SERVER,
         "/VirtualBox/GuestInfo/Net/0/V4/IP"): "Value: 10.0.2.15\n",
    }

    def setUp(self):
        self.fake = FakeRun(self.outputs)
        patcher_run = mock.patch.object(scaner, "run", self.fake)
        patcher_tool = mock.patch.object(scaner, "find_tool", return_value="/usr/bin/VBoxManage")
        patcher_fast = mock.patch.object(scaner, "FAST", False)
        self.addCleanup(patcher_fast.stop)
        self.addCleanup(patcher_tool.stop)
        self.addCleanup(patcher_run.stop)
        patcher_run.start()
        patcher_tool.start()
        patcher_fast.start()

    def test_parses_vms(self):
        installed, vms = scaner.scan_virtualbox()
        self.assertTrue(installed)
        self.assertEqual([vm["name"] for vm in vms], ["Mint test", "Server"])

        mint, server = vms
        self.assertEqual(mint["state"], scaner.STATE_STOPPED)
        self.assertEqual((mint["is_linux"], mint["distro"]), (True, "Linux Mint"))
        self.assertEqual(mint["details"]["memory_mb"], "4096")

        self.assertEqual(server["state"], scaner.STATE_RUNNING)
        self.assertEqual(server["details"]["ip"], "10.0.2.15")
        # IP спрашиваем только у запущенной ВМ
        self.assertNotIn(("guestproperty", "get", UUID_MINT,
                          "/VirtualBox/GuestInfo/Net/0/V4/IP"), self.fake.calls)

    def test_fast_mode_skips_details(self):
        with mock.patch.object(scaner, "FAST", True):
            _, vms = scaner.scan_virtualbox()
        self.assertEqual(vms[0]["details"], {})
        self.assertFalse(any("showvminfo" in call for call in self.fake.calls))


class WslTest(unittest.TestCase):
    table = ("  NAME            STATE           VERSION\n"
             "* Ubuntu 22.04    Running         2\n"
             "  Debian          Stopped         1\n"
             "  docker-desktop  Running         2\n")

    def test_names_with_spaces_and_skip_docker(self):
        with mock.patch.object(scaner, "run", return_value=self.table):
            installed, vms = scaner.scan_wsl()
        self.assertTrue(installed)
        self.assertEqual([vm["name"] for vm in vms], ["Ubuntu 22.04", "Debian"])
        self.assertEqual(vms[0]["state"], scaner.STATE_RUNNING)
        self.assertEqual(vms[0]["details"]["wsl_version"], "2")
        self.assertTrue(all(vm["is_linux"] for vm in vms))


class ProcessTest(unittest.TestCase):
    def test_ps_fallback_without_psutil(self):
        with mock.patch.object(scaner, "SYSTEM", "Darwin"), \
             mock.patch.object(scaner, "psutil", None), \
             mock.patch.object(scaner, "run",
                               return_value="/usr/bin/qemu-system-x86_64 -m 2048\n/sbin/launchd\n"):
            procs = scaner.running_process_cmdlines()
            found = scaner.scan_hypervisor_processes()
        self.assertEqual(procs[0], ("qemu-system-x86_64", "/usr/bin/qemu-system-x86_64 -m 2048"))
        self.assertEqual(found, {"qemu-system-x86_64": 1})


class ReportTest(unittest.TestCase):
    def test_broken_scanner_does_not_break_report(self):
        def boom():
            raise RuntimeError("бум")

        scanners = [("Сломанный", boom), ("Пустой", lambda: (False, []))]
        with mock.patch.object(scaner, "hypervisor_scanners", return_value=scanners), \
             mock.patch.object(scaner, "get_system_info", return_value={}), \
             mock.patch.object(scaner, "detect_self_virtualization",
                               return_value={"is_vm": None, "detail": "test"}), \
             mock.patch.object(scaner, "scan_hypervisor_processes", return_value={}):
            report = scaner.collect_report()

        broken, empty = report["hypervisors"]
        self.assertTrue(broken["error"].startswith("RuntimeError: бум"))
        self.assertEqual(broken["vms"], [])
        self.assertFalse(empty["installed"])
        self.assertEqual(report["summary"]["total_vms"], 0)

    def test_summary_counts(self):
        vms = [
            scaner.make_vm("VB", "a", scaner.STATE_RUNNING, "Ubuntu_64"),
            scaner.make_vm("VB", "b", scaner.STATE_STOPPED, "Windows11_64"),
        ]
        scanners = [("VB", lambda: (True, vms))]
        with mock.patch.object(scaner, "hypervisor_scanners", return_value=scanners), \
             mock.patch.object(scaner, "get_system_info", return_value={}), \
             mock.patch.object(scaner, "detect_self_virtualization",
                               return_value={"is_vm": None, "detail": "test"}), \
             mock.patch.object(scaner, "scan_hypervisor_processes", return_value={}):
            report = scaner.collect_report()

        self.assertEqual(report["summary"],
                         {"total_vms": 2, "running_vms": 1, "linux_vms": 1})
        # запущенные ВМ идут первыми
        self.assertEqual([vm["name"] for vm in report["hypervisors"][0]["vms"]], ["a", "b"])


class CliTest(unittest.TestCase):
    def test_fast_flag_sets_global(self):
        # --fast и --timeout читаются до запуска сканирования
        with mock.patch.object(scaner, "collect_report", return_value={
                "system": {}, "inside_vm": {"is_vm": None, "detail": ""},
                "hypervisors": [], "hypervisor_processes": {},
                "fast_mode": True, "psutil_available": False,
                "summary": {"total_vms": 0, "running_vms": 0, "linux_vms": 0}}), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            scaner.main(["--fast", "--timeout", "7", "--json"])
        self.assertTrue(scaner.FAST)
        self.assertEqual(scaner.CMD_TIMEOUT, 7)
        scaner.FAST = False
        scaner.CMD_TIMEOUT = 15


if __name__ == "__main__":
    unittest.main(verbosity=2)
