"""Regression tests for megaraid-console 1.0.6.

Two fixes are covered, both hardware-independent:

*   battery reporting on cards that answer `/cN/bbu` instead of `/cN/cv`
    (Dell PERC H840 and other battery-backed cards), and
*   the UI action bug that turned every successfully submitted write
    (locate LED, buzzer silence, ...) into
    "Cannot read properties of null (reading 'target')".

Nothing here touches the controller: storcli is replaced by fixtures, so the
suite is safe to run on a live system and cannot trigger a dangerous operation.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "app", "server")
UI = os.path.join(ROOT, "app", "ui")
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, SERVER)

import raidcore  # noqa: E402
import raidwatchd  # noqa: E402


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def doc(response_data):
    return {
        "Controllers": [{
            "Command Status": {"Controller": 0, "Status": "Success",
                               "Description": "None"},
            "Response Data": response_data,
        }]
    }


class FakeStorcli:
    """Stand-in for run_storcli_json: routes argv to a fixture or an error."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, binary, args, timeout=60):
        key = " ".join(str(a) for a in args)
        self.calls.append(key)
        if key not in self.routes:
            raise raidcore.StorcliError("unexpected storcli call: %s" % key)
        value = self.routes[key]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def patch(self, testcase):
        original = raidcore.run_storcli_json
        raidcore.run_storcli_json = self
        testcase.addCleanup(setattr, raidcore, "run_storcli_json", original)
        return self


NOT_FOUND = raidcore.StorcliError("Controller 0 not found")


class BatteryCollectorTests(unittest.TestCase):
    def test_battery_card_answers_on_bbu_only(self):
        """H840 shape: /c0/cv does not exist, /c0/bbu carries everything."""
        fake = FakeStorcli({
            "/c0/cv show all": NOT_FOUND,
            "/c0/bbu show all": load("bbu_show_all.json"),
        }).patch(self)

        battery = raidcore.gather_battery("/opt/MegaRAID/storcli/storcli64")

        self.assertEqual(battery["kind"], "bbu")
        self.assertEqual(battery["state"], "Optimal")
        self.assertEqual(battery["model"], "BBU")
        self.assertEqual(battery["voltage"], "4196 mV")
        self.assertEqual(battery["current"], "0 mA")
        self.assertEqual(battery["temp"], "25 C")
        self.assertEqual(battery["temp_value"], 25)
        self.assertEqual(battery["retention"], "24 hours")
        self.assertEqual(battery["replacement_required"], "No")
        self.assertEqual(battery["design_capacity"], "4400 mAh")
        self.assertEqual(battery["serial"], "SN-BBU-0001")
        self.assertEqual(battery["learn_period"], "90 days")
        self.assertEqual(battery["next_learn"], "2026-11-10 02:00:00")
        # CacheVault is probed first and only then the battery.
        self.assertEqual(fake.calls, ["/c0/cv show all", "/c0/bbu show all"])

    def test_supercapacitor_card_still_reports_as_cachevault(self):
        fake = FakeStorcli({
            "/c0/cv show all": load("cv_show_all.json"),
            "/c0/bbu show all": NOT_FOUND,
        }).patch(self)

        battery = raidcore.gather_battery("/opt/MegaRAID/storcli/storcli64")

        self.assertEqual(battery["kind"], "cachevault")
        self.assertEqual(battery["state"], "Optimal")
        self.assertEqual(battery["model"], "CacheVault")
        self.assertEqual(battery["pack_energy"], "1050 J")
        self.assertEqual(battery["capacitance"], "98 %")
        self.assertEqual(battery["nvcache_state"], "OK")
        self.assertEqual(battery["firmware"], "1.0")
        self.assertEqual(battery["learn_mode"], "Autonomic")
        # The battery object must not be queried once CacheVault answered.
        self.assertEqual(fake.calls, ["/c0/cv show all"])

    def test_no_power_backup_reports_nothing(self):
        FakeStorcli({
            "/c0/cv show all": NOT_FOUND,
            "/c0/bbu show all": NOT_FOUND,
        }).patch(self)
        self.assertEqual(raidcore.gather_battery("/usr/bin/storcli64"), {})

    def test_empty_answer_is_not_mistaken_for_a_battery(self):
        """A bare success block carries neither state nor model."""
        FakeStorcli({
            "/c0/cv show all": load("bare.json"),
            "/c0/bbu show all": load("bare.json"),
        }).patch(self)
        self.assertEqual(raidcore.gather_battery("/usr/bin/storcli64"), {})

    def test_gather_cachevault_keeps_working_as_alias(self):
        FakeStorcli({
            "/c0/cv show all": NOT_FOUND,
            "/c0/bbu show all": load("bbu_show_all.json"),
        }).patch(self)
        self.assertEqual(
            raidcore.gather_cachevault("/usr/bin/storcli64")["kind"], "bbu")


class OverviewTests(unittest.TestCase):
    def test_bbu_info_is_read_from_show_all(self):
        FakeStorcli({"/c0 show all": load("show_all_bbu.json")}).patch(self)
        overview = raidcore.gather_overview("/usr/bin/storcli64")
        cv = overview["cachevault"]
        self.assertEqual(cv["kind"], "bbu")
        self.assertEqual(cv["state"], "Optimal")
        self.assertEqual(cv["model"], "BBU")
        self.assertEqual(cv["temp"], "27 C")
        self.assertEqual(cv["mfg_date"], "2025/03/11")
        # The rest of the controller block must still parse on this card.
        self.assertEqual(overview["controller"]["product"], "PERC H840 Adapter")
        self.assertEqual(overview["controller"]["backup_units"], "Present")

    def test_cachevault_info_is_read_from_show_all(self):
        FakeStorcli({"/c0 show all": doc({
            "Basics": {"Model": "LSI MegaRAID 9361-8i"},
            "Cachevault_Info": [{"Type": "CacheVault", "State": "Optimal",
                                 "Temp": "31C", "MfgDate": "2024/06/02"}],
        })}).patch(self)
        cv = raidcore.gather_overview("/usr/bin/storcli64")["cachevault"]
        self.assertEqual(cv["kind"], "cachevault")
        self.assertEqual(cv["state"], "Optimal")
        self.assertEqual(cv["temp"], "31C")

    def test_controller_without_backup_reports_empty_kind(self):
        FakeStorcli({"/c0 show all": doc({
            "Basics": {"Model": "Some HBA"},
        })}).patch(self)
        cv = raidcore.gather_overview("/usr/bin/storcli64")["cachevault"]
        self.assertEqual(cv["kind"], "")
        self.assertEqual(cv["state"], "-")


class AdvisoryTests(unittest.TestCase):
    def test_battery_fault_is_worded_for_a_battery(self):
        out = raidwatchd.power_backup_advisories(
            {"kind": "bbu", "state": "Failed"})
        self.assertEqual(len(out), 1)
        self.assertIn("电池", out[0]["title"])
        self.assertNotIn("超级电容", out[0]["title"] + out[0]["body"])

    def test_supercap_fault_keeps_its_wording(self):
        out = raidwatchd.power_backup_advisories(
            {"kind": "cachevault", "state": "Failed"})
        self.assertIn("CacheVault", out[0]["title"])
        self.assertIn("超级电容", out[0]["body"])

    def test_replacement_required_is_an_error(self):
        out = raidwatchd.power_backup_advisories(
            {"kind": "bbu", "state": "Optimal", "replacement_required": "Yes"})
        self.assertEqual([a["level"] for a in out], ["bad"])

    def test_learn_cycle_is_reported(self):
        out = raidwatchd.power_backup_advisories(
            {"kind": "bbu", "state": "Learning"})
        self.assertEqual(out[0]["level"], "info")

    def test_optimal_is_silent(self):
        self.assertEqual(raidwatchd.power_backup_advisories(
            {"kind": "bbu", "state": "Optimal"}), [])
        self.assertEqual(raidwatchd.power_backup_advisories({}), [])


class LogRotationTests(unittest.TestCase):
    """storcli.log must be bounded: it is appended to on every controller call."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rw-log-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name, size):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x" * size)
        return path

    def test_rotates_once_the_limit_is_reached(self):
        path = self._write("storcli.log", 200)
        self.assertTrue(raidcore.rotate_log(path, 100))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.path.getsize(path + ".1"), 200)

    def test_small_file_is_left_alone(self):
        path = self._write("storcli.log", 50)
        self.assertFalse(raidcore.rotate_log(path, 100))
        self.assertTrue(os.path.exists(path))

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(raidcore.rotate_log(
            os.path.join(self.tmp, "nope.log"), 100))

    def test_previous_backup_is_replaced_not_accumulated(self):
        path = self._write("storcli.log", 200)
        with open(path + ".1", "w", encoding="utf-8") as fh:
            fh.write("old")
        self.assertTrue(raidcore.rotate_log(path, 100))
        self.assertEqual(os.path.getsize(path + ".1"), 200)
        # Only one backup may survive: two files, never three.
        self.assertEqual(
            sorted(os.listdir(self.tmp)), ["storcli.log.1"])

    def test_zero_limit_disables_rotation(self):
        path = self._write("storcli.log", 10)
        self.assertFalse(raidcore.rotate_log(path, 0))

    def test_configured_limit_is_clamped(self):
        original = raidcore.DEFAULT_LOG_LIMIT_MB
        self.addCleanup(raidcore.set_log_limit_mb, original)
        self.assertEqual(raidcore.set_log_limit_mb(0), 1)
        self.assertEqual(raidcore.set_log_limit_mb(-5), 1)
        self.assertEqual(raidcore.set_log_limit_mb(99999), 4096)
        self.assertEqual(raidcore.set_log_limit_mb("128"), 128)
        self.assertEqual(raidcore.set_log_limit_mb("abc"), original)

    def test_daemon_default_is_128mb(self):
        self.assertEqual(raidwatchd.DEFAULT_CONFIG["storcli_log_max_mb"], 128)
        low, high, cast, _label = raidwatchd.CONFIG_RANGES["storcli_log_max_mb"]
        self.assertEqual((low, high, cast), (1, 4096, int))

    def test_stats_report_size_and_limit(self):
        original_dir = raidcore._WORKDIR
        self.addCleanup(setattr, raidcore, "_WORKDIR", original_dir)
        self.addCleanup(raidcore.set_log_limit_mb, raidcore.DEFAULT_LOG_LIMIT_MB)
        raidcore._WORKDIR = self.tmp
        raidcore.set_log_limit_mb(1)
        path = self._write(raidcore.LOG_NAME, 2 * 1024 * 1024)
        self.assertTrue(raidcore.rotate_storcli_log())
        stats = raidcore.log_stats()
        self.assertEqual(stats["limit_mb"], 1)
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertEqual(stats["bytes"], 0)


class MergeTests(unittest.TestCase):
    def test_probe_outranks_the_one_line_summary(self):
        merged = raidwatchd.merge_power_backup(
            {"kind": "", "model": "-", "state": "-", "temp": "-"},
            {"kind": "bbu", "model": "BBU", "state": "Optimal", "temp": "25 C"})
        self.assertEqual(merged["kind"], "bbu")
        self.assertEqual(merged["state"], "Optimal")
        self.assertEqual(merged["temp"], "25 C")

    def test_placeholders_never_overwrite_a_reading(self):
        merged = raidwatchd.merge_power_backup(
            {"kind": "bbu", "state": "Optimal", "temp": "27 C"},
            {"kind": "bbu", "state": "-", "temp": "", "voltage": None})
        self.assertEqual(merged["state"], "Optimal")
        self.assertEqual(merged["temp"], "27 C")
        self.assertNotIn("voltage", merged)


class CapabilityTests(unittest.TestCase):
    """A card without a buzzer or without an SES channel must not be offered
    actions it can never satisfy (observed on a PERC H840)."""

    def test_absent_alarm_is_not_capable(self):
        caps = raidwatchd.controller_capabilities("ABSENT", [], ["/c0/s0"])
        self.assertFalse(caps["alarm"])
        self.assertFalse(caps["locate"])

    def test_present_alarm_and_enclosure_are_capable(self):
        caps = raidwatchd.controller_capabilities("Enabled", [{"EID": "252"}],
                                                  ["/c0/e252/s0"])
        self.assertTrue(caps["alarm"])
        self.assertTrue(caps["locate"])

    def test_enclosure_path_alone_marks_locate_capable(self):
        caps = raidwatchd.controller_capabilities("Disabled", [],
                                                  ["/c0/e252/s2"])
        self.assertTrue(caps["locate"])

    def test_unknown_alarm_token_is_treated_as_capable(self):
        # Anything that is not the explicit "ABSENT"/"NA"/"-" family is left
        # alone: guessing "unsupported" would disable buttons on cards that
        # simply answer differently.
        caps = raidwatchd.controller_capabilities("SomeOtherToken", [], [])
        self.assertTrue(caps["alarm"])

    def test_guard_explains_why(self):
        caps = {"alarm": False, "locate": False}
        for name in ("alarm_silence", "alarm_on", "alarm_off"):
            self.assertIn("蜂鸣器", raidwatchd.action_unavailable(name, caps))
        for name in ("locate_start", "locate_stop"):
            self.assertIn("定位灯", raidwatchd.action_unavailable(name, caps))

    def test_guard_is_silent_when_supported_or_unknown(self):
        self.assertEqual(raidwatchd.action_unavailable(
            "alarm_silence", {"alarm": True, "locate": True}), "")
        self.assertEqual(raidwatchd.action_unavailable("locate_start", None), "")

    def test_unrelated_actions_are_never_blocked(self):
        caps = {"alarm": False, "locate": False}
        for name in ("rebuild_stop", "cc_start", "set_rebuildrate", "foreign_import"):
            self.assertEqual(raidwatchd.action_unavailable(name, caps), "")


class ActionSubmitTests(unittest.TestCase):
    """Static regression test for the null-`pending` crash.

    The bug was order-dependent inside submitAction(): the server returns a job,
    closeModal() sets `pending = null`, and the next line read
    `pending.target`.  The controller command had already been accepted, so the
    operator saw "执行失败" for an action that actually ran.
    """

    def setUp(self):
        with open(os.path.join(UI, "app.js"), encoding="utf-8") as fh:
            self.src = fh.read()

    def _submit_body(self):
        """submitAction source with comment lines removed.

        Comments legitimately mention the offending expression; only real code
        can crash, so the count below ignores them.
        """
        start = self.src.index("async function submitAction")
        end = self.src.index("function shortTarget")
        lines = [line for line in self.src[start:end].splitlines()
                 if not line.strip().startswith(("//", "*", "/*"))]
        return "\n".join(lines)

    def test_target_is_captured_before_the_modal_closes(self):
        body = self._submit_body()
        self.assertEqual(body.count("pending.target"), 1,
                         "submitAction must read pending.target exactly once")
        self.assertLess(body.index("pending.target"), body.index("closeModal()"),
                        "pending.target read after closeModal() crashes")

    def test_job_summary_uses_the_captured_target(self):
        self.assertIn("job.summary = label + (target ?", self.src)
        self.assertIn("target: target,", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
