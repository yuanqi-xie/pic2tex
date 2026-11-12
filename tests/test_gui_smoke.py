# Pic2TeX GUI smoke test: builds the Tk window without showing it.

from __future__ import annotations

import os
import unittest


class GuiSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-only GUI")
    def test_window_builds_and_content_fits(self) -> None:
        try:
            import tkinter as tk
        except Exception:  # pragma: no cover - tkinter missing
            self.skipTest("tkinter is not available")

        from pic2tex.config import Config
        from pic2tex.gui import Pic2TeXApp
        from pic2tex.winutil import enable_high_dpi

        enable_high_dpi()
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless CI
            self.skipTest(f"no display: {exc}")
        root.withdraw()
        try:
            config = Config(api_key="sk-test", auto_monitor=False)
            app = Pic2TeXApp(root, config)
            root.update_idletasks()
            self.assertEqual(root.title(), "Pic2TeX · 图片转 LaTeX")
            geometry = root.wm_geometry()
            width = int(geometry.split("+")[0].split("x")[0])
            height = int(geometry.split("+")[0].split("x")[1])
            self.assertGreaterEqual(width, root.winfo_reqwidth())
            self.assertGreaterEqual(height, root.winfo_reqheight())
            # The main action buttons must be inside the window.
            for widget in (app.monitor_btn, app.settings_btn, app.copy_btn):
                right = widget.winfo_rootx() + widget.winfo_width() - root.winfo_rootx()
                self.assertLessEqual(right, width + 2, msg=f"{widget} overflows the window")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
