import unittest
from zoneinfo import ZoneInfo

from nova_history import METRICS, full_match, payload, plan, validate


class NovaRoundTrip(unittest.TestCase):
    def setUp(self):
        self.source = dict.fromkeys(METRICS, 1.0)
        self.source.update(weightControl=2.5, reportId="source", deviceType="00053",
                           mac="AA:BB:CC:DD:EE:FF", timeStamp=1735689600, id=2, userId=42,
                           localCreatedAt="2025-01-01 08:00:00")
        self.device = dict(self.source, reportId="destination", timeStamp=1, z20Body=3, displayModuleType=1, isAuto=2)
        self.existing = [(self.device, True)]

    def test_upload(self):
        self.assertEqual(len(METRICS), 89)
        result = payload(self.source, self.device, 77, ZoneInfo("America/Los_Angeles"))
        self.assertEqual(result["WC"], 2.5)
        self.assertNotIn("weightControl", result)
        self.assertNotIn("reportId", result)
        self.assertEqual((result["userId"], result["bUserId"], result["id"], result["dataSource"]), (77, 77, 0, 1))
        self.assertEqual(result["localCreatedAt"], "2024-12-31 16:00:00")
        self.assertEqual(result["timeStamp"], self.source["timeStamp"])

    def test_duplicate_reports(self):
        copy = dict(self.source, id=3, reportId="second-source-id")
        missing, conflicts = plan([self.source, copy], self.existing)
        self.assertEqual(len(missing), 1)
        self.assertFalse(conflicts)
        matched = dict(self.source, reportId="new-report")
        self.assertEqual(plan([self.source], [(matched, True)]), ([], []))
        _, conflicts = plan([self.source, dict(copy, z20Body=123)], self.existing)
        self.assertEqual(conflicts, [self.source["timeStamp"]])

    def test_full_report_required(self):
        for actual in (dict(self.source, z20Body=2), dict(self.source, reportId=None)):
            _, conflicts = plan([self.source], self.existing + [(actual, True)])
            self.assertEqual(conflicts, [self.source["timeStamp"]])
        self.assertFalse(full_match(self.source, dict(self.source, weightControl=0)))
        with self.assertRaises(ValueError):
            validate(dict(self.source, z20Body=None))
        with self.assertRaises(ValueError):
            plan([self.source], [(dict(self.device, mac="11:11:11:11:11:11"), True)])
        _, conflicts = plan([self.source], self.existing + [(self.source, False)])
        self.assertEqual(conflicts, [self.source["timeStamp"]])

    def test_date_shift_conflict(self):
        shifted = dict(self.source, timeStamp=self.source["timeStamp"] + 3600)
        pending, conflicts = plan([self.source], [(shifted, True)])
        self.assertFalse(pending)
        self.assertEqual(conflicts, [self.source["timeStamp"]])
        changed_local = dict(self.source, localCreatedAt="2025-01-02 08:00:00")
        pending, conflicts = plan([self.source], [(changed_local, True)])
        self.assertFalse(pending)
        self.assertEqual(conflicts, [self.source["timeStamp"]])
