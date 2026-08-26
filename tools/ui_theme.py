# -*- coding: utf-8 -*-
"""Apple-style theme and reusable widgets for the OptiComp2 Tk GUI (pure ttk, no new dependencies).

apply_theme(root) switches ttk to the 'clam' engine (the only built-in theme that lets us colour
buttons on both Windows and macOS), resolves the platform fonts, registers every named style used
by the pages and sets the option database for the classic widgets (Listbox/Text). The components
below only depend on ttk and the tokens in this module; they never import a panel and never open a
message box themselves (confirm_abort is the one explicit helper for the Esc shortcut).

Python 3.9 / Tk 8.6 compatible.
"""
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

COLORS = {
    "bg": "#F5F5F7", "card": "#FFFFFF", "border": "#E5E5EA", "sep": "#D2D2D7",
    "text": "#1D1D1F", "text2": "#6E6E73", "text3": "#AEAEB2",
    "accent": "#0A84FF", "accent_hover": "#3395FF", "accent_pressed": "#0066CC", "accent_disabled": "#B9D9FF", "accent_tint": "#E5F1FF",
    "danger": "#FF3B30", "danger_hover": "#FF5A50", "danger_pressed": "#D9291F", "danger_disabled": "#FFB4AF", "danger_tint": "#FFE9E7",
    "success": "#34C759", "success_tint": "#E4F8EA", "success_text": "#1B7F3B",
    "warning": "#FF9F0A", "warning_tint": "#FFF2DD", "warning_text": "#8A5300",
    "sidebar": "#E9E9EE", "sidebar_hover": "#DEDEE3", "sidebar_sel": "#0A84FF", "sidebar_sel_text": "#FFFFFF",
    "field": "#FFFFFF", "field_border": "#D2D2D7", "field_disabled": "#F5F5F7",
    "row_alt": "#F7F7F9", "row_sel": "#DCE9F9",
    "thumb": "#C7C7CC", "thumb_hover": "#A1A1A6",
    "plot_active": "#EEF4FB", "plot_limit": "#FF3B30",
    "tooltip": "#3A3A3C",
}

SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

FONT_CANDIDATES = {
    "win32": {"ui": ["Segoe UI", "Microsoft YaHei UI", "Arial"], "mono": ["Cascadia Mono", "Consolas", "Courier New"]},
    "darwin": {"ui": [".AppleSystemUIFont", "Helvetica Neue", "Helvetica"], "mono": ["SF Mono", "Menlo", "Monaco"]},
    "linux": {"ui": ["Noto Sans CJK SC", "DejaVu Sans"], "mono": ["DejaVu Sans Mono"]},
}
FONT_SIZES = {
    "win32": {"ui": 11, "title": 20, "section": 13, "caption": 10, "value": 17, "mono": 10},
    "darwin": {"ui": 13, "title": 22, "section": 15, "caption": 11, "value": 20, "mono": 12},
    "linux": {"ui": 11, "title": 20, "section": 13, "caption": 10, "value": 17, "mono": 10},
}
TONES = ("neutral", "accent", "success", "warning", "danger")

# tone -> (tint background, text colour) for pills / banners / readouts
_TONE_COLORS = {
    "neutral": ("bg", "text2"),
    "accent": ("accent_tint", "accent_pressed"),
    "success": ("success_tint", "success_text"),
    "warning": ("warning_tint", "warning_text"),
    "danger": ("danger_tint", "danger_pressed"),
}

# Tk named fonts that every ttk widget inherits by default
_NAMED_UI_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                   "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont")


def _platform():
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


class Theme(object):
    """Result of apply_theme(): resolved fonts/colours/spacing plus px() for DPI scaling."""

    def __init__(self, root, platform, fonts, style, ok=True):
        self._root = root
        self.platform = platform
        self.colors = COLORS
        self.space = SPACE
        self.fonts = fonts
        self._style = style
        self.ok = ok
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        # Tk reports 72 dpi on macOS (points == pixels) and 96 dpi on Windows/X11 at 100 %
        self._scale = dpi / (72.0 if platform == "darwin" else 96.0)

    def font(self, name):
        return self.fonts.get(name) or self.fonts.get("ui")

    def px(self, n):
        return int(round(n * self._scale))

    def style(self):
        return self._style


def _pick_family(candidates, families, platform):
    for cand in candidates:
        if cand in families:
            return cand
        if platform == "darwin" and cand.startswith("."):
            return cand                         # the system UI font is not listed but usable
    return candidates[-1]


def _resolve_fonts(root, platform):
    families = set(tkfont.families(root))
    cands = FONT_CANDIDATES.get(platform, FONT_CANDIDATES["linux"])
    sizes = FONT_SIZES.get(platform, FONT_SIZES["linux"])
    ui = _pick_family(cands["ui"], families, platform)
    mono = _pick_family(cands["mono"], families, platform)
    fonts = {
        "ui": tkfont.Font(root=root, family=ui, size=sizes["ui"], weight="normal"),
        "ui_bold": tkfont.Font(root=root, family=ui, size=sizes["ui"], weight="bold"),
        "title": tkfont.Font(root=root, family=ui, size=sizes["title"], weight="bold"),
        "section": tkfont.Font(root=root, family=ui, size=sizes["section"], weight="bold"),
        "caption": tkfont.Font(root=root, family=ui, size=sizes["caption"], weight="normal"),
        "caption_bold": tkfont.Font(root=root, family=ui, size=sizes["caption"], weight="bold"),
        "value": tkfont.Font(root=root, family=ui, size=sizes["value"], weight="bold"),
        "mono": tkfont.Font(root=root, family=mono, size=sizes["mono"], weight="normal"),
    }
    fonts["family"] = ui
    fonts["mono_family"] = mono
    return fonts


def _configure_named_fonts(root, fonts):
    ui, mono = fonts["ui"], fonts["mono"]
    for name in _NAMED_UI_FONTS:
        try:
            f = tkfont.Font(root=root, name=name, exists=True)
            f.configure(family=ui.cget("family"), size=ui.cget("size"), weight="normal")
        except Exception:
            pass
    try:
        f = tkfont.Font(root=root, name="TkFixedFont", exists=True)
        f.configure(family=mono.cget("family"), size=mono.cget("size"))
    except Exception:
        pass


def _fixed_map(style, option):
    # Tk 8.6.9+ quirk: without this, Treeview row tags are ignored for background/foreground
    return [e for e in style.map("Treeview", query_opt=option) if e[:2] != ("!disabled", "!selected")]


def _register_styles(style, theme):
    C = COLORS
    px = theme.px
    f = theme.font
    style.configure(".", background=C["bg"], foreground=C["text"], font=f("ui"), borderwidth=0,
                    focuscolor=C["bg"], bordercolor=C["border"], lightcolor=C["bg"], darkcolor=C["bg"],
                    troughcolor=C["bg"], selectbackground=C["row_sel"], selectforeground=C["text"], insertcolor=C["text"])
    # ---- frames
    style.configure("TFrame", background=C["bg"])
    style.configure("Page.TFrame", background=C["bg"])
    style.configure("Card.TFrame", background=C["card"], bordercolor=C["border"], lightcolor=C["border"],
                    darkcolor=C["border"], relief="solid", borderwidth=1)
    style.configure("CardBody.TFrame", background=C["card"])
    style.configure("Sidebar.TFrame", background=C["sidebar"])
    style.configure("Sep.TFrame", background=C["border"])
    style.configure("StatusBar.TFrame", background=C["card"])
    style.configure("Tooltip.TFrame", background=C["tooltip"])
    for tone, (tint, fg) in _TONE_COLORS.items():
        name = tone.capitalize()
        style.configure("Banner.%s.TFrame" % name, background=C[tint], bordercolor=C[{"neutral": "sep", "accent": "accent", "success": "success", "warning": "warning", "danger": "danger"}[tone]],
                        lightcolor=C[tint], darkcolor=C[tint], relief="solid", borderwidth=1)
        style.configure("Banner.%s.TLabel" % name, background=C[tint], foreground=C[fg], font=f("ui"))
        style.configure("Banner.%s.TButton" % name, background=C[tint], foreground=C[fg], bordercolor=C[tint],
                        lightcolor=C[tint], darkcolor=C[tint], focuscolor=C[tint], relief="flat", padding=(px(6), px(2)), font=f("ui_bold"))
        style.map("Banner.%s.TButton" % name, background=[("active", C["card"]), ("pressed", C["card"])])
        style.configure("Pill.%s.TLabel" % name, background=C[tint], foreground=C[fg], font=f("caption"), padding=(px(8), px(2)))
        style.configure("PillCard.%s.TLabel" % name, background=C[tint] if tone != "neutral" else C["bg"], foreground=C[fg],
                        font=f("caption"), padding=(px(8), px(2)))
    # ---- labels
    style.configure("TLabel", background=C["bg"], foreground=C["text"], font=f("ui"))
    style.configure("Card.TLabel", background=C["card"], foreground=C["text"], font=f("ui"))
    style.configure("Title.TLabel", background=C["bg"], foreground=C["text"], font=f("title"))
    style.configure("Subtitle.TLabel", background=C["bg"], foreground=C["text2"], font=f("ui"))
    style.configure("Section.TLabel", background=C["card"], foreground=C["text"], font=f("section"))
    style.configure("Caption.TLabel", background=C["bg"], foreground=C["text2"], font=f("caption"))
    style.configure("Card.Caption.TLabel", background=C["card"], foreground=C["text2"], font=f("caption"))
    style.configure("Card.CaptionBold.TLabel", background=C["card"], foreground=C["text"], font=f("caption_bold"))
    style.configure("Value.TLabel", background=C["card"], foreground=C["text"], font=f("value"))
    style.configure("Mono.TLabel", background=C["card"], foreground=C["text"], font=f("mono"))
    style.configure("FormLabel.TLabel", background=C["card"], foreground=C["text2"], font=f("ui"), anchor="e")
    style.configure("Empty.TLabel", background=C["card"], foreground=C["text3"], font=f("ui"), anchor="center", justify="center")
    style.configure("EmptyHint.TLabel", background=C["card"], foreground=C["text3"], font=f("caption"), anchor="center", justify="center")
    style.configure("Status.TLabel", background=C["card"], foreground=C["text"], font=f("caption"))
    style.configure("SidebarBrand.TLabel", background=C["sidebar"], foreground=C["text"], font=f("section"))
    style.configure("SidebarCaption.TLabel", background=C["sidebar"], foreground=C["text2"], font=f("caption"))
    style.configure("Badge.TLabel", background=C["danger"], foreground="#FFFFFF", font=f("caption"), padding=(px(6), 0))
    style.configure("Tooltip.TLabel", background=C["tooltip"], foreground="#FFFFFF", font=f("caption"), padding=(px(8), px(5)))
    style.configure("Readout.TLabel", background=C["card"], foreground=C["text"], font=f("value"))
    # ---- buttons
    style.configure("TButton", width=-6, background=C["card"], foreground=C["text"], bordercolor=C["sep"], lightcolor=C["card"],
                    darkcolor=C["card"], focuscolor=C["card"], focusthickness=0, relief="flat", padding=(px(12), px(5)), font=f("ui"))
    style.map("TButton", background=[("disabled", C["bg"]), ("pressed", C["border"]), ("active", C["bg"])],
              foreground=[("disabled", C["text3"])], bordercolor=[("focus", C["accent"])],
              lightcolor=[("disabled", C["bg"])], darkcolor=[("disabled", C["bg"])])
    for name, key in (("Primary", "accent"), ("Destructive", "danger")):
        base, hover, pressed, disabled = C[key], C[key + "_hover"], C[key + "_pressed"], C[key + "_disabled"]
        style.configure("%s.TButton" % name, background=base, foreground="#FFFFFF", bordercolor=base, lightcolor=base,
                        darkcolor=base, focuscolor=base, focusthickness=0, relief="flat", padding=(px(14), px(6)), font=f("ui_bold"))
        style.map("%s.TButton" % name,
                  background=[("disabled", disabled), ("pressed", pressed), ("active", hover)],
                  bordercolor=[("disabled", disabled), ("pressed", pressed), ("active", hover)],
                  lightcolor=[("disabled", disabled), ("pressed", pressed), ("active", hover)],
                  darkcolor=[("disabled", disabled), ("pressed", pressed), ("active", hover)],
                  foreground=[("disabled", "#FFFFFF")])
    style.configure("Ghost.TButton", background=C["card"], foreground=C["accent"], bordercolor=C["card"], lightcolor=C["card"],
                    darkcolor=C["card"], focuscolor=C["card"], focusthickness=0, relief="flat", padding=(px(6), px(4)), font=f("ui"))
    style.map("Ghost.TButton", foreground=[("disabled", C["text3"]), ("active", C["accent_pressed"])],
              background=[("active", C["accent_tint"]), ("pressed", C["accent_tint"])],
              bordercolor=[("active", C["accent_tint"])], lightcolor=[("active", C["accent_tint"])], darkcolor=[("active", C["accent_tint"])])
    style.configure("Icon.TButton", background=C["card"], foreground=C["text"], bordercolor=C["sep"], lightcolor=C["card"],
                    darkcolor=C["card"], focuscolor=C["card"], focusthickness=0, relief="flat", padding=(px(6), px(4)), width=3, anchor="center", font=f("ui_bold"))
    style.map("Icon.TButton", background=[("disabled", C["bg"]), ("pressed", C["border"]), ("active", C["bg"])],
              foreground=[("disabled", C["text3"])], bordercolor=[("focus", C["accent"])])
    style.configure("Toggle.TButton", background=C["card"], foreground=C["text"], bordercolor=C["sep"], lightcolor=C["card"],
                    darkcolor=C["card"], focuscolor=C["card"], focusthickness=0, relief="flat", padding=(px(12), px(5)), font=f("ui"))
    style.map("Toggle.TButton", background=[("disabled", C["bg"]), ("pressed", C["border"]), ("active", C["bg"])],
              foreground=[("disabled", C["text3"])], bordercolor=[("focus", C["accent"])])
    style.configure("ToggleOn.TButton", background=C["accent_tint"], foreground=C["accent_pressed"], bordercolor=C["accent"],
                    lightcolor=C["accent_tint"], darkcolor=C["accent_tint"], focuscolor=C["accent_tint"], focusthickness=0,
                    relief="flat", padding=(px(12), px(5)), font=f("ui_bold"))
    style.map("ToggleOn.TButton", background=[("active", "#D5E8FF"), ("pressed", "#D5E8FF")],
              lightcolor=[("active", "#D5E8FF")], darkcolor=[("active", "#D5E8FF")])
    style.configure("SidebarItem.TButton", background=C["sidebar"], foreground=C["text"], bordercolor=C["sidebar"],
                    lightcolor=C["sidebar"], darkcolor=C["sidebar"], focuscolor=C["sidebar"], focusthickness=0, relief="flat",
                    anchor="w", padding=(px(12), px(7)), font=f("ui"))
    style.map("SidebarItem.TButton", background=[("pressed", C["sidebar_hover"]), ("active", C["sidebar_hover"])],
              bordercolor=[("pressed", C["sidebar_hover"]), ("active", C["sidebar_hover"])],
              lightcolor=[("pressed", C["sidebar_hover"]), ("active", C["sidebar_hover"])],
              darkcolor=[("pressed", C["sidebar_hover"]), ("active", C["sidebar_hover"])])
    style.configure("SidebarItemSelected.TButton", background=C["sidebar_sel"], foreground=C["sidebar_sel_text"],
                    bordercolor=C["sidebar_sel"], lightcolor=C["sidebar_sel"], darkcolor=C["sidebar_sel"], focuscolor=C["sidebar_sel"],
                    focusthickness=0, relief="flat", anchor="w", padding=(px(12), px(7)), font=f("ui_bold"))
    style.map("SidebarItemSelected.TButton", background=[("pressed", C["accent_hover"]), ("active", C["accent_hover"])],
              bordercolor=[("active", C["accent_hover"])], lightcolor=[("active", C["accent_hover"])], darkcolor=[("active", C["accent_hover"])],
              foreground=[("active", C["sidebar_sel_text"])])
    style.configure("SidebarItemActive.TButton", background=C["sidebar_hover"], foreground=C["accent_pressed"],
                    bordercolor=C["sidebar_hover"], lightcolor=C["sidebar_hover"], darkcolor=C["sidebar_hover"], focuscolor=C["sidebar_hover"],
                    focusthickness=0, relief="flat", anchor="w", padding=(px(12), px(7)), font=f("ui_bold"))
    style.map("SidebarItemActive.TButton", background=[("active", C["border"])], bordercolor=[("active", C["border"])],
              lightcolor=[("active", C["border"])], darkcolor=[("active", C["border"])])
    style.configure("Disclosure.TButton", background=C["card"], foreground=C["text2"], bordercolor=C["card"], lightcolor=C["card"],
                    darkcolor=C["card"], focuscolor=C["card"], focusthickness=0, relief="flat", anchor="w", padding=(0, px(4)), font=f("caption"))
    style.map("Disclosure.TButton", foreground=[("active", C["text"])], background=[("active", C["card"]), ("pressed", C["card"])],
              bordercolor=[("active", C["card"])], lightcolor=[("active", C["card"])], darkcolor=[("active", C["card"])])
    style.configure("DisclosurePage.TButton", background=C["bg"], foreground=C["text2"], bordercolor=C["bg"], lightcolor=C["bg"],
                    darkcolor=C["bg"], focuscolor=C["bg"], focusthickness=0, relief="flat", anchor="w", padding=(0, px(4)), font=f("caption"))
    style.map("DisclosurePage.TButton", foreground=[("active", C["text"])], background=[("active", C["bg"]), ("pressed", C["bg"])],
              bordercolor=[("active", C["bg"])], lightcolor=[("active", C["bg"])], darkcolor=[("active", C["bg"])])
    # ---- fields
    field_map = dict(bordercolor=[("focus", C["accent"]), ("invalid", C["danger"])],
                     lightcolor=[("focus", C["accent"]), ("invalid", C["danger"])],
                     darkcolor=[("focus", C["accent"]), ("invalid", C["danger"])],
                     fieldbackground=[("disabled", C["field_disabled"]), ("readonly", C["field"])],
                     foreground=[("disabled", C["text3"])])
    style.configure("TEntry", fieldbackground=C["field"], foreground=C["text"], bordercolor=C["field_border"], lightcolor=C["field_border"],
                    darkcolor=C["field_border"], padding=(px(6), px(4)), insertcolor=C["text"], selectbackground=C["row_sel"], selectforeground=C["text"])
    style.map("TEntry", **field_map)
    for name in ("TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=C["field"], foreground=C["text"], background=C["field"], bordercolor=C["field_border"],
                        lightcolor=C["field_border"], darkcolor=C["field_border"], padding=(px(6), px(4)), insertcolor=C["text"],
                        arrowcolor=C["text2"], arrowsize=px(14), selectbackground=C["row_sel"], selectforeground=C["text"])
        m = dict(field_map)
        m["arrowcolor"] = [("active", C["text"]), ("disabled", C["text3"])]
        m["background"] = [("active", C["bg"]), ("pressed", C["border"]), ("readonly", C["field"])]
        style.map(name, **m)
    # ---- check buttons
    for name, bg in (("TCheckbutton", C["bg"]), ("Card.TCheckbutton", C["card"])):
        style.configure(name, background=bg, foreground=C["text"], indicatorbackground=C["field"], indicatorforeground="#FFFFFF",
                        indicatormargin=(0, 0, px(6), 0), padding=(0, px(2)), focuscolor=bg, font=f("ui"))
        style.map(name, indicatorbackground=[("disabled", C["field_disabled"]), ("selected", C["accent"]), ("active", "#F0F0F5")],
                  background=[("active", bg), ("disabled", bg)], foreground=[("disabled", C["text3"])])
    # ---- treeview
    style.configure("Treeview", background=C["card"], fieldbackground=C["card"], foreground=C["text"], rowheight=px(24),
                    borderwidth=0, font=f("ui"), bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"])
    style.map("Treeview", foreground=_fixed_map(style, "foreground"), background=_fixed_map(style, "background"))
    style.map("Treeview", background=[("selected", C["row_sel"])], foreground=[("selected", C["text"])])
    style.configure("Treeview.Heading", background=C["bg"], foreground=C["text2"], font=f("caption"), relief="flat",
                    padding=(px(6), px(4)), bordercolor=C["border"], lightcolor=C["bg"], darkcolor=C["bg"])
    style.map("Treeview.Heading", background=[("active", "#EBEBF0"), ("pressed", "#EBEBF0")])
    # ---- scrollbars (thumb only, no arrows)
    for orient, key in (("Vertical", "ns"), ("Horizontal", "ew")):
        name = "%s.TScrollbar" % orient
        style.layout(name, [("%s.Scrollbar.trough" % orient, {"sticky": key, "children": [
            ("%s.Scrollbar.thumb" % orient, {"expand": 1, "sticky": "nswe"})]})])
        style.configure(name, gripcount=0, background=C["thumb"], troughcolor=C["card"], bordercolor=C["card"],
                        lightcolor=C["thumb"], darkcolor=C["thumb"], arrowsize=px(10), width=px(10))
        style.map(name, background=[("active", C["thumb_hover"]), ("pressed", C["thumb_hover"])],
                  lightcolor=[("active", C["thumb_hover"])], darkcolor=[("active", C["thumb_hover"])])
    # ---- progress bar
    style.layout("Horizontal.TProgressbar", [("Horizontal.Progressbar.trough", {"sticky": "nswe", "children": [
        ("Horizontal.Progressbar.pbar", {"side": "left", "sticky": "ns"})]})])
    style.configure("Horizontal.TProgressbar", troughcolor=C["border"], background=C["accent"], bordercolor=C["border"],
                    lightcolor=C["accent"], darkcolor=C["accent"], thickness=px(6), borderwidth=0)
    # ---- misc
    style.configure("TSeparator", background=C["sep"])
    style.configure("Card.TSeparator", background=C["border"])
    style.configure("TPanedwindow", background=C["bg"])
    style.configure("Sash", sashthickness=px(6), gripcount=0, handlesize=0)
    style.configure("TLabelframe", background=C["bg"], bordercolor=C["border"], lightcolor=C["bg"], darkcolor=C["bg"])
    style.configure("TLabelframe.Label", background=C["bg"], foreground=C["text2"], font=f("caption"))


def _option_db(root, theme):
    C = COLORS
    ui, mono = theme.font("ui"), theme.font("mono")
    opts = {
        "*Listbox.background": C["card"], "*Listbox.foreground": C["text"], "*Listbox.selectBackground": C["row_sel"],
        "*Listbox.selectForeground": C["text"], "*Listbox.highlightThickness": 0, "*Listbox.borderWidth": 0,
        "*Listbox.activeStyle": "none", "*Listbox.font": mono, "*Listbox.relief": "flat",
        "*Text.background": C["card"], "*Text.foreground": C["text"], "*Text.selectBackground": C["row_sel"],
        "*Text.selectForeground": C["text"], "*Text.highlightThickness": 0, "*Text.borderWidth": 0, "*Text.font": mono,
        "*Text.insertBackground": C["text"], "*Text.relief": "flat",
        "*TCombobox*Listbox.background": C["card"], "*TCombobox*Listbox.foreground": C["text"],
        "*TCombobox*Listbox.selectBackground": C["row_sel"], "*TCombobox*Listbox.selectForeground": C["text"],
        "*TCombobox*Listbox.font": ui, "*TCombobox*Listbox.borderWidth": 0, "*TCombobox*Listbox.highlightThickness": 0,
        "*Canvas.highlightThickness": 0, "*Canvas.background": C["card"],
        "*Toplevel.background": C["bg"],
    }
    for k, v in opts.items():
        try:
            root.option_add(k, v)
        except Exception:
            pass


def apply_theme(root, platform=None):
    """Switch ttk to 'clam', resolve fonts, reconfigure the Tk named fonts, register every style,
    set the option database for classic widgets and call apply_mpl_theme(); returns Theme and stores
    it as root.theme. Idempotent. Never raises: on any failure it logs to stderr and returns a Theme
    built on the untouched default ttk theme (the GUI must start on the lab PC whatever happens)."""
    plat = platform or _platform()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
        fonts = _resolve_fonts(root, plat)
        _configure_named_fonts(root, fonts)
        theme = Theme(root, plat, fonts, style, ok=True)
        _register_styles(style, theme)
        _option_db(root, theme)
        try:
            root.configure(background=COLORS["bg"])
        except Exception:
            pass
    except Exception as e:                                    # pragma: no cover - defensive
        sys.stderr.write("ui_theme: falling back to the default ttk theme: %s\n" % e)
        fonts = {}
        try:
            base = tkfont.Font(root=root, name="TkDefaultFont", exists=True)
            fam, size = base.cget("family"), abs(int(base.cget("size"))) or 11
            fonts = {"ui": base, "ui_bold": tkfont.Font(root=root, family=fam, size=size, weight="bold"),
                     "title": tkfont.Font(root=root, family=fam, size=size + 8, weight="bold"),
                     "section": tkfont.Font(root=root, family=fam, size=size + 2, weight="bold"),
                     "caption": tkfont.Font(root=root, family=fam, size=max(8, size - 1)),
                     "caption_bold": tkfont.Font(root=root, family=fam, size=max(8, size - 1), weight="bold"),
                     "value": tkfont.Font(root=root, family=fam, size=size + 6, weight="bold"),
                     "mono": tkfont.Font(root=root, name="TkFixedFont", exists=True), "family": fam, "mono_family": "TkFixedFont"}
        except Exception:
            pass
        theme = Theme(root, plat, fonts, style, ok=False)
    try:
        apply_mpl_theme(plat)
    except Exception as e:                                    # pragma: no cover
        sys.stderr.write("ui_theme: matplotlib theme not applied: %s\n" % e)
    root.theme = theme
    return theme


_MPL_FONTS = {
    "win32": ["Microsoft YaHei", "Segoe UI", "Microsoft YaHei UI", "Arial Unicode MS", "Noto Sans CJK SC", "DejaVu Sans"],
    "darwin": ["Helvetica Neue", "PingFang SC", "Arial Unicode MS", "DejaVu Sans"],
    "linux": ["Noto Sans CJK SC", "DejaVu Sans"],
}


def apply_mpl_theme(platform=None):
    """rcParams for figures embedded in the GUI (white, light grid, no top/right spines, accent lines).
    Safe to call before matplotlib is imported by the panels (imports it itself); a missing matplotlib
    is ignored. Legends contain Chinese, so a CJK-capable family comes first on Windows: matplotlib
    3.4 (lab PC) has no per-glyph font fallback."""
    try:
        import matplotlib
        from cycler import cycler
    except Exception:
        return False
    plat = platform or _platform()
    C = COLORS
    fonts = _MPL_FONTS.get(plat, _MPL_FONTS["linux"])
    if plat == "darwin":
        # per-glyph fallback exists on the development Mac (matplotlib >= 3.6); older versions get Hiragino first
        try:
            major, minor = [int(x) for x in matplotlib.__version__.split(".")[:2]]
            if (major, minor) < (3, 6):
                fonts = ["Hiragino Sans GB", "Arial Unicode MS", "Helvetica Neue", "DejaVu Sans"]
        except Exception:
            pass
    matplotlib.rcParams.update({
        "figure.facecolor": C["card"], "axes.facecolor": C["card"], "savefig.facecolor": C["card"],
        "axes.edgecolor": C["sep"], "axes.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": C["border"], "grid.linewidth": 0.6, "grid.alpha": 1.0,
        "axes.labelcolor": C["text2"], "axes.labelsize": 9, "axes.titlesize": 10, "axes.titlecolor": C["text"],
        "xtick.color": C["text2"], "ytick.color": C["text2"], "xtick.labelsize": 8, "ytick.labelsize": 8,
        "xtick.direction": "out", "ytick.direction": "out", "xtick.major.size": 3, "ytick.major.size": 3,
        "lines.linewidth": 1.0, "lines.antialiased": True,
        "axes.prop_cycle": cycler(color=[C["accent"], "#FF9F0A", "#34C759", "#FF3B30", "#AF52DE", "#5AC8FA", "#FF2D55"]),
        "legend.frameon": False, "legend.fontsize": 8, "legend.handlelength": 1.6,
        # an explicit family list: matplotlib >= 3.6 falls back per glyph along it (CJK legends on the Mac);
        # 3.4 (lab PC) simply uses the first installed one, hence the CJK-capable family first on Windows
        "font.family": list(fonts), "font.sans-serif": list(fonts),
        "axes.unicode_minus": False, "figure.autolayout": False, "figure.dpi": 100,
    })
    return True


def mpl_style_axes(ax):
    """Per-axes touch-ups that rcParams cannot express (spine colours after clear(), tick padding)."""
    C = COLORS
    for name, sp in ax.spines.items():
        sp.set_visible(name in ("left", "bottom"))
        sp.set_color(C["sep"])
        sp.set_linewidth(0.8)
    ax.grid(True, color=C["border"], linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=C["text2"], labelsize=8, length=3, pad=3)
    ax.xaxis.label.set_color(C["text2"])
    ax.yaxis.label.set_color(C["text2"])
    ax.set_facecolor(C["card"])
    return ax


def mpl_empty(ax, text):
    """Draw a centred secondary-text hint on an empty axes; returns the Text artist (remove() on first data)."""
    return ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=COLORS["text3"], fontsize=10)


def mpl_bind_resize(canvas, fig, delay=300):
    """Re-run tight_layout (throttled) when the Tk widget hosting `canvas` changes size."""
    widget = canvas.get_tk_widget()
    state = {"job": None}

    def relayout():
        state["job"] = None
        try:
            fig.tight_layout(pad=1.2)
            canvas.draw_idle()
        except Exception:
            pass

    def on_configure(_event):
        if state["job"] is not None:
            try:
                widget.after_cancel(state["job"])
            except Exception:
                pass
        state["job"] = widget.after(delay, relayout)
    widget.bind("<Configure>", on_configure, add="+")
    return widget


# ---- widgets --------------------------------------------------------------------------------
def _tone_style(tone, prefix="Pill", on_card=False):
    tone = tone if tone in TONES else "neutral"
    if on_card and prefix == "Pill":
        return "PillCard.%s.TLabel" % tone.capitalize()
    return "%s.%s.TLabel" % (prefix, tone.capitalize())


class Card(ttk.Frame):
    """White bordered card. `title` (Section.TLabel) and `subtitle` (Card.Caption.TLabel) sit in
    self.header; `actions` is a list of (text, command, style) rendered as buttons on the header's
    right; children go into self.body (CardBody.TFrame). padding defaults to SPACE['lg']."""

    def __init__(self, parent, title=None, subtitle=None, padding=None, actions=None, **kw):
        kw.setdefault("style", "Card.TFrame")
        if padding is None:
            padding = SPACE["lg"]
        ttk.Frame.__init__(self, parent, padding=padding, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.header = ttk.Frame(self, style="CardBody.TFrame")
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.columnconfigure(0, weight=1)
        self.title_var = tk.StringVar(value=title or "")
        self.subtitle_var = tk.StringVar(value=subtitle or "")
        self.title_label = ttk.Label(self.header, textvariable=self.title_var, style="Section.TLabel", anchor="w")
        self.subtitle_label = ttk.Label(self.header, textvariable=self.subtitle_var, style="Card.Caption.TLabel", anchor="w")
        self.actions = ttk.Frame(self.header, style="CardBody.TFrame")
        self.actions.grid(row=0, column=1, rowspan=2, sticky="ne")
        self.action_buttons = {}
        for spec in actions or []:
            text, command = spec[0], spec[1]
            st = spec[2] if len(spec) > 2 and spec[2] else "TButton"
            b = ttk.Button(self.actions, text=text, command=command, style=st)
            b.pack(side="left", padx=(SPACE["sm"], 0))
            self.action_buttons[text] = b
        self.body = ttk.Frame(self, style="CardBody.TFrame")
        self.body.grid(row=1, column=0, sticky="nsew")
        self._layout_header()

    def _layout_header(self):
        has_title, has_sub = bool(self.title_var.get()), bool(self.subtitle_var.get())
        self.title_label.grid_forget()
        self.subtitle_label.grid_forget()
        if has_title:
            self.title_label.grid(row=0, column=0, sticky="w")
        if has_sub:
            self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        if has_title or has_sub:
            self.header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["md"]))
        else:
            self.header.grid_forget()

    def set_title(self, text):
        self.title_var.set(text or "")
        self._layout_header()

    def set_subtitle(self, text):
        self.subtitle_var.set(text or "")
        self._layout_header()


def section_header(parent, text, caption=None, pady=(SPACE["md"], SPACE["xs"])):
    """Small in-card section divider: bold caption-size text + optional secondary caption; returns the Frame."""
    f = ttk.Frame(parent, style="CardBody.TFrame")
    ttk.Label(f, text=text, style="Card.CaptionBold.TLabel").pack(side="left")
    if caption:
        ttk.Label(f, text=caption, style="Card.Caption.TLabel").pack(side="left", padx=(SPACE["sm"], 0))
    ttk.Separator(f, orient="horizontal", style="Card.TSeparator").pack(side="left", fill="x", expand=True, padx=(SPACE["sm"], 0))
    f.section_pady = pady
    return f


class StatusPill(ttk.Label):
    """Tinted label 'Pill.<Tone>.TLabel'; with dot=True the text is prefixed by '● '. on_card picks the
    card-background neutral variant."""

    def __init__(self, parent, text="", tone="neutral", dot=True, on_card=False, **kw):
        self._dot = dot
        self._on_card = on_card
        self._tone = tone if tone in TONES else "neutral"
        self._text = text
        kw.setdefault("style", _tone_style(self._tone, on_card=on_card))
        ttk.Label.__init__(self, parent, text=self._render(text), **kw)

    def _render(self, text):
        return ("● " + text) if self._dot else text

    def set(self, text=None, tone=None):
        changed = False
        if text is not None and text != self._text:
            self._text = text
            self.configure(text=self._render(text))
            changed = True
        if tone is not None and tone != self._tone and tone in TONES:
            self._tone = tone
            self.configure(style=_tone_style(tone, on_card=self._on_card))
            changed = True
        return changed

    @property
    def tone(self):
        return self._tone

    @property
    def text(self):
        return self._text


class Tooltip(object):
    """Toplevel(overrideredirect) with Tooltip.TLabel shown `delay` ms after <Enter>, hidden on <Leave>,
    <ButtonPress>, <Destroy>. Positioned below-right of the pointer, clamped to the screen."""

    def __init__(self, widget, text, delay=600, wraplength=320):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wraplength = wraplength
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def set_text(self, text):
        self.text = text

    def _schedule(self, _event=None):
        self._cancel()
        if self.text:
            self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if self._tip is not None or not self.text:
            return
        try:
            if not self.widget.winfo_exists():
                return
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            try:
                tip.wm_attributes("-topmost", True)
            except Exception:
                pass
            tip.configure(background=COLORS["tooltip"])
            lbl = ttk.Label(tip, text=self.text, style="Tooltip.TLabel", wraplength=self.wraplength, justify="left")
            lbl.pack()
            tip.update_idletasks()
            x = self.widget.winfo_pointerx() + 12
            y = self.widget.winfo_pointery() + 18
            w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
            sw, sh = self.widget.winfo_screenwidth(), self.widget.winfo_screenheight()
            x = max(0, min(x, sw - w - 4))
            y = max(0, min(y, sh - h - 4))
            tip.wm_geometry("+%d+%d" % (x, y))
            self._tip = tip
        except Exception:
            self._tip = None

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def destroy(self):
        self._hide()


def tooltip(widget, text, **kw):
    """Convenience: attach a Tooltip and return the widget (chainable)."""
    if text:
        widget._tooltip = Tooltip(widget, text, **kw)
    return widget


class Disclosure(ttk.Frame):
    """Collapsible area: a Disclosure.TButton header showing '▸ 高级' / '▾ 高级' and self.body below it.
    on_toggle(is_open) is called after every change. The body is grid-forgotten when closed so the
    layout collapses; state is per instance (no persistence)."""

    def __init__(self, parent, title="高级", opened=False, on_toggle=None, on_card=True, **kw):
        kw.setdefault("style", "CardBody.TFrame" if on_card else "TFrame")
        ttk.Frame.__init__(self, parent, **kw)
        self.title = title
        self.on_toggle = on_toggle
        self._open = False
        self.columnconfigure(0, weight=1)
        self.button = ttk.Button(self, text=self._label(), style="Disclosure.TButton" if on_card else "DisclosurePage.TButton",
                                 command=self.toggle, takefocus=0)
        self.button.grid(row=0, column=0, sticky="ew")
        self.body = ttk.Frame(self, style="CardBody.TFrame" if on_card else "TFrame")
        if opened:
            self.open()

    def _label(self):
        return "%s %s" % ("▾" if self._open else "▸", self.title)

    def open(self):
        if not self._open:
            self._open = True
            self.body.grid(row=1, column=0, sticky="nsew", pady=(SPACE["xs"], 0))
            self.button.configure(text=self._label())
            if self.on_toggle:
                self.on_toggle(True)

    def close(self):
        if self._open:
            self._open = False
            self.body.grid_forget()
            self.button.configure(text=self._label())
            if self.on_toggle:
                self.on_toggle(False)

    def toggle(self):
        if self._open:
            self.close()
        else:
            self.open()

    @property
    def is_open(self):
        return self._open


class Sidebar(ttk.Frame):
    """items = [(key, label, hint), ...]; hint is the tooltip. on_select(key) is called on click and on
    select(key, notify=True). Exactly one item is SidebarItemSelected.TButton. Buttons take no focus."""

    def __init__(self, parent, items, on_select, brand="OptiComp2", brand_caption=None, width=200):
        ttk.Frame.__init__(self, parent, style="Sidebar.TFrame", width=width)
        self.pack_propagate(False)
        self.on_select = on_select
        self.buttons = {}
        self.footer_buttons = {}
        self._badges = {}
        self._selected = None
        head = ttk.Frame(self, style="Sidebar.TFrame")
        head.pack(fill="x", padx=SPACE["lg"], pady=(SPACE["lg"] + SPACE["xs"], SPACE["md"]))
        ttk.Label(head, text=brand, style="SidebarBrand.TLabel").pack(anchor="w")
        self.caption_label = ttk.Label(head, text=brand_caption or "", style="SidebarCaption.TLabel")
        if brand_caption:
            self.caption_label.pack(anchor="w")
        self.items_frame = ttk.Frame(self, style="Sidebar.TFrame")
        self.items_frame.pack(fill="x", padx=SPACE["sm"])
        for key, label, hint in items:
            b = ttk.Button(self.items_frame, text=label, style="SidebarItem.TButton", takefocus=0,
                           command=lambda k=key: self.select(k, notify=True))
            b.pack(fill="x", pady=1)
            if hint:
                tooltip(b, hint)
            self.buttons[key] = b
        self.footer = ttk.Frame(self, style="Sidebar.TFrame")
        self.footer.pack(side="bottom", fill="x", padx=SPACE["sm"], pady=SPACE["sm"])

    def select(self, key, notify=True):
        if key not in self.buttons:
            return
        for k, b in self.buttons.items():
            b.configure(style="SidebarItemSelected.TButton" if k == key else "SidebarItem.TButton")
        self._selected = key
        if notify and self.on_select:
            self.on_select(key)

    def set_badge(self, key, text=None):
        row = self.footer_buttons.get(key)
        if row is None:
            return
        badge = self._badges.get(key)
        if not text:
            if badge is not None:
                badge.grid_remove()
            return
        if badge is None:
            badge = ttk.Label(row.master, text=str(text), style="Badge.TLabel")
            self._badges[key] = badge
        badge.configure(text=str(text))
        badge.grid(row=0, column=1, padx=(0, SPACE["sm"]))

    def add_footer_item(self, key, label, command, hint=None):
        row = ttk.Frame(self.footer, style="Sidebar.TFrame")
        row.pack(fill="x", pady=1)
        row.columnconfigure(0, weight=1)
        b = ttk.Button(row, text=label, style="SidebarItem.TButton", takefocus=0, command=command)
        b.grid(row=0, column=0, sticky="ew")
        if hint:
            tooltip(b, hint)
        self.footer_buttons[key] = b
        return b

    def set_footer_active(self, key, active):
        b = self.footer_buttons.get(key)
        if b is not None:
            b.configure(style="SidebarItemActive.TButton" if active else "SidebarItem.TButton")

    @property
    def selected(self):
        return self._selected


class PageHeader(ttk.Frame):
    """Large title + one-line subtitle; `actions` = [(text, command, style), ...] right-aligned.
    self.actions is the Frame for extra widgets; self.buttons maps text -> ttk.Button."""

    def __init__(self, parent, title, subtitle=None, actions=None):
        ttk.Frame.__init__(self, parent, style="Page.TFrame")
        self.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(self, text=title, style="Title.TLabel", anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.subtitle_var = tk.StringVar(value=subtitle or "")
        self.subtitle_label = ttk.Label(self, textvariable=self.subtitle_var, style="Subtitle.TLabel", anchor="w")
        self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(SPACE["xs"], 0))
        self.actions = ttk.Frame(self, style="Page.TFrame")
        self.actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(SPACE["lg"], 0))
        self.buttons = {}
        for spec in actions or []:
            text, command = spec[0], spec[1]
            st = spec[2] if len(spec) > 2 and spec[2] else "TButton"
            b = ttk.Button(self.actions, text=text, command=command, style=st)
            b.pack(side="left", padx=(SPACE["sm"], 0))
            self.buttons[text] = b

    def set_subtitle(self, text):
        self.subtitle_var.set(text or "")


class StatusBar(ttk.Frame):
    """fields = [(key, initial_text), ...] rendered as StatusPill(dot=True) separated by TSeparator;
    action = (text, command) renders a Destructive.TButton at the far right (关闭快门)."""

    def __init__(self, parent, fields, action=None):
        ttk.Frame.__init__(self, parent, style="Sep.TFrame")
        self.inner = ttk.Frame(self, style="StatusBar.TFrame", padding=(SPACE["md"], 5))
        self.inner.pack(fill="x", pady=(1, 0))
        self.pills = {}
        first = True
        for key, text in fields:
            if not first:
                ttk.Separator(self.inner, orient="vertical").pack(side="left", fill="y", padx=SPACE["md"], pady=3)
            first = False
            p = StatusPill(self.inner, text=text, tone="neutral", dot=True, on_card=True)
            p.pack(side="left")
            self.pills[key] = p
        self.action_button = None
        if action:
            self.action_button = ttk.Button(self.inner, text=action[0], command=action[1], style="Destructive.TButton")
            self.action_button.pack(side="right")

    def set(self, key, text, tone="neutral"):
        p = self.pills.get(key)
        if p is not None:
            p.set(text, tone)

    def get(self, key):
        p = self.pills.get(key)
        return p.text if p is not None else None

    def tone(self, key):
        p = self.pills.get(key)
        return p.tone if p is not None else None

    def set_action(self, text=None, command=None, state=None):
        if self.action_button is None:
            return
        if text is not None:
            self.action_button.configure(text=text)
        if command is not None:
            self.action_button.configure(command=command)
        if state is not None:
            self.action_button.configure(state=state)


class Banner(ttk.Frame):
    """Inline notice (Banner.<Tone>.TFrame/TLabel) with optional 'x' Ghost button and action buttons.
    Hidden until show(). Place it with grid(); hide()/show() use grid_remove()/grid()."""

    def __init__(self, parent, text="", tone="warning", closable=True, actions=None):
        self._tone = tone if tone in TONES else "warning"
        ttk.Frame.__init__(self, parent, style="Banner.%s.TFrame" % self._tone.capitalize(), padding=(SPACE["md"], SPACE["sm"]))
        self.columnconfigure(0, weight=1)
        self.text_var = tk.StringVar(value=text)
        self.label = ttk.Label(self, textvariable=self.text_var, style="Banner.%s.TLabel" % self._tone.capitalize(), justify="left", anchor="w")
        self.label.grid(row=0, column=0, sticky="w")
        self.actions = ttk.Frame(self, style="Banner.%s.TFrame" % self._tone.capitalize())
        self.actions.grid(row=0, column=1, sticky="e", padx=(SPACE["md"], 0))
        self.buttons = {}
        for spec in actions or []:
            b = ttk.Button(self.actions, text=spec[0], command=spec[1], style="Banner.%s.TButton" % self._tone.capitalize())
            b.pack(side="left", padx=(SPACE["xs"], 0))
            self.buttons[spec[0]] = b
        self.close_button = None
        if closable:
            self.close_button = ttk.Button(self.actions, text="✕", command=self.hide, style="Banner.%s.TButton" % self._tone.capitalize(), width=2)
            self.close_button.pack(side="left", padx=(SPACE["sm"], 0))
        self._visible = False
        self.bind("<Configure>", self._wrap, add="+")

    def _wrap(self, event=None):
        try:
            w = self.winfo_width() - self.actions.winfo_reqwidth() - 2 * SPACE["md"] - SPACE["md"]
            if w > 100:
                self.label.configure(wraplength=w)
        except Exception:
            pass

    def _restyle(self):
        n = self._tone.capitalize()
        self.configure(style="Banner.%s.TFrame" % n)
        self.label.configure(style="Banner.%s.TLabel" % n)
        self.actions.configure(style="Banner.%s.TFrame" % n)
        for b in list(self.buttons.values()) + ([self.close_button] if self.close_button else []):
            b.configure(style="Banner.%s.TButton" % n)

    def show(self, text, tone=None):
        self.text_var.set(text)
        if tone and tone in TONES and tone != self._tone:
            self._tone = tone
            self._restyle()
        if not self._visible:
            self._visible = True
            if self.winfo_manager() == "pack":
                self.pack_configure()
            else:
                self.grid()
        return self

    def hide(self):
        # also unmaps a banner that was gridded but never shown (the flag starts False)
        self._visible = False
        manager = self.winfo_manager()
        if manager == "pack":
            self.pack_forget()
        elif manager == "grid":
            self.grid_remove()

    @property
    def visible(self):
        return self._visible

    @property
    def tone(self):
        return self._tone


class Readout(ttk.Frame):
    """Row of labelled values: fields = [(key, label), ...]; each cell = Card.Caption.TLabel over
    Value.TLabel. columns defaults to len(fields)."""

    def __init__(self, parent, fields, columns=None):
        ttk.Frame.__init__(self, parent, style="CardBody.TFrame")
        self.values = {}
        self.captions = {}
        columns = columns or len(fields)
        for i, (key, label) in enumerate(fields):
            cell = ttk.Frame(self, style="CardBody.TFrame")
            cell.grid(row=i // columns, column=i % columns, sticky="w", padx=(0, SPACE["xl"]), pady=(0, SPACE["xs"]))
            cap = ttk.Label(cell, text=label, style="Card.Caption.TLabel")
            cap.pack(anchor="w")
            val = ttk.Label(cell, text="—", style="Readout.TLabel")
            val.pack(anchor="w")
            self.captions[key] = cap
            self.values[key] = val

    def set(self, key, value, tone=None):
        lbl = self.values.get(key)
        if lbl is None:
            return
        lbl.configure(text=value if value not in (None, "") else "—")
        color = {"danger": COLORS["danger_pressed"], "warning": COLORS["warning_text"], "success": COLORS["success_text"],
                 "accent": COLORS["accent_pressed"]}.get(tone, COLORS["text"])
        lbl.configure(foreground=color)


class LogDrawer(ttk.Frame):
    """Title row + tk.Text (self.text, state=disabled, mono font) + ttk.Scrollbar. Colour tags of the
    spec are pre-registered: 'error', 'warning', 'tx', 'rx', 'event'."""

    def __init__(self, parent, on_clear, on_save, on_hide, theme):
        ttk.Frame.__init__(self, parent, style="Sep.TFrame")
        inner = ttk.Frame(self, style="CardBody.TFrame", padding=(SPACE["md"], SPACE["sm"], SPACE["md"], SPACE["sm"]))
        inner.pack(fill="both", expand=True, pady=(1, 0))
        head = ttk.Frame(inner, style="CardBody.TFrame")
        head.pack(fill="x", pady=(0, SPACE["xs"]))
        ttk.Label(head, text="日志", style="Card.CaptionBold.TLabel").pack(side="left")
        ttk.Label(head, text="TX/RX 与事件", style="Card.Caption.TLabel").pack(side="left", padx=(SPACE["sm"], 0))
        self.hide_button = ttk.Button(head, text="收起  ⌃L", command=on_hide, style="Ghost.TButton", takefocus=0)
        self.hide_button.pack(side="right")
        self.save_button = ttk.Button(head, text="保存日志…", command=on_save, style="Ghost.TButton", takefocus=0)
        self.save_button.pack(side="right")
        self.clear_button = ttk.Button(head, text="清空", command=on_clear, style="Ghost.TButton", takefocus=0)
        self.clear_button.pack(side="right")
        body = ttk.Frame(inner, style="CardBody.TFrame")
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, height=8, state="disabled", wrap="none", font=theme.font("mono"), background=COLORS["card"],
                            foreground=COLORS["text"], borderwidth=0, highlightthickness=0, relief="flat", padx=4, pady=2)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("error", foreground=COLORS["danger"])
        self.text.tag_configure("warning", foreground=COLORS["warning_text"])
        self.text.tag_configure("tx", foreground=COLORS["text2"])
        self.text.tag_configure("rx", foreground=COLORS["text"])
        self.text.tag_configure("event", foreground=COLORS["accent_pressed"])

    def append(self, line, tag=None):
        self.text.configure(state="normal")
        if tag:
            self.text.insert("end", line + "\n", tag)
        else:
            self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


def form_row(parent, row, label, *widgets, **kw):
    """Grid helper inside a card body: FormLabel.TLabel (right-aligned, `label_width` chars) in column 0,
    then the given widgets left-to-right from column 1 (padx=(0, SPACE['sm'])), an optional unit
    Card.Caption.TLabel, and a tooltip `help` on the label. Returns the list of widgets. Column 0 gets
    weight 0, the last used column weight 1."""
    unit = kw.get("unit")
    help_text = kw.get("help")
    label_width = kw.get("label_width", 12)
    sticky = kw.get("sticky", "w")
    pady = kw.get("pady")
    if pady is None:
        pady = SPACE["xs"]
    lbl = ttk.Label(parent, text=label, style="FormLabel.TLabel", width=label_width, anchor="e")
    lbl.grid(row=row, column=0, sticky="e", padx=(0, SPACE["sm"]), pady=pady)
    if help_text:
        tooltip(lbl, help_text)
    col = 1
    placed = []
    n = len(widgets)
    for i, w in enumerate(widgets):
        w.grid(row=row, column=col, sticky=sticky, padx=(0, SPACE["xs"] if (unit and i == n - 1) else SPACE["sm"]), pady=pady)
        placed.append(w)
        col += 1
    if unit:
        u = ttk.Label(parent, text=unit, style="Card.Caption.TLabel")
        u.grid(row=row, column=col, sticky="w", padx=(0, SPACE["sm"]), pady=pady)
        col += 1
    parent.columnconfigure(0, weight=0)
    parent.columnconfigure(col - 1, weight=1)
    return placed


def unit_label(parent, unit):
    """Caption unit ('°', 'ms', '%') packed right after the field(s) already packed left-to-right in
    `parent`, so the unit hugs its entry whatever the width of the surrounding grid column."""
    lbl = ttk.Label(parent, text=unit, style="Card.Caption.TLabel")
    lbl.pack(side="left", padx=(SPACE["xs"], 0))
    return lbl


def bind_enter(widget, command):
    """<Return> and <KP_Enter> -> command() (event argument dropped); returns widget."""
    def handler(_event):
        command()
        return "break"
    widget.bind("<Return>", handler)
    widget.bind("<KP_Enter>", handler)
    return widget


def bind_shortcut(root, key, command, ctrl=True, shift=False):
    """Bind both '<Control-…>' and '<Command-…>' (macOS) variants, e.g. key='r', '1', 'l', 'Escape'.
    Handlers return 'break'. With ctrl=False the plain key (Escape, F5) is bound."""
    def handler(_event):
        command()
        return "break"
    keys = [key]
    if len(key) == 1 and key.isalpha():
        keys = [key.lower(), key.upper()] if shift else [key.lower()]
    sequences = []
    for k in keys:
        if ctrl:
            mods = "Shift-" if shift else ""
            sequences.append("<Control-%sKey-%s>" % (mods, k))
            sequences.append("<Command-%sKey-%s>" % (mods, k))
        else:
            sequences.append("<Key-%s>" % k)
    bound = []
    for seq in sequences:
        try:
            root.bind(seq, handler)
            bound.append(seq)
        except tk.TclError:
            pass
    return bound


def empty_state(parent, title, hint=None):
    """Centered Empty.TLabel block for lists/tables with nothing to show; returns the Frame (grid/pack/place it
    over the empty widget; destroy or hide when data arrives)."""
    f = ttk.Frame(parent, style="CardBody.TFrame", padding=SPACE["md"])
    ttk.Label(f, text=title, style="Empty.TLabel", justify="center", anchor="center").pack()
    if hint:
        ttk.Label(f, text=hint, style="EmptyHint.TLabel", justify="center", anchor="center").pack(pady=(SPACE["xs"], 0))
    return f


def confirm_abort(parent):
    """askyesno('中止序列', ...) with default 'no' - used by the Esc shortcut."""
    return messagebox.askyesno("中止序列", "将请求中止，当前步骤结束后停止并关闭快门。继续？", default="no", parent=parent)
