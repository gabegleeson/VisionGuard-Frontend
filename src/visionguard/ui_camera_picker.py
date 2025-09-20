# ui_camera_picker.py
import json
import pathlib
import tkinter as tk
from tkinter import ttk, messagebox

STORE_PATH = pathlib.Path("camera_source.json")

def _load_last():
    try:
        if STORE_PATH.exists():
            data = json.loads(STORE_PATH.read_text())
            return data
    except Exception:
        pass
    return {}

def _save_last(payload: dict):
    try:
        STORE_PATH.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass

_PRESETS = [
    "Default (stream/camera)",
    "640x480",
    "1280x720",
    "1920x1080",
    "2560x1440",
    "3840x2160",
    "Custom…",
]

def _parse_res(s: str):
    try:
        w, h = s.lower().split("x")
        w = int(w.strip())
        h = int(h.strip())
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return None

def pick_camera_source(title="Select Camera Source", enable_test=True):
    """
    Returns a dict:
      {
        "source": int|str,         # webcam index or RTSP URL
        "resolution": (w, h)|None  # None = default for stream/camera
      }
    Returns None if cancelled.
    """
    last = _load_last()
    last_source = last.get("source", 0)
    last_res = last.get("resolution", None)
    last_mode = "webcam" if (isinstance(last_source, int) or (isinstance(last_source, str) and last_source.isdigit())) else "rtsp"

    root = tk.Tk()
    root.title(title)
    root.resizable(False, False)

    # --- State
    mode = tk.StringVar(value=last_mode)
    webcam_index = tk.StringVar(value=str(last_source if isinstance(last_source, int) or str(last_source).isdigit() else 0))
    rtsp_url = tk.StringVar(value=str(last_source if isinstance(last_source, str) and not str(last_source).isdigit()
                           else "rtsp://user:pass@192.168.1.90:554/axis-media/media.amp"))

    preset = tk.StringVar(value="Default (stream/camera)")
    custom_w = tk.StringVar(value="")
    custom_h = tk.StringVar(value="")

    if isinstance(last_res, (list, tuple)) and len(last_res) == 2:
        # Try to map back to preset
        preset_val = f"{last_res[0]}x{last_res[1]}"
        if preset_val in _PRESETS:
            preset.set(preset_val)
        else:
            preset.set("Custom…")
            custom_w.set(str(last_res[0]))
            custom_h.set(str(last_res[1]))

    # --- Layout
    pad = {"padx": 10, "pady": 6}
    frm = ttk.Frame(root, padding=12)
    frm.grid(row=0, column=0)

    ttk.Label(frm, text="Choose source type:").grid(row=0, column=0, sticky="w", **pad)
    ttk.Radiobutton(frm, text="Webcam (index)", variable=mode, value="webcam").grid(row=1, column=0, sticky="w", **pad)
    ttk.Radiobutton(frm, text="RTSP URL",       variable=mode, value="rtsp").grid(row=2, column=0, sticky="w", **pad)

    ttk.Label(frm, text="Index:").grid(row=1, column=1, sticky="e", **pad)
    e_idx = ttk.Entry(frm, textvariable=webcam_index, width=8)
    e_idx.grid(row=1, column=2, sticky="w", **pad)

    ttk.Label(frm, text="URL:").grid(row=2, column=1, sticky="e", **pad)
    e_url = ttk.Entry(frm, textvariable=rtsp_url, width=48)
    e_url.grid(row=2, column=2, sticky="w", **pad)

    sep = ttk.Separator(frm)
    sep.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 8))

    ttk.Label(frm, text="Resolution:").grid(row=4, column=0, sticky="w", **pad)
    cb = ttk.Combobox(frm, textvariable=preset, values=_PRESETS, state="readonly", width=28)
    cb.grid(row=4, column=1, columnspan=2, sticky="w", **pad)

    # Custom WxH
    cw_lbl = ttk.Label(frm, text="W:")
    cw_ent = ttk.Entry(frm, textvariable=custom_w, width=8)
    ch_lbl = ttk.Label(frm, text="H:")
    ch_ent = ttk.Entry(frm, textvariable=custom_h, width=8)

    def _toggle_custom_fields(*_):
        is_custom = preset.get() == "Custom…"
        state = "normal" if is_custom else "disabled"
        for w in (cw_lbl, cw_ent, ch_lbl, ch_ent):
            w.configure(state=state)

    cw_lbl.grid(row=5, column=1, sticky="e", **pad)
    cw_ent.grid(row=5, column=2, sticky="w", **pad)
    ch_lbl.grid(row=6, column=1, sticky="e", **pad)
    ch_ent.grid(row=6, column=2, sticky="w", **pad)
    _toggle_custom_fields()
    cb.bind("<<ComboboxSelected>>", _toggle_custom_fields)

    # --- Actions
    def _resolve_source_and_res():
        # Source
        if mode.get() == "webcam":
            val = webcam_index.get().strip()
            if not val.isdigit():
                raise ValueError("Webcam index must be an integer (e.g., 0).")
            src = int(val)
        else:
            url = rtsp_url.get().strip()
            if not url.startswith("rtsp://"):
                # Give user a chance to proceed anyway
                if not messagebox.askyesno(
                    "Confirm URL",
                    "The URL doesn't look like a typical RTSP URL.\nContinue anyway?"
                ):
                    raise RuntimeError("RTSP URL not confirmed.")
            src = url

        # Resolution
        res = None
        if preset.get() == "Default (stream/camera)":
            res = None
        elif preset.get() == "Custom…":
            if not custom_w.get().strip().isdigit() or not custom_h.get().strip().isdigit():
                raise ValueError("Custom width/height must be positive integers.")
            res = (int(custom_w.get().strip()), int(custom_h.get().strip()))
        else:
            res = _parse_res(preset.get())
            if res is None:
                raise ValueError("Invalid preset resolution selected.")

        return src, res

    def do_ok():
        try:
            src, res = _resolve_source_and_res()
        except Exception as e:
            messagebox.showerror("Invalid selection", str(e))
            return
        payload = {"source": src, "resolution": res}
        _save_last(payload)
        root.result = payload
        root.destroy()

    def do_cancel():
        root.result = None
        root.destroy()

    def do_test():
        try:
            import cv2
        except Exception:
            messagebox.showerror("OpenCV not found", "Install opencv-python to test streams.")
            return

        try:
            src, res = _resolve_source_and_res()
        except Exception as e:
            messagebox.showerror("Invalid selection", str(e))
            return

        # Try to open and (for webcams) apply width/height before read
        try:
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG) if isinstance(src, str) else cv2.VideoCapture(src)
            if res and isinstance(src, int):
                w, h = res
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                messagebox.showerror("Test failed", "Could not read a frame from the selected source.")
            else:
                h_, w_ = frame.shape[:2]
                messagebox.showinfo("Test OK", f"Got a frame: {w_}x{h_}")
        except Exception as e:
            messagebox.showerror("Test error", str(e))

    btns = ttk.Frame(frm)
    btns.grid(row=7, column=0, columnspan=3, sticky="e", padx=10, pady=10)

    if enable_test:
        ttk.Button(btns, text="Test", command=do_test).grid(row=0, column=0, padx=6)
    ttk.Button(btns, text="Cancel", command=do_cancel).grid(row=0, column=1, padx=6)
    ttk.Button(btns, text="OK", command=do_ok).grid(row=0, column=2)

    # Focus friendly defaults
    (e_idx if mode.get()=="webcam" else e_url).focus_set()

    # Center window
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

    root.result = None
    root.mainloop()
    return root.result
