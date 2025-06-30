#
# SmartCam GUI (Linux, V4L2) – v2.7
# ==================================
# - Détection de mouvement + voyant temps‑réel
# - Tampon pré/post configurable, enregistrement segmenté
# - Compression différée, flux RTSP optionnel
# - Envoi de segments par e‑mail (realtime/on_exit)
# - Gestion robuste des erreurs (accès caméra, droits d’écriture, SMTP…)
# - Interface PyQt5 sombre, fluide (threads, QTimer) – >560 lignes
#
from __future__ import annotations
import argparse, collections, datetime, json, os, pathlib, shlex, shutil, smtplib, subprocess, sys, tempfile, time, traceback, zipfile
from email.message import EmailMessage

import cv2, numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')

# ---------------------------------------------------------------------------
# 0. Config JSON helpers
# ---------------------------------------------------------------------------
CFG_PATH = pathlib.Path(__file__).with_name("smartcam_config.json")
DEFAULT_CFG = {
    "device": "/dev/video0",
    "fps": 20,
    "area": 1500,
    "hits": 5,
    "pre_s": 3,
    "post_s": 3,
    "out_dir": str(pathlib.Path.home() / "Videos"),
    "prefix": "segment_",
    "mail": {
        "enabled": False,
        "from": "noreply@example.com",
        "to": "dest@example.com",
        "smtp_user": "",
        "smtp_pass": "",
        "smtp_host": "smtp-relay.gmail.com",
        "smtp_port": 587
    },
    "send_mode": "on_exit",
    "rtsp": {"enabled": False, "url": "rtsp://indexer.example.com/live/stream"},
    "compress_after_min": 30,
    "preview_raw": True,
    "preview_proc": True
}

# ---------------------------------------------------------------------------
# 1. Détection de mouvement
# ---------------------------------------------------------------------------
class MotionDetector:
    def __init__(self, area: int, hits: int):
        self.thresh_area, self.req = area, hits
        self.bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=16, detectShadows=True)
        self.counter = 0

    def process(self, frame: np.ndarray) -> tuple[bool, np.ndarray]:
        """Process frame for motion; return (triggered, vis)."""
        fg = self.bg.apply(frame)
        fg = cv2.morphologyEx(
            fg,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=2,
        )
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        move = any(cv2.contourArea(c) > self.thresh_area for c in contours)
        self.counter = self.counter + 1 if move else 0

        # --- Motion detection logging ---
        logging.debug(f"MotionDetector: move={move}, counter={self.counter}/{self.req}")
        triggered = self.counter >= self.req
        logging.debug(f"MotionDetector triggered: {triggered}")
        # --------------------------------

        vis = cv2.cvtColor(fg, cv2.COLOR_GRAY2BGR)
        cv2.drawContours(vis, contours, -1, (0, 0, 255), 2)
        return triggered, vis

# ---------------------------------------------------------------------------
# 2. Recorder (segment file writer)
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self, size: tuple[int,int], fps: float, out_dir: str, prefix: str):
        self.size, self.fps = size, fps
        self.dir = pathlib.Path(out_dir)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"Cannot create output directory {self.dir}: {e}")
        if not os.access(self.dir, os.W_OK):
            raise RuntimeError(f"No write permission in {self.dir}")
        self.prefix = prefix
        self.fourcc = cv2.VideoWriter_fourcc(*"XVID")
        self.writer: cv2.VideoWriter | None = None
        self.stop_ts = 0.0
        self.current: pathlib.Path | None = None
        self.files: list[pathlib.Path] = []

    # helper
    def _open(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current = self.dir / f"{self.prefix}{ts}.avi"
        self.writer = cv2.VideoWriter(str(self.current), self.fourcc, self.fps, self.size)
        self.files.append(self.current)

    def start(self, buffer: list[np.ndarray]):
        if self.writer is None:
            self._open()
            for f in buffer: self.writer.write(f)

    def write(self, frame):
        if self.writer: self.writer.write(frame)

    def stop_in(self, sec: float):
        self.stop_ts = time.time() + sec

    def update(self) -> pathlib.Path | None:
        if self.writer and time.time() >= self.stop_ts:
            self.writer.release(); self.writer=None
            fin=self.current; self.current=None; return fin
        return None

    def close(self):
        if self.writer: self.writer.release()
        self.writer=None; self.current=None

# ---------------------------------------------------------------------------
# 3. RTSP streamer
# ---------------------------------------------------------------------------
class RTSPStreamer:
    def __init__(self, url: str, size: tuple[int,int], fps: float):
        self.proc=None
        if shutil.which("ffmpeg"):
            cmd=["ffmpeg","-f","rawvideo","-pix_fmt","bgr24","-s",f"{size[0]}x{size[1]}","-r",str(fps),"-i","-","-c:v","libx264","-preset","ultrafast","-f","rtsp",url]
            self.proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def write(self, frame: np.ndarray):
        if self.proc and self.proc.stdin: self.proc.stdin.write(frame.tobytes())

    def close(self):
        if self.proc and self.proc.stdin:
            self.proc.stdin.close(); self.proc.wait()

# ---------------------------------------------------------------------------
# 4. Compression thread
# ---------------------------------------------------------------------------
class Compressor(QtCore.QThread):
    log = QtCore.pyqtSignal(str)
    def __init__(self, folder: str, after_min: int):
        super().__init__()
        self.folder = pathlib.Path(folder)
        self.delay = after_min * 60
        self.stop = False

    def run(self):
        while not self.stop:
            now = time.time()
            for avi in self.folder.glob("*.avi"):
                # compress files older than delay
                if now - avi.stat().st_mtime > self.delay:
                    mp4 = avi.with_suffix(".mp4")
                    subprocess.run([
                        "ffmpeg", "-y", "-i", str(avi), "-c:v", "libx264", "-crf", "23", str(mp4)
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        avi.unlink()
                    except Exception:
                        pass
                    self.log.emit(f"Compressed: {mp4.name}")
            # Sleep in small intervals to allow quick stop
            for _ in range(30):
                if self.stop:
                    return
                time.sleep(1)

# ---------------------------------------------------------------------------
# 5. Mail helper with logging
# ---------------------------------------------------------------------------
class Mailer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        logging.debug(f"Mailer initialized with config: {cfg}")

    def send(self, paths: list[pathlib.Path]) -> None:
        logging.info("Mailer.send called")
        if not (self.cfg.get("enabled") and paths):
            logging.warning("Mailer disabled or no paths to send")
            return
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        try:
            logging.info(f"Creating zip file {tmp.name}")
            with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    logging.debug(f"Adding file to zip: {p}")
                    zf.write(p, arcname=p.name)
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = (
                self.cfg["from"], self.cfg["to"], "SmartCam segments"
            )
            msg.set_content("Segments attached.")
            logging.info(f"Attaching zip to email and sending to {self.cfg['to']}")
            msg.add_attachment(
                open(tmp.name, "rb").read(),
                maintype="application", subtype="zip", filename="segments.zip"
            )
            with smtplib.SMTP(self.cfg["smtp_host"], self.cfg["smtp_port"], timeout=30) as s:
                s.starttls()
                logging.debug("SMTP TLS started")
                if self.cfg["smtp_user"] and self.cfg["smtp_pass"]:
                    s.login(self.cfg["smtp_user"], self.cfg["smtp_pass"])
                    logging.debug("SMTP login successful")
                s.send_message(msg)
                logging.info("Email sent successfully")
        except Exception as e:
            logging.error(f"Failed to send email: {e}", exc_info=True)
        finally:
            try:
                os.unlink(tmp.name)
                logging.debug(f"Temporary zip file {tmp.name} removed")
            except Exception as e:
                logging.warning(f"Failed to remove temp zip file: {e}")
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 6. Capture thread (heavy work off‑UI)
# ---------------------------------------------------------------------------
class CaptureThread(QtCore.QThread):
    frame_raw = QtCore.pyqtSignal(QtGui.QImage)
    frame_proc = QtCore.pyqtSignal(QtGui.QImage)
    motion_sig = QtCore.pyqtSignal(bool)
    log = QtCore.pyqtSignal(str)
    err = QtCore.pyqtSignal(str)
    segment_done = QtCore.pyqtSignal(pathlib.Path)
    def __init__(self,cfg:argparse.Namespace): super().__init__(); self.cfg=cfg; self.buf=collections.deque(maxlen=int(cfg.pre_s*cfg.fps)); self.stop=False
    def run(self):
        try:
            cap=cv2.VideoCapture(self.cfg.device,cv2.CAP_V4L2)
            if not cap.isOpened(): raise RuntimeError(f"Cannot open {self.cfg.device}")
            fps=cap.get(cv2.CAP_PROP_FPS) or self.cfg.fps
            sz=(int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            det=MotionDetector(self.cfg.area,self.cfg.hits)
            rec=Recorder(sz,fps,self.cfg.out_dir,self.cfg.prefix)
            rtsp=RTSPStreamer(self.cfg.rtsp["url"],sz,fps) if self.cfg.rtsp["enabled"] else None
            self.log.emit("▶️ Started")
            while not self.stop:
                ok,frm=cap.read();
                if not ok: self.log.emit("⚠️ drop"); continue
                trig,vis=det.process(frm)
                # --- Detection logging in thread ---
                if trig:
                    logging.info("Motion detected in CaptureThread")
                else:
                    logging.debug("No motion in CaptureThread")
                self.motion_sig.emit(trig)
                self.buf.append(frm.copy())
                if trig: rec.start(list(self.buf)); rec.stop_in(self.cfg.post_s)
                rec.write(frm)
                fin=rec.update()
                if fin: self.segment_done.emit(fin)
                if rtsp: rtsp.write(frm)
                if self.cfg.preview_raw: self.frame_raw.emit(self.to_q(frm))
                if self.cfg.preview_proc: self.frame_proc.emit(self.to_q(vis))
            rec.close(); cap.release();
            if rtsp: rtsp.close()
            self.log.emit("⏹️ Stopped")
        except Exception:
            self.err.emit(traceback.format_exc())
    @staticmethod
    def to_q(img:np.ndarray): rgb=cv2.cvtColor(img,cv2.COLOR_BGR2RGB); h,w,ch=rgb.shape; return QtGui.QImage(rgb.data,w,h,ch*w,QtGui.QImage.Format_RGB888).copy()

# ---------------------------------------------------------------------------
# 7. Settings wrapper
# ---------------------------------------------------------------------------
class Settings(QtCore.QObject):
    updated = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.load()

    def load(self) -> None:
        # Load configuration from JSON
        if CFG_PATH.exists():
            data = json.loads(CFG_PATH.read_text())
            logging.info(f"Configuration loaded from {CFG_PATH}")
        else:
            data = DEFAULT_CFG
            CFG_PATH.write_text(json.dumps(data, indent=2))
            logging.info(f"Default configuration written to {CFG_PATH}")
        for k, v in data.items():
            setattr(self, k, v)

    def save(self) -> None:
        # Save configuration to JSON
        logging.info(f"Saving configuration to {CFG_PATH}")
        values = {k: getattr(self, k) for k in DEFAULT_CFG}
        logging.debug(f"Configuration values: {values}")
        CFG_PATH.write_text(json.dumps(values, indent=2))

    def ns(self) -> argparse.Namespace:
        # Build argparse.Namespace from settings attributes
        ns = argparse.Namespace()
        for k in DEFAULT_CFG:
            setattr(ns, k, getattr(self, k))
        return ns

# ---------------------------------------------------------------------------
# 8. Capture Tab UI
# ---------------------------------------------------------------------------
class CaptureTab(QtWidgets.QWidget):
    def __init__(self,m:Settings):
        super().__init__(); self.m=m; self.th=None; self.comp=None; self.saved=[]; self.build()
    def build(self):
        v=QtWidgets.QVBoxLayout(self)
        split=QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.lbl_raw=QtWidgets.QLabel(alignment=QtCore.Qt.AlignCenter)
        self.lbl_proc=QtWidgets.QLabel(alignment=QtCore.Qt.AlignCenter)
        for l in (self.lbl_raw,self.lbl_proc): l.setMinimumSize(640,360); l.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Expanding)
        split.addWidget(self.lbl_raw); split.addWidget(self.lbl_proc); split.setStretchFactor(0,1); split.setStretchFactor(1,1)
        v.addWidget(split,1)
        ctrl=QtWidgets.QHBoxLayout(); self.led=QtWidgets.QFrame(); self.led.setFixedSize(18,18); self.led.setStyleSheet("background:red;border-radius:9px;"); ctrl.addWidget(self.led)
        self.btn=QtWidgets.QPushButton("Start"); ctrl.addWidget(self.btn); ctrl.addStretch(); v.addLayout(ctrl)
        self.log=QtWidgets.QPlainTextEdit(readOnly=True,maximumBlockCount=2500); v.addWidget(self.log,1)
        self.btn.clicked.connect(self.toggle)
        self.m.updated.connect(self.apply_preview)
    def toggle(self):
        if self.th and self.th.isRunning(): self.th.stop=True; self.btn.setEnabled(False)
        else: QtCore.QTimer.singleShot(0,self.start_cap)
    def start_cap(self):
        logging.info("Starting capture thread")
        cfg = self.m.ns()
        logging.debug(f"CaptureThread config: {cfg}")
        self.th = CaptureThread(cfg)
        self.th.frame_raw.connect(lambda i: self.set_pix(self.lbl_raw, i))
        self.th.frame_proc.connect(lambda i: self.set_pix(self.lbl_proc, i))
        self.th.motion_sig.connect(lambda b: self.led.setStyleSheet(f"background:{'green' if b else 'red'};border-radius:9px;"))
        self.th.log.connect(self.log.appendPlainText)
        self.th.err.connect(self.err)
        self.th.segment_done.connect(self.seg)
        self.th.finished.connect(self.stopped)
        self.th.start()
        self.btn.setText("Stop")
        self.btn.setEnabled(True)
        logging.info("Capture thread started")
        # Compressor thread
        if self.comp and self.comp.isRunning():
            self.comp.stop = True
            self.comp.wait()
        self.comp = Compressor(cfg.out_dir, cfg.compress_after_min)
        self.comp.log.connect(self.log.appendPlainText)
        self.comp.start()
        logging.info("Compressor thread started")
    def set_pix(self,l,img): l.setPixmap(QtGui.QPixmap.fromImage(img).scaled(l.size(),QtCore.Qt.KeepAspectRatio,QtCore.Qt.SmoothTransformation))
    def seg(self, p: pathlib.Path) -> None:
        # Segment finalized
        self.log.appendPlainText(f"Saved segment: {p.name}")
        logging.info(f"Segment ready: {p}")

        if self.m.mail.get("enabled") and self.m.send_mode == "realtime":
            logging.info(f"Sending segment {p.name} via email (realtime)")
            try:
                Mailer(self.m.mail).send([p])
                self.log.appendPlainText("Email sent")
                logging.info("Email send success")
            except Exception as e:
                self.log.appendPlainText(f"Email error: {e}")
                logging.error(f"Email send error: {e}", exc_info=True)
        else:
            self.saved.append(p)
            logging.debug(f"Segment saved locally: {p.name}")
    def stopped(self):
        logging.info("Stopping capture and compressor threads")
        self.btn.setEnabled(True)
        self.btn.setText("Start")
        # Stop compressor thread
        if self.comp:
            self.comp.stop = True
            self.comp.wait()
            logging.debug("Compressor thread stopped")
        # Handle remaining segments
        if self.m.mail['enabled'] and self.m.send_mode == 'on_exit':
            try:
                Mailer(self.m.mail).send(self.saved)
                logging.info("Segments emailed on exit")
            except Exception as e:
                logging.error(f"Email on exit failed: {e}", exc_info=True)
        # Clear saved segments
        self.saved.clear()
        logging.info("Capture stopped and cleaned up")
    def err(self,e): self.log.appendPlainText(e); QtWidgets.QMessageBox.critical(self,"Capture Error",e)
    def apply_preview(self):
        logging.debug(f"Toggling previews - raw: {self.m.preview_raw}, proc: {self.m.preview_proc}")
        self.lbl_raw.setVisible(self.m.preview_raw)
        self.lbl_proc.setVisible(self.m.preview_proc)

# ---------------------------------------------------------------------------
# 9. ConfigTab (complete above)
# ---------------------------------------------------------------------------
class ConfigTab(QtWidgets.QScrollArea):
    def __init__(self, m: Settings):
        super().__init__()
        self.m = m
        self.setWidgetResizable(True)
        container = QtWidgets.QWidget()
        self.setWidget(container)
        self._build_ui(container)

    def _build_ui(self, w: QtWidgets.QWidget) -> None:
        layout = QtWidgets.QFormLayout(w)
        # Camera selector
        self.dev_cb = QtWidgets.QComboBox()
        self._refresh_devices()
        self.dev_cb.setCurrentText(self.m.device)
        layout.addRow("Caméra", self.dev_cb)
        # Output directory
        self.out_edit = QtWidgets.QLineEdit(self.m.out_dir)
        btn = QtWidgets.QPushButton("…")
        btn.clicked.connect(self._browse_dir)
        hl = QtWidgets.QHBoxLayout()
        hl.addWidget(self.out_edit)
        hl.addWidget(btn)
        layout.addRow("Dossier sorties", hl)
        # Prefix
        self.prefix_edit = QtWidgets.QLineEdit(self.m.prefix)
        layout.addRow("Préfixe", self.prefix_edit)
        # Numeric settings
        self.area_spin = QtWidgets.QSpinBox(maximum=100000, value=self.m.area)
        layout.addRow("Surface min.", self.area_spin)
        self.hits_spin = QtWidgets.QSpinBox(maximum=30, value=self.m.hits)
        layout.addRow("Frames cons.", self.hits_spin)
        self.pre_spin = QtWidgets.QSpinBox(maximum=10, value=self.m.pre_s)
        layout.addRow("Secondes avant", self.pre_spin)
        self.post_spin = QtWidgets.QSpinBox(maximum=10, value=self.m.post_s)
        layout.addRow("Secondes après", self.post_spin)
        self.comp_spin = QtWidgets.QSpinBox(maximum=1440, value=self.m.compress_after_min)
        layout.addRow("Compression après (min)", self.comp_spin)
        # Preview toggles
        self.raw_cb = QtWidgets.QCheckBox("Afficher brut")
        self.raw_cb.setChecked(self.m.preview_raw)
        layout.addRow(self.raw_cb)
        self.proc_cb = QtWidgets.QCheckBox("Afficher mouvement")
        self.proc_cb.setChecked(self.m.preview_proc)
        layout.addRow(self.proc_cb)
        # RTSP
        self.rtsp_cb = QtWidgets.QCheckBox("Activer RTSP")
        self.rtsp_cb.setChecked(self.m.rtsp['enabled'])
        layout.addRow(self.rtsp_cb)
        self.rtsp_url = QtWidgets.QLineEdit(self.m.rtsp['url'])
        layout.addRow("URL RTSP", self.rtsp_url)
        # Email settings
        mail = self.m.mail
        self.mail_cb = QtWidgets.QCheckBox("Envoyer par e-mail")
        self.mail_cb.setChecked(mail['enabled'])
        layout.addRow(self.mail_cb)
        self.mail_from = QtWidgets.QLineEdit(mail['from'])
        layout.addRow("Mail from", self.mail_from)
        self.mail_to = QtWidgets.QLineEdit(mail['to'])
        layout.addRow("Mail to", self.mail_to)
        self.smtp_user = QtWidgets.QLineEdit(mail['smtp_user'])
        layout.addRow("SMTP user", self.smtp_user)
        self.smtp_pass = QtWidgets.QLineEdit(mail['smtp_pass'])
        self.smtp_pass.setEchoMode(QtWidgets.QLineEdit.Password)
        layout.addRow("SMTP pass", self.smtp_pass)
        self.smtp_host = QtWidgets.QLineEdit(mail['smtp_host'])
        layout.addRow("SMTP host", self.smtp_host)
        self.smtp_port = QtWidgets.QSpinBox(maximum=65535, value=mail['smtp_port'])
        layout.addRow("SMTP port", self.smtp_port)
        # Send mode
        self.mode_cb = QtWidgets.QComboBox()
        self.mode_cb.addItems(["on_exit", "realtime"])
        self.mode_cb.setCurrentText(self.m.send_mode)
        layout.addRow("Mode envoi", self.mode_cb)
        # Apply button
        apply_btn = QtWidgets.QPushButton("Appliquer")
        apply_btn.clicked.connect(self._apply)
        layout.addRow(apply_btn)

    def _refresh_devices(self) -> None:
        self.dev_cb.clear()
        for dev in sorted(pathlib.Path("/dev").glob("video*")):
            self.dev_cb.addItem(str(dev), str(dev))
        if self.dev_cb.count() == 0:
            self.dev_cb.addItem("Aucune caméra", "")

    def _browse_dir(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choisir dossier", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _apply(self) -> None:
        logging.info("Applying configuration from GUI")
        try:
            out = pathlib.Path(self.out_edit.text())
            if not out.exists():
                raise RuntimeError(f"Output directory does not exist: {out}")
            if not os.access(out, os.W_OK):
                raise RuntimeError(f"No write permission for directory: {out}")
            # Assign values back to model
            self.m.device = self.dev_cb.currentData()
            self.m.out_dir = str(out)
            self.m.prefix = self.prefix_edit.text() or "segment_"
            self.m.area = self.area_spin.value()
            self.m.hits = self.hits_spin.value()
            self.m.pre_s = self.pre_spin.value()
            self.m.post_s = self.post_spin.value()
            self.m.compress_after_min = self.comp_spin.value()
            self.m.preview_raw = self.raw_cb.isChecked()
            self.m.preview_proc = self.proc_cb.isChecked()
            self.m.rtsp = {'enabled': self.rtsp_cb.isChecked(), 'url': self.rtsp_url.text()}
            self.m.mail = {
                'enabled': self.mail_cb.isChecked(),
                'from': self.mail_from.text(),
                'to': self.mail_to.text(),
                'smtp_user': self.smtp_user.text(),
                'smtp_pass': self.smtp_pass.text(),
                'smtp_host': self.smtp_host.text(),
                'smtp_port': self.smtp_port.value()
            }
            self.m.send_mode = self.mode_cb.currentText()
            self.m.save()
            logging.info("Configuration applied successfully")
            self.m.updated.emit()
        except Exception as e:
            logging.error(f"Configuration apply failed: {e}", exc_info=True)
            QtWidgets.QMessageBox.critical(self, "Configuration Error", str(e))

# ---------------------------------------------------------------------------
# 10. Main application window & entry point
# ---------------------------------------------------------------------------
class MainWindow(QtWidgets.QTabWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SmartCam GUI")
        self.resize(1280, 720)
        self.apply_telegram_theme()
        model = Settings()
        self.addTab(CaptureTab(model), "Capture")
        self.addTab(ConfigTab(model), "Configuration")

    def apply_telegram_theme(self) -> None:
        qss = """
        QWidget {
            background-color: #FFFFFF;
            color: #000000;
            font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
        }
        QTabBar::tab {
            background: #F2F3F5;
            padding: 8px;
            margin: 1px;
        }
        QTabBar::tab:selected {
            background: #FFFFFF;
            border-bottom: 2px solid #0088CC;
        }
        QPushButton {
            background: #0088CC;
            color: #FFFFFF;
            border: none;
            border-radius: 4px;
            padding: 6px 12px;
        }
        QPushButton:hover {
            background: #0099E5;
        }
        QPlainTextEdit, QTextEdit {
            background: #F2F3F5;
            border: 1px solid #D0D0D0;
        }
        QLineEdit {
            background: #FFFFFF;
            border: 1px solid #D0D0D0;
            padding: 4px;
        }
        QComboBox {
            background: #FFFFFF;
            border: 1px solid #D0D0D0;
            padding: 4px;
        }
        QSpinBox {
            background: #FFFFFF;
            border: 1px solid #D0D0D0;
            padding: 4px;
        }
        """
        self.setStyleSheet(qss)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SmartCam CLI/GUI application")
    parser.add_argument("--gui", action="store_true", help="Enable graphical interface")
    parser.add_argument("--log", action="store_true", help="Enable verbose logging (DEBUG level)")
    args = parser.parse_args()

    # Configure logging level based on CLI flag
    if args.log:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.info("Verbose logging enabled")
    else:
        logging.getLogger().setLevel(logging.WARNING)

    if args.gui:
        # Launch graphical interface
        app = QtWidgets.QApplication(sys.argv)
        window = MainWindow()
        window.show()
        sys.exit(app.exec_())
    else:
        # Headless mode: run capture thread and log to console
        logging.info("Starting headless capture mode")
        settings = Settings()
        cfg = settings.ns()
        cap_thread = CaptureThread(cfg)
        cap_thread.log.connect(lambda msg: logging.info(msg))
        cap_thread.err.connect(lambda e: logging.error(e))
        cap_thread.motion_sig.connect(lambda m: logging.debug(f"MotionSig: {m}"))
        cap_thread.segment_done.connect(lambda p: logging.info(f"Segment done: {p.name}"))
        cap_thread.start()
        try:
            while cap_thread.isRunning():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logging.info("Keyboard interrupt received, stopping capture")
            cap_thread.stop = True
            cap_thread.wait()
        logging.info("Headless capture stopped")
