# -*- coding: utf-8 -*-
"""Synthetic screenshot renderer and layout audit for the OptiComp2 GUI.

``render_window(root, path)`` paints a PNG of a live Tk window from the real widget tree: every
viewable widget is drawn at its actual position and size with its actual text, colours and fonts,
and matplotlib canvases are copied from their Agg buffers. ``manual_gui --screenshot`` uses it when
a real screen grab is impossible (macOS without the Screen Recording permission only captures the
wallpaper). The output is layout-faithful - sizes, alignment, clipping and text are real - but not
pixel-exact: native widget chrome (rounded corners, focus rings, bevels) is approximated.

``audit_layout(root)`` lists widgets whose allocated size is smaller than their requested size or
that overflow their parent, i.e. clipped text or controls; the GUI tests assert it is empty.

Only the Python standard library, Pillow and (optionally) fontTools are used; the module degrades
gracefully when Pillow is missing (render_window raises RuntimeError, audit_layout still works).
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, font as tkfont

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:                                   # pragma: no cover - Pillow is optional
    Image = ImageDraw = ImageFont = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ui_theme import COLORS

TITLE_BAR = 28                                      # fake macOS-style title bar height (px)
SHRINKABLE = {"Listbox", "Treeview", "Text", "Canvas", "TScrollbar", "Scrollbar", "TProgressbar", "TSeparator",
              "TFrame", "Frame", "Labelframe", "TLabelframe", "Tk", "Toplevel", "TPanedwindow", "Panedwindow",
              "TNotebook", "Scale", "TScale", "Sizegrip", "TSizegrip"}
LEAF = {"Label", "TLabel", "Button", "TButton", "Entry", "TEntry", "Spinbox", "TSpinbox", "Combobox", "TCombobox",
        "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton", "Message", "Menubutton", "TMenubutton"}

# ---------------------------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------------------------
_FONT_FILES = {
    "darwin": {
        "latin": [("/System/Library/Fonts/SFNS.ttf", 0, "Regular", "Bold"), ("/System/Library/Fonts/HelveticaNeue.ttc", 0, None, 1),
                  ("/System/Library/Fonts/Helvetica.ttc", 0, None, 1)],
        "cjk": [("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, None, 2), ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0, None, None)],
        "mono": [("/System/Library/Fonts/Menlo.ttc", 0, None, 1), ("/System/Library/Fonts/SFNSMono.ttf", 0, "Regular", "Bold")],
        "symbol": [("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0, None, None), ("/System/Library/Fonts/Apple Symbols.ttf", 0, None, None)],
    },
    "win32": {
        "latin": [("C:/Windows/Fonts/segoeui.ttf", 0, None, "C:/Windows/Fonts/segoeuib.ttf"), ("C:/Windows/Fonts/arial.ttf", 0, None, "C:/Windows/Fonts/arialbd.ttf")],
        "cjk": [("C:/Windows/Fonts/msyh.ttc", 0, None, "C:/Windows/Fonts/msyhbd.ttc"), ("C:/Windows/Fonts/simhei.ttf", 0, None, None)],
        "mono": [("C:/Windows/Fonts/consola.ttf", 0, None, "C:/Windows/Fonts/consolab.ttf"), ("C:/Windows/Fonts/cour.ttf", 0, None, None)],
        "symbol": [("C:/Windows/Fonts/seguisym.ttf", 0, None, None), ("C:/Windows/Fonts/arial.ttf", 0, None, None)],
    },
    "linux": {
        "latin": [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0, None, "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")],
        "cjk": [("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0, None, None), ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0, None, None)],
        "mono": [("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0, None, "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")],
        "symbol": [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0, None, None)],
    },
}
_MONO_HINTS = ("menlo", "mono", "consolas", "courier", "cascadia", "sf mono")


def _is_cjk(ch):
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F


class _Face(object):
    """One PIL font face plus (optionally) its cmap for glyph-coverage checks."""

    def __init__(self, font, cmap):
        self.font = font
        self.cmap = cmap

    def covers(self, ch):
        if self.cmap is None:
            return True
        return ord(ch) in self.cmap


class _FontSet(object):
    """A Tk font resolved to a chain of PIL faces: main, CJK fallback, symbol fallback."""

    def __init__(self, faces, size_px, linespace, ascent, tk_font=None):
        self.faces = faces
        self.size = size_px
        self.linespace = linespace
        self.ascent = ascent
        self.tk_font = tk_font

    def measure(self, text):
        """Width as Tk measures it (used for line breaking so the line count matches Tk's own layout;
        PIL glyph widths differ slightly from Tk's and would otherwise wrap a line early and overlap
        the widget below)."""
        if self.tk_font is not None:
            try:
                return self.tk_font.measure(text)
            except tk.TclError:
                pass
        return self.width(text)

    def pick(self, ch):
        for f in self.faces:
            if f.covers(ch):
                return f.font
        return self.faces[0].font

    def runs(self, text):
        out = []
        cur_font, cur = None, []
        for ch in text:
            f = self.pick(ch)
            if f is not cur_font and cur:
                out.append((cur_font, "".join(cur)))
                cur = []
            cur_font = f
            cur.append(ch)
        if cur:
            out.append((cur_font, "".join(cur)))
        return out

    def width(self, text):
        return sum(f.getlength(s) for f, s in self.runs(text))

    def draw(self, draw, x, y, text, fill, anchor="lm"):
        """Draw text left-aligned at x with the given vertical anchor ('lm' middle, 'la' top, 'ls' baseline)."""
        cx = x
        for f, s in self.runs(text):
            draw.text((cx, y), s, font=f, fill=fill, anchor=anchor)
            cx += f.getlength(s)
        return cx - x


class Fonts(object):
    """Resolves Tk font descriptions to PIL fonts, with per-glyph CJK / symbol fallback."""

    def __init__(self, root, platform=None):
        self.root = root
        self.platform = platform or ("darwin" if sys.platform == "darwin" else "win32" if sys.platform.startswith("win") else "linux")
        self.dpi = float(root.winfo_fpixels("1i"))
        self.files = _FONT_FILES.get(self.platform, _FONT_FILES["linux"])
        self._faces = {}
        self._cmaps = {}
        self._sets = {}

    # -- low level
    def _cmap(self, path, index):
        key = (path, index)
        if key not in self._cmaps:
            cm = None
            try:
                from fontTools.ttLib import TTFont
                cm = set(TTFont(path, fontNumber=index, lazy=True).getBestCmap().keys())
            except Exception:
                cm = None
            self._cmaps[key] = cm
        return self._cmaps[key]

    def _face(self, spec, size, bold):
        path, index, reg_var, bold_sel = spec
        key = (path, index, size, bold)
        if key in self._faces:
            return self._faces[key]
        face = None
        try:
            p, i = path, index
            if bold and isinstance(bold_sel, str) and bold_sel.endswith((".ttf", ".ttc", ".otf")):
                p = bold_sel
            elif bold and isinstance(bold_sel, int):
                i = bold_sel
            if os.path.isfile(p):
                f = ImageFont.truetype(p, size, index=i)
                try:
                    if bold and isinstance(bold_sel, str) and not bold_sel.endswith((".ttf", ".ttc", ".otf")):
                        f.set_variation_by_name(bold_sel)
                    elif reg_var and not bold:
                        f.set_variation_by_name(reg_var)
                except Exception:
                    pass
                face = _Face(f, self._cmap(p, i))
        except Exception:
            face = None
        self._faces[key] = face
        return face

    def _chain(self, role, size, bold):
        faces = []
        for spec in self.files.get(role, ()):
            f = self._face(spec, size, bold)
            if f is not None:
                faces.append(f)
                break
        return faces

    def resolve(self, spec):
        """spec: a Tk font name / descriptor ('' -> TkDefaultFont)."""
        key = str(spec) if spec else "TkDefaultFont"
        if key in self._sets:
            return self._sets[key]
        try:
            f = tkfont.Font(root=self.root, font=spec or "TkDefaultFont")
            a = f.actual()
            linespace = f.metrics("linespace")
            ascent = f.metrics("ascent")
        except tk.TclError:
            f = tkfont.Font(root=self.root, font="TkDefaultFont")
            a = f.actual()
            linespace = f.metrics("linespace")
            ascent = f.metrics("ascent")
        size = a.get("size", 12)
        px = int(round(-size)) if size < 0 else int(round(size * self.dpi / 72.0))
        px = max(6, px)
        bold = a.get("weight") == "bold"
        family = (a.get("family") or "").lower()
        mono = any(h in family for h in _MONO_HINTS)
        faces = []
        if mono:
            faces += self._chain("mono", px, bold)
        faces += self._chain("latin", px, bold)
        faces += self._chain("cjk", px, bold)
        faces += self._chain("symbol", px, False)
        if not faces:
            faces = [_Face(ImageFont.load_default(), None)]
        fs = _FontSet(faces, px, linespace, ascent, f)
        self._sets[key] = fs
        return fs


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _rgb(root, color, default=None):
    """Tk colour name / #rgb -> (r, g, b) tuple; '' -> default."""
    if not color:
        return default
    try:
        r, g, b = root.winfo_rgb(color)
        return (r // 256, g // 256, b // 256)
    except tk.TclError:
        return default


def _intersect(a, b):
    if a is None or b is None:
        return None
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _padding(root, value):
    """Tk padding -> (left, top, right, bottom) in px."""
    if value in ("", None):
        return (0, 0, 0, 0)
    if isinstance(value, (int, float)):
        parts = [value]
    elif isinstance(value, str):
        parts = value.split()
    else:
        parts = list(value)
    px = []
    for p in parts:
        try:
            px.append(int(round(float(root.winfo_fpixels(str(p))))))
        except tk.TclError:
            px.append(0)
    if not px:
        return (0, 0, 0, 0)
    if len(px) == 1:
        return (px[0],) * 4
    if len(px) == 2:
        return (px[0], px[1], px[0], px[1])
    if len(px) == 3:
        return (px[0], px[1], px[2], px[1])
    return tuple(px[:4])


_NO_LINE_START = u"。，、；：？！）」』】》〉’”…‧·-"


def _is_cjk(ch):
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F


def _tokens(para):
    """Break opportunities the way Tk's macOS text layout (CoreText) sees them: at spaces, and
    between any two CJK characters. Latin words stay whole."""
    out, cur = [], ""
    for ch in para:
        if ch == " ":
            if cur:
                out.append(cur)
                cur = ""
            out.append(" ")
        elif _is_cjk(ch):
            if cur:
                out.append(cur)
                cur = ""
            out.append(ch)
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _wrap(fs, text, width):
    """Tk-like wrapping. Widths come from the Tk font (fs.measure) so the number of lines equals
    what Tk laid out; breaks happen at spaces and between CJK characters (closing punctuation
    hangs on to the previous line), and inside an over-long Latin word at character boundaries."""
    lines = []
    for para in text.split("\n"):
        if width <= 0 or fs.measure(para) <= width:
            lines.append(para)
            continue
        cur = ""
        for tok in _tokens(para):
            if tok == " ":
                if cur:
                    cur += " "
                continue
            cand = cur + tok
            if fs.measure(cand) <= width or (cur and tok in _NO_LINE_START):
                cur = cand
                continue
            if cur:
                lines.append(cur.rstrip(" "))
                cur = ""
            if fs.measure(tok) <= width:
                cur = tok
                continue
            piece = ""
            for ch in tok:
                if fs.measure(piece + ch) <= width or not piece:
                    piece += ch
                else:
                    lines.append(piece)
                    piece = ch
            cur = piece
        lines.append(cur.rstrip(" "))
    return lines


def _has(obj, name):
    """hasattr() that tolerates __getattr__ implementations raising non-AttributeError
    (matplotlib 3.4's Spines raises ValueError for unknown names)."""
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


def find_mpl_canvases(app, depth=2):
    """Collect FigureCanvasTkAgg instances reachable from the App object (spectro/sequence/analysis)."""
    found = {}
    seen = set()

    def visit(obj, d):
        if id(obj) in seen or d < 0:
            return
        seen.add(id(obj))
        try:
            items = list(vars(obj).values())
        except TypeError:
            return
        for v in items:
            if _has(v, "get_tk_widget") and _has(v, "figure"):
                try:
                    found[str(v.get_tk_widget())] = v
                except Exception:
                    pass
            elif d > 0 and _has(v, "__dict__") and not isinstance(v, type):
                visit(v, d - 1)
    visit(app, depth)
    return found


# ---------------------------------------------------------------------------------------------
# renderer
# ---------------------------------------------------------------------------------------------
class Renderer(object):
    def __init__(self, root, app=None, title_bar=True):
        if Image is None:
            raise RuntimeError("Pillow is required for ui_render")
        self.root = root
        self.app = app if app is not None else getattr(root, "app", None)
        self.style = ttk.Style(root)
        self.fonts = Fonts(root)
        self.title_bar = TITLE_BAR if title_bar else 0
        self.hidden = set()
        self.canvases = {}
        self.errors = []
        if self.app is None and hasattr(root, "pages"):
            self.app = root
        if self.app is not None:
            pages = getattr(self.app, "pages", None) or {}
            cur = getattr(self.app, "current_page", None)
            for k, p in pages.items():
                if k != cur:
                    self.hidden.add(str(p))
            self.canvases = find_mpl_canvases(self.app)

    # -- style helpers
    def _tcl_list(self, value):
        """Tk may hand back a Tcl list as one string or as a tuple; always return a list of str."""
        if value in ("", None):
            return []
        if isinstance(value, str):
            try:
                return [str(v) for v in self.root.tk.splitlist(value)]
            except tk.TclError:
                return [value]
        try:
            return [str(v) for v in value]
        except TypeError:
            return [str(value)]

    def _style_name(self, w):
        cls = w.winfo_class()
        try:
            sty = w.cget("style")
        except tk.TclError:
            sty = ""
        if not sty and cls in ("TProgressbar", "TScrollbar", "TSeparator", "TScale", "TPanedwindow"):
            try:
                orient = str(w.cget("orient"))
            except tk.TclError:
                orient = "horizontal"
            sty = ("Horizontal." if orient.startswith("h") else "Vertical.") + cls
        return sty or cls

    def _state(self, w):
        try:
            return list(w.state())
        except (tk.TclError, AttributeError):
            return []

    def _lookup(self, w, option, state=None, default=""):
        sty = self._style_name(w)
        try:
            v = self.style.lookup(sty, option, state if state is not None else self._state(w))
        except tk.TclError:
            v = ""
        return v if v not in ("", None) else default

    def _opt(self, w, name, default=""):
        try:
            v = w.cget(name)
        except tk.TclError:
            return default
        return v if v not in ("", None) else default

    def _text(self, w):
        tv = self._opt(w, "textvariable")
        if tv:
            try:
                return str(w.getvar(str(tv)))
            except tk.TclError:
                pass
        return str(self._opt(w, "text"))

    def _font(self, w, style_font=True):
        f = self._opt(w, "font")
        if not f and style_font:
            f = self._lookup(w, "font")
        return self.fonts.resolve(f)

    def _fg(self, w, default=None):
        c = self._opt(w, "foreground") or self._lookup(w, "foreground")
        return _rgb(self.root, c, default or _rgb(self.root, COLORS["text"]))

    def _bg(self, w, inherited):
        c = self._opt(w, "background") if w.winfo_class() in ("Label", "Frame", "Listbox", "Text", "Canvas", "Button", "Checkbutton", "Toplevel", "Tk", "Labelframe", "Scrollbar", "Entry", "Spinbox") else ""
        if not c:
            c = self._lookup(w, "background")
        return _rgb(self.root, c, inherited)

    # -- geometry
    def _rect(self, w):
        x = w.winfo_rootx() - self.root.winfo_rootx()
        y = w.winfo_rooty() - self.root.winfo_rooty() + self.title_bar
        return (x, y, x + w.winfo_width(), y + w.winfo_height())

    # -- entry point
    def render(self, path):
        root = self.root
        root.update_idletasks()
        W, H = root.winfo_width(), root.winfo_height()
        self.img = Image.new("RGBA", (W, H + self.title_bar), _rgb(root, COLORS["bg"]) + (255,))
        if self.title_bar:
            self._paint_title_bar(W)
        self._walk(root, (0, self.title_bar, W, H + self.title_bar), _rgb(root, COLORS["bg"]))
        out = self.img.convert("RGB")
        out.save(path)
        return path

    def _paint_title_bar(self, W):
        d = ImageDraw.Draw(self.img)
        d.rectangle((0, 0, W, self.title_bar), fill=(232, 232, 232, 255))
        d.line((0, self.title_bar - 1, W, self.title_bar - 1), fill=(200, 200, 200, 255))
        for i, col in enumerate(((255, 95, 87), (254, 188, 46), (40, 200, 64))):
            cx, cy, r = 20 + i * 20, self.title_bar // 2, 6
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col + (255,))
        fs = self.fonts.resolve(("", 13, "bold"))
        title = self.root.title()
        tw = fs.width(title)
        fs.draw(d, (W - tw) / 2.0, self.title_bar / 2.0, title, (77, 77, 77, 255))

    def _walk(self, w, clip, inherited_bg):
        try:
            if not w.winfo_viewable() and w is not self.root:
                return
        except tk.TclError:
            return
        if str(w) in self.hidden:
            return
        cls = w.winfo_class()
        if cls in ("Toplevel", "Menu") and w is not self.root:
            return
        rect = self._rect(w)
        vis = _intersect(rect, clip)
        if vis is None:
            return
        bg = self._paint(w, rect, vis, inherited_bg)
        for c in w.winfo_children():
            self._walk(c, vis, bg)

    def _paint(self, w, rect, vis, inherited_bg):
        """Paint one widget into a temp image and paste the clipped part; returns the bg colour for children."""
        cls = w.winfo_class()
        wd, ht = rect[2] - rect[0], rect[3] - rect[1]
        if wd <= 0 or ht <= 0:
            return inherited_bg
        tmp = Image.new("RGBA", (wd, ht), (0, 0, 0, 0))
        d = ImageDraw.Draw(tmp)
        handler = getattr(self, "_paint_" + cls.lower().lstrip("t") if cls.startswith("T") and cls not in ("Tk", "Text", "Toplevel", "Treeview") else "_paint_" + cls.lower(), None)
        if cls == "Treeview":
            handler = self._paint_treeview
        elif cls == "Tk" or cls == "Toplevel":
            handler = self._paint_frame
        elif cls == "Text":
            handler = self._paint_text
        if handler is None:
            handler = self._paint_frame
        bg = handler(w, d, tmp, wd, ht, inherited_bg)
        crop = tmp.crop((vis[0] - rect[0], vis[1] - rect[1], vis[2] - rect[0], vis[3] - rect[1]))
        self.img.paste(crop, (vis[0], vis[1]), crop)
        return bg

    # -- containers
    def _paint_frame(self, w, d, tmp, wd, ht, inherited):
        bg = self._bg(w, None)
        if bg is not None:
            d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,))
        relief = self._lookup(w, "relief") or self._opt(w, "relief")
        bw = self._lookup(w, "borderwidth") or self._opt(w, "borderwidth", 0)
        try:
            bw = int(float(bw))
        except (TypeError, ValueError):
            bw = 0
        if bw > 0 and relief in ("solid", "sunken", "raised", "groove", "ridge"):
            bc = _rgb(self.root, self._lookup(w, "bordercolor"), _rgb(self.root, COLORS["border"]))
            d.rectangle((0, 0, wd - 1, ht - 1), outline=bc + (255,), width=bw)
        cls = w.winfo_class()
        if cls in ("Labelframe", "TLabelframe"):
            text = self._text(w)
            if text:
                fs = self._font(w)
                fs.draw(d, 8, fs.linespace / 2.0, text, self._fg(w) + (255,))
        return bg if bg is not None else inherited

    _paint_labelframe = _paint_frame
    _paint_panedwindow = _paint_frame
    _paint_notebook = _paint_frame
    _paint_sizegrip = _paint_frame

    def _paint_canvas(self, w, d, tmp, wd, ht, inherited):
        key = str(w)
        mpl = self.canvases.get(key)
        if mpl is not None:
            try:
                mpl.draw()
                cw, chh = mpl.get_width_height()
                buf = mpl.buffer_rgba()
                im = Image.frombuffer("RGBA", (cw, chh), bytes(buf), "raw", "RGBA", 0, 1)
                if (cw, chh) != (wd, ht):
                    im = im.resize((wd, ht))
                tmp.paste(im, (0, 0))
                return inherited
            except Exception as e:
                self.errors.append("%s: %s: %s" % (key, type(e).__name__, e))
        bg = _rgb(self.root, self._opt(w, "background"), inherited)
        d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,))
        # plain canvas items (dots, lines) drawn by the theme widgets
        try:
            for item in w.find_all():
                typ = w.type(item)
                coords = [float(c) for c in w.coords(item)]
                fill = _rgb(self.root, w.itemcget(item, "fill"), None)
                if typ in ("oval", "rectangle") and len(coords) == 4:
                    outline = _rgb(self.root, w.itemcget(item, "outline"), None)
                    box = (coords[0], coords[1], coords[2], coords[3])
                    if typ == "oval":
                        d.ellipse(box, fill=fill + (255,) if fill else None, outline=outline + (255,) if outline else None)
                    else:
                        d.rectangle(box, fill=fill + (255,) if fill else None, outline=outline + (255,) if outline else None)
                elif typ == "line" and len(coords) >= 4 and fill:
                    d.line(coords, fill=fill + (255,), width=max(1, int(float(w.itemcget(item, "width") or 1))))
                elif typ == "text" and len(coords) == 2 and fill:
                    fs = self.fonts.resolve(w.itemcget(item, "font"))
                    fs.draw(d, coords[0], coords[1], w.itemcget(item, "text"), fill + (255,))
        except tk.TclError:
            pass
        return bg

    # -- text widgets
    def _draw_label_text(self, w, d, wd, ht, text, fs, fg, pad, anchor, justify, wraplength):
        l, t, r, b = pad
        inner_w = max(1, wd - l - r)
        inner_h = max(1, ht - t - b)
        lines = _wrap(fs, text, wraplength if wraplength > 0 else 0)
        lh = fs.linespace
        block_h = lh * len(lines)
        block_w = max([fs.width(s) for s in lines] or [0])
        anchor = anchor or "center"
        if "n" in anchor:
            y0 = t
        elif "s" in anchor:
            y0 = t + inner_h - block_h
        else:
            y0 = t + (inner_h - block_h) / 2.0
        if "w" in anchor:
            bx = l
        elif "e" in anchor:
            bx = l + inner_w - block_w
        else:
            bx = l + (inner_w - block_w) / 2.0
        for i, line in enumerate(lines):
            lw = fs.width(line)
            if justify == "right":
                x = bx + block_w - lw
            elif justify == "center":
                x = bx + (block_w - lw) / 2.0
            else:
                x = bx
            fs.draw(d, x, y0 + i * lh + lh / 2.0, line, fg + (255,))

    def _paint_label(self, w, d, tmp, wd, ht, inherited):
        bg = self._bg(w, None)
        if bg is not None:
            d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,))
        relief = self._opt(w, "relief") or self._lookup(w, "relief")
        bw = self._lookup(w, "borderwidth") or self._opt(w, "borderwidth", 0)
        try:
            bw = int(float(bw))
        except (TypeError, ValueError):
            bw = 0
        if bw > 0 and relief in ("solid", "sunken", "raised", "groove", "ridge"):
            bc = _rgb(self.root, self._lookup(w, "bordercolor"), _rgb(self.root, COLORS["border"]))
            d.rectangle((0, 0, wd - 1, ht - 1), outline=bc + (255,), width=bw)
        text = self._text(w)
        if text:
            fs = self._font(w)
            pad = _padding(self.root, self._opt(w, "padding") or self._lookup(w, "padding"))
            if w.winfo_class() == "Label":
                px_, py_ = int(self._opt(w, "padx", 0) or 0), int(self._opt(w, "pady", 0) or 0)
                pad = (px_ + 1, py_ + 1, px_ + 1, py_ + 1)
            try:
                wl = int(self.root.winfo_fpixels(str(self._opt(w, "wraplength", 0) or 0)))
            except tk.TclError:
                wl = 0
            self._draw_label_text(w, d, wd, ht, text, fs, self._fg(w), pad, str(self._opt(w, "anchor", "w") or "w"),
                                  str(self._opt(w, "justify", "left") or "left"), wl)
        return bg if bg is not None else inherited

    _paint_message = _paint_label

    def _paint_button(self, w, d, tmp, wd, ht, inherited):
        state = self._state(w)
        bg = _rgb(self.root, self._lookup(w, "background", state), _rgb(self.root, COLORS["field"]))
        fg = _rgb(self.root, self._lookup(w, "foreground", state), _rgb(self.root, COLORS["text"]))
        bc = _rgb(self.root, self._lookup(w, "bordercolor", state), bg)
        if w.winfo_class() == "Button":
            bg = _rgb(self.root, self._opt(w, "background"), bg)
            fg = _rgb(self.root, self._opt(w, "foreground"), fg)
        r = 6
        try:
            d.rounded_rectangle((0, 0, wd - 1, ht - 1), radius=r, fill=bg + (255,), outline=bc + (255,))
        except AttributeError:                       # very old Pillow
            d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,), outline=bc + (255,))
        text = self._text(w)
        if text:
            fs = self._font(w)
            pad = _padding(self.root, self._opt(w, "padding") or self._lookup(w, "padding"))
            self._draw_label_text(w, d, wd, ht, text, fs, fg, pad, "center", "center", 0)
        return bg

    _paint_menubutton = _paint_button

    def _paint_field(self, w, d, wd, ht, text, justify, arrow=None):
        state = self._state(w)
        bg = _rgb(self.root, self._lookup(w, "fieldbackground", state), _rgb(self.root, COLORS["field"]))
        bc = _rgb(self.root, self._lookup(w, "bordercolor", state), _rgb(self.root, COLORS["field_border"]))
        fg = _rgb(self.root, self._lookup(w, "foreground", state), _rgb(self.root, COLORS["text"]))
        if "focus" in state:
            bc = _rgb(self.root, COLORS["accent"])
        try:
            d.rounded_rectangle((0, 0, wd - 1, ht - 1), radius=5, fill=bg + (255,), outline=bc + (255,))
        except AttributeError:
            d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,), outline=bc + (255,))
        pad = _padding(self.root, self._lookup(w, "padding") or "6 4")
        right_reserved = 0
        if arrow == "combo":
            right_reserved = 18
            ax, ay = wd - 11, ht / 2.0
            d.line((ax - 4, ay - 2, ax, ay + 2, ax + 4, ay - 2), fill=_rgb(self.root, COLORS["text2"]) + (255,), width=1)
        elif arrow == "spin":
            right_reserved = 16
            ax, ay = wd - 9, ht / 2.0
            col = _rgb(self.root, COLORS["text2"]) + (255,)
            d.polygon((ax - 3, ay - 2, ax + 3, ay - 2, ax, ay - 6), fill=col)
            d.polygon((ax - 3, ay + 2, ax + 3, ay + 2, ax, ay + 6), fill=col)
        if text:
            fs = self._font(w)
            l, t, r, b = pad
            inner_w = wd - l - r - right_reserved
            tw = fs.width(text)
            x = l
            if justify == "right":
                x = l + inner_w - tw
            elif justify == "center":
                x = l + (inner_w - tw) / 2.0
            fs.draw(d, x, ht / 2.0, text, fg + (255,))
        return bg

    def _paint_entry(self, w, d, tmp, wd, ht, inherited):
        try:
            text = w.get()
        except tk.TclError:
            text = ""
        if self._opt(w, "show"):
            text = "•" * len(text)
        return self._paint_field(w, d, wd, ht, text, str(self._opt(w, "justify", "left") or "left"))

    def _paint_combobox(self, w, d, tmp, wd, ht, inherited):
        try:
            text = w.get()
        except tk.TclError:
            text = ""
        return self._paint_field(w, d, wd, ht, text, str(self._opt(w, "justify", "left") or "left"), arrow="combo")

    def _paint_spinbox(self, w, d, tmp, wd, ht, inherited):
        try:
            text = w.get()
        except tk.TclError:
            text = ""
        return self._paint_field(w, d, wd, ht, text, str(self._opt(w, "justify", "left") or "left"), arrow="spin")

    def _var_value(self, w):
        var = self._opt(w, "variable")
        if not var:
            return None
        try:
            return str(w.getvar(str(var)))
        except tk.TclError:
            return None

    def _paint_checkbutton(self, w, d, tmp, wd, ht, inherited):
        bg = self._bg(w, None)
        if bg is not None:
            d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,))
        state = self._state(w)
        val = self._var_value(w)
        checked = val is not None and val == str(self._opt(w, "onvalue", "1"))
        fs = self._font(w)
        pad = _padding(self.root, self._opt(w, "padding") or self._lookup(w, "padding"))
        size = 14
        cy = ht / 2.0
        x0 = pad[0]
        y0 = cy - size / 2.0
        border = _rgb(self.root, COLORS["field_border"])
        fill = _rgb(self.root, COLORS["accent"]) if checked else _rgb(self.root, COLORS["field"])
        if "disabled" in state:
            fill = _rgb(self.root, COLORS["accent_disabled"]) if checked else fill
        try:
            d.rounded_rectangle((x0, y0, x0 + size, y0 + size), radius=3, fill=fill + (255,), outline=(fill if checked else border) + (255,))
        except AttributeError:
            d.rectangle((x0, y0, x0 + size, y0 + size), fill=fill + (255,), outline=border + (255,))
        if checked:
            d.line((x0 + 3.5, cy, x0 + 6, cy + 3, x0 + 10.5, cy - 3.5), fill=(255, 255, 255, 255), width=2)
        text = self._text(w)
        if text:
            fg = _rgb(self.root, self._lookup(w, "foreground", state), _rgb(self.root, COLORS["text"]))
            fs.draw(d, x0 + size + 7, cy, text, fg + (255,))
        return bg if bg is not None else inherited

    def _paint_radiobutton(self, w, d, tmp, wd, ht, inherited):
        bg = self._bg(w, None)
        if bg is not None:
            d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,))
        val = self._var_value(w)
        selected = val is not None and val == str(self._opt(w, "value", ""))
        fs = self._font(w)
        pad = _padding(self.root, self._opt(w, "padding") or self._lookup(w, "padding"))
        size = 14
        cy = ht / 2.0
        x0 = pad[0]
        y0 = cy - size / 2.0
        accent = _rgb(self.root, COLORS["accent"])
        d.ellipse((x0, y0, x0 + size, y0 + size), fill=(accent if selected else _rgb(self.root, COLORS["field"])) + (255,),
                  outline=(accent if selected else _rgb(self.root, COLORS["field_border"])) + (255,))
        if selected:
            d.ellipse((x0 + 4.5, y0 + 4.5, x0 + size - 4.5, y0 + size - 4.5), fill=(255, 255, 255, 255))
        text = self._text(w)
        if text:
            fs.draw(d, x0 + size + 7, cy, text, self._fg(w) + (255,))
        return bg if bg is not None else inherited

    def _paint_separator(self, w, d, tmp, wd, ht, inherited):
        col = _rgb(self.root, self._lookup(w, "background"), _rgb(self.root, COLORS["sep"]))
        d.rectangle((0, 0, wd - 1, ht - 1), fill=col + (255,))
        return inherited

    def _paint_progressbar(self, w, d, tmp, wd, ht, inherited):
        trough = _rgb(self.root, self._lookup(w, "troughcolor"), _rgb(self.root, COLORS["border"]))
        bar = _rgb(self.root, self._lookup(w, "background"), _rgb(self.root, COLORS["accent"]))
        r = min(wd, ht) / 2.0
        try:
            d.rounded_rectangle((0, 0, wd - 1, ht - 1), radius=r, fill=trough + (255,))
        except AttributeError:
            d.rectangle((0, 0, wd - 1, ht - 1), fill=trough + (255,))
        try:
            value, maximum = float(w["value"]), float(w["maximum"])
        except (tk.TclError, ValueError):
            value, maximum = 0.0, 1.0
        mode = str(self._opt(w, "mode", "determinate"))
        if mode == "indeterminate":
            x0, x1 = wd * 0.35, wd * 0.65
        else:
            frac = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
            x0, x1 = 0, wd * frac
        if x1 - x0 >= 2:
            try:
                d.rounded_rectangle((x0, 0, x1 - 1, ht - 1), radius=r, fill=bar + (255,))
            except AttributeError:
                d.rectangle((x0, 0, x1 - 1, ht - 1), fill=bar + (255,))
        return inherited

    def _paint_scrollbar(self, w, d, tmp, wd, ht, inherited):
        trough = _rgb(self.root, self._lookup(w, "troughcolor"), inherited or _rgb(self.root, COLORS["card"]))
        thumb = _rgb(self.root, self._lookup(w, "background"), _rgb(self.root, COLORS["thumb"]))
        d.rectangle((0, 0, wd - 1, ht - 1), fill=trough + (255,))
        try:
            first, last = w.get()[:2]
        except (tk.TclError, ValueError, IndexError):
            first, last = 0.0, 1.0
        if last - first < 0.999:
            vertical = str(self._opt(w, "orient", "vertical")).startswith("v")
            if vertical:
                y0, y1 = ht * first, ht * last
                box = (2, y0, wd - 3, max(y0 + 8, y1))
            else:
                x0, x1 = wd * first, wd * last
                box = (x0, 2, max(x0 + 8, x1), ht - 3)
            try:
                d.rounded_rectangle(box, radius=3, fill=thumb + (255,))
            except AttributeError:
                d.rectangle(box, fill=thumb + (255,))
        return inherited

    def _paint_scale(self, w, d, tmp, wd, ht, inherited):
        trough = _rgb(self.root, self._lookup(w, "troughcolor"), _rgb(self.root, COLORS["border"]))
        d.rounded_rectangle((0, ht / 2.0 - 2, wd - 1, ht / 2.0 + 2), radius=2, fill=trough + (255,))
        try:
            v = float(w.get())
            lo, hi = float(w["from"]), float(w["to"])
            frac = 0.0 if hi == lo else (v - lo) / (hi - lo)
        except (tk.TclError, ValueError):
            frac = 0.0
        cx = 8 + (wd - 16) * max(0.0, min(1.0, frac))
        d.ellipse((cx - 8, ht / 2.0 - 8, cx + 8, ht / 2.0 + 8), fill=(255, 255, 255, 255), outline=_rgb(self.root, COLORS["field_border"]) + (255,))
        return inherited

    def _paint_listbox(self, w, d, tmp, wd, ht, inherited):
        bg = _rgb(self.root, self._opt(w, "background"), _rgb(self.root, COLORS["field"]))
        fg = _rgb(self.root, self._opt(w, "foreground"), _rgb(self.root, COLORS["text"]))
        selbg = _rgb(self.root, self._opt(w, "selectbackground"), _rgb(self.root, COLORS["row_sel"]))
        selfg = _rgb(self.root, self._opt(w, "selectforeground"), fg)
        d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,))
        fs = self._font(w, style_font=False)
        try:
            selected = set(int(i) for i in w.curselection())
        except tk.TclError:
            selected = set()
        try:
            n = w.size()
        except tk.TclError:
            n = 0
        for i in range(n):
            bb = w.bbox(i)
            if not bb:
                continue
            x, y, tw, th = bb
            row_h = max(th, fs.linespace)
            if i in selected:
                d.rectangle((0, y - 1, wd - 1, y + row_h), fill=selbg + (255,))
            item_fg = _rgb(self.root, w.itemcget(i, "foreground"), None)
            item_bg = _rgb(self.root, w.itemcget(i, "background"), None)
            if item_bg is not None and i not in selected:
                d.rectangle((0, y - 1, wd - 1, y + row_h), fill=item_bg + (255,))
            col = selfg if i in selected else (item_fg or fg)
            fs.draw(d, x, y + row_h / 2.0, w.get(i), col + (255,))
        return bg

    def _paint_text(self, w, d, tmp, wd, ht, inherited):
        bg = _rgb(self.root, self._opt(w, "background"), _rgb(self.root, COLORS["field"]))
        fg = _rgb(self.root, self._opt(w, "foreground"), _rgb(self.root, COLORS["text"]))
        d.rectangle((0, 0, wd - 1, ht - 1), fill=bg + (255,))
        fs = self._font(w, style_font=False)
        tag_fg = {}
        try:
            for tag in w.tag_names():
                c = w.tag_cget(tag, "foreground")
                if c:
                    tag_fg[tag] = _rgb(self.root, c, fg)
        except tk.TclError:
            pass
        try:
            idx = w.index("@0,0")
            guard = 0
            while guard < 5000:
                guard += 1
                info = w.dlineinfo(idx)
                if info is None:
                    break
                x, y, lw, lh, baseline = info
                end = w.index("%s display lineend" % idx)
                line = w.get(idx, end)
                col = fg
                for tag in w.tag_names(idx):
                    if tag in tag_fg:
                        col = tag_fg[tag]
                fs.draw(d, x, y + baseline, line, col + (255,), anchor="ls")
                nxt = w.index("%s + 1 display lines" % idx)
                if w.compare(nxt, "<=", idx) or w.compare(nxt, ">=", "end"):
                    break
                idx = nxt
        except tk.TclError:
            pass
        return bg

    def _paint_treeview(self, w, d, tmp, wd, ht, inherited):
        state = self._state(w)
        field = _rgb(self.root, self._lookup(w, "fieldbackground", state), _rgb(self.root, COLORS["card"]))
        fg = _rgb(self.root, self._lookup(w, "foreground", state), _rgb(self.root, COLORS["text"]))
        d.rectangle((0, 0, wd - 1, ht - 1), fill=field + (255,))
        fs = self._font(w)
        try:
            rowh = int(self.style.lookup(self._style_name(w), "rowheight") or fs.linespace + 6)
        except (tk.TclError, ValueError):
            rowh = fs.linespace + 6
        show = " ".join(self._tcl_list(self._opt(w, "show", "tree headings")))
        cols = []
        if "tree" in show:
            cols.append("#0")
        disp = self._tcl_list(self._opt(w, "displaycolumns", "#all"))
        if not disp or disp == ["#all"]:
            cols += self._tcl_list(self._opt(w, "columns", ()))
        else:
            cols += disp
        widths = []
        for c in cols:
            try:
                widths.append(int(w.column(c, "width")))
            except tk.TclError:
                widths.append(80)
        anchors = []
        for c in cols:
            try:
                anchors.append(str(w.column(c, "anchor")) or "w")
            except tk.TclError:
                anchors.append("w")
        y = 0
        if "headings" in show:
            hfs = self.fonts.resolve(self.style.lookup("Heading", "font") or "TkHeadingFont")
            hpad = _padding(self.root, self.style.lookup("Heading", "padding") or 3)
            hh = hfs.linespace + hpad[1] + hpad[3] + 2
            hbg = _rgb(self.root, self.style.lookup("Heading", "background"), _rgb(self.root, COLORS["bg"]))
            hfg = _rgb(self.root, self.style.lookup("Heading", "foreground"), fg)
            sep = _rgb(self.root, COLORS["sep"])
            d.rectangle((0, 0, wd - 1, hh - 1), fill=hbg + (255,))
            x = 0
            for c, cw, an in zip(cols, widths, anchors):
                try:
                    text = str(w.heading(c, "text"))
                    han = str(w.heading(c, "anchor") or an)
                except tk.TclError:
                    text, han = "", an
                self._cell(d, hfs, x, 0, cw, hh, text, hfg, han)
                x += cw
                d.line((x - 1, 2, x - 1, hh - 3), fill=sep + (255,))
            d.line((0, hh - 1, wd, hh - 1), fill=sep + (255,))
            y = hh
        # rows
        items = self._tree_rows(w, "")
        n = len(items)
        try:
            first = int(round(w.yview()[0] * n))
        except (tk.TclError, ValueError):
            first = 0
        try:
            selected = set(w.selection())
        except tk.TclError:
            selected = set()
        tag_cache = {}
        for iid, depth in items[first:]:
            if y >= ht:
                break
            row_bg = None
            row_fg = fg
            try:
                tags = self._tcl_list(w.item(iid, "tags"))
            except tk.TclError:
                tags = []
            for tag in tags:
                if tag not in tag_cache:
                    try:
                        tag_cache[tag] = (_rgb(self.root, w.tag_configure(tag, "background"), None), _rgb(self.root, w.tag_configure(tag, "foreground"), None))
                    except tk.TclError:
                        tag_cache[tag] = (None, None)
                tb, tf = tag_cache[tag]
                row_bg = tb or row_bg
                row_fg = tf or row_fg
            if iid in selected:
                row_bg = _rgb(self.root, COLORS["row_sel"])
            if row_bg is not None:
                d.rectangle((0, y, wd - 1, y + rowh - 1), fill=row_bg + (255,))
            try:
                values = self._tcl_list(w.item(iid, "values"))
                text0 = str(w.item(iid, "text") or "")
            except tk.TclError:
                values, text0 = [], ""
            x = 0
            vi = 0
            for c, cw, an in zip(cols, widths, anchors):
                if c == "#0":
                    self._cell(d, fs, x + 14 * depth + 4, y, cw - 14 * depth - 4, rowh, text0, row_fg, an)
                else:
                    self._cell(d, fs, x, y, cw, rowh, values[vi] if vi < len(values) else "", row_fg, an)
                    vi += 1
                x += cw
            y += rowh
        return field

    def _tree_rows(self, w, parent, depth=0):
        out = []
        try:
            for iid in w.get_children(parent):
                out.append((iid, depth))
                try:
                    is_open = w.item(iid, "open") in (True, 1, "1", "true")
                except tk.TclError:
                    is_open = False
                if is_open:
                    out += self._tree_rows(w, iid, depth + 1)
        except tk.TclError:
            pass
        return out

    def _cell(self, d, fs, x, y, cw, rh, text, fg, anchor):
        pad = 6
        if not text:
            return
        # clip the text to the column
        avail = cw - 2 * pad
        if avail <= 4:
            return
        s = text
        while s and fs.width(s) > avail:
            s = s[:-1]
        tw = fs.width(s)
        if "e" in anchor and anchor != "center":
            tx = x + cw - pad - tw
        elif anchor == "center" or anchor == "n" or anchor == "s":
            tx = x + (cw - tw) / 2.0
        else:
            tx = x + pad
        fs.draw(d, tx, y + rh / 2.0, s, fg + (255,))


def render_window(root, path, app=None, title_bar=True):
    """Paint the root window into a PNG at `path` (see module docstring). Returns path."""
    return Renderer(root, app=app, title_bar=title_bar).render(path)


# ---------------------------------------------------------------------------------------------
# layout audit
# ---------------------------------------------------------------------------------------------
def audit_layout(root, app=None, tolerance=1):
    """Return a list of {"widget", "class", "kind", "detail"} for viewable leaf widgets whose
    allocation is smaller than their requested size ("clipped") or that extend beyond their parent
    ("overflow"). Pages that are stacked under the current page are skipped when `app` (or root.app)
    exposes `pages` / `current_page`."""
    app = app if app is not None else getattr(root, "app", None)
    if app is None and hasattr(root, "pages"):
        app = root
    hidden = set()
    if app is not None:
        cur = getattr(app, "current_page", None)
        for k, p in (getattr(app, "pages", None) or {}).items():
            if k != cur:
                hidden.add(str(p))
    problems = []

    def walk(w):
        try:
            if w is not root and not w.winfo_viewable():
                return
        except tk.TclError:
            return
        if str(w) in hidden:
            return
        cls = w.winfo_class()
        if cls in ("Toplevel", "Menu") and w is not root:
            return
        if cls in LEAF:
            rw, rh = w.winfo_reqwidth(), w.winfo_reqheight()
            aw, ah = w.winfo_width(), w.winfo_height()
            if aw + tolerance < rw or ah + tolerance < rh:
                problems.append({"widget": str(w), "class": cls, "kind": "clipped",
                                 "detail": "requested %dx%d, allocated %dx%d" % (rw, rh, aw, ah)})
            try:
                parent = w.nametowidget(w.winfo_parent())
            except (tk.TclError, KeyError):
                parent = None
            if parent is not None:
                x = w.winfo_rootx() - parent.winfo_rootx()
                y = w.winfo_rooty() - parent.winfo_rooty()
                if x < -tolerance or y < -tolerance or x + aw > parent.winfo_width() + tolerance or y + ah > parent.winfo_height() + tolerance:
                    problems.append({"widget": str(w), "class": cls, "kind": "overflow",
                                     "detail": "at %d,%d size %dx%d in parent %dx%d" % (x, y, aw, ah, parent.winfo_width(), parent.winfo_height())})
        for c in w.winfo_children():
            walk(c)

    root.update_idletasks()
    walk(root)
    return problems
