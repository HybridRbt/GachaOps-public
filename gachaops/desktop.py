"""Small Windows frontend. Game execution stays in a separate supervisor."""
import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import traceback
import uuid

from .app_paths import workspace_root
from .desktop_model import NAMES, STATUS, selected_configuration, save_configuration, worker_command, elevation_command, register_bettergi, desktop_command
from .assistants import load_assistants, PENDING_ADAPTERS
from .windows_observation import elevation_report
from .result_details import details_for
from .recovery import read_state, RecoveryRequired
from .profiles import ProfileStore

# These adapters implement manual preflight, borrowed game identity and cleanup.
from .adapter_catalog import list_adapters, get_adapter
from .registration import inspect_registration, commit_registration

MANUAL_START_ADAPTERS = frozenset(s.adapter_id for s in list_adapters() if 'manual' in s.start_modes)


def worker(arguments):
    # Windowed executables have no stdout/stderr. Keep a real log for both CLI
    # output and errors; don't depend on a terminal or Qt in the worker process.
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', required=True)
    options, remaining = parser.parse_known_args(arguments)
    if getattr(sys, 'frozen', False) and os.name == 'nt':
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    with open(options.log, 'w', encoding='utf-8', buffering=1) as stream:
        sys.stdout = sys.stderr = stream
        sys.argv = ['gachaops', *remaining]
        try:
            from .__main__ import main as cli_main
            return cli_main()
        except BaseException:
            traceback.print_exc()
            return 2


AVATARS = {
    'wuthering-waves': ('#e2f5e9', '#177a3e'), 'zenless-zone-zero': ('#fdeeda', '#b45309'),
    'endfield': ('#dcf1f4', '#0e7490'), 'girls-frontline-2': ('#e7e9fb', '#4353c9'),
    'neverness-to-everness': ('#ebe5fa', '#6d3fc0'), 'genshin': ('#eef1f5', '#8a94a3'),
    'honkai-star-rail': ('#fae7ee', '#b3446c')}
ICON_DIR = Path(__file__).parent / 'assets' / 'icons'
SUBTITLE_IDLE = '选好助手，一次开始。任务沿用各助手中已保存的设置。'
SUBTITLE_RUNNING = '运行中，配置已锁定。可随时停止并清理。'
BLOCKER_TEXT = {
    'version_unsupported': '版本或启动协议尚未核验',
    'permission_or_desktop': '权限或桌面状态需要处理',
    'native_setup_required': '原助手安装或原生设置需要处理',
    'maagf2_mxu_unsafe_native_action': '少前2预设含未核验的 MXU 特殊动作，请在原助手处理',
    'maagf2_mxu_disable_external_triggers': '少前2的独立任务触发入口需要关闭',
    'maaend_disable_native_launch_task': '手动模式：请在 MaaEnd 当前实例中禁用内置“启动游戏（PC）”任务',
    'maaend_disable_launch_preactions': '手动模式：请在 MaaEnd 当前实例中禁用所有启动前动作，保留 Endfield.exe 路径记录',
    'native_command_protocol_unverified': '助手版本已变化，启动通信尚未核验；需要更新 GachaOps 的兼容支持，解除隔离不能解决此项。',
    'bettergi_direct_game_start_required': '请在 BetterGI 中启用自动进入游戏，并关闭命令行启动游戏',
    'bettergi_game_arguments_unverified': 'BetterGI 的附加游戏启动参数尚未核验',
    'bettergi_disable_cli_update_required': '请在 BetterGI 中关闭命令行运行前自动更新',
    'supervisor_elevation_required': '需要管理员权限',
    'manual_game_requires_exactly_one_running_instance': '请先手动进入游戏，并只保留一个游戏窗口',
    'existing_runner_not_exclusive': '请退出已有助手',
    'existing_runner_or_game_not_exclusive': '请退出已有助手和该游戏',
    'disable_auto_start_game_in_assistant_gui': '请在助手中关闭自动启动游戏',
    'game_path_unverified': '助手保存的游戏路径需要检查',
    'desktop_unavailable': '请保持桌面解锁且可操作',
    'admin_requirement_unknown': '助手的管理员启动要求需要核实',
    'official_launcher_path_unverified': '官方启动器路径需要检查',
    'march7_local_game_required': '三月七目前只接入本地游戏模式，请在助手中关闭云游戏',
    'march7_select_exit_in_assistant_gui': '请在三月七助手中将任务完成后操作设为 Exit（退出）',
    'march7_complete_native_first_setup': '请先打开三月七原生 GUI 完成首次设置',
    'march7_info_logging_required': '请在三月七助手中启用 INFO 或 DEBUG 日志'}
BLOCKER_SUGGESTIONS = {
    'maagf2_mxu_unsafe_native_action': '在少前2原助手中关闭按名称结束进程、外部程序、关机等特殊任务，保存并退出后重新检查；GachaOps 不会代改预设。',
    'maagf2_mxu_disable_external_triggers': '在少前2原助手中关闭定时策略、Web 控制、全局热键和独立自动运行，保存并退出后重新检查，由 GachaOps 统一开始。',
    'maaend_disable_launch_preactions': '打开 MaaEnd 的当前日常实例，关闭启动前动作后保存并退出助手；用原生启动器进入游戏，再重新检查。',
    'supervisor_elevation_required': '点击开始运行时在 UAC 弹窗确认；程序会以管理员身份重新打开。',
    'manual_game_requires_exactly_one_running_instance': '先手动进入游戏并只保留一个窗口，再点开始运行。',
    'existing_runner_not_exclusive': '退出正在运行的助手程序后点“重新检查”。',
    'existing_runner_or_game_not_exclusive': '退出助手和游戏窗口后点“重新检查”。',
    'disable_auto_start_game_in_assistant_gui': '在助手中关闭自动启动游戏后点“重新检查”。',
    'game_path_unverified': '打开助手确认能正常进入游戏，再点“重新检查”。',
    'desktop_unavailable': '解锁桌面并保持可操作后点“重新检查”。',
    'admin_requirement_unknown': '在助手中核实管理员启动要求后点“重新检查”。',
    'official_launcher_path_unverified': '在助手或系统中核实官方启动器路径后点“重新检查”。',
    'march7_local_game_required': '在三月七助手中关闭云游戏模式。',
    'march7_select_exit_in_assistant_gui': '把任务完成后操作设为 Exit（退出）后点“重新检查”。',
    'march7_complete_native_first_setup': '先打开三月七原生 GUI 完成首次设置。',
    'march7_info_logging_required': '在三月七助手中启用 INFO 或 DEBUG 日志。'}

STATUS_VERB = {'success': '任务完成', 'partial_failure': '任务完成，含失败项', 'failed': '任务失败',
               'timeout': '任务超时', 'completion_unconfirmed': '结果未确认', 'cancelled': '已停止',
               'blocked': '启动被阻止', 'running': '运行中', 'starting': '正在启动',
               'desktop_environment_interrupted': '桌面环境中断'}
RUNNING_PILL = {'success': ('已完成', '#e5f4ec', '#177a3e'), 'partial_failure': ('部分失败', '#fdf0dd', '#b45309'),
                'desktop_environment_interrupted': ('桌面中断', '#fdf0dd', '#b45309'),
                'failed': ('失败', '#fdeaea', '#c0392b'), 'timeout': ('超时', '#fdf0dd', '#b45309'),
                'completion_unconfirmed': ('结果未确认', '#fdf0dd', '#b45309'),
                'blocked': ('需处理', '#fdf0dd', '#b45309'), 'cancelled': ('已停止', '#eef1f5', '#64748b'),
                'starting': ('运行中', '#e3edff', '#1d4ed8'), 'running': ('运行中', '#e3edff', '#1d4ed8'),
                'execution_error': ('运行异常', '#fdeaea', '#c0392b'),
                'interrupted': ('已中断', '#fdf0dd', '#b45309'),
                'ownership_or_cleanup_unconfirmed': ('清理未确认', '#fdf0dd', '#b45309'),
                'stopped_cleanup_unconfirmed': ('清理未确认', '#fdf0dd', '#b45309'),
                'stopped_external_game_running': ('手动游戏仍在运行', '#fdf0dd', '#b45309')}


SEG_COLORS = {'success': '#13795b', 'partial_failure': '#c77d00', 'failed': '#c77d00',
              'timeout': '#c77d00', 'completion_unconfirmed': '#c77d00', 'blocked': '#c77d00',
              'desktop_environment_interrupted': '#c77d00', 'starting': '#1f5fd9',
              'running': '#1f5fd9', 'execution_error': '#c0392b', 'interrupted': '#c77d00',
              'ownership_or_cleanup_unconfirmed': '#c77d00', 'stopped_cleanup_unconfirmed': '#c77d00',
              'cancelled': '#64748b', 'stopped_external_game_running': '#64748b'}
SEG_QUEUED = '#9aa4b8'


def fmt_dur(seconds):
    """m:ss under an hour, h:mm:ss beyond it; minutes carry no leading zero."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def sanitize_detail(text):
    """Surface real failure data without raw errno tokens or internal markers."""
    text = str(text)
    bettergi_errors = {
        'missing or external BetterGI installation/configuration entry': '所选目录缺少 BetterGI.exe 或已保存配置，请选择完整安装目录',
        'select and save a BetterGI one-dragon preset first': '请先在 BetterGI 中选择并保存一条龙配置',
        'BetterGI preset name does not match its saved file': 'BetterGI 配置名称与文件不一致，请在助手中重新保存',
        'duplicate BetterGI preset names; automatic target is ambiguous': 'BetterGI 存在同名配置，请在助手中改名后重试',
        'BetterGI completion action must be 关闭游戏和软件; change it in BetterGI': '请在 BetterGI 中将结束操作设为“关闭游戏和软件”',
        'BetterGI resume marker is set; choose full run in BetterGI first': 'BetterGI 已标记续跑任务，请先在助手中选择从头运行',
        'BetterGI saved task order/definitions cannot be verified': 'BetterGI 保存的任务顺序无法核验，请在助手中重新保存',
        'BetterGI first integration supports nonempty built-in one-dragon tasks only': '本次接入支持内置一条龙任务；请至少启用一项，自定义脚本组暂不支持',
        'BetterGI entry must be installation_root/BetterGI.exe': '请选择安装目录中的 BetterGI.exe',
    }
    text = bettergi_errors.get(text, text)
    text = re.sub(r'BetterGI version (\S+) is not verified; supported: (\S+)',
                  r'BetterGI \1 尚未核验，当前支持版本为 \2', text)
    text = re.sub(r'\[(?:Errno|WinError)[^\]]*\]', '系统调用失败', text)
    text = re.sub(r'integration_pending:\s*\S+;?', '助手尚未接入', text)
    text = text.replace('saved daily entry unavailable or ambiguous; integration must be checked',
                        '日常入口不可用或存在歧义，需人工核对安装')
    text = re.sub(r'Traceback \(most recent call last\):[\s\S]*', '程序内部异常，详情见运行日志', text)
    text = re.sub(r'(?<![\w:/.])[A-Za-z]:\\[^\s"\'，。；]*', '程序文件路径', text)
    text = re.sub(r'(?<![\w:/.])/(?:[\w.-]+/)+[\w.-]*', '程序文件路径', text)
    text = re.sub(r'0x[0-9a-fA-F]+', '#', text)
    text = re.sub(r'\bgachaops(?:\.\w+)+', '程序模块', text)
    return text.strip('：: ,') or '启动条件未通过'


def check_indicator_style():
    """Boxed checkbox with a drawn check glyph; Fusion on Windows loses the frame.

    The combo arrow is a generated triangle because the native arrow bitmap
    bleeds through styled combos on Windows as a stray corner line."""
    import tempfile
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
    temp = Path(tempfile.gettempdir())
    check = temp / 'gachaops-check-indicator.png'
    if not check.exists():
        pixmap = QPixmap(12, 12)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        pen = QPen(QColor('white'), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.drawPolyline(QPolygonF([QPointF(2.5, 6.5), QPointF(5.0, 9.0), QPointF(9.5, 3.0)]))
        painter.end()
        pixmap.save(str(check))
    arrow = temp / 'gachaops-combo-arrow.png'
    if not arrow.exists():
        pixmap = QPixmap(10, 6)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#64748b'))
        painter.drawPolygon(QPolygonF([QPointF(0, 0.5), QPointF(10, 0.5), QPointF(5, 5.5)]))
        painter.end()
        pixmap.save(str(arrow))
    return f'''
        QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid #c3cfe0; border-radius: 4px; background: white; }}
        QCheckBox::indicator:hover {{ border-color: #7ea6e8; }}
        QCheckBox::indicator:checked {{ background: #2563eb; border: 1px solid #2563eb; image: url({check.as_posix()}); }}
        QCheckBox::indicator:disabled {{ border: 1px solid #e4e9f2; background: #f1f5f9; }}
        QComboBox::drop-down {{ border: 0; width: 26px; background: transparent; }}
        QComboBox::down-arrow {{ width: 10px; height: 6px; image: url({arrow.as_posix()}); margin-right: 10px; }}
        QFrame#hline {{ border: 0; border-top: 1px solid #eef2f9; }}
        QComboBox:disabled {{ background: #eef1f6; color: #97a3b5; border-color: #e2e8f0; }}
        QFrame#segBox {{ background: #f1f5fb; border-radius: 8px; }}
        QPushButton#segView {{ background: transparent; border: 0; color: #64748b; padding: 5px 12px; border-radius: 6px; font-size: 12px; }}
        QPushButton#segView:checked {{ background: #e3edff; color: #1d4ed8; font-weight: 600; }}
        QPushButton#segView:hover {{ color: #1d4ed8; }}
        QPushButton#removeRow {{ background: transparent; border: 0; color: #64748b; font-size: 15px; padding: 2px 8px; border-radius: 5px; }}
        QPushButton#removeRow:hover {{ background: #fdeaea; color: #c0392b; }}
    '''


def configure_appearance(app):
    """Keep the light interface readable even when Windows supplies a dark palette."""
    from PySide6.QtGui import QColor, QFont, QPalette

    app.setStyle('Fusion')
    palette = QPalette()
    colors = {
        'Window': '#f5f7fb', 'WindowText': '#334155',
        'Base': '#ffffff', 'AlternateBase': '#edf2fa', 'Text': '#334155',
        'Button': '#ffffff', 'ButtonText': '#334155',
        'Highlight': '#dce9ff', 'HighlightedText': '#172554',
        'PlaceholderText': '#64748b', 'ToolTipBase': '#ffffff',
        'ToolTipText': '#334155', 'Link': '#2563eb', 'LinkVisited': '#7c3aed',
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
                  QPalette.ColorGroup.Disabled):
        for role, color in colors.items():
            palette.setColor(group, getattr(QPalette.ColorRole, role), QColor(color))
    for role in ('WindowText', 'Text', 'ButtonText'):
        palette.setColor(QPalette.ColorGroup.Disabled,
                         getattr(QPalette.ColorRole, role), QColor('#64748b'))
    app.setPalette(palette)
    app.setFont(QFont('Microsoft YaHei UI', 10))
    app.setStyleSheet('''
        QMainWindow { background: #f5f7fb; }
        QLabel { color: #334155; }
        QLabel#title { font-size: 26px; font-weight: 700; color: #16233b; }
        QLabel#subtitle { color: #64748b; }
        QFrame#card { background: white; border: 1px solid #e2e9f3; border-radius: 12px; }
        QFrame#progressCard { background: #ecf3fe; border: 1px solid #d7e4fa; border-radius: 12px; }
        QLabel#cardTitle { font-size: 15px; font-weight: 600; color: #1e293b; }
        QLabel#linkButton { color: #2563eb; }
        QPushButton#linkButton { background: transparent; border: 0; color: #2563eb; font-weight: 600; padding: 4px 8px; }
        QPushButton#linkButton:hover { color: #1d4ed8; }
        QPushButton { background: white; color: #334155; border: 1px solid #d5deec; border-radius: 7px; padding: 8px 15px; }
        QPushButton:hover { background: #eaf1ff; }
        QPushButton:disabled { background: #edf0f5; color: #97a3b5; }
        QPushButton#primary { background: #2563eb; color: white; border: 1px solid #2563eb; }
        QPushButton#primary:hover { background: #1d4ed8; }
        QPushButton#primary:disabled { background: #b8c9ec; border-color: #b8c9ec; }
        QPushButton#danger { background: white; color: #d0342c; border: 1px solid #f0c4c0; }
        QPushButton#danger:hover { background: #fdf1f0; }
        QPushButton#small { padding: 5px 12px; font-size: 12px; }
        QComboBox { color: #334155; background: white; border: 1px solid #d5deec; border-radius: 6px; padding: 7px; }
        QPlainTextEdit { color: #334155; background: white; border: 1px solid #dce3ef; border-radius: 8px; }
        QComboBox QAbstractItemView { background: white; color: #334155; selection-background-color: #dce9ff; selection-color: #172554; }
        QHeaderView::section { background: #f2f6fc; border: 0; padding: 6px; color: #7b8aa0; font-weight: 600; }
        QTableWidget { background: white; border: 0; }
        QTableWidget::item { padding: 4px; }
        QTableWidget::item:selected { background: transparent; }
        QCheckBox { padding-left: 2px; }
        QProgressBar { background: #dbe7fb; border: 0; border-radius: 3px; max-height: 6px; }
        QProgressBar::chunk { background: #2563eb; border-radius: 3px; }
        QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
        QScrollBar::handle:vertical { background: #c9d4e5; border-radius: 4px; min-height: 30px; }
        QScrollBar::handle:vertical:hover { background: #b3c2d8; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
        QScrollBar::handle:horizontal { background: #c9d4e5; border-radius: 4px; min-width: 30px; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
    ''' + check_indicator_style())


def main():
    if '--march7-worker' in sys.argv[1:]:
        from .march7 import worker_main
        return worker_main(sys.argv[sys.argv.index('--march7-worker') + 1:])
    if '--worker' in sys.argv[1:]:
        return worker(sys.argv[sys.argv.index('--worker') + 1:])
    from PySide6.QtCore import QRectF, Qt, QProcess, QProcessEnvironment, QTimer, QUrl
    from PySide6.QtGui import QColor, QBrush, QCursor, QDesktopServices, QFont, QIcon, QIntValidator, QPainter, QPainterPath, QPixmap
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QSpinBox,
                                  QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QDialog,
                                  QPushButton, QPlainTextEdit, QSizePolicy, QTableWidget, QInputDialog,
                                  QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QFormLayout, QDialogButtonBox)

    class SegmentBar(QWidget):
        """Whole-run progress: one segment per participant colored by its state."""
        def __init__(self):
            super().__init__()
            self.segments = []
            self.setMinimumHeight(6)

        def set_segments(self, statuses):
            self.segments = list(statuses)
            self.update()

        def paintEvent(self, event):
            if not self.segments:
                return
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            width, height = self.width(), self.height()
            count = len(self.segments)
            gap = 2 if count > 1 else 0
            seg_w = (width - gap * (count - 1)) / count
            x = 0.0
            for status in self.segments:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(SEG_COLORS.get(status, SEG_QUEUED)))
                painter.drawRoundedRect(QRectF(x, 0, seg_w, height), 3, 3)
                x += seg_w + gap
            painter.end()

    def pill_label(text, background, color, border=None):
        label = QLabel(text)
        border_style = f' border: 1px solid {border};' if border else ''
        label.setStyleSheet(f'color: {color}; background: {background};{border_style}'
                            ' border-radius: 9px; padding: 3px 10px; font-size: 12px; font-weight: 600;')
        return label

    def badge_label(text, background, color, size=24):
        label = QLabel(text)
        label.setFixedSize(size, size)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(f'color: {color}; background: {background}; border-radius: {size // 2}px;'
                            f' font-size: {size // 2 + 1}px; font-weight: 700;')
        return label

    icon_cache = {}

    class AvatarLabel(QLabel):
        # The style grays disabled pixmaps; the locked table must keep real icons colored.
        def paintEvent(self, event):
            pixmap = self.pixmap()
            if pixmap is not None and not pixmap.isNull():
                painter = QPainter(self)
                painter.drawPixmap(0, 0, pixmap)
                painter.end()
            else:
                super().paintEvent(event)

    def avatar_label(assistant_id, name, pending, size=30):
        pixmap = icon_cache.get((assistant_id, size, pending), False)
        if pixmap is False:
            source = ICON_DIR / f'{assistant_id}.png'
            base = QPixmap(str(source)) if source.exists() else QPixmap()
            if base.isNull():
                pixmap = None
            else:
                scaled = base.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                circular = QPixmap(size, size)
                circular.fill(Qt.GlobalColor.transparent)
                painter = QPainter(circular)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                clip = QPainterPath()
                clip.addEllipse(0, 0, size, size)
                painter.setClipPath(clip)
                painter.drawPixmap(0, 0, scaled)
                if pending:
                    painter.fillRect(0, 0, size, size, QColor(238, 241, 245, 160))
                painter.end()
                pixmap = circular
            icon_cache[(assistant_id, size, pending)] = pixmap
        if pixmap is None:
            background, color = AVATARS.get(assistant_id, ('#edf0f4', '#8a94a3'))
            return badge_label(name[0], background, color, size)
        label = AvatarLabel()
        label.setFixedSize(size, size)
        label.setPixmap(pixmap)
        return label

    class Table(QTableWidget):
        def __init__(self, window):
            super().__init__(0, 6)
            self.host = window
            self.setHorizontalHeaderLabels(['', '游戏 / 助手', '游戏启动', '最长等待', '状态', ''])
            self.verticalHeader().hide(); self.setShowGrid(False)
            self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.setDragEnabled(True); self.setAcceptDrops(True)
            self.setDragDropMode(QTableWidget.DragDropMode.DragDrop)
            self.setDefaultDropAction(Qt.MoveAction)
            header = self.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
            self.setColumnWidth(0, 84); self.setColumnWidth(2, 190)
            self.setColumnWidth(3, 96); self.setColumnWidth(5, 44)
            self.setColumnHidden(5, True)
            self.setMouseTracking(True)
            self._hover_row = None
            # Cell widgets swallow hover events, so track the cursor by polling;
            # the poll only maps one point and repaints at most two rows.
            self._hover_timer = QTimer(self)
            self._hover_timer.setInterval(90)
            self._hover_timer.timeout.connect(self.refresh_hover)
            self._hover_timer.start()
            header.model().setHeaderData(1, Qt.Horizontal, Qt.AlignLeft | Qt.AlignVCenter,
                                         Qt.TextAlignmentRole)
            for column, alignment in ((2, Qt.AlignHCenter), (3, Qt.AlignHCenter), (4, Qt.AlignHCenter)):
                header.model().setHeaderData(column, Qt.Horizontal, alignment, Qt.TextAlignmentRole)

        def dragEnterEvent(self, event):
            if self.host.can_reorder():
                event.acceptProposedAction()

        def dragMoveEvent(self, event):
            event.acceptProposedAction()

        def dropEvent(self, event):
            source, target = self.currentRow(), self.indexAt(event.position().toPoint()).row()
            if self.host.can_reorder() and 0 <= target < self.rowCount() and source != target:
                self.host.move_row(source, target)
            event.acceptProposedAction()

        def refresh_hover(self):
            if self.isVisible():
                pos = self.viewport().mapFromGlobal(self._cursor_global_pos())
                # A row index from y alone would highlight rows beside the table.
                if not self.viewport().rect().contains(pos): row = None
                else:
                    row = self.rowAt(pos.y())
                    if not 0 <= row < self.rowCount(): row = None
            else:
                row = None
            self.set_hover_row(row)

        def _cursor_global_pos(self):
            return QCursor.pos()

        def set_hover_row(self, row):
            if row == self._hover_row: return
            previous, self._hover_row = self._hover_row, row
            for changed in (previous, row):
                if changed is not None and 0 <= changed < self.rowCount():
                    self.host.apply_row_hover(changed, changed == row)

        def repaint_hover(self):
            """render() rebuilds items with base colors; restore the hover paint."""
            row = self._hover_row
            if row is not None and 0 <= row < self.rowCount():
                self.host.apply_row_hover(row, True)

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.workspace = (workspace_root(sys.argv[sys.argv.index('--data-root') + 1])
                              if '--data-root' in sys.argv else workspace_root())
            self.profile_store = ProfileStore(self.workspace)
            self.profile_ids = {}
            self.profile_names = {}
            self.data = None
            self.process = None
            self.session = None
            self.action = None
            self.summary_path = None
            self.check_report = None
            self.log_seen = 0
            self.started = None
            self.node_started = None
            self.results = {}
            self.finished_report_key = None
            self.selected_ids = None
            self.view = 'selected'
            self.undo_info = None
            self.check_origin = None
            self.stop_requested = False
            self.row_tints = {}
            self.check_collapsed = False
            self.closing = False
            self.setWindowTitle('GachaOps — 助手编排')
            self.setWindowIcon(QIcon(str(Path(__file__).parent / 'assets/branding/program.png')))
            self.resize(1080, 960); self.setMinimumSize(960, 800)
            body = QWidget(); outer = QVBoxLayout(body)
            outer.setContentsMargins(24, 12, 24, 10); outer.setSpacing(7)
            title = QLabel('GachaOps'); title.setObjectName('title')
            self.subtitle = QLabel(SUBTITLE_IDLE); self.subtitle.setObjectName('subtitle')
            outer.addWidget(title); outer.addWidget(self.subtitle)
            outer.addSpacing(4)
            profiles = QHBoxLayout()
            profiles.addWidget(QLabel('运行组合'))
            self.profiles = QComboBox(); self.profiles.setMinimumWidth(280); self.profiles.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToContents)
            if '--config' not in sys.argv:
                for profile in self.profile_store.list_profiles():
                    self.profiles.addItem(profile['name'], profile['path'])
                    self.profile_ids[profile['path']] = profile['id']
                    self.profile_names[profile['path']] = profile['name']
            if '--config' in sys.argv:
                path = sys.argv[sys.argv.index('--config') + 1]
                self.profiles.addItem('已选择的组合', path)
                self.profiles.setCurrentIndex(self.profiles.count() - 1)
            profiles.addWidget(self.profiles, 1)
            self.choose = QPushButton('导入'); self.choose.clicked.connect(self.browse)
            self.save_button = QPushButton('保存组合'); self.save_button.clicked.connect(self.save_selection)
            profiles.addWidget(self.choose); profiles.addWidget(self.save_button)
            self.new_profile = QPushButton('新建'); self.new_profile.clicked.connect(self.prompt_new_profile)
            self.rename_profile = QPushButton('改名'); self.rename_profile.clicked.connect(self.prompt_rename_profile)
            self.delete_profile = QPushButton('删除'); self.delete_profile.clicked.connect(self.prompt_delete_profile)
            for button in (self.new_profile, self.rename_profile, self.delete_profile): profiles.addWidget(button)
            outer.addLayout(profiles)

            self.progress_card = QFrame(); self.progress_card.setObjectName('progressCard')
            progress_layout = QVBoxLayout(self.progress_card)
            progress_layout.setContentsMargins(16, 8, 16, 8); progress_layout.setSpacing(6)
            running_row = QHBoxLayout()
            self.running_title = QLabel(); self.running_title.setObjectName('cardTitle')
            running_row.addWidget(self.running_title); running_row.addStretch()
            self.stop = QPushButton('停止并清理'); self.stop.setObjectName('danger')
            self.stop.clicked.connect(self.request_stop); self.stop.setEnabled(False)
            running_row.addWidget(self.stop)
            progress_layout.addLayout(running_row)
            stats = QHBoxLayout(); stats.setSpacing(48)
            self.stat_labels = {}
            for key, name in (('node', '本个已用时'), ('left', '距超时还剩'), ('total', '本轮累计')):
                box = QVBoxLayout(); box.setSpacing(2)
                caption = QLabel(name); caption.setStyleSheet('color: #64748b; font-size: 12px;')
                value = QLabel('00:00'); value.setStyleSheet('color: #1e293b; font-size: 20px; font-weight: 700;')
                box.addWidget(caption); box.addWidget(value); stats.addLayout(box)
                self.stat_labels[key] = value
            stats.addStretch()
            progress_layout.addLayout(stats)
            self.segments = SegmentBar()
            progress_layout.addWidget(self.segments)
            outer.addWidget(self.progress_card)
            self.progress_card.hide()

            self.table_card = QFrame(); self.table_card.setObjectName('card')
            table_layout = QVBoxLayout(self.table_card)
            table_layout.setContentsMargins(16, 12, 16, 10); table_layout.setSpacing(6)
            self.table = Table(self)
            table_layout.addWidget(self.table)
            self.table_header = self.table.horizontalHeader()
            self.empty_state = QWidget()
            empty_layout = QVBoxLayout(self.empty_state)
            empty_layout.addStretch()
            empty_caption = QLabel('还没有选择助手'); empty_caption.setAlignment(Qt.AlignCenter)
            empty_caption.setStyleSheet('color: #64748b; font-size: 14px;')
            empty_layout.addWidget(empty_caption)
            empty_row = QHBoxLayout(); empty_row.addStretch()
            self.go_select = QPushButton('去选择'); self.go_select.setObjectName('primary')
            self.go_select.clicked.connect(lambda: self.set_view('all'))
            empty_row.addWidget(self.go_select); empty_row.addStretch()
            empty_layout.addLayout(empty_row); empty_layout.addStretch()
            table_layout.addWidget(self.empty_state)
            self.empty_state.hide()
            rule = QFrame(); rule.setObjectName('hline'); rule.setFrameShape(QFrame.Shape.HLine)
            table_layout.addWidget(rule)
            table_footer = QHBoxLayout(); table_footer.setSpacing(12)
            self.hint = QLabel(); self.hint.setStyleSheet('color: #64748b;')
            self.undo_label = QLabel('撤销'); self.undo_label.setObjectName('linkButton')
            self.undo_label.setCursor(Qt.PointingHandCursor); self.undo_label.hide()
            self.undo_label.mousePressEvent = lambda _: self.undo_remove()
            self.view_all = QPushButton(); self.view_all.setObjectName('segView'); self.view_all.setCheckable(True)
            self.view_all.clicked.connect(lambda: self.set_view('all'))
            self.view_selected = QPushButton(); self.view_selected.setObjectName('segView'); self.view_selected.setCheckable(True)
            self.view_selected.clicked.connect(lambda: self.set_view('selected'))
            self.seg_box = QFrame(); self.seg_box.setObjectName('segBox')
            seg_layout = QHBoxLayout(self.seg_box)
            seg_layout.setContentsMargins(2, 2, 2, 2); seg_layout.setSpacing(2)
            seg_layout.addWidget(self.view_all); seg_layout.addWidget(self.view_selected)
            table_footer.addWidget(self.hint); table_footer.addWidget(self.undo_label)
            table_footer.addStretch(); table_footer.addWidget(self.seg_box)
            table_layout.addLayout(table_footer)
            outer.addWidget(self.table_card, 6)

            self.check_card = QFrame(); self.check_card.setObjectName('card')
            check_layout = QVBoxLayout(self.check_card)
            check_layout.setContentsMargins(20, 14, 20, 16); check_layout.setSpacing(10)
            check_header = QHBoxLayout()
            check_title = QLabel('启动前检查'); check_title.setObjectName('cardTitle')
            self.recheck = QPushButton('重新检查'); self.recheck.setObjectName('linkButton')
            self.recheck.setCursor(Qt.PointingHandCursor); self.recheck.clicked.connect(
                lambda: self.begin('check-assistants'))
            check_header.addWidget(check_title); check_header.addStretch(); check_header.addWidget(self.recheck)
            check_layout.addLayout(check_header)
            self.checklist = QVBoxLayout(); self.checklist.setSpacing(8)
            check_layout.addLayout(self.checklist)
            outer.addWidget(self.check_card, 0)

            self.run_card = QFrame(); self.run_card.setObjectName('card')
            run_layout = QVBoxLayout(self.run_card)
            run_layout.setContentsMargins(20, 14, 20, 16); run_layout.setSpacing(8)
            run_header = QHBoxLayout()
            run_title = QLabel('运行情况'); run_title.setObjectName('cardTitle')
            self.repeat_summary = QLabel(); self.repeat_summary.hide()
            self.repeat_summary.setStyleSheet('color: #64748b; font-size: 12px;')
            self.result = QPushButton('本轮报告'); self.result.setObjectName('small')
            self.recovery_button = QPushButton('检查并恢复运行资格'); self.recovery_button.setObjectName('small')
            self.recovery_button.clicked.connect(self.begin_recovery)
            self.result.setEnabled(False); self.result.clicked.connect(self.open_result)
            folder = QPushButton('运行记录'); folder.setObjectName('small')
            folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.workspace / 'runs'))))
            run_header.addWidget(run_title); run_header.addWidget(self.repeat_summary); run_header.addStretch()
            run_header.addWidget(self.result); run_header.addWidget(folder)
            run_header.addWidget(self.recovery_button)
            run_layout.addLayout(run_header)
            self.problems_box = QWidget(); self.problems_layout = QVBoxLayout(self.problems_box)
            self.problems_layout.setContentsMargins(0, 0, 0, 0); self.problems_layout.setSpacing(4)
            run_layout.addWidget(self.problems_box)
            self.problems_box.hide()
            self.log_caption = QLabel('日志'); self.log_caption.setStyleSheet('color: #94a3b8; font-size: 12px;')
            run_layout.addWidget(self.log_caption)
            self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(2000)
            self.log.setPlaceholderText('运行进度和需要处理的问题会显示在这里。')
            self.log.setFont(QFont('Consolas', 10)); self.log.setMinimumHeight(70)
            run_layout.addWidget(self.log, 1)
            self.run_placeholder = QLabel('开始运行后，这里显示进度与需处理的问题。')
            self.run_placeholder.setStyleSheet('color: #94a3b8; font-size: 12px;')
            run_layout.addWidget(self.run_placeholder)
            outer.addWidget(self.run_card, 2)

            bottom = QHBoxLayout()
            self.repeat_label = QLabel('失败后最多重复')
            self.repeat_input = QSpinBox(); self.repeat_input.setObjectName('repeatLimitInput')
            self.repeat_input.setRange(0, 2147483646); self.repeat_input.setSuffix(' 次')
            self.repeat_input.setToolTip('0 表示只运行一次；2 表示首次运行后最多再运行两轮。仅重复未成功的助手。')
            self.repeat_input.setMinimumWidth(90)
            bottom.addWidget(self.repeat_label); bottom.addWidget(self.repeat_input)
            bottom.addStretch()
            self.start = QPushButton('开始运行'); self.start.setObjectName('primary')
            self.start.setMinimumWidth(168)
            self.start.clicked.connect(self.begin_run)
            bottom.addWidget(self.start)
            outer.addLayout(bottom)
            self.setCentralWidget(body)
            self.outer_layout = outer
            self.set_problems([])
            self.profiles.currentIndexChanged.connect(self.load_profile)
            self.repeat_input.valueChanged.connect(lambda _: self.sync_combo_label())
            self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(500)
            self.recheck_timer = QTimer(self); self.recheck_timer.setSingleShot(True)
            self.recheck_timer.timeout.connect(self.auto_recheck)
            if self.profiles.count(): self.load_profile()
            else:
                self.hint.setText('请打开已有助手配置'); self.start.setEnabled(False)
                self.choose.setEnabled(False); self.save_button.setEnabled(False)
                self.recheck.setEnabled(False)
            self.refresh_controls()
            self.refresh_run_card()

        # ---------- helpers ----------

        def busy(self):
            return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

        def running(self):
            return self.busy() and self.action == 'run-assistants'

        def can_reorder(self):
            return (self.data is not None and not self.busy()
                    and self.view == 'selected' and not self.running())

        def adapter_of(self, assistant_id):
            for row in self.data['assistants']:
                if row['id'] == assistant_id: return row.get('adapter', assistant_id)
            return assistant_id

        def selected_rows(self):
            if self.data is None: return []
            return [r for r in self.data['assistants'] if r.get('enabled', True)]

        def run_session_active(self):
            return self.running()

        def refresh_controls(self):
            idle = not self.busy()
            for widget in (self.profiles, self.choose, self.save_button, self.recheck, self.repeat_input,
                           self.new_profile, self.rename_profile, self.delete_profile):
                widget.setEnabled(idle)
            # Lock the table only for the preflight check; run rows carry the
            # reason entry, which must stay clickable while the run continues.
            self.table.setEnabled(idle or self.action != 'check-assistants')
            selected = self.selected_ids if self.selected_ids is not None else [r['id'] for r in self.selected_rows()]
            self.start.setEnabled(idle and bool(selected))
            self.stop.setEnabled(self.running())
            self.recovery_button.setVisible(self.needs_recovery())
            self.recovery_button.setEnabled(idle)
            active = self.run_session_active()
            self.progress_card.setVisible(active)
            self.check_card.setVisible(not active)
            self.seg_box.setVisible(not active)
            self.start.setVisible(not active)
            self.repeat_input.setVisible(not active); self.repeat_label.setVisible(not active)
            self.refresh_run_card()

        def needs_recovery(self):
            try:
                return read_state(self.workspace / 'state')['phase'] not in {'unused', 'idle'}
            except (RecoveryRequired, OSError):
                return True

        def refresh_run_card(self):
            expanded = self.running() or self.summary_path is not None
            self.log.setVisible(expanded); self.log_caption.setVisible(expanded)
            self.run_placeholder.setVisible(not expanded and self.problems_box.isHidden())
            policy = QSizePolicy.Policy.Preferred if expanded else QSizePolicy.Policy.Maximum
            sp = self.run_card.sizePolicy(); sp.setVerticalPolicy(policy)
            self.run_card.setSizePolicy(sp)
            self.outer_layout.setStretch(5, 5 if expanded else 6)
            self.outer_layout.setStretch(7, 2 if expanded else 0)

        # ---------- rendering ----------

        def load_profile(self):
            if self.busy(): return
            try:
                self.session = None; self.summary_path = None; self.result.setEnabled(False)
                self.finished_report_key = None
                self.repeat_summary.hide()
                self.check_report = None; self.log.clear(); self.log_seen = 0
                self.results = {}; self.action = None; self.undo_info = None
                self.set_problems([])
                self.data = load_assistants(self.profiles.currentData())
                self.repeat_input.setValue(self.data.get('repeat_limit', 0))
                self.selected_ids = [r['id'] for r in self.data['assistants'] if r.get('enabled', True)]
                self.view = 'selected' if self.selected_ids else 'all'
                self.render(); self.refresh_checklist(); self.sync_combo_label()
                self.recheck_timer.start(900)
            except (OSError, ValueError, TypeError) as error:
                self.data = None; self.table.setRowCount(0)
                QMessageBox.warning(self, '配置无法读取', str(error))
            self.refresh_controls()

        def visible_rows(self):
            rows = self.data['assistants']
            if self.running():
                return self.participants()
            if self.view == 'selected' and self.selected_ids is not None:
                return [r for r in rows if r['id'] in self.selected_ids]
            return list(rows)

        def set_view(self, mode):
            self.view = mode
            self.render()

        def render(self):
            rows = self.visible_rows()
            self.row_tints = {}
            table = self.table
            scroll_position = table.verticalScrollBar().value()
            empty = self.view == 'selected' and not self.running() and not rows
            self.table_header.setVisible(not empty)
            self.empty_state.setVisible(empty)
            self.table.setVisible(not empty)
            table.setColumnHidden(5, self.running())
            table.setRowCount(0 if empty else len(rows))
            if not empty:
                for index, row in enumerate(rows):
                    pending = row['adapter'] in PENDING_ADAPTERS or not row.get('installation_root')
                    table.setRowHeight(index, 52)
                    if self.view == 'selected':
                        tint = '#f0f6ff'
                        result = self.results.get(row['id'], {}) or {}
                        table.setCellWidget(index, 0, self.handle_cell(index))
                        table.setCellWidget(index, 1, self.identity_cell(row, False, tint))
                        table.setCellWidget(index, 2, self.game_start_cell(row, False, tint))
                        table.setCellWidget(index, 3, self.timeout_cell(row, tint))
                        table.setCellWidget(index, 4, self.status_cell_idle(row, False, True, tint))
                        table.setCellWidget(index, 5, self.remove_cell(row))
                    else:
                        selected = (row['id'] in self.selected_ids if self.selected_ids is not None
                                    else row.get('enabled', True))
                        tint = '#f0f6ff' if selected and not pending else None
                        table.setCellWidget(index, 0, self.checkbox_cell(row, selected))
                        table.setCellWidget(index, 1, self.identity_cell(row, pending, tint))
                        table.setCellWidget(index, 2, self.game_start_cell(row, pending, tint))
                        table.setCellWidget(index, 3, self.timeout_cell(row, tint))
                        table.setCellWidget(index, 4, self.status_cell_idle(row, pending, selected, tint))
                        table.setCellWidget(index, 5, QWidget())
                    self.row_tints[index] = tint
                    self.row_items(index, row['id'], tint)
            self.table.repaint_hover()
            self.refresh_footer()
            self.selection_changed(schedule_check=False)
            table.updateGeometries()
            table.verticalScrollBar().setValue(scroll_position)

        def tinted(self, widget, tint):
            return widget

        def row_items(self, index, row_id, tint):
            for column in range(6):
                item = QTableWidgetItem()
                if column == 0: item.setData(Qt.ItemDataRole.UserRole, row_id)
                if tint: item.setBackground(QColor(tint))
                self.table.setItem(index, column, item)

        def apply_row_hover(self, index, on):
            """Row-wide hover: one step deeper on tinted rows, neutral grey on plain rows."""
            tint = self.row_tints.get(index)
            hover = QColor('#e2ecfd') if tint else QColor('#f5f8fc')
            base = QColor(tint) if tint else QBrush()
            color = hover if on else base
            for column in range(6):
                item = self.table.item(index, column)
                if item is not None: item.setBackground(color)

        def checkbox_cell(self, row, selected):
            box = QWidget(); layout = QHBoxLayout(box)
            layout.setContentsMargins(14, 0, 0, 0)
            pending = row['adapter'] in PENDING_ADAPTERS or not row.get('installation_root')
            checkbox = QCheckBox(); checkbox.setChecked(selected)
            checkbox.setEnabled(not pending and bool(row.get('installation_root')))
            if pending or not row.get('installation_root'): checkbox.setToolTip('请先完成本机接入')
            checkbox.setProperty('assistant_id', row['id'])
            checkbox.toggled.connect(lambda checked, identity=row['id']: self.set_selected(identity, checked))
            layout.addWidget(checkbox); layout.addStretch()
            return box

        def handle_cell(self, ordinal):
            box = QWidget(); layout = QHBoxLayout(box)
            layout.setContentsMargins(10, 0, 0, 0); layout.setSpacing(6)
            handle = QLabel('⠿'); handle.setStyleSheet('color: #b6c2d4; font-size: 14px;')
            handle.setCursor(Qt.OpenHandCursor)
            layout.addWidget(handle)
            layout.addWidget(badge_label(str(ordinal + 1), '#2563eb', 'white'))
            layout.addStretch()
            return box

        def remove_cell(self, row):
            box = QWidget(); layout = QHBoxLayout(box)
            layout.setContentsMargins(0, 0, 6, 0)
            button = QPushButton('×'); button.setObjectName('removeRow')
            button.setCursor(Qt.PointingHandCursor); button.setToolTip('从本轮移除')
            button.clicked.connect(lambda: self.remove_assistant(row['id']))
            layout.addWidget(button); layout.addStretch()
            return box

        def identity_cell(self, row, pending, tint):
            from PySide6.QtWidgets import QVBoxLayout
            box = QWidget(); layout = QHBoxLayout(box)
            layout.setContentsMargins(4, 6, 4, 6); layout.setSpacing(12)
            name = NAMES.get(row['id'], row['id'])
            layout.addWidget(avatar_label(row['id'], name, pending, 30))
            text = QVBoxLayout(); text.setSpacing(0)
            title = QLabel(name); title.setStyleSheet(
                'color: #1e293b; font-weight: 600;' if not pending else 'color: #94a3b8; font-weight: 600;')
            adapter = QLabel(row['adapter'])
            adapter.setStyleSheet('color: #94a3b8; font-size: 11px; font-family: "Consolas", monospace;')
            text.addWidget(title); text.addWidget(adapter)
            layout.addLayout(text); layout.addStretch()
            return self.tinted(box, tint)

        def game_start_cell(self, row, pending, tint):
            if row['adapter'] in MANUAL_START_ADAPTERS and row.get('installation_root') and not self.running():
                box = QWidget(); layout = QHBoxLayout(box); layout.setContentsMargins(4, 0, 4, 0)
                mode = QComboBox(); mode.setObjectName('gameStartInput')
                mode.addItem('助手自动启动', 'assistant'); mode.addItem('先手动进入游戏', 'manual')
                mode.setCurrentIndex(mode.findData(row.get('game_start', 'assistant')))
                mode.setEnabled(not self.busy())
                preparation = ('先在鸣潮助手关闭“启动助手时自动启动游戏”并退出助手；通过官方启动器进入游戏世界。'
                               if row['adapter'] == 'ok-ww' else
                               '先禁用 MaaEnd 当前实例的启动前动作和内置启动任务，保留已登记的游戏路径；通过原生启动器进入游戏。')
                mode.setToolTip('手动模式：' + preparation + '结束后若游戏仍开着，串行暂停。保存组合后沿用此设置。')
                mode.currentIndexChanged.connect(lambda _: self.commit_game_start(row['id'], mode))
                layout.addStretch(); layout.addWidget(mode); layout.addStretch()
                edit = QPushButton('⋯'); edit.setObjectName('linkButton'); edit.setEnabled(not self.busy())
                edit.setToolTip('设置接入'); edit.clicked.connect(lambda _: self.register_assistant(row['id']))
                layout.addWidget(edit)
                return self.tinted(box, tint)
            if row['adapter'] == 'bettergi' and not self.running():
                box = QWidget(); layout = QHBoxLayout(box); layout.setContentsMargins(4, 0, 4, 0)
                link = QPushButton('设置接入' if row.get('installation_root') else '去接入 →')
                link.setObjectName('linkButton'); link.setCursor(Qt.PointingHandCursor)
                link.setEnabled(not self.busy())
                link.clicked.connect(lambda _, identity=row['id']: self.register_local_bettergi(identity))
                layout.addStretch(); layout.addWidget(link); layout.addStretch()
                return self.tinted(box, tint)
            if not self.running():
                box = QWidget(); layout = QHBoxLayout(box); layout.setContentsMargins(4, 0, 4, 0)
                link = QPushButton('设置接入' if row.get('installation_root') else '去接入 →'); link.setObjectName('linkButton')
                link.setCursor(Qt.PointingHandCursor)
                link.setEnabled(not self.busy())
                link.clicked.connect(lambda _: self.register_assistant(row['id']))
                layout.addStretch(); layout.addWidget(link); layout.addStretch()
                return self.tinted(box, tint)
            if row.get('game_start') == 'manual':
                box = QWidget(); layout = QHBoxLayout(box); layout.setContentsMargins(4, 0, 4, 0)
                layout.addStretch(); layout.addWidget(pill_label('▸ 先手动进入游戏', '#fdf3dd', '#a16207', '#f3e3b3'),
                                                      0, Qt.AlignVCenter)
                layout.addStretch()
                return self.tinted(box, tint)
            label = QLabel('助手自动启动'); label.setStyleSheet('color: #7b8aa0;')
            label.setAlignment(Qt.AlignCenter)
            return self.tinted(label, tint)

        def commit_game_start(self, identity, mode):
            if self.busy() or self.data is None: return
            row = next((r for r in self.data['assistants'] if r['id'] == identity), None)
            if row is None or row['adapter'] not in MANUAL_START_ADAPTERS: return
            row['game_start'] = mode.currentData()
            self.check_report = None
            self.selection_changed()
            self.refresh_checklist()

        def timeout_cell(self, row, tint):
            if self.running():
                label = QLabel(f"{row.get('timeout_seconds', 1800) / 60:g} 分钟")
                label.setStyleSheet('color: #7b8aa0;'); label.setAlignment(Qt.AlignCenter)
                return self.tinted(label, tint)
            box = QWidget(); layout = QHBoxLayout(box); layout.setContentsMargins(4, 0, 4, 0)
            layout.setSpacing(4); layout.addStretch()
            value = QLineEdit(str(max(10, math.ceil(row.get('timeout_seconds', 1800) / 60))))
            value.setObjectName('timeoutInput'); value.setProperty('assistant_id', row['id'])
            value.setValidator(QIntValidator(-2147483648, 2147483647, value))
            value.setAlignment(Qt.AlignCenter); value.setFixedWidth(54)
            value.setToolTip('运行时限（分钟），最少10分钟；保存组合后下次沿用')
            value.setStyleSheet('QLineEdit { color: #334155; background: white; border: 1px solid #d5deec; border-radius: 5px; padding: 4px 2px; }')
            value.setEnabled(not self.busy())
            value.editingFinished.connect(lambda: self.commit_timeout(value))
            layout.addWidget(value); layout.addWidget(QLabel('分钟')); layout.addStretch()
            return self.tinted(box, tint)

        def commit_timeout(self, value):
            if self.busy() or self.data is None: return
            row = next((r for r in self.data['assistants'] if r['id'] == value.property('assistant_id')), None)
            if row is None: return
            try:
                minutes = max(10, int(value.text()))
            except ValueError:
                minutes = max(10, math.ceil(row.get('timeout_seconds', 1800) / 60))
            if row.get('timeout_seconds', 1800) != minutes * 60:
                row['timeout_seconds'] = minutes * 60
            value.setText(str(minutes))

        def timeout_inputs(self):
            # Replaced cell widgets can remain children until deleteLater.
            # Only read the editors currently installed in the table.
            for index in range(self.table.rowCount()):
                cell = self.table.cellWidget(index, 3)
                value = cell.findChild(QLineEdit, 'timeoutInput') if cell else None
                if value is not None: yield value

        def status_cell_idle(self, row, pending, selected, tint):
            result = self.results.get(row['id'], {})
            if not pending and result.get('status') not in (None, 'running', 'starting'):
                return self.status_cell_running(row, result['status'], result, False, tint)
            box = QWidget(); layout = QVBoxLayout(box); layout.setContentsMargins(4, 5, 4, 5)
            layout.setSpacing(2)
            if pending or not row.get('installation_root'):
                pill = pill_label('待接入', '#fdf3dd', '#a16207')
            elif selected:
                status = self.checked_status(row['id'])
                pill = (pill_label('就绪', '#e5f4ec', '#177a3e') if status == 'ready' else
                        pill_label('需处理', '#fdf0dd', '#b45309') if status == 'blocked' else
                        pill_label('待运行', '#e3edff', '#1d4ed8'))
            else:
                pill = QLabel('—'); pill.setStyleSheet('color: #cbd5e1;')
            layout.addStretch(); layout.addWidget(pill, 0, Qt.AlignHCenter); layout.addStretch()
            return self.tinted(box, tint)

        def checked_status(self, assistant_id):
            if not self.check_report: return None
            for result in self.check_report.get('assistants', []):
                if result.get('id') == assistant_id: return result.get('status')
            return None

        def render_running(self, participants):
            table = self.table
            self.row_tints = {}
            self.table_header.show(); self.empty_state.hide(); table.show()
            table.setColumnHidden(5, True)
            table.setRowCount(len(participants))
            for index, row in enumerate(participants):
                table.setRowHeight(index, 52)
                result = self.results.get(row['id'], {})
                status = result.get('status')
                active = self.active_node_id() == row['id'] and self.running()
                tint = '#f0f6ff' if active else None
                table.setCellWidget(index, 0, self.sequence_cell(index, status, result, active, tint))
                table.setCellWidget(index, 1, self.identity_cell(row, False, tint))
                manual = row.get('game_start') == 'manual'
                box = QWidget(); layout = QHBoxLayout(box); layout.setContentsMargins(4, 0, 4, 0)
                text = QLabel('已手动进入游戏' if manual and status else
                              ('先手动进入游戏' if manual else '助手自动启动'))
                text.setStyleSheet('color: #7b8aa0;'); text.setAlignment(Qt.AlignCenter)
                layout.addWidget(text)
                table.setCellWidget(index, 2, self.tinted(box, tint))
                table.setCellWidget(index, 3, self.timeout_cell(row, tint))
                table.setCellWidget(index, 4, self.status_cell_running(row, status, result, active, tint))
                self.row_tints[index] = tint
                self.row_items(index, row['id'], tint)
            self.table.repaint_hover()

        def sequence_cell(self, index, status, result, active, tint):
            box = QWidget(); layout = QHBoxLayout(box)
            layout.setContentsMargins(12, 0, 0, 0); layout.setSpacing(6)
            if status == 'success':
                layout.addWidget(badge_label('✓', '#e5f4ec', '#177a3e'))
            elif status in ('failed', 'timeout', 'partial_failure', 'completion_unconfirmed', 'blocked',
                            'desktop_environment_interrupted', 'execution_error', 'interrupted',
                            'ownership_or_cleanup_unconfirmed', 'stopped_cleanup_unconfirmed'):
                layout.addWidget(badge_label('!', '#fdf0dd', '#b45309'))
            elif status in ('running', 'starting') or (active and status is None):
                layout.addWidget(badge_label(str(index + 1), '#2563eb', 'white'))
            else:
                layout.addWidget(badge_label(str(index + 1), '#eef1f5', '#94a3b8'))
            layout.addStretch()
            return self.tinted(box, tint)

        def status_cell_running(self, row, status, result, active, tint):
            box = QWidget(); layout = QVBoxLayout(box); layout.setContentsMargins(4, 2, 4, 2)
            layout.setSpacing(1); layout.addStretch()
            # Not-started rows carry no result yet: keep them queued; only unknown
            # terminal states fall back to the attention pill.
            text, background, color = (('排队中', '#eef1f5', '#64748b') if status is None
                                       else RUNNING_PILL.get(status, ('需处理', '#fdf0dd', '#b45309')))
            pill = pill_label(text, background, color)
            sub = None
            reason_link = None
            failed = result.get('completion', {}).get('failed_applications') or []
            if status == 'success' and result.get('duration_seconds') is not None:
                sub = QLabel(f"用时 {fmt_dur(result['duration_seconds'])}")
            elif status in ('running', 'starting') and active:
                row_timeout = row.get('timeout_seconds', 1800)
                sub = QLabel(f'{fmt_dur(self.node_elapsed())} / 上限 {int(round(row_timeout / 60))} 分钟')
            elif status == 'running' or status == 'starting':
                sub = QLabel('等待确认…')
            elif status not in (None, 'disabled', 'success'):
                if result.get('duration_seconds') is not None:
                    sub = QLabel(f"用时 {fmt_dur(result['duration_seconds'])}"
                                 + (f" · {'、'.join(failed)}" if failed else ''))
                reason_link = QLabel('查看原因'); reason_link.setObjectName('linkButton')
                reason_link.setCursor(Qt.PointingHandCursor)
                reason_link.mousePressEvent = lambda *_, game_id=row['id']: self.open_result_reason(game_id)
            if result.get('round') and self.data.get('repeat_limit', 0):
                if sub:
                    sub.setText(f"第 {result['round']} 轮 · " + sub.text())
                else:
                    sub = QLabel(f"第 {result['round']} 轮")
            if ((status == 'success' and len(result.get('attempts', [])) > 1)
                    or (status in ('running', 'starting') and result.get('attempts'))):
                reason_link = QLabel('运行详情'); reason_link.setObjectName('linkButton')
                reason_link.setCursor(Qt.PointingHandCursor)
                reason_link.mousePressEvent = lambda *_, game_id=row['id']: self.open_result_reason(game_id)
            if sub:
                sub.setStyleSheet('color: #94a3b8; font-size: 11px;')
            layout.addStretch(); layout.addWidget(pill, 0, Qt.AlignHCenter)
            if sub or reason_link:
                detail_row = QHBoxLayout(); detail_row.setContentsMargins(0, 0, 0, 0)
                detail_row.setSpacing(6)
                if sub: detail_row.addWidget(sub)
                if reason_link:
                    reason_link.setStyleSheet('font-size: 11px; padding: 0;')
                    detail_row.addWidget(reason_link)
                detail_row.addStretch()
                detail_box = QWidget(); detail_box.setLayout(detail_row)
                layout.addWidget(detail_box, 0, Qt.AlignHCenter)
            layout.addStretch()
            return self.tinted(box, tint)

        def selection(self):
            # Also commit an active editor when Save/Start is invoked through a
            # shortcut, without requiring focus to move first.
            for value in self.timeout_inputs():
                self.commit_timeout(value)
            order = [r['id'] for r in self.data['assistants']]
            if self.selected_ids is None:
                selected = [r['id'] for r in self.data['assistants'] if r.get('enabled', True)]
            else:
                selected = [i for i in order if i in self.selected_ids]
            data = selected_configuration(self.data, order, selected)
            self.repeat_input.interpretText()
            data['repeat_limit'] = self.repeat_input.value()
            return data

        def selection_changed(self, *_, schedule_check=True):
            if self.data is None: return
            try:
                data = self.selection()
            except ValueError:
                return
            enabled = [r for r in data['assistants'] if r['enabled']]
            self.data = data
            self.selected_ids = [r['id'] for r in enabled]
            if self.busy_check():
                self.start.setText('检查中…')
            else:
                self.start.setText(f'开始运行（{len(enabled)} 个）')
            self.start.setEnabled(bool(enabled) and not self.busy())
            self.start.setToolTip('' if enabled else '请至少选择一个已接入的助手')
            self.sync_combo_label()
            self.refresh_footer()
            if schedule_check and not self.busy():
                self.recheck_timer.start(900)

        def busy_check(self):
            return self.busy() and self.action == 'check-assistants'

        def refresh_footer(self):
            total = len(self.data['assistants']) if self.data else 0
            selected = len(self.selected_ids or [])
            self.view_all.setText(f'全部 {total}')
            self.view_selected.setText(f'已选 {selected}')
            self.view_all.setChecked(self.view == 'all')
            self.view_selected.setChecked(self.view == 'selected')
            if self.running():
                return  # the running hint is owned by render_running_state
            if self.undo_info is not None:
                name = NAMES.get(self.undo_info, self.undo_info)
                self.hint.setText(f'已移除 {name}')
                self.undo_label.setVisible(True)
                return
            self.undo_label.setVisible(False)
            if self.view == 'all':
                self.hint.setText('请至少选择一个已接入的助手' if not selected else '勾选要运行的助手')
            else:
                self.hint.setText('按序号顺序运行，拖动手柄调整' if selected else '')

        def remove_assistant(self, assistant_id):
            if self.busy(): return
            selected = [i for i in (self.selected_ids or []) if i != assistant_id]
            self.undo_info = assistant_id
            self.selected_ids = [r['id'] for r in self.data['assistants'] if r['id'] in selected]
            self.check_report = None
            if not self.selected_ids and self.view == 'selected':
                self.view = 'all'
            self.render(); self.refresh_checklist()
            # Hold the recheck until the undo toast window is over: begin() clears
            # undo_info and a busy check swallows undo clicks.
            self.recheck_timer.start(6000)

        def undo_remove(self):
            if self.undo_info is None or self.busy(): return
            identity = self.undo_info; self.undo_info = None
            self.set_selected(identity, True)
            # set_selected only updates the model; re-render so checkboxes in the
            # current view (e.g. the all view after the last removal) reflect it.
            self.render()

        def flash_hint(self, message):
            self.hint.setText(message)
            self.undo_label.setVisible(False)
            QTimer.singleShot(6000, self.refresh_footer)

        def set_selected(self, identity, checked):
            selected = set(self.selected_ids or [])
            if checked: selected.add(identity)
            else: selected.discard(identity)
            self.selected_ids = [r['id'] for r in self.data['assistants'] if r['id'] in selected]
            self.undo_info = None
            self.check_report = None
            self.selection_changed()
            self.refresh_checklist()
            # Re-render so row tints follow the checkbox: blue must mean selected.
            self.render()

        def sync_combo_label(self):
            combo = self.profiles
            if self.data is None or combo.currentIndex() < 0: return
            path = combo.currentData()
            try:
                on_disk = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                return
            disk = [(r.get('id'), bool(r.get('enabled', True))) for r in on_disk.get('assistants', [])]
            try:
                current = [(r['id'], bool(r.get('enabled', True))) for r in self.selection()['assistants']]
            except ValueError:
                return
            name = combo.itemText(combo.currentIndex())
            if disk != current or on_disk.get('repeat_limit', 0) != self.repeat_input.value():
                label = f'自定义 · 已选 {sum(1 for _, enabled in current if enabled)} 项'
            else:
                label = self.profile_names.get(path, name)
            if label != name:
                combo.blockSignals(True); combo.setItemText(combo.currentIndex(), label); combo.blockSignals(False)

        def move_row(self, source, target):
            if self.busy() or self.data is None: return
            visible = self.visible_rows()
            if not (0 <= source < len(visible) and 0 <= target < len(visible)) or source == target: return
            src_id, dst_id = visible[source]['id'], visible[target]['id']
            order = [r['id'] for r in self.data['assistants']]
            moving_down = source < target
            order.remove(src_id)
            dst_pos = order.index(dst_id)
            order.insert(dst_pos + 1 if moving_down else dst_pos, src_id)
            data = self.selection()
            by_id = {r['id']: r for r in data['assistants']}
            data['assistants'] = [by_id[i] for i in order]
            self.data = data; self.render()
            self.table.selectRow(target)

        # ---------- checklist ----------

        def refresh_checklist(self):
            while self.checklist.count():
                item = self.checklist.takeAt(0)
                if item.widget(): item.widget().deleteLater()
            warnings, passes = [], []
            elevated = elevation_report().get('elevated')
            if elevated is not True:
                warnings.append(('需要管理员权限', '点击开始运行时确认 UAC；程序会以管理员身份重新打开。'))
            for row in self.selected_rows():
                name = NAMES.get(row['id'], row['id'])
                if row.get('game_start') == 'manual':
                    warnings.append((f'请先手动进入{name}', '停在游戏内再点“开始运行”，其余游戏由助手自动启动。'))
            for result in (self.check_report or {}).get('assistants', []):
                if result.get('status') != 'blocked': continue
                name = NAMES.get(result['id'], result['id'])
                blockers = list(result.get('blockers') or [])
                if blockers:
                    primary = BLOCKER_TEXT.get(blockers[0], blockers[0])
                    extra = ''.join(f'另外：{BLOCKER_TEXT.get(b, b)}' for b in blockers[1:])
                    suggestion = BLOCKER_SUGGESTIONS.get(blockers[0])
                    sub = suggestion or '处理后点“重新检查”。'
                    if extra: sub = f'{sub}{extra}'
                else:
                    primary = sanitize_detail(result.get('error', '')) or '需要处理'
                    sub = '处理后点“重新检查”。'
                warnings.append((f'{name}：{primary}', sub))
            if self.check_report:
                exclusive = all(not any('not_exclusive' in b or 'existing_runner' in b
                                        for b in (r.get('blockers') or []))
                                for r in self.check_report.get('assistants', []))
                passes.append(('没有其他助手或游戏在运行', None) if exclusive
                              else None)
                passes.append(('已选助手均已接入', None))
                passes = [p for p in passes if p]
                ready = [r for r in self.check_report.get('assistants', []) if r.get('status') == 'ready']
                if ready:
                    passes.insert(0, (f"已就绪：{'、'.join(NAMES.get(r['id'], r['id']) for r in ready)}", None))
            if self.busy_check():
                hint = QLabel('正在检查…'); hint.setStyleSheet('color: #94a3b8;')
                self.checklist.addWidget(hint)
                return
            if warnings:
                self.check_collapsed = False
            summary = f'检查通过 · {len(passes)} 项' if self.check_report and passes else None
            if not warnings and summary and self.check_collapsed:
                self.checklist.addWidget(self.check_summary_row(summary, expanded=False))
                return
            if not warnings and not passes and not self.check_report:
                hint = QLabel('等待检查：点击“重新检查”验证启动条件。')
                hint.setStyleSheet('color: #94a3b8;')
                self.checklist.addWidget(hint)
                return
            if summary and not warnings:
                self.checklist.addWidget(self.check_summary_row(summary, expanded=True))
            for index, (title, sub) in enumerate(warnings + passes):
                warning = index < len(warnings)
                row = QWidget(); layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)
                icon = QLabel('⚠' if warning else '✓')
                icon.setStyleSheet(f'color: {"#d97706" if warning else "#178a58"}; font-size: 13px; font-weight: 700;')
                icon.setFixedWidth(18); icon.setAlignment(Qt.AlignTop)
                text = QVBoxLayout(); text.setSpacing(1)
                head = QLabel(title); head.setStyleSheet(
                    f'color: {"#92400e" if warning else "#334155"};'
                    + (' font-weight: 600;' if warning else ''))
                text.addWidget(head)
                if sub:
                    detail = QLabel(sub); detail.setStyleSheet('color: #94a3b8; font-size: 12px;')
                    detail.setWordWrap(True); text.addWidget(detail)
                layout.addWidget(icon); layout.addLayout(text, 1)
                self.checklist.addWidget(row)

        def check_summary_row(self, summary, expanded):
            row = QWidget(); layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)
            icon = QLabel('✓'); icon.setStyleSheet('color: #178a58; font-size: 13px; font-weight: 700;')
            icon.setFixedWidth(18)
            head = QLabel(summary)
            head.setStyleSheet('color: #178a58; text-decoration: none;')
            head_font = head.font(); head_font.setUnderline(False); head.setFont(head_font)
            toggle = QLabel('▴' if expanded else '▾'); toggle.setStyleSheet('color: #94a3b8; font-size: 14px;')
            toggle.setFixedWidth(18)
            layout.addWidget(icon); layout.addWidget(head); layout.addStretch()
            layout.addWidget(toggle, 0, Qt.AlignVCenter)

            def flip(_):
                self.check_collapsed = not self.check_collapsed
                self.refresh_checklist()
            row.setCursor(Qt.PointingHandCursor); row.mousePressEvent = flip
            return row

        def auto_recheck(self):
            if any(value.hasFocus() for value in self.timeout_inputs()):
                self.recheck_timer.start(900)
                return
            if self.data is not None and not self.busy():
                self.begin('check-assistants')

        # ---------- actions ----------

        def persist_profile_data(self, data):
            identity = self.profile_ids.get(self.profiles.currentData())
            if identity is None:
                identity = self.profile_store.create_profile('我的组合', data)['id']
            else:
                self.profile_store.save_profile(identity, data)
            self.reload_profiles(identity)

        def commit_registration_inspection(self, identity, inspection):
            if self.busy() or self.data is None: return
            data = commit_registration(self.selection(), inspection, identity)
            self.persist_profile_data(data)
            self.check_report = None
            self.flash_hint('登记已保存；运行前请重新检查' if not inspection.blockers else '登记已保存为禁用状态，请处理检查阻碍')

        def register_assistant(self, identity):
            if self.busy() or self.data is None: return
            row = next(r for r in self.data['assistants'] if r['id'] == identity)
            spec = get_adapter(row['adapter'])
            dialog = QDialog(self); dialog.setWindowTitle(spec.display_name + ' · 登记已安装助手')
            dialog.setMinimumWidth(620)
            layout = QVBoxLayout(dialog)
            note = QLabel('仅读取安装和原生设置；登记不会启动游戏或修改原生任务。'); note.setWordWrap(True)
            layout.addWidget(note)
            official = QPushButton('官方安装与说明'); official.setObjectName('linkButton')
            official.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(spec.official_url)))
            layout.addWidget(official)
            form = QFormLayout(); editors = {}
            labels = {'installation_root': '助手安装目录', 'entry_executable': '助手入口（可选）', 'game_path': '游戏 EXE'}
            for field in spec.registration_fields:
                editor = QLineEdit(row.get(field) or ''); editor.setObjectName(field)
                editors[field] = editor
                box = QWidget(); line = QHBoxLayout(box); line.setContentsMargins(0, 0, 0, 0)
                pick = QPushButton('选择')
                def choose_path(_, field=field, editor=editor):
                    value = (QFileDialog.getExistingDirectory(dialog, '选择助手目录', editor.text()) if field == 'installation_root'
                             else QFileDialog.getOpenFileName(dialog, '选择 EXE', editor.text(), '程序 (*.exe)')[0])
                    if value: editor.setText(value)
                pick.clicked.connect(choose_path); line.addWidget(editor, 1); line.addWidget(pick)
                form.addRow(labels[field], box)
            mode = QComboBox()
            for value in spec.start_modes: mode.addItem('先手动进入游戏' if value == 'manual' else '助手自动启动', value)
            mode.setCurrentIndex(max(0, mode.findData(row.get('game_start', 'assistant'))))
            if len(spec.start_modes) > 1: form.addRow('游戏启动', mode)
            layout.addLayout(form)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
            buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
            while dialog.exec() == QDialog.DialogCode.Accepted:
                if self.busy(): return
                try:
                    fields = {field: editor.text().strip() for field, editor in editors.items() if editor.text().strip()}
                    fields['game_start'] = mode.currentData()
                    inspection = inspect_registration(row['adapter'], fields)
                except (OSError, ValueError, TypeError) as error:
                    warning = QMessageBox(QMessageBox.Icon.Warning, '登记需要处理', sanitize_detail(error),
                                          QMessageBox.StandardButton.NoButton, dialog)
                    retry = warning.addButton('重新选择', QMessageBox.ButtonRole.ActionRole)
                    cancel = warning.addButton('取消', QMessageBox.ButtonRole.RejectRole)
                    warning.setDefaultButton(cancel); warning.setEscapeButton(cancel)
                    warning.exec()
                    if warning.clickedButton() is not retry or self.busy(): return
                    editor = editors['installation_root']
                    value = QFileDialog.getExistingDirectory(dialog, '选择助手目录', editor.text())
                    if value: editor.setText(value)
                    continue
                try:
                    message = ('检查发现以下阻碍，将保存为禁用状态：\n' + '\n'.join(BLOCKER_TEXT.get(b, b) for b in inspection.blockers)
                               if inspection.blockers else '路径检查完成，将保留组合顺序和选择；不会开始运行。')
                    if QMessageBox.question(self, '登记预览', message) != QMessageBox.StandardButton.Yes: return
                    self.commit_registration_inspection(identity, inspection)
                except (OSError, ValueError, TypeError) as error:
                    QMessageBox.warning(self, '登记需要处理', sanitize_detail(error))
                return

        def register_local_bettergi(self, identity):
            if self.busy() or self.data is None: return
            executable, _ = QFileDialog.getOpenFileName(self, '选择已安装的 BetterGI.exe',
                'C:/BetterGI/BetterGI.exe', 'BetterGI (BetterGI.exe)')
            if not executable: return
            try:
                data, prepared = register_bettergi(self.selection(), identity, executable)
                message = ('版本：' + prepared.get('runner_version', '未核验') + '\n已保存的一条龙：' + prepared.get('instance_name', '待处理')
                    + '\n原神：' + str(prepared.get('game_path', '待处理'))
                    + '\n结束操作：关闭游戏和软件\n\n仅保存 GachaOps 的本机路径，任务沿用 BetterGI 设置。')
                if prepared['blockers']:
                    message += '\n\n运行前需要处理：\n' + '\n'.join(
                        BLOCKER_TEXT.get(b, b) for b in prepared['blockers'])
                self.persist_profile_data(data)
                self.check_report = None
                self.render(); self.refresh_controls(); self.refresh_checklist()
                self.flash_hint('本机接入已保存到“我的组合”；游戏顺序和选择保留')
                QMessageBox.information(self, 'BetterGI 本机接入', message)
                self.recheck_timer.start(900)
            except (OSError, ValueError, TypeError, KeyError) as error:
                QMessageBox.warning(self, 'BetterGI 接入需要处理', sanitize_detail(error))

        def browse(self):
            if self.busy(): return
            path, _ = QFileDialog.getOpenFileName(self, '打开 GachaOps 助手配置', str(self.workspace / 'config'), 'JSON 配置 (*.json)')
            if path:
                try:
                    preview = self.profile_store.preview_import(path)
                    data = preview['configuration']
                    text = f"将导入 {len(data['assistants'])} 项助手，已选 {sum(r.get('enabled', True) for r in data['assistants'])} 项，重复上限 {data.get('repeat_limit', 0)}。导入后不会开始运行。"
                    if QMessageBox.question(self, '预览导入', text) != QMessageBox.StandardButton.Yes: return
                    profile = self.profile_store.import_profile(Path(path).stem, preview)
                    self.reload_profiles(profile['id'])
                except (OSError, ValueError, TypeError) as error: QMessageBox.warning(self, '导入失败', str(error))

        def reload_profiles(self, selected=None):
            self.profiles.blockSignals(True); self.profiles.clear()
            self.profile_ids.clear(); self.profile_names.clear()
            for profile in self.profile_store.list_profiles():
                self.profiles.addItem(profile['name'], profile['path'])
                self.profile_ids[profile['path']] = profile['id']
                self.profile_names[profile['path']] = profile['name']
                if profile['id'] == selected: self.profiles.setCurrentIndex(self.profiles.count() - 1)
            self.profiles.blockSignals(False)
            self.load_profile()

        def create_profile_named(self, name):
            if self.busy(): return
            profile = self.profile_store.create_profile(name)
            self.reload_profiles(profile['id'])

        def rename_profile_named(self, name):
            if self.busy(): return
            identity = self.profile_ids.get(self.profiles.currentData())
            if identity is None: raise ValueError('先保存或导入此组合')
            self.profile_store.rename_profile(identity, name)
            # A name change must not reload the saved configuration over edits.
            self.profile_names[self.profiles.currentData()] = name.strip()
            self.profiles.setItemText(self.profiles.currentIndex(), name.strip())
            self.sync_combo_label()

        def delete_current_profile(self):
            if self.busy(): return
            identity = self.profile_ids.get(self.profiles.currentData())
            if identity is None: raise ValueError('先保存或导入此组合')
            result = self.profile_store.delete_profile(identity)
            self.reload_profiles()
            if not result['cleanup_complete']:
                QMessageBox.warning(self, '组合文件未完全删除',
                    '组合已从列表移除，但部分配置文件未能删除。请检查数据目录中的残留；这不是可恢复备份：\n'
                    + result['residual_path'])

        def prompt_new_profile(self):
            if self.busy(): return
            name, accepted = QInputDialog.getText(self, '新建组合', '组合名称')
            if accepted:
                try: self.create_profile_named(name)
                except (OSError, ValueError) as error: QMessageBox.warning(self, '新建失败', str(error))

        def prompt_rename_profile(self):
            if self.busy(): return
            name, accepted = QInputDialog.getText(self, '修改名称', '组合名称', text=self.profiles.currentText())
            if accepted:
                try: self.rename_profile_named(name)
                except (OSError, ValueError) as error: QMessageBox.warning(self, '改名失败', str(error))

        def prompt_delete_profile(self):
            if self.busy(): return
            name = self.profile_names.get(self.profiles.currentData(), self.profiles.currentText())
            message = (f'永久删除组合“{name}”？此操作不可恢复，不会新增备份。\n'
                       '仅删除此组合配置，不会删除助手或游戏；既有变更备份保留。\n'
                       '最后一个组合删除后会建立默认组合。')
            if QMessageBox.question(self, '删除组合', message,
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                try: self.delete_current_profile()
                except (OSError, ValueError) as error: QMessageBox.warning(self, '删除失败', str(error))

        def save_selection(self):
            if self.busy(): return
            try:
                identity = self.profile_ids.get(self.profiles.currentData())
                if identity is None:
                    profile = self.profile_store.create_profile('我的组合', self.selection())
                    identity = profile['id']
                else:
                    self.profile_store.save_profile(identity, self.selection())
                self.reload_profiles(identity)
                self.flash_hint('组合已保存；旧配置和变更前备份保留')
            except (OSError, ValueError, TypeError) as error: QMessageBox.warning(self, '保存失败', str(error))

        def begin_run(self):
            if self.busy() or self.data is None: return
            if elevation_report().get('elevated') is not True:
                self.elevate(); return
            self.begin('run-assistants')

        def begin_recovery(self):
            if self.busy(): return
            if elevation_report().get('elevated') is not True:
                QMessageBox.warning(self, '需要管理员权限', '请以管理员权限重新打开 GachaOps，再点击“检查并恢复运行资格”。')
                return
            self.begin('recover-assistants')

        def begin(self, action):
            if self.busy() or (self.data is None and action != 'recover-assistants'): return
            try:
                data = self.selection() if action != 'recover-assistants' else None
                if action == 'run-assistants':
                    self.data = data
                if data is not None and not any(r['enabled'] for r in data['assistants']): return
                self.check_origin = ((self.session, self.summary_path)
                                     if action in {'check-assistants', 'recover-assistants'} else None)
                self.session = self.workspace / 'runs' / ('gui-' + uuid.uuid4().hex)
                self.session.mkdir(parents=True)
                config = self.session / 'assistants.json'
                if data is not None:
                    save_configuration(config, data)
                if action == 'recover-assistants':
                    self.recheck_timer.stop()
                self.action = action; self.undo_info = None
                if action == 'run-assistants':
                    self.recheck_timer.stop()
                    self.repeat_summary.hide()
                    self.finished_report_key = None
                    self.summary_path = None
                    self.log.clear(); self.log_seen = 0
                    self.started = time.monotonic(); self.node_started = None
                    self.stop_requested = False
                    self.results = {}
                    self.result.setEnabled(False)
                    self.subtitle.setText(SUBTITLE_RUNNING)
                    self.running_title.setStyleSheet('')
                    self.stop.show()
                    self.set_problems([])
                else:
                    self.start.setText('核验中…' if action == 'recover-assistants' else '检查中…')
                    self.start.setEnabled(False)
                process = QProcess(self); self.process = process
                env = QProcessEnvironment.systemEnvironment(); env.insert('PYTHONIOENCODING', 'utf-8')
                env.insert('PYINSTALLER_RESET_ENVIRONMENT', '1'); process.setProcessEnvironment(env)
                process.setWorkingDirectory(str(self.workspace))
                command = worker_command(action, config, self.session, self.session / 'stop.request', self.session / 'worker.log', data_root=self.workspace)
                process.setProgram(command[0]); process.setArguments(command[1:])
                process.finished.connect(self.finished); process.errorOccurred.connect(self.process_error)
                def on_started():
                    self.refresh_controls(); self.refresh_checklist()
                    if action == 'run-assistants':
                        self.render_running(self.participants())
                process.started.connect(on_started)
                if action == 'run-assistants':
                    def handoff():
                        from .foreground import allow_foreground
                        allow_foreground(int(process.processId()))
                        self.showMinimized()
                    process.started.connect(handoff)
                self.refresh_controls(); self.refresh_checklist()
                process.start()
            except (OSError, ValueError, TypeError) as error:
                self.refresh_controls(); QMessageBox.warning(self, '无法开始', str(error))

        def elevate(self):
            if self.busy(): return
            if os.name != 'nt':
                self.flash_hint('管理员提权仅在 Windows 上可用')
                return
            try:
                path = self.workspace / 'runs' / ('gui-setup-' + uuid.uuid4().hex) / 'assistants.json'
                save_configuration(path, self.selection())
                args = desktop_command()[1:] + ['--config', str(path), '--data-root', str(self.workspace)]
                shell = ctypes.windll.shell32.ShellExecuteW
                shell.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                  ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
                shell.restype = ctypes.c_void_p
                previous = os.environ.get('PYINSTALLER_RESET_ENVIRONMENT')
                os.environ['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
                try:
                    code = shell(None, 'runas', sys.executable, elevation_command(args), str(self.workspace), 1)
                finally:
                    if previous is None: os.environ.pop('PYINSTALLER_RESET_ENVIRONMENT', None)
                    else: os.environ['PYINSTALLER_RESET_ENVIRONMENT'] = previous
                self.flash_hint('已打开管理员窗口；请在新窗口中开始运行。' if code and code > 32
                                else '管理员窗口未打开，尚未启动任何助手')
            except (OSError, ValueError, TypeError) as error:
                QMessageBox.warning(self, '无法打开管理员窗口', str(error))

        def request_stop(self):
            if self.running():
                try:
                    (self.session / 'stop.request').touch(exist_ok=True)
                    self.stop_requested = True
                    self.stop.setEnabled(False)
                    self.running_title.setText('停止中，等待清理确认…')
                    self.running_title.setStyleSheet('color: #d97706; font-size: 15px; font-weight: 600;')
                except OSError as error: QMessageBox.warning(self, '停止请求失败', str(error))

        # ---------- polling ----------

        def node_elapsed(self):
            return 0 if self.node_started is None else time.monotonic() - self.node_started

        def active_node_id(self):
            if self.summary_path is None: return None
            try:
                report = json.loads(self.summary_path.read_text(encoding='utf-8-sig'))
                active = report.get('active_node_summary')
                if not active or not Path(active).exists(): return None
                return json.loads(Path(active).read_text(encoding='utf-8-sig')).get('id')
            except (OSError, ValueError, KeyError):
                return None

        def poll(self):
            if self.session is None: return
            try:
                if self.action == 'run-assistants':
                    self.poll_log()
                summaries = list(self.session.glob('assistants-*/summary.json'))
                if not summaries: return
                self.summary_path = summaries[0]
                if not self.running():
                    self.result.setEnabled(True)
                data = json.loads(self.summary_path.read_text(encoding='utf-8-sig'))
                results = list(data.get('assistants', []))
                active = data.get('active_node_summary')
                node = None
                if active and Path(active).exists():
                    node = json.loads(Path(active).read_text(encoding='utf-8-sig'))
                    if node.get('status') in ('running', 'starting'):
                        previous = next((r for r in results if r.get('id') == node.get('id')), {})
                        node = dict(node, node_summary=active, round=data.get('current_round', 1),
                                    attempts=previous.get('attempts', []))
                        results = [r for r in results if r.get('id') != node.get('id')]
                        results.append(node)
                self.results = {r['id']: r for r in results if isinstance(r, dict) and r.get('id')}
                if self.running():
                    self.finished_report_key = None
                    self.render_running_state(data, node)
                else:
                    self.render_finished_states(data)
            except (OSError, ValueError, KeyError):
                pass  # An atomic report may be briefly unavailable; never infer success.

        def participants(self):
            return [r for r in self.data['assistants']
                    if (r['id'] in self.selected_ids if self.selected_ids is not None
                        else r.get('enabled', True))]

        def render_running_state(self, data, node):
            participants = self.participants()
            self.repeat_summary.setVisible(bool(data.get('repeat_limit', 0)))
            self.repeat_summary.setText(f"第 {data.get('current_round', 1)} / {data.get('repeat_limit', 0) + 1} 轮")
            self.render_running(participants)
            excluded = [NAMES.get(r['id'], r['id']) for r in self.data['assistants'] if r not in participants]
            self.hint.setText('未参与本轮：' + '、'.join(excluded) if excluded else '')
            self.undo_label.setVisible(False)
            index = next((i for i, r in enumerate(participants) if node and r['id'] == node.get('id')), None)
            self.running_title.setStyleSheet('color: #1f5fd9; font-size: 15px; font-weight: 600;')
            if index is not None:
                name = NAMES.get(participants[index]['id'], participants[index]['id'])
                round_text = (f"第 {data.get('current_round', 1)} / {data['repeat_limit'] + 1} 轮 · "
                              if data.get('repeat_limit', 0) else '')
                self.running_title.setText(f'运行中 · {round_text}第 {index + 1} / {len(participants)} 个：{name}')
                row_timeout = participants[index].get('timeout_seconds', 1800)
                elapsed = self.node_elapsed()
                self.stat_labels['node'].setText(fmt_dur(elapsed))
                self.stat_labels['left'].setText(fmt_dur(math.ceil(row_timeout - elapsed)))
            else:
                self.running_title.setText((f"第 {data.get('current_round', 1)} / {data['repeat_limit'] + 1} 轮 · "
                                           if data.get('repeat_limit', 0) else '') + '正在启动…')
                self.stat_labels['node'].setText('—'); self.stat_labels['left'].setText('—')
            if self.started is not None:
                self.stat_labels['total'].setText(fmt_dur(time.monotonic() - self.started))
            self.segments.set_segments([
                (self.results.get(r['id'], {}) or {}).get('status') for r in participants])
            self.set_problems(self.problem_entries(
                [self.results.get(r['id'], {}) for r in participants]))
            if self.stop_requested:
                self.running_title.setText('停止中，等待清理确认…')
                self.running_title.setStyleSheet('color: #d97706; font-size: 15px; font-weight: 600;')

        def render_finished_states(self, data):
            status = data.get('status', 'running')
            if status in ('running', 'starting'):
                return
            # Continue reading final reports for late details, but don't rebuild
            # interactive rows every 500 ms once their report is unchanged.
            report_key = (self.session, json.dumps(data, sort_keys=True))
            if report_key == self.finished_report_key:
                return
            self.refresh_controls()
            self.result.setEnabled(True)
            title = STATUS.get(status, status)
            if data.get('repeat_limit', 0):
                title += f" · 共 {data.get('current_round', 1)} 轮"
                if data.get('repeat_limit_reached'):
                    title += ' · 已达到重复上限'
            self.running_title.setText(title)
            self.repeat_summary.setVisible(bool(data.get('repeat_limit', 0)))
            self.repeat_summary.setText(title)
            color = '#178a58' if status == 'success' else '#64748b' if status == 'cancelled' else '#d97706'
            self.running_title.setStyleSheet(f'color: {color}; font-size: 15px; font-weight: 600;')
            self.stop.hide()
            self.subtitle.setText(SUBTITLE_IDLE)
            if self.started is not None:
                self.stat_labels['total'].setText(fmt_dur(time.monotonic() - self.started))
            self.set_problems(self.problem_entries(data.get('assistants', [])))
            self.render()
            self.finished_report_key = report_key

        def problem_entries(self, results):
            problems = []
            for result in results:
                status = result.get('status')
                if status in (None, 'success', 'disabled', 'running', 'starting'): continue
                detail = None
                failed = result.get('completion', {}).get('failed_applications') or []
                if failed: detail = '、'.join(failed)
                elif result.get('error'): detail = sanitize_detail(result['error'])
                problems.append({'name': NAMES.get(result.get('id'), result.get('id')),
                                 'id': result.get('id'),
                                 'summary': STATUS.get(status, '状态待确认，详情见原因'), 'detail': detail})
            return problems

        def open_result_reason(self, game_id, problem=None):
            result = self.results.get(game_id, {}) if game_id else {
                'id': '本轮', 'status': 'execution_error',
                'cause_unknown': (problem or {}).get('cause_unknown', False),
                'error': '\n'.join(str((problem or {}).get(key) or '') for key in ('summary', 'detail', 'error'))}
            details = details_for(result, self.summary_path if game_id else None)
            dialog = QDialog(self); dialog.setWindowTitle('查看原因'); dialog.resize(680, 520)
            layout = QVBoxLayout(dialog)
            text = QPlainTextEdit(); text.setReadOnly(True); text.setPlainText(details['text'])
            layout.addWidget(text)
            buttons = QHBoxLayout()
            if self.summary_path is not None or self.session is not None:
                export = QPushButton('导出诊断包')
                export.clicked.connect(self.export_diagnostic_bundle)
                buttons.addWidget(export)
            if self.needs_recovery():
                recovery = QPushButton('检查并恢复运行资格')
                recovery.setEnabled(not self.busy())
                def request_recovery():
                    dialog.accept()
                    self.begin_recovery()
                recovery.clicked.connect(request_recovery)
                buttons.addWidget(recovery)
            if details['source']:
                raw = QPushButton('打开本轮原始记录')
                raw.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(details['source']))))
                buttons.addWidget(raw)
            buttons.addStretch()
            close = QPushButton('关闭'); close.clicked.connect(dialog.accept); buttons.addWidget(close)
            layout.addLayout(buttons)
            dialog.exec()

        def export_diagnostic_bundle(self):
            from .diagnostics import build_diagnostic_preview, export_diagnostics
            source = self.summary_path.parent if self.summary_path is not None else self.session
            if source is None: return
            try:
                preview = build_diagnostic_preview(source)
                notice = '将导出：' + '、'.join(preview.files) + '\n只包含允许的任务状态、清理布尔和证据摘要；不包含截图、日志原文、命令、账号和原生配置。'
                if preview.missing: notice += '\n缺失证据：' + '、'.join(preview.missing)
                if QMessageBox.question(self, '诊断包预览', notice) != QMessageBox.StandardButton.Yes: return
                path, _ = QFileDialog.getSaveFileName(self, '保存脱敏诊断包', '', 'ZIP (*.zip)')
                if not path: return
                export_diagnostics(preview, path)
                self.flash_hint('脱敏诊断包已保存，未上传')
            except (OSError, ValueError) as error: QMessageBox.warning(self, '导出失败', str(error))

        def open_run_log(self):
            target = self.workspace / 'runs'
            if self.session is not None:
                worker_log = self.session / 'worker.log'
                if worker_log.exists(): target = worker_log
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

        def set_problems(self, problems):
            def clear_layout(layout):
                while layout.count():
                    item = layout.takeAt(0)
                    if item.widget(): item.widget().deleteLater()
                    elif item.layout(): clear_layout(item.layout())
            clear_layout(self.problems_layout)
            self.problems_box.setVisible(bool(problems))
            self.refresh_run_card()
            if not problems: return
            head = QHBoxLayout(); head.setSpacing(8)
            icon = QLabel('⚠'); icon.setStyleSheet('color: #d97706; font-size: 13px; font-weight: 700;')
            title = QLabel(f'需处理 {len(problems)}'); title.setStyleSheet('color: #92400e; font-weight: 600;')
            head.addWidget(icon); head.addWidget(title); head.addStretch()
            self.problems_layout.addLayout(head)
            for problem in problems:
                row = QWidget(); layout = QHBoxLayout(row)
                layout.setContentsMargins(18, 0, 0, 0); layout.setSpacing(6)
                text = QLabel(f"[{problem['name']}] {problem['summary']}"
                              + (f" · {problem['detail']}" if problem.get('detail') else ''))
                text.setStyleSheet('color: #92400e;')
                open_log = QLabel('查看原因'); open_log.setObjectName('linkButton')
                open_log.setCursor(Qt.PointingHandCursor)
                open_log.mousePressEvent = lambda *_, game_id=problem.get('id'), problem=problem: self.open_result_reason(game_id, problem)
                layout.addWidget(text, 1); layout.addWidget(open_log)
                self.problems_layout.addWidget(row)

        LOG_PATTERNS = (
            (re.compile(r'^\[(\d+)/(\d+)\] (\S+): checking'), 'launch'),
            (re.compile(r'^(\S+): starting current saved preset'), 'start'),
            (re.compile(r'^Running: (\d+)s elapsed'), 'alive'),
            (re.compile(r'^Foreground: (\w+)'), 'foreground'),
            (re.compile(r'^(\S+): (.+)$'), 'status'),
        )

        def map_log_line(self, line):
            if line.startswith('{'):
                try:
                    payload = json.loads(line)
                    status = payload.get('status')
                    return f"本轮结束：{STATUS.get(status) or '结果待确认，详情见日志'}"
                except ValueError:
                    return line
            for pattern, kind in self.LOG_PATTERNS:
                match = pattern.match(line)
                if not match: continue
                if kind == 'launch':
                    self.node_started = time.monotonic()
                    name = NAMES.get(match.group(3), match.group(3))
                    return f'[{name}] 启动助手（第 {match.group(1)} / {match.group(2)} 个）'
                if kind == 'start':
                    return f'[{NAMES.get(match.group(1), match.group(1))}] 开始任务'
                if kind == 'status':
                    assistant, status = match.group(1), match.group(2)
                    verb = STATUS_VERB.get(status, '状态待确认，详情见日志')
                    result = self.results.get(assistant, {})
                    if result.get('duration_seconds') is not None and status in STATUS_VERB:
                        verb += f"，用时 {fmt_dur(result['duration_seconds'])}"
                    return f'[{NAMES.get(assistant, assistant)}] {verb}'
                if kind == 'alive':
                    self.node_started = time.monotonic() - int(match.group(1))
                    return f'运行中 · 已用时 {fmt_dur(int(match.group(1)))}'
                if kind == 'foreground':
                    return f'前台请求：{match.group(1)}'
            return line

        def poll_log(self):
            path = self.session / 'worker.log'
            if not path.exists(): return
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            if len(lines) <= self.log_seen: return
            stamp = time.strftime('%H:%M:%S')
            for line in lines[self.log_seen:]:
                if line.strip():
                    self.log.appendPlainText(f'{stamp}  {self.map_log_line(line)}')
            self.log_seen = len(lines)
            bar = self.log.verticalScrollBar(); bar.setValue(bar.maximum())

        # ---------- completion ----------

        def refresh_status_pills(self):
            if self.data is None: return
            for index, row in enumerate(self.visible_rows()):
                pending = row['adapter'] in PENDING_ADAPTERS or not row.get('installation_root')
                selected = (row['id'] in self.selected_ids if self.selected_ids is not None
                            else row.get('enabled', True))
                tint = '#f0f6ff' if selected and not pending else None
                self.table.setCellWidget(index, 4, self.status_cell_idle(row, pending, selected, tint))

        def finished(self, code, exit_status):
            self.poll(); self.refresh_controls()
            try:
                if self.action == 'recover-assistants':
                    recovery_path = self.session / 'recovery.json'
                    if recovery_path.exists():
                        report = json.loads(recovery_path.read_text(encoding='utf-8'))
                    else:
                        log_path = self.session / 'worker.log'
                        error = log_path.read_text(encoding='utf-8', errors='replace')[-12000:] if log_path.exists() else '核验进程未写入结果，请重新检查。'
                        report = {'status': 'blocked', 'message': '恢复结果无法确认；不会重新运行游戏。\n' + error}
                    QMessageBox.information(self, '运行资格核验', report['message'])
                    if report['status'] in {'recovered', 'already_ready'}:
                        self.check_report = None
                        self.recheck_timer.start(900)
                elif self.action == 'check-assistants':
                    report = json.loads((self.session / 'worker.log').read_text(encoding='utf-8'))
                    self.check_report = report
                    blocked = [r for r in report['assistants'] if r['status'] == 'blocked']
                    self.check_collapsed = not blocked
                    self.refresh_checklist(); self.refresh_status_pills()
                elif self.summary_path:
                    self.poll()
                    final = json.loads(self.summary_path.read_text(encoding='utf-8-sig'))
                    if final.get('status') in ('running', 'starting') or exit_status == QProcess.ExitStatus.CrashExit:
                        self.set_problems([{'name': '本轮', 'summary': '运行进程异常结束',
                                            'detail': '完成与清理结果尚未确认，请查看日志'}])
                else:
                    error = ''
                    if self.session is not None:
                        try:
                            with (self.session / 'worker.log').open('rb') as stream:
                                stream.seek(0, 2)
                                stream.seek(max(0, stream.tell() - 12000))
                                error = stream.read().decode('utf-8', errors='replace').strip()
                        except OSError:
                            pass
                    if 'previous execution has unverified cleanup; manual inspection required' in error:
                        explanation = '上一轮清理未确认，当前轮未启动。需要核对上一轮进程与清理证据；不会自动解除隔离。'
                    else:
                        explanation = '当前轮未启动；不会自动重试。'
                    if not error:
                        explanation += '\n运行器未写入错误记录，无法确认具体原因。'
                    self.set_problems([{'name': '本轮', 'summary': '未能完成启动',
                                        'detail': explanation,
                                        'cause_unknown': not error or 'previous execution has unverified cleanup; manual inspection required' in error,
                                        'error': ('运行器原始错误：\n' + error) if error else ''}])
            except (OSError, ValueError, KeyError):
                self.set_problems([{'name': '本轮', 'summary': '结果无法读取',
                                    'detail': '请查看日志'}])
            if self.action in {'check-assistants', 'recover-assistants'} and self.check_origin is not None:
                self.session, self.summary_path = self.check_origin
                self.check_origin = None
                self.refresh_run_card()
            self.selection_changed(schedule_check=False)
            if self.closing: self.close()

        def process_error(self, error):
            if error == QProcess.ProcessError.FailedToStart:
                self.refresh_controls()
                self.set_problems([{'name': '本轮', 'summary': '程序启动失败',
                                    'detail': sanitize_detail(self.process.errorString())}])
                if self.closing: self.close()

        def open_result(self):
            if self.summary_path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.summary_path)))

        def closeEvent(self, event):
            if self.busy():
                self.closing = True
                self.request_stop()
                event.ignore()
            else: event.accept()

    app = QApplication(sys.argv)
    configure_appearance(app)
    try:
        window = Window(); window.show()
    except (OSError, ValueError) as error:
        QMessageBox.critical(None, 'GachaOps 无法打开', str(error)); return 2
    # Layout-preview hook for off-Windows UI work; never set during real runs.
    preview_factory = globals().get('PREVIEW_FACTORY')
    if preview_factory is not None:
        preview_factory(window)
    if '--smoke-test' in sys.argv:
        output = Path(sys.argv[sys.argv.index('--smoke-test') + 1])
        def smoke():
            if window.busy():
                QTimer.singleShot(500, smoke); return
            output.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(output)); window.close(); app.quit()
        if '--smoke-check' in sys.argv: window.begin('check-assistants')
        QTimer.singleShot(800, smoke)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
