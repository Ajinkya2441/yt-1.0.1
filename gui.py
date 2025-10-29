"""PySide6 GUI for the YouTube downloader."""

from __future__ import annotations

from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot, QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QRadioButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from downloader import DownloadCancelled, DownloadControl, DownloadError, download_video


RESOLUTION_CHOICES: tuple[tuple[str, str | None], ...] = (
    ("Auto (best available)", None),
    ("240p", "240p"),
    ("360p", "360p"),
    ("480p", "480p"),
    ("1K (1080p)", "1080p"),
    ("2K (1440p)", "1440p"),
    ("4K (2160p)", "2160p"),
    ("8K (4320p)", "4320p"),
)


APP_STYLESHEET = """
QMainWindow { background-color: #101421; color: #F5F7FF; }
QFrame#card { background-color: #161B2B; border-radius: 14px; padding: 18px; }
QLabel { color: #E2E6F3; font-size: 13px; }
QLineEdit, QComboBox {
    background-color: #1F2538;
    color: #F5F7FF;
    border: 1px solid #2E3650;
    border-radius: 8px;
    padding: 8px 10px;
}
QComboBox QAbstractItemView {
    background-color: #1F2538;
    color: #F5F7FF;
    selection-background-color: #2563EB;
    selection-color: #FFFFFF;
    border: 1px solid #2E3650;
    border-radius: 8px;
}
QLineEdit:focus, QComboBox:focus { border-color: #3B82F6; }
QProgressBar {
    background-color: #1F2538;
    color: #F5F7FF;
    border: 1px solid #2E3650;
    border-radius: 8px;
    text-align: center;
    padding: 3px;
}
QProgressBar::chunk {
    background-color: #3B82F6;
    border-radius: 6px;
}
QPushButton {
    background-color: #3B82F6;
    color: white;
    border-radius: 8px;
    padding: 10px 18px;
    font-weight: 600;
}
QPushButton:hover { background-color: #2563EB; }
QPushButton:disabled { background-color: #2E3650; color: #8A94B4; }
QCheckBox { color: #E2E6F3; font-size: 13px; }
QStatusBar { background-color: #161B2B; color: #E2E6F3; }
QRadioButton {
    color: #F5F7FF;
    font-size: 13px;
    font-weight: 600;
}
QRadioButton::indicator:checked {
    background-color: #3B82F6;
    border: 1px solid #3B82F6;
}
QRadioButton::indicator:unchecked {
    background-color: #1F2538;
    border: 1px solid #3B82F6;
}
"""


class WorkerControl(DownloadControl):
    """Thread-safe control object for pause and cancel functionality."""

    _sleep_interval = 0.05

    def __init__(self) -> None:
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._cancel_event = threading.Event()

    def pause(self) -> None:
        self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    def cancel(self) -> None:
        self._cancel_event.set()
        self._resume_event.set()

    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    def wait_if_paused(self) -> None:
        while not self._resume_event.is_set():
            self.raise_if_cancelled()
            time.sleep(self._sleep_interval)

    def raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise DownloadCancelled()


class DownloadWorker(QObject):
    """Background worker that performs the download on a separate thread."""

    succeeded = Signal(str)
    failed = Signal(str)
    progress = Signal(object, str)
    cancelled = Signal()

    def __init__(
        self,
        url: str,
        output_dir: Path,
        filename: str | None,
        audio_only: bool,
        resolution: str | None,
        control: DownloadControl | None,
    ) -> None:
        super().__init__()
        self._url = url
        self._output_dir = output_dir
        self._filename = filename
        self._audio_only = audio_only
        self._resolution = resolution
        self._control = control

    @Slot()
    def run(self) -> None:
        def _progress(percent: object, message: str) -> None:
            self.progress.emit(percent, message)

        try:
            destination = download_video(
                url=self._url,
                output_dir=self._output_dir,
                filename=self._filename,
                audio_only=self._audio_only,
                resolution=self._resolution,
                progress_callback=_progress,
                control=self._control,
            )
        except DownloadCancelled:
            self.cancelled.emit()
        except DownloadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(str(destination))


class DownloaderWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YT downloader")
        icon_path = Path(__file__).resolve().parent / "logo.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._default_download_dir = self._determine_default_download_dir()

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        self.url_edit = QLineEdit()
        self.output_edit = QLineEdit(str(self._default_download_dir))
        self.filename_edit = QLineEdit()
        self.resolution_combo = QComboBox()
        for label, value in RESOLUTION_CHOICES:
            self.resolution_combo.addItem(label, value)

        self.video_radio = QRadioButton("Video")
        self.audio_radio = QRadioButton("Audio")
        self.video_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.video_radio)
        self._mode_group.addButton(self.audio_radio)
        self.video_radio.toggled.connect(self._update_mode_ui)
        self.audio_radio.toggled.connect(self._update_mode_ui)

        self.progress_bar = QProgressBar()
        self._set_progress_ready()

        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self.start_download)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_download)

        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self.choose_directory)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(16)

        form_layout = QFormLayout()
        form_layout.setLabelAlignment(Qt.AlignLeft)
        form_layout.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form_layout.setSpacing(14)
        form_layout.addRow("YouTube URL", self.url_edit)

        output_layout = QHBoxLayout()
        output_layout.addWidget(self.output_edit)
        output_layout.addWidget(browse_button)
        form_layout.addRow("Output directory", output_layout)

        form_layout.addRow("Custom filename", self.filename_edit)

        mode_layout = QHBoxLayout()
        mode_layout.addWidget(self.video_radio)
        mode_layout.addWidget(self.audio_radio)
        mode_layout.addStretch(1)
        form_layout.addRow("Mode", mode_layout)

        self._quality_label = QLabel("Quality")
        form_layout.addRow(self._quality_label, self.resolution_combo)

        card_layout.addLayout(form_layout)
        card_layout.addWidget(self.progress_bar)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.pause_button)
        button_layout.addWidget(self.download_button)
        card_layout.addLayout(button_layout)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.addWidget(card)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        self._thread: QThread | None = None
        self._worker: DownloadWorker | None = None
        self._control: WorkerControl | None = None
        self._last_progress_value: int | None = None
        self._last_progress_time = 0.0

        self._update_mode_ui()

    def choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select output directory",
            self.output_edit.text() or str(self._default_download_dir),
        )
        if directory:
            self.output_edit.setText(directory)

    def _determine_default_download_dir(self) -> Path:
        download_path = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        if download_path:
            return Path(download_path)

        fallback = Path.home() / "Downloads"
        if fallback.exists():
            return fallback

        return Path.cwd()

    def start_download(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter a YouTube video URL.")
            return

        output_dir = Path(self.output_edit.text().strip() or self._default_download_dir)
        filename = self.filename_edit.text().strip() or None
        audio_only = self.audio_radio.isChecked()
        resolution = self.resolution_combo.currentData() if not audio_only else None

        self.download_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.cancel_button.setEnabled(True)
        self.status_bar.showMessage("Downloading…")

        self._reset_progress_tracking()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")

        self._control = WorkerControl()

        self._thread = QThread()
        self._worker = DownloadWorker(url, output_dir, filename, audio_only, resolution, self._control)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.progress.connect(self._on_progress)
        self._worker.cancelled.connect(self._on_cancelled)

        self._worker.succeeded.connect(self._cleanup_thread)
        self._worker.failed.connect(self._cleanup_thread)
        self._worker.cancelled.connect(self._cleanup_thread)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()

    @Slot(str)
    def _on_success(self, destination: str) -> None:
        self.status_bar.showMessage(f"Saved to {destination}")
        QMessageBox.information(self, "Download complete", f"File saved to:\n{destination}")
        self.download_button.setEnabled(True)

    @Slot(str)
    def _on_failure(self, message: str) -> None:
        self.status_bar.showMessage(f"Error: {message}")
        QMessageBox.critical(self, "Download failed", message)
        self.download_button.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Error")

    @Slot()
    def _on_cancelled(self) -> None:
        self.status_bar.showMessage("Cancelled")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFormat("Cancelled")
        QMessageBox.information(self, "Download cancelled", "The download was cancelled.")

    @Slot()
    def _cleanup_thread(self) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
        self._control = None
        self.download_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pause")
        self.cancel_button.setEnabled(False)
        self._set_progress_ready()
        self._reset_progress_tracking()
        self.status_bar.showMessage("Ready")

    @Slot(object, str)
    def _on_progress(self, percent: object, message: str) -> None:
        if percent is None:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("Processing…")
            self.status_bar.showMessage("Processing…")
            self._reset_progress_tracking()
            return

        try:
            percent_value = float(percent)
        except (TypeError, ValueError):
            self.status_bar.showMessage("Downloading…")
            return

        clamped = max(0, min(100, int(round(percent_value))))
        if self._should_update_progress(clamped):
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(clamped)
            self.progress_bar.setFormat(f"{clamped}%")
            self.status_bar.showMessage(f"Downloading… {percent_value:.1f}%")

    def toggle_pause(self) -> None:
        if not self._control:
            return
        if self._control.is_paused():
            self._control.resume()
            self.pause_button.setText("Pause")
            self.status_bar.showMessage("Downloading…")
            self.progress_bar.setFormat(f"{self.progress_bar.value()}%")
        else:
            self._control.pause()
            self.pause_button.setText("Resume")
            self.status_bar.showMessage("Paused")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setFormat("Paused")

    def cancel_download(self) -> None:
        if not self._control:
            return
        self._control.cancel()
        self.cancel_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.status_bar.showMessage("Cancelling…")

    def _update_mode_ui(self) -> None:
        is_video = self.video_radio.isChecked()
        self.resolution_combo.setEnabled(is_video)
        self.resolution_combo.setVisible(is_video)
        self._quality_label.setVisible(is_video)

    def _set_progress_ready(self) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Ready")

    def _reset_progress_tracking(self) -> None:
        self._last_progress_value = None
        self._last_progress_time = 0.0

    def _should_update_progress(self, value: int) -> bool:
        now = time.monotonic()
        last_value = self._last_progress_value
        if (
            last_value is None
            or value >= 100
            or value - last_value >= 1
            or now - self._last_progress_time >= 0.2
        ):
            self._last_progress_value = value
            self._last_progress_time = now
            return True
        return False



def main() -> None:
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    window = DownloaderWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
