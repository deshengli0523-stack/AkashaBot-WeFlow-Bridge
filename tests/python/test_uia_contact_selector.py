import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from uia_contact_selector import OcrContactSelector
from uia_support import (
    CONTACT_SELECTION_FAILED,
    CalibrationError,
    ClientMetrics,
    ScreenRect,
)


METRICS = ClientMetrics(
    hwnd=10,
    left=0,
    top=0,
    width=2560,
    height=1368,
    dpi=144,
    visible=True,
    maximized=True,
    foreground=True,
)
POPUP = ScreenRect(70, 95, 625, 740)
SEARCH_POINT = {"x": 0.10, "y": 0.06}


class FakeDriver:
    def __init__(self):
        self.calls = []

    def find_search_popup(self, main_hwnd, search_point):
        self.calls.append(("find_popup", main_hwnd, search_point))
        return 20, POPUP

    def click_search_popup_point(self, main_hwnd, popup_hwnd, point):
        self.calls.append(("click_popup", main_hwnd, popup_hwnd, point))

    def get_client_metrics(self, main_hwnd):
        self.calls.append(("metrics", main_hwnd))
        return METRICS


class SequencedSelector(OcrContactSelector):
    def __init__(self, driver, search_points, title_matches=True):
        super().__init__(
            driver,
            sleep_fn=lambda _seconds: None,
            selection_timeout=2,
        )
        self.search_points = list(search_points)
        self.title_matches = title_matches
        self.ocr_calls = []

    def _invoke_ocr(
        self,
        rect,
        *,
        mode,
        expected_text,
        section_text="",
    ):
        self.ocr_calls.append((mode, expected_text, section_text, rect))
        if mode == "title":
            return {"status": "ok", "matched": self.title_matches}
        point = self.search_points.pop(0) if self.search_points else (200, 300)
        return {
            "status": "ok",
            "matched": True,
            "candidate": {"x": point[0], "y": point[1]},
        }


class OcrContactSelectorTests(unittest.TestCase):
    def test_ocr_identity_normalization_preserves_case_and_symbols(self):
        source = (
            BRIDGE / "windows_ocr_selector.ps1"
        ).read_text(encoding="utf-8")

        self.assertNotIn(r"\p{S}", source)
        self.assertNotIn(r"\p{C}", source)
        self.assertNotIn("ToLowerInvariant", source)
        self.assertIn(r"\p{Cc}", source)
        self.assertIn("$Line.Normalized -ceq $Expected", source)
        self.assertIn("$Line.Compact -ceq", source)
        self.assertIn("$Expected.Contains(' ')", source)
        self.assertIn("Test-HanOnly -Text $Expected", source)
        self.assertIn(
            "Test-HanOnly -Text ([string]$Line.Compact)",
            source,
        )
        self.assertIn("$wordText.Length -ne 1", source)
        self.assertIn("if ($gap -gt 1.0)", source)
        self.assertTrue(
            source.isascii(),
            "Windows PowerShell -File helper must remain ASCII-only",
        )

    def test_powershell_normalization_preserves_emoji_and_rejects_narrow_gap(self):
        source = (
            BRIDGE / "windows_ocr_selector.ps1"
        ).read_text(encoding="utf-8")
        marker = "\ntry {\n  Add-Type -AssemblyName System.Runtime.WindowsRuntime"
        self.assertIn(marker, source)
        functions = source.split(marker, 1)[0]
        probe = functions + r"""
$emoji = 'Alice' + [char]::ConvertFromUtf32(0x1F642)
if ((Normalize-OcrText -Text $emoji) -cne $emoji) { exit 2 }
$first = [pscustomobject]@{
  Text = (ConvertFrom-CodePoints @(0x5F20))
  BoundingRect = [pscustomobject]@{ X = 0; Y = 0; Width = 20; Height = 20 }
}
$second = [pscustomobject]@{
  Text = (ConvertFrom-CodePoints @(0x4E09))
  BoundingRect = [pscustomobject]@{ X = 22; Y = 0; Width = 20; Height = 20 }
}
$line = [pscustomobject]@{
  Text = (ConvertFrom-CodePoints @(0x5F20)) + [char]0x2009 + (ConvertFrom-CodePoints @(0x4E09))
  Words = @($first, $second)
}
$record = @(Get-OcrLineRecords -Result ([pscustomobject]@{ Lines = @($line) }))[0]
$expected = ConvertFrom-CodePoints @(0x5F20, 0x4E09)
if (Test-OcrLineMatch -Line $record -Expected $expected) { exit 3 }
exit 0
"""
        powershell = pathlib.Path(
            os.environ["SystemRoot"],
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        completed = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "-",
            ],
            input=probe,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout,
        )

    def test_two_stable_ocr_reads_are_required_before_click_and_title_check(self):
        driver = FakeDriver()
        selector = SequencedSelector(driver, [(200, 300), (202, 299)])

        selector.select_contact(10, SEARCH_POINT, "private-contact")

        self.assertIn(("click_popup", 10, 20, (272, 394)), driver.calls)
        self.assertEqual(
            [call[0] for call in selector.ocr_calls],
            ["search", "search", "title"],
        )
        self.assertEqual(
            [call[2] for call in selector.ocr_calls[:2]],
            ["联系人", "联系人"],
        )
        self.assertEqual(
            selector.ocr_calls[-1][3],
            ScreenRect(461, 0, 1408, 123),
        )

    def test_file_transfer_helper_is_selected_from_features_section(self):
        driver = FakeDriver()
        selector = SequencedSelector(driver, [(200, 200), (201, 201)])

        selector.select_contact(10, SEARCH_POINT, "文件传输助手")

        self.assertIn(("click_popup", 10, 20, (271, 296)), driver.calls)
        self.assertEqual(
            [call[0] for call in selector.ocr_calls],
            ["search", "search", "title"],
        )
        self.assertEqual(
            [call[2] for call in selector.ocr_calls[:2]],
            ["功能", "功能"],
        )

    def test_moving_async_results_are_not_clicked_until_stable(self):
        driver = FakeDriver()
        selector = SequencedSelector(
            driver,
            [(200, 100), (200, 300), (201, 301)],
        )

        selector.select_contact(10, SEARCH_POINT, "private-contact")

        self.assertIn(("click_popup", 10, 20, (271, 396)), driver.calls)
        self.assertEqual(
            [call[0] for call in selector.ocr_calls],
            ["search", "search", "search", "title"],
        )

    def test_title_mismatch_fails_closed_after_selection(self):
        driver = FakeDriver()
        selector = SequencedSelector(
            driver,
            [(200, 300), (200, 300)],
            title_matches=False,
        )

        with self.assertRaises(CalibrationError) as raised:
            selector.select_contact(10, SEARCH_POINT, "private-contact")

        self.assertEqual(raised.exception.code, CONTACT_SELECTION_FAILED)
        self.assertEqual(
            len([call for call in driver.calls if call[0] == "click_popup"]),
            1,
        )

    def test_contact_text_is_not_put_on_the_powershell_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            script = root / "selector.ps1"
            powershell = root / "powershell.exe"
            script.write_text("", encoding="utf-8")
            powershell.write_text("", encoding="utf-8")
            observed = {}

            def capture(_rect):
                return b"private pixels"

            def run(command, **kwargs):
                observed["command"] = command
                observed["input"] = kwargs["input"]
                return types.SimpleNamespace(
                    returncode=0,
                    stdout='{"status":"ok","matched":true}',
                    stderr="",
                )

            selector = OcrContactSelector(
                FakeDriver(),
                script_path=str(script),
                subprocess_run=run,
                capture_fn=capture,
            )
            selector.powershell_path = powershell
            response = selector._invoke_ocr(
                ScreenRect(0, 0, 100, 100),
                mode="title",
                expected_text="sensitive-contact",
            )

            self.assertIs(response["matched"], True)
            self.assertNotIn(
                "sensitive-contact",
                " ".join(str(part) for part in observed["command"]),
            )
            self.assertIn("sensitive-contact", observed["input"])
            request = json.loads(observed["input"])
            self.assertNotIn("image_path", request)
            self.assertEqual(
                base64.b64decode(request["image_base64"]),
                b"private pixels",
            )


if __name__ == "__main__":
    unittest.main()
