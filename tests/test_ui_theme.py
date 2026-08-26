# -*- coding: utf-8 -*-
"""Widget-library tests for tools/ui_theme.py (skipped when no display is available)."""
import os
import sys
import unittest
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))


def _has_display():
    try:
        r = tk.Tk()
        r.withdraw()
        r.destroy()
        return True
    except tk.TclError:
        return False


HAS_DISPLAY = _has_display()


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ui_theme
        cls.ui = ui_theme
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.theme = ui_theme.apply_theme(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def test_apply_theme(self):
        self.assertEqual(self.theme.style().theme_use(), "clam")
        self.assertTrue(self.theme.ok)
        for name in ("ui", "ui_bold", "title", "section", "caption", "mono"):
            self.assertIn(name, self.theme.fonts)
        again = self.ui.apply_theme(self.root)          # idempotent
        self.assertEqual(again.style().theme_use(), "clam")

    def test_px_scaling(self):
        px = self.theme.px
        self.assertEqual(px(0), 0)
        self.assertGreaterEqual(px(10), 10)
        self.assertLessEqual(px(10), px(20))

    def test_styles_defined(self):
        s = self.theme.style()
        for name in ("Page.TFrame", "Card.TFrame", "CardBody.TFrame", "Sidebar.TFrame", "Primary.TButton", "Destructive.TButton",
                     "Ghost.TButton", "Card.TLabel", "Card.Caption.TLabel", "Value.TLabel", "FormLabel.TLabel", "Treeview", "Heading",
                     "TEntry", "TCombobox", "TSpinbox", "Card.TCheckbutton"):
            self.assertTrue(s.lookup(name, "background") or s.lookup(name, "foreground") or s.lookup(name, "fieldbackground"), name)
        # accent buttons must not look like default grey Tk buttons
        self.assertEqual(s.lookup("Primary.TButton", "background").upper(), self.ui.COLORS["accent"].upper())
        self.assertNotEqual(s.lookup("TButton", "background").lower(), "#d9d9d9")
        self.assertEqual(int(s.lookup("Treeview", "rowheight")), self.theme.px(24))

    def test_card(self):
        host = ttk.Frame(self.root)
        c = self.ui.Card(host, title="标题", subtitle="副标题")
        c.pack()
        self.root.update_idletasks()
        self.assertEqual(c.title_var.get(), "标题")
        c.set_title("新标题")
        c.set_subtitle("新副标题")
        self.assertEqual(c.title_label.cget("text") or c.title_var.get(), "新标题")
        self.assertEqual(c.header.winfo_manager(), "grid")
        self.assertEqual(c.body.winfo_manager(), "grid")
        bare = self.ui.Card(host)
        bare.pack()
        self.root.update_idletasks()
        self.assertEqual(bare.header.winfo_manager(), "")
        acts = self.ui.Card(host, title="x", actions=[("动作", lambda: None)])
        self.assertIn("动作", acts.action_buttons)

    def test_status_pill(self):
        p = self.ui.StatusPill(self.root, "空闲", "neutral")
        p.set("运行中", "accent")
        self.assertEqual(p.text, "运行中")
        self.assertEqual(p.tone, "accent")
        p.set(tone="danger")
        self.assertEqual(p.text, "运行中")
        self.assertEqual(p.tone, "danger")

    def test_disclosure(self):
        seen = []
        host = ttk.Frame(self.root)
        d = self.ui.Disclosure(host, title="高级", on_toggle=seen.append)
        d.pack()
        self.assertFalse(d.is_open)
        d.open()
        self.assertTrue(d.is_open)
        d.close()
        self.assertFalse(d.is_open)
        d.toggle()
        self.assertTrue(d.is_open)
        self.assertEqual(seen, [True, False, True])

    def test_banner(self):
        host = ttk.Frame(self.root)
        b = self.ui.Banner(host, "提示", tone="warning")
        b.grid(row=0, column=0)
        b.show("提示")                                  # hidden until show()
        self.root.update_idletasks()
        self.assertTrue(b.visible)
        self.assertEqual(b.winfo_manager(), "grid")
        b.hide()
        self.assertFalse(b.visible)
        self.assertEqual(b.winfo_manager(), "")
        b.show("危险", "danger")
        self.assertTrue(b.visible)
        self.assertEqual(b.tone, "danger")
        self.assertEqual(b.label.cget("text"), "危险")

    def test_readout(self):
        r = self.ui.Readout(self.root, [("a", "甲"), ("b", "乙")], columns=2)
        r.set("a", "1.0 ms", "accent")
        r.set("b", None)
        self.assertEqual(r.values["a"].cget("text"), "1.0 ms")
        self.assertEqual(r.values["b"].cget("text"), "—")

    def test_statusbar(self):
        sb = self.ui.StatusBar(self.root, [("k1", "一"), ("k2", "二")], action=("关闭快门", lambda: None))
        sb.set("k1", "三", "success")
        self.assertEqual(sb.get("k1"), "三")
        self.assertEqual(sb.tone("k1"), "success")
        self.assertIsNone(sb.get("missing"))
        sb.set_action(state="disabled")
        self.assertEqual(str(sb.action_button["state"]), "disabled")

    def test_form_row(self):
        f = ttk.Frame(self.root)
        e = ttk.Entry(f)
        b = ttk.Button(f, text="x")
        placed = self.ui.form_row(f, 0, "标签", e, b, unit="ms")
        self.assertEqual(placed, [e, b])
        lab = f.grid_slaves(row=0, column=0)[0]
        self.assertEqual(lab.cget("text"), "标签")
        self.assertEqual(e.grid_info()["column"], 1)
        self.assertEqual(b.grid_info()["column"], 2)
        unit = f.grid_slaves(row=0, column=3)[0]
        self.assertEqual(unit.cget("text"), "ms")

    def test_empty_state_and_page_header(self):
        es = self.ui.empty_state(self.root, "空", "提示")
        self.assertEqual(len([c for c in es.winfo_children() if isinstance(c, ttk.Label)]), 2)
        calls = []
        h = self.ui.PageHeader(self.root, "页", "副标题", actions=[("主要", lambda: calls.append(1), "Primary.TButton"), ("次要", lambda: calls.append(2))])
        self.assertIn("主要", h.buttons)
        h.buttons["主要"].invoke()
        self.assertEqual(calls, [1])
        h.set_subtitle("新副标题")

    def test_tooltip_and_bind_enter(self):
        b = ttk.Button(self.root, text="t")
        self.assertIs(self.ui.tooltip(b, "提示"), b)
        e = ttk.Entry(self.root)
        self.assertIs(self.ui.bind_enter(e, lambda: None), e)
        self.assertIn("<Key-Return>", e.bind())
        self.assertIn("<Key-KP_Enter>", e.bind())

    def test_mpl_theme(self):
        try:
            import matplotlib
        except ImportError:
            self.skipTest("matplotlib missing")
        self.assertTrue(self.ui.apply_mpl_theme())
        rc = matplotlib.rcParams
        self.assertEqual(rc["axes.facecolor"].upper(), self.ui.COLORS["card"].upper())
        self.assertFalse(rc["axes.spines.top"])
        fams = list(rc["font.sans-serif"]) + list(rc["font.family"])
        self.assertTrue(any(k in " ".join(fams) for k in ("YaHei", "Hiragino", "PingFang", "Noto", "Arial Unicode")), fams)
        from matplotlib.figure import Figure
        fig = Figure()
        ax = fig.add_subplot(111)
        self.ui.mpl_style_axes(ax)
        t = self.ui.mpl_empty(ax, "尚无数据")
        self.assertEqual(t.get_text(), "尚无数据")


if __name__ == "__main__":
    unittest.main()
