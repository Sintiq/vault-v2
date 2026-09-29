"""Claude-like visual theme for Qt widgets: warm light, deep dark."""

from __future__ import annotations

LIGHT = {
    "bg": "#F5F4EF",
    "pane": "#FFFFFF",
    "pane_alt": "#FAF9F6",
    "text": "#1F1E1D",
    "muted": "#6B6A66",
    "border": "#E4E2DB",
    "accent": "#D97757",
    "accent_text": "#FFFFFF",
    "select": "#EFE9E3",
    "user_bubble": "#F0EEE8",
    "agent_bubble": "#FFFFFF",
}

DARK = {
    "bg": "#262624",
    "pane": "#2B2A28",
    "pane_alt": "#30302D",
    "text": "#ECEAE3",
    "muted": "#A8A69F",
    "border": "#3B3A37",
    "accent": "#D97757",
    "accent_text": "#1F1E1D",
    "select": "#3D3B37",
    "user_bubble": "#383733",
    "agent_bubble": "#2B2A28",
}


def stylesheet(t: dict[str, str]) -> str:
    return f"""
    QMainWindow, QWidget {{
        background: {t['bg']};
        color: {t['text']};
        font-family: "Segoe UI", "Inter", sans-serif;
        font-size: 10.5pt;
    }}
    QToolBar {{
        background: {t['bg']};
        border: none;
        spacing: 3px;
        padding: 4px 6px;
    }}
    QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 5px 7px;
        color: {t['text']};
    }}
    QToolButton:hover {{ background: {t['select']}; border-color: {t['border']}; }}
    QToolButton:pressed {{ background: {t['border']}; }}
    QLabel#paneTitle {{
        font-size: 11pt;
        font-weight: 600;
        padding: 6px 10px 2px 10px;
        color: {t['text']};
    }}
    QLabel#paneSub {{
        font-size: 9pt;
        color: {t['muted']};
        padding: 0 10px 6px 10px;
    }}
    QFrame#pane {{
        background: {t['pane']};
        border: 1px solid {t['border']};
        border-radius: 12px;
    }}
    QFrame#pane[active="true"] {{ border: 1.5px solid {t['accent']}; }}
    QTreeView {{
        background: {t['pane']};
        alternate-background-color: {t['pane_alt']};
        border: none;
        border-radius: 0 0 12px 12px;
        selection-background-color: {t['select']};
        selection-color: {t['text']};
        outline: 0;
        padding: 2px 4px;
    }}
    QTreeView::item {{ padding: 4px 6px; border-radius: 6px; }}
    QTreeView::item:selected {{ background: {t['select']}; }}
    QHeaderView::section {{
        background: {t['pane']};
        color: {t['muted']};
        border: none;
        border-bottom: 1px solid {t['border']};
        padding: 4px 8px;
        font-size: 9pt;
    }}
    QSplitter::handle {{ background: transparent; width: 8px; height: 8px; }}
    QStatusBar {{ background: {t['bg']}; color: {t['muted']}; border-top: 1px solid {t['border']}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 5px; min-height: 24px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {t['border']}; border-radius: 5px; min-width: 24px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    QTabWidget#side::pane {{ border: none; }}
    QTabBar::tab {{
        background: transparent;
        color: {t['muted']};
        padding: 6px 16px;
        border: none;
        border-bottom: 2px solid transparent;
        font-weight: 600;
    }}
    QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; }}
    QListWidget {{
        background: {t['pane']};
        border: 1px solid {t['border']};
        border-radius: 10px;
        padding: 4px;
    }}
    QListWidget::item {{ padding: 6px 4px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {t['select']}; color: {t['text']}; }}

    /* chat */
    QFrame#chat {{
        background: {t['pane']};
        border: 1px solid {t['border']};
        border-radius: 12px;
    }}
    QLabel#chatTitle {{ font-size: 11pt; font-weight: 600; padding: 8px 12px 2px 12px; }}
    QLabel#chatSub {{ font-size: 9pt; color: {t['muted']}; padding: 0 12px 8px 12px; }}
    QScrollArea#chatScroll {{ background: {t['pane']}; border: none; }}
    QWidget#chatBody {{ background: {t['pane']}; }}
    QLabel#userBubble {{
        background: {t['user_bubble']};
        border-radius: 14px;
        padding: 10px 14px;
        color: {t['text']};
    }}
    QLabel#agentBubble {{
        background: {t['agent_bubble']};
        border: 1px solid {t['border']};
        border-radius: 14px;
        padding: 10px 14px;
        color: {t['text']};
    }}
    QLabel#sysBubble {{
        color: {t['muted']};
        padding: 4px 14px;
        font-size: 9pt;
    }}
    QTextEdit#chatInput {{
        background: {t['pane']};
        border: 1px solid {t['border']};
        border-radius: 14px;
        padding: 10px 12px;
        color: {t['text']};
        font-size: 10.5pt;
    }}
    QTextEdit#chatInput:focus {{ border: 1.5px solid {t['accent']}; }}
    QPushButton#sendBtn {{
        background: {t['accent']};
        color: {t['accent_text']};
        border: none;
        border-radius: 14px;
        padding: 8px 16px;
        font-weight: 600;
    }}
    QPushButton#sendBtn:disabled {{ background: {t['border']}; color: {t['muted']}; }}
    QPushButton#ghostBtn {{
        background: transparent;
        color: {t['muted']};
        border: 1px solid {t['border']};
        border-radius: 12px;
        padding: 6px 12px;
    }}
    QPushButton#ghostBtn:hover {{ background: {t['select']}; color: {t['text']}; }}
    QMenu {{
        background: {t['pane']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {t['select']}; }}
    QLineEdit {{
        background: {t['pane']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 6px 10px;
        color: {t['text']};
    }}
    """
