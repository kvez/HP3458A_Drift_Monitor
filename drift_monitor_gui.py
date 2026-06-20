"""
HP 3458A Drift Monitor – GUI
==============================
Grafikus drift megfigyelő alkalmazás.
Tkinter alapú, önálló exe-be buildelhető (nincs külső függőség).

Ciklus (óránként):
  TEMP? → ACAL DCV → 5 perc várakozás → CALVAL?72 + CALVAL?175 + TEMP? → CSV

Futtatás:
  C:\Python311\python.exe drift_monitor_gui.py

Build:
  C:\Python311\python.exe -m PyInstaller drift_monitor_gui.spec
"""

import ctypes
import csv
import os
import queue
import sys
import threading
import time
from datetime import datetime
from enum import Enum, auto
from tkinter import ttk
import tkinter as tk
import tkinter.font as tkfont


# ════════════════════════════════════════════════════════════════
#  Konfiguráció
# ════════════════════════════════════════════════════════════════

GPIB_BOARD   = 0
GPIB_ADDR    = 22
ACAL_WAIT_S  = 300      # 5 perc várakozás ACAL után
INTERVAL_S   = 3600     # 1 órás mérési intervallum
CSV_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drift_log.csv")
CSV_HEADER   = ["timestamp", "temp_before_C", "temp_after_C", "cal72", "cal175"]
HISTORY_MAX  = 50       # ennyi sort tárolunk a nézetben


# ════════════════════════════════════════════════════════════════
#  NI-488.2 GPIB réteg
# ════════════════════════════════════════════════════════════════

NI4882_DLL = r"C:\Windows\System32\ni4882.dll"
T100s, T10s, ERR = 15, 13, 0x8000

try:
    _lib = ctypes.WinDLL(NI4882_DLL)
    _lib.ibdev.restype  = ctypes.c_int
    _lib.ibdev.argtypes = [ctypes.c_int] * 6
    _lib.ibwrt.restype  = ctypes.c_int
    _lib.ibwrt.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_long]
    _lib.ibrd.restype   = ctypes.c_int
    _lib.ibrd.argtypes  = [ctypes.c_int, ctypes.c_char_p, ctypes.c_long]
    _lib.ibonl.restype  = ctypes.c_int
    _lib.ibonl.argtypes = [ctypes.c_int, ctypes.c_int]
    DLL_OK = True
except OSError as e:
    DLL_OK = False
    _lib   = None

_gpib_lock = threading.Lock()


def _gpib_open(board, addr, tmo=T100s):
    ud = _lib.ibdev(board, addr, 0, tmo, 1, 0)
    if ud < 0:
        raise OSError(f"ibdev hiba (board={board}, addr={addr})")
    return ud

def _gpib_close(ud):
    _lib.ibonl(ud, 0)

def _gpib_write(ud, cmd):
    if not cmd.endswith('\n'):
        cmd += '\n'
    data = cmd.encode('ascii')
    sta = _lib.ibwrt(ud, data, len(data))
    if sta & ERR:
        raise OSError(f"ibwrt hiba 0x{sta:04X}: {cmd!r}")

def _gpib_read(ud, maxbytes=4096):
    buf = ctypes.create_string_buffer(maxbytes)
    sta = _lib.ibrd(ud, buf, maxbytes)
    if sta & ERR:
        raise OSError(f"ibrd hiba 0x{sta:04X}")
    return buf.raw.rstrip(b'\x00').decode('ascii', errors='replace').strip()

def _query(ud, cmd):
    _gpib_write(ud, cmd)
    return _gpib_read(ud)


# ════════════════════════════════════════════════════════════════
#  Mérési logika (háttérszál)
# ════════════════════════════════════════════════════════════════

class CycleState(Enum):
    IDLE        = auto()
    TEMP_BEFORE = auto()
    ACAL        = auto()
    WAITING     = auto()
    CAL_READ    = auto()
    SAVING      = auto()
    SLEEPING    = auto()
    ERROR       = auto()

# Üzenet típusok a háttérszálból a GUI felé
class Msg:
    def __init__(self, kind, **kw):
        self.kind = kind      # str: state/log/row/countdown/error
        self.__dict__.update(kw)


class MonitorThread(threading.Thread):
    """
    Háttérszál, amely elvégzi a GPIB kommunikációt és
    queue-n keresztül értesíti a GUI-t.
    """
    def __init__(self, q: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.q     = q
        self.stop  = stop_event
        self.cycle = 0

    def _put(self, kind, **kw):
        self.q.put(Msg(kind, **kw))

    def _log(self, text):
        self._put("log", text=f"[{datetime.now().strftime('%H:%M:%S')}]  {text}")

    def _state(self, state: CycleState, detail=""):
        self._put("state", state=state, detail=detail)

    def _countdown(self, remaining: int, total: int):
        self._put("countdown", remaining=remaining, total=total)

    def _wait(self, seconds: int, label: str = "") -> bool:
        """Interruptálható várakozás. False-t ad ha le kell állni."""
        total = seconds
        while seconds > 0 and not self.stop.is_set():
            self._countdown(seconds, total)
            time.sleep(min(1, seconds))
            seconds -= 1
        self._countdown(0, total)
        return not self.stop.is_set()

    def _gpib_session(self, tmo=T100s):
        """Context manager: GPIB megnyitás/zárás a lock-kal együtt."""
        return _GpibSession(tmo)

    def run(self):
        while not self.stop.is_set():
            self.cycle += 1
            self._state(CycleState.TEMP_BEFORE, f"#{self.cycle}. ciklus")
            self._log(f"─── #{self.cycle}. mérési ciklus ───")

            try:
                # 1. TEMP? (ACAL előtt)
                self._log("TEMP? lekérdezés (ACAL előtt)...")
                with _gpib_lock:
                    ud = _gpib_open(GPIB_BOARD, GPIB_ADDR, T100s)
                    try:
                        _gpib_write(ud, "END ALWAYS")
                        temp_before = _query(ud, "TEMP?")
                    finally:
                        _gpib_close(ud)

                self._log(f"Hőmérséklet (előtte): {temp_before} °C")
                self._put("live", key="temp_before", value=f"{temp_before} °C")

                if self.stop.is_set():
                    break

                # 2–4. ACAL DCV + várakozás + CAL kiolvasás (egy kapcsolaton belül)
                # A kapcsolatot nyitva tartjuk az ACAL teljes ideje alatt,
                # mert ibonl(ud,0) GTL jelet küld, ami megszakíthatja az ACAL-t.
                self._state(CycleState.ACAL, "ACAL DCV fut...")
                self._log("ACAL DCV küldése...")
                with _gpib_lock:
                    ud = _gpib_open(GPIB_BOARD, GPIB_ADDR, T100s)
                    try:
                        _gpib_write(ud, "END ALWAYS")
                        _gpib_write(ud, "ACAL DCV")
                    except Exception:
                        _gpib_close(ud)
                        raise
                self._log("ACAL DCV elküldve (kapcsolat nyitva marad).")

                # 3. Várakozás (kapcsolat nyitva)
                self._state(CycleState.WAITING, f"{ACAL_WAIT_S}s várakozás")
                self._log(f"Várakozás {ACAL_WAIT_S}s (ACAL befejezéséig)...")
                if not self._wait(ACAL_WAIT_S):
                    with _gpib_lock:
                        _gpib_close(ud)
                    break

                # 4. CAL értékek + TEMP kiolvasása (ugyanazon a kapcsolaton)
                self._state(CycleState.CAL_READ, "CAL értékek kiolvasása...")
                self._log("CALVAL? 72, CALVAL? 175, TEMP? kiolvasása...")
                with _gpib_lock:
                    try:
                        cal72      = _query(ud, "CAL? 72")
                        cal175     = _query(ud, "CAL? 175")
                        temp_after = _query(ud, "TEMP?")
                    finally:
                        _gpib_close(ud)

                self._log(f"CAL72  : {cal72}")
                self._log(f"CAL175 : {cal175}")
                self._log(f"Hőmérséklet (utána): {temp_after} °C")

                for key, val in [("cal72", cal72), ("cal175", cal175),
                                  ("temp_after", f"{temp_after} °C")]:
                    self._put("live", key=key, value=val)

                # 5. CSV mentés
                self._state(CycleState.SAVING)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                row = {
                    "timestamp":     ts,
                    "temp_before_C": temp_before,
                    "temp_after_C":  temp_after,
                    "cal72":         cal72,
                    "cal175":        cal175,
                }
                _csv_append(row)
                self._put("row", row=row)
                self._log(f"CSV sor mentve: {ts}")

            except Exception as exc:
                self._state(CycleState.ERROR, str(exc))
                self._log(f"HIBA: {exc}")
                # Hibás ciklus után is folytatjuk (várakozunk)

            # 6. Alvás a következő ciklusig
            if self.stop.is_set():
                break
            self._state(CycleState.SLEEPING, f"Következő ciklus {INTERVAL_S // 60} perc múlva")
            self._log(f"Alvás {INTERVAL_S}s...")
            if not self._wait(INTERVAL_S):
                break

        self._state(CycleState.IDLE, "Leállítva")
        self._log("Monitor leállítva.")


class _GpibSession:
    """Segédosztály (nem használt, de megtartjuk bővíthetőséghez)."""
    pass


# ════════════════════════════════════════════════════════════════
#  CSV kezelés
# ════════════════════════════════════════════════════════════════

def _csv_ensure_header():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)

def _csv_append(row: dict):
    _csv_ensure_header()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_HEADER).writerow(row)

def _csv_load_history() -> list[dict]:
    """Betölti az utolsó HISTORY_MAX sort a CSV-ből."""
    if not os.path.exists(CSV_FILE):
        return []
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return rows[-HISTORY_MAX:]
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════
#  GUI alkalmazás
# ════════════════════════════════════════════════════════════════

# Szín paletta
C = {
    "bg":       "#1e1e2e",
    "bg2":      "#2a2a3e",
    "bg3":      "#313145",
    "accent":   "#7aa2f7",
    "green":    "#9ece6a",
    "yellow":   "#e0af68",
    "red":      "#f7768e",
    "cyan":     "#7dcfff",
    "text":     "#cdd6f4",
    "muted":    "#6c7086",
    "border":   "#414168",
}

STATE_META = {
    CycleState.IDLE:        ("●", C["muted"],  "Várakozás"),
    CycleState.TEMP_BEFORE: ("●", C["cyan"],   "Hőmérséklet olvasás"),
    CycleState.ACAL:        ("●", C["yellow"], "ACAL DCV fut"),
    CycleState.WAITING:     ("●", C["accent"], "Várakozás (ACAL után)"),
    CycleState.CAL_READ:    ("●", C["cyan"],   "CAL értékek kiolvasása"),
    CycleState.SAVING:      ("●", C["green"],  "CSV mentés"),
    CycleState.SLEEPING:    ("●", C["muted"],  "Alvás (következő ciklusig)"),
    CycleState.ERROR:       ("●", C["red"],    "HIBA"),
}


class DriftMonitorApp:
    def __init__(self, root: tk.Tk):
        self.root  = root
        self.q     = queue.Queue()
        self._stop = threading.Event()
        self._thread: MonitorThread | None = None
        self._running = False

        self._build_ui()
        self._load_history()
        self._poll()

    # ── UI építés ────────────────────────────────────────────────

    def _build_ui(self):
        root = self.root
        root.title("HP 3458A – Drift Monitor")
        root.configure(bg=C["bg"])
        root.resizable(True, True)
        root.minsize(780, 580)

        # Betűtípusok
        fam = "Consolas"
        self.fn      = tkfont.Font(family=fam, size=10)
        self.fn_bold = tkfont.Font(family=fam, size=10, weight="bold")
        self.fn_lg   = tkfont.Font(family=fam, size=14, weight="bold")
        self.fn_xl   = tkfont.Font(family=fam, size=22, weight="bold")
        self.fn_sm   = tkfont.Font(family=fam, size=9)

        # ttk stílus
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=C["bg"], foreground=C["text"],
                         font=self.fn, bordercolor=C["border"],
                         troughcolor=C["bg2"], fieldbackground=C["bg2"])
        style.configure("Treeview",
                         background=C["bg2"], foreground=C["text"],
                         fieldbackground=C["bg2"], rowheight=22,
                         bordercolor=C["border"])
        style.configure("Treeview.Heading",
                         background=C["bg3"], foreground=C["accent"],
                         font=self.fn_bold, bordercolor=C["border"])
        style.map("Treeview", background=[("selected", C["accent"])],
                  foreground=[("selected", C["bg"])])
        style.configure("TScrollbar",
                         background=C["bg3"], troughcolor=C["bg2"],
                         bordercolor=C["border"], arrowcolor=C["muted"])
        style.configure("prog.Horizontal.TProgressbar",
                         troughcolor=C["bg3"], background=C["accent"],
                         bordercolor=C["border"])

        # ── Főkeret ──
        main = tk.Frame(root, bg=C["bg"])
        main.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        # ── Fejléc ──
        hdr = tk.Frame(main, bg=C["bg"])
        hdr.pack(fill=tk.X, pady=(0, 8))
        tk.Label(hdr, text="HP 3458A  ·  Drift Monitor",
                 font=self.fn_lg, bg=C["bg"], fg=C["accent"]).pack(side=tk.LEFT)
        tk.Label(hdr, text=f"GPIB{GPIB_BOARD}:{GPIB_ADDR}",
                 font=self.fn, bg=C["bg"], fg=C["muted"]).pack(side=tk.LEFT, padx=12)

        self._dll_lbl = tk.Label(hdr, font=self.fn_sm, bg=C["bg"],
                                  fg=C["green"] if DLL_OK else C["red"],
                                  text="ni4882.dll  OK" if DLL_OK else "ni4882.dll  HIÁNYZIK")
        self._dll_lbl.pack(side=tk.RIGHT)

        # ── Start/Stop gomb ──
        self._btn = tk.Button(hdr, text="▶  Start", font=self.fn_bold,
                               bg=C["green"], fg=C["bg"], relief=tk.FLAT,
                               activebackground=C["accent"], activeforeground=C["bg"],
                               padx=14, pady=4,
                               command=self._toggle,
                               state=tk.NORMAL if DLL_OK else tk.DISABLED)
        self._btn.pack(side=tk.RIGHT, padx=(0, 10))

        # ── Állapot + countdown sor ──
        status_row = tk.Frame(main, bg=C["bg2"], bd=0,
                               highlightthickness=1, highlightbackground=C["border"])
        status_row.pack(fill=tk.X, pady=(0, 8))

        left = tk.Frame(status_row, bg=C["bg2"])
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=8)

        self._state_dot = tk.Label(left, text="●", font=self.fn_lg,
                                    bg=C["bg2"], fg=C["muted"])
        self._state_dot.pack(side=tk.LEFT)

        state_txt = tk.Frame(left, bg=C["bg2"])
        state_txt.pack(side=tk.LEFT, padx=8)
        self._state_lbl = tk.Label(state_txt, text="Várakozás",
                                    font=self.fn_bold, bg=C["bg2"], fg=C["text"])
        self._state_lbl.pack(anchor=tk.W)
        self._state_detail = tk.Label(state_txt, text="Nyomja a Start gombot",
                                       font=self.fn_sm, bg=C["bg2"], fg=C["muted"])
        self._state_detail.pack(anchor=tk.W)

        right = tk.Frame(status_row, bg=C["bg2"])
        right.pack(side=tk.RIGHT, padx=14, pady=8)
        self._countdown_lbl = tk.Label(right, text="--:--",
                                        font=self.fn_xl, bg=C["bg2"], fg=C["accent"])
        self._countdown_lbl.pack()
        tk.Label(right, text="hátramaradó idő",
                 font=self.fn_sm, bg=C["bg2"], fg=C["muted"]).pack()

        self._progress = ttk.Progressbar(main, style="prog.Horizontal.TProgressbar",
                                          orient=tk.HORIZONTAL, length=100, mode="determinate")
        self._progress.pack(fill=tk.X, pady=(0, 8))

        # ── Aktuális értékek ──
        vals_frame = tk.Frame(main, bg=C["bg"])
        vals_frame.pack(fill=tk.X, pady=(0, 8))

        self._live_vars: dict[str, tk.StringVar] = {}
        fields = [
            ("temp_before", "Hőmérséklet előtte"),
            ("temp_after",  "Hőmérséklet utána"),
            ("cal72",       "CAL 72"),
            ("cal175",      "CAL 175"),
        ]
        for i, (key, label) in enumerate(fields):
            var = tk.StringVar(value="—")
            self._live_vars[key] = var
            cell = tk.Frame(vals_frame, bg=C["bg2"],
                             highlightthickness=1, highlightbackground=C["border"])
            cell.grid(row=0, column=i, padx=4, sticky=tk.NSEW)
            vals_frame.columnconfigure(i, weight=1)
            tk.Label(cell, text=label, font=self.fn_sm,
                     bg=C["bg2"], fg=C["muted"]).pack(anchor=tk.W, padx=8, pady=(6, 0))
            tk.Label(cell, textvariable=var, font=self.fn_bold,
                     bg=C["bg2"], fg=C["cyan"]).pack(anchor=tk.W, padx=8, pady=(0, 6))

        # ── Előzmények tábla ──
        hist_hdr = tk.Frame(main, bg=C["bg"])
        hist_hdr.pack(fill=tk.X, pady=(4, 2))
        tk.Label(hist_hdr, text="Előzmények", font=self.fn_bold,
                 bg=C["bg"], fg=C["text"]).pack(side=tk.LEFT)
        tk.Label(hist_hdr, text=f"CSV: {CSV_FILE}", font=self.fn_sm,
                 bg=C["bg"], fg=C["muted"]).pack(side=tk.RIGHT)

        tree_frame = tk.Frame(main, bg=C["bg"])
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("timestamp", "temp_before_C", "temp_after_C", "cal72", "cal175")
        col_labels = ("Időbélyeg", "Temp előtte (°C)", "Temp utána (°C)", "CAL 72", "CAL 175")
        col_widths  = (155, 120, 120, 160, 160)

        self._tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                   selectmode="browse")
        for col, lbl, w in zip(cols, col_labels, col_widths):
            self._tree.heading(col, text=lbl)
            self._tree.column(col, width=w, minwidth=80, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                             command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Log panel ──
        log_frame = tk.Frame(main, bg=C["bg"])
        log_frame.pack(fill=tk.X, pady=(8, 0))
        tk.Label(log_frame, text="Napló", font=self.fn_bold,
                 bg=C["bg"], fg=C["text"]).pack(anchor=tk.W)
        self._log_text = tk.Text(log_frame, height=5, font=self.fn_sm,
                                  bg=C["bg2"], fg=C["text"], relief=tk.FLAT,
                                  insertbackground=C["text"], state=tk.DISABLED,
                                  wrap=tk.WORD, bd=0,
                                  highlightthickness=1, highlightbackground=C["border"])
        self._log_text.pack(fill=tk.X)

        self._log_text.tag_config("err",  foreground=C["red"])
        self._log_text.tag_config("ok",   foreground=C["green"])
        self._log_text.tag_config("info", foreground=C["text"])

    # ── Start / Stop ─────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            self._stop_monitor()
        else:
            self._start_monitor()

    def _start_monitor(self):
        self._stop.clear()
        self._thread = MonitorThread(self.q, self._stop)
        self._thread.start()
        self._running = True
        self._btn.config(text="■  Stop", bg=C["red"])

    def _stop_monitor(self):
        self._stop.set()
        self._running = False
        self._btn.config(text="▶  Start", bg=C["green"])

    # ── GUI frissítés (queue polling) ────────────────────────────

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _handle_msg(self, msg: Msg):
        if msg.kind == "state":
            self._set_state(msg.state, getattr(msg, "detail", ""))
        elif msg.kind == "log":
            self._append_log(msg.text)
        elif msg.kind == "live":
            if msg.key in self._live_vars:
                self._live_vars[msg.key].set(msg.value)
        elif msg.kind == "row":
            self._add_tree_row(msg.row)
        elif msg.kind == "countdown":
            self._set_countdown(msg.remaining, msg.total)

    def _set_state(self, state: CycleState, detail=""):
        dot, color, label = STATE_META.get(state, ("●", C["muted"], str(state)))
        self._state_dot.config(fg=color)
        self._state_lbl.config(text=label)
        self._state_detail.config(text=detail)

    def _set_countdown(self, remaining: int, total: int):
        if total == 0:
            self._countdown_lbl.config(text="--:--")
            self._progress["value"] = 0
            return
        m, s = divmod(remaining, 60)
        self._countdown_lbl.config(text=f"{m:02d}:{s:02d}")
        pct = (1 - remaining / total) * 100 if total > 0 else 0
        self._progress["value"] = pct

    def _append_log(self, text: str):
        tag = "err" if "HIBA" in text else ("ok" if "mentve" in text else "info")
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, text + "\n", tag)
        self._log_text.see(tk.END)
        # max 200 sor
        lines = int(self._log_text.index("end-1c").split(".")[0])
        if lines > 200:
            self._log_text.delete("1.0", f"{lines - 200}.0")
        self._log_text.config(state=tk.DISABLED)

    def _add_tree_row(self, row: dict):
        vals = (row.get("timestamp", ""),
                row.get("temp_before_C", ""),
                row.get("temp_after_C", ""),
                row.get("cal72", ""),
                row.get("cal175", ""))
        iid = self._tree.insert("", 0, values=vals)   # legfrissebb felülre
        # max HISTORY_MAX sor
        all_items = self._tree.get_children()
        if len(all_items) > HISTORY_MAX:
            self._tree.delete(all_items[-1])

    def _load_history(self):
        rows = _csv_load_history()
        for row in rows:
            self._add_tree_row(row)
        if rows:
            last = rows[-1]
            for key in ("temp_before_C", "temp_after_C", "cal72", "cal175"):
                var_key = key.replace("_C", "").replace("temp_before", "temp_before").replace("temp_after", "temp_after")
                if var_key in self._live_vars:
                    val = last.get(key, "—")
                    if "_C" in key:
                        val = f"{val} °C"
                    self._live_vars[var_key].set(val)


# ════════════════════════════════════════════════════════════════
#  Belépési pont
# ════════════════════════════════════════════════════════════════

def main():
    root = tk.Tk()
    app  = DriftMonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app._stop_monitor(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
