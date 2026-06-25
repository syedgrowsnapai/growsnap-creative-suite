# Premium dark theme for GrowSnap Dola Video Automation — GrowSnap AI Edition.

APP_STYLE = """
/* ─── Global ────────────────────────────────────────── */
QMainWindow, QWidget {
    background-color: #0A1810;
    color: #F0FDF4;
    font-family: "Sora", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}

QMainWindow {
    border: none;
}

/* ─── Title ─────────────────────────────────────────── */
QLabel#title {
    font-size: 32px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: #ffffff;
    padding: 6px 0px;
}

QLabel#subtitle {
    color: rgba(255, 255, 255, 0.65);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
}

QLabel#version_badge {
    color: #2ecc71;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    background: rgba(46, 204, 113, 0.08);
    border: 1px solid rgba(46, 204, 113, 0.22);
    border-radius: 6px;
    padding: 3px 10px;
}

/* ─── Cards (glassmorphism) ─────────────────────────── */
QFrame#card {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(18, 34, 24, 0.95),
        stop:1 rgba(14, 26, 18, 0.90));
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 14px;
}

QFrame#card:hover {
    border: 1px solid rgba(46, 204, 113, 0.22);
}

QFrame#stat_card {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(18, 34, 24, 0.95),
        stop:1 rgba(12, 22, 16, 0.90));
    border: 1px solid rgba(46, 74, 56, 0.40);
    border-radius: 12px;
    padding: 8px;
}

QFrame#stat_card:hover {
    border: 1px solid rgba(46, 204, 113, 0.25);
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(24, 46, 31, 0.98),
        stop:1 rgba(16, 30, 22, 0.95));
}

/* ─── Stat Values ───────────────────────────────────── */
QLabel#statValue {
    font-size: 28px;
    font-weight: 800;
    color: #2ecc71;
    letter-spacing: -0.5px;
}

QLabel#statLabel {
    color: rgba(255, 255, 255, 0.45);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
}

/* ─── Buttons ───────────────────────────────────────── */
QPushButton {
    background-color: #122218;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 10px;
    padding: 9px 18px;
    color: #F0FDF4;
    font-weight: 600;
    font-size: 13px;
}

QPushButton:hover {
    background-color: #182E1F;
    border-color: rgba(46, 204, 113, 0.35);
    color: #ffffff;
}

QPushButton:pressed {
    background-color: #0A1810;
}

QPushButton:disabled {
    background-color: #0c1a12;
    color: rgba(255, 255, 255, 0.3);
    border-color: #122218;
}

QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #22c55e, stop:1 #15803d);
    border: none;
    color: white;
    font-weight: 700;
    letter-spacing: 0.5px;
    padding: 10px 22px;
}

QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2ecc71, stop:1 #16a34a);
}

QPushButton#primary:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #15803d, stop:1 #14532d);
}

QPushButton#primary:disabled {
    background: #182E1F;
    color: rgba(255, 255, 255, 0.4);
}

QPushButton#danger {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #dc2626, stop:1 #ef4444);
    border: none;
    color: white;
    font-weight: 700;
}

QPushButton#danger:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #e83a3a, stop:1 #f45555);
}

QPushButton#danger:disabled {
    background: #3b1c1c;
    color: #7f5050;
}

QPushButton#nav_button {
    background-color: rgba(18, 34, 24, 0.5);
    border: 1px solid rgba(46, 74, 56, 0.40);
    border-radius: 8px;
    padding: 8px 16px;
    color: rgba(240, 253, 244, 0.7);
    font-weight: 600;
    font-size: 13px;
    min-height: 20px;
}

QPushButton#nav_button:hover {
    background-color: rgba(24, 46, 31, 0.7);
    border-color: rgba(46, 204, 113, 0.25);
    color: #ffffff;
}

QPushButton#nav_button:checked {
    background-color: rgba(46, 204, 113, 0.12);
    border: 1px solid rgba(46, 204, 113, 0.4);
    color: #2ecc71;
    font-weight: 700;
}

/* ─── Inputs ────────────────────────────────────────── */
QTextEdit, QPlainTextEdit, QLineEdit {
    background-color: #0c1a12;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 10px;
    padding: 10px 12px;
    color: #F0FDF4;
    font-size: 13px;
    selection-background-color: rgba(46, 204, 113, 0.3);
    selection-color: #ffffff;
}

QTextEdit:focus, QPlainTextEdit:focus, QLineEdit:focus {
    border-color: rgba(46, 204, 113, 0.5);
}

QSpinBox, QComboBox {
    background-color: #0c1a12;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 10px;
    padding: 5px 12px;
    color: #F0FDF4;
    font-size: 13px;
    min-height: 30px;
}

QSpinBox:focus, QComboBox:focus {
    border-color: rgba(46, 204, 113, 0.5);
}

QComboBox::drop-down {
    border: none;
    padding-right: 10px;
}

QComboBox QAbstractItemView {
    background-color: #122218;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 8px;
    color: #F0FDF4;
    selection-background-color: rgba(46, 204, 113, 0.25);
    padding: 4px;
}

/* ─── Checkboxes ────────────────────────────────────── */
QCheckBox {
    spacing: 8px;
    color: rgba(255, 255, 255, 0.75);
    font-weight: 500;
}

QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid rgba(46, 74, 56, 0.70);
    border-radius: 5px;
    background-color: #0c1a12;
}

QCheckBox::indicator:checked {
    background-color: #2ecc71;
    border-color: #2ecc71;
}

QCheckBox::indicator:hover {
    border-color: #2ecc71;
}

/* ─── Tables ────────────────────────────────────────── */
QTableWidget {
    background-color: #0A1810;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 12px;
    gridline-color: #122218;
    font-size: 12px;
    selection-background-color: rgba(46, 204, 113, 0.22);
    selection-color: #ffffff;
}

QTableWidget::item {
    padding: 6px 10px;
    border-bottom: 1px solid rgba(46, 74, 56, 0.2);
}

QTableWidget::item:selected {
    background-color: rgba(46, 204, 113, 0.22);
    color: #ffffff;
}

QHeaderView::section {
    background-color: #122218;
    color: rgba(255, 255, 255, 0.45);
    border: none;
    border-bottom: 2px solid rgba(46, 74, 56, 0.55);
    padding: 10px 12px;
    font-weight: 700;
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
}

/* ─── Lists ─────────────────────────────────────────── */
QListWidget {
    background-color: #0c1a12;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 10px;
    padding: 4px;
}

QListWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
    margin: 1px 2px;
}

QListWidget::item:selected {
    background-color: rgba(46, 204, 113, 0.15);
    color: #ffffff;
}

QListWidget::item:hover:!selected {
    background-color: rgba(46, 204, 113, 0.06);
}

/* ─── Progress Bar ──────────────────────────────────── */
QProgressBar {
    border: none;
    border-radius: 6px;
    background-color: #122218;
    height: 8px;
    text-align: center;
}

QProgressBar::chunk {
    border-radius: 6px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2ecc71, stop:0.5 #22c55e, stop:1 #2ecc71);
}

/* ─── Group Boxes ───────────────────────────────────── */
QGroupBox {
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 12px;
    margin-top: 12px;
    padding: 18px 12px 12px 12px;
    font-weight: 700;
    font-size: 11px;
    color: rgba(255, 255, 255, 0.65);
    letter-spacing: 1px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 12px;
    color: rgba(255, 255, 255, 0.65);
}

/* ─── Tab Widget ────────────────────────────────────── */
QTabWidget::pane {
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 12px;
    background-color: #0A1810;
    top: -1px;
}

QTabBar::tab {
    background-color: #122218;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    padding: 10px 24px;
    color: rgba(255, 255, 255, 0.45);
    font-weight: 600;
    font-size: 12px;
    margin-right: 3px;
    letter-spacing: 0.5px;
}

QTabBar::tab:selected {
    background-color: #0A1810;
    color: #2ecc71;
    border-bottom-color: #0A1810;
    font-weight: 700;
}

QTabBar::tab:hover:!selected {
    background-color: #182E1F;
    color: rgba(255, 255, 255, 0.65);
}

/* ─── Scrollbars ────────────────────────────────────── */
QScrollBar:vertical {
    background-color: transparent;
    width: 8px;
    margin: 4px 0px;
}

QScrollBar::handle:vertical {
    background-color: rgba(46, 74, 56, 0.70);
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background-color: #2ecc71;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar:horizontal {
    background-color: transparent;
    height: 8px;
    margin: 0px 4px;
}

QScrollBar::handle:horizontal {
    background-color: rgba(46, 74, 56, 0.70);
    border-radius: 4px;
    min-width: 30px;
}

QScrollBar::handle:horizontal:hover {
    background-color: #2ecc71;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* ─── Splitter ──────────────────────────────────────── */
QSplitter::handle {
    background-color: rgba(46, 74, 56, 0.55);
    width: 2px;
}

QSplitter::handle:hover {
    background-color: #2ecc71;
}

/* ─── Tooltips ──────────────────────────────────────── */
QToolTip {
    background-color: #122218;
    color: #F0FDF4;
    border: 1px solid rgba(46, 74, 56, 0.55);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}

/* ─── Status-aware labels ───────────────────────────── */
QLabel#status_ready {
    color: #22c55e;
    font-weight: 700;
    font-size: 11px;
    letter-spacing: 1px;
}

QLabel#status_running {
    color: #D97706;
    font-weight: 700;
    font-size: 11px;
    letter-spacing: 1px;
}

QLabel#timer_label {
    font-weight: 700;
    color: #2ecc71;
    font-size: 15px;
    letter-spacing: 0.5px;
}

QLabel#made_by {
    color: #2ecc71;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
}
"""

STATUS_COLORS = {
    'pending': '#94a3b8',
    'running': '#e8eaed',
    'waiting': '#D97706',
    'downloading': '#2ecc71',
    'completed': '#22c55e',
    'submitted': '#2ecc71',
    'failed': '#ef4444',
    'not found': '#D97706',
    'skipped': '#94a3b8',
    'cancelled': '#64748b',
}

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel
from PyQt6.QtGui import QPainter, QLinearGradient, QColor, QPen

class GradientLabel(QLabel):
    def __init__(self, text, parent=None, font_size=28, bold=True):
        super().__init__(text, parent)
        self.gradient_start = QColor("#2ecc71")
        self.gradient_end = QColor("#D9CB04")
        
        f = self.font()
        f.setFamily("Sora")
        f.setPointSize(font_size)
        f.setBold(bold)
        self.setFont(f)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        rect = self.contentsRect()
        align = self.alignment()
        
        # Calculate horizontal text bounds for precise gradient alignment
        fm = painter.fontMetrics()
        text_width = fm.horizontalAdvance(self.text())
        
        if align & Qt.AlignmentFlag.AlignHCenter:
            left = rect.left() + (rect.width() - text_width) / 2.0
        elif align & Qt.AlignmentFlag.AlignRight:
            left = rect.right() - text_width
        else:
            left = rect.left()
            
        right = left + max(1, text_width)
        
        # Linear gradient horizontally across the text letters
        gradient = QLinearGradient(float(left), 0.0, float(right), 0.0)
        gradient.setColorAt(0.0, self.gradient_start)
        gradient.setColorAt(1.0, self.gradient_end)
        
        pen = QPen()
        pen.setBrush(gradient)
        painter.setPen(pen)
        painter.setFont(self.font())
        
        painter.drawText(rect, align, self.text())
        painter.end()


