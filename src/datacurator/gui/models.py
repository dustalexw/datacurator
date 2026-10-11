"""Qt models over a PlanStore. Rows are fetched in pages, so huge plans stay responsive."""
from __future__ import annotations

import os
from collections import OrderedDict

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel

from ..engine.planstore import PlanStore

ACTION_LABEL = {"move": "Move", "none": "No change", "review": "Review", "skip": "Leave", "rewrite": "Update refs",
                "write_tags": "Write tags", "quarantine": "Quarantine"}
FLAG_LABEL = {"conflict": "conflict", "uncertain": "uncertain", "duplicate": "duplicate", "error": "error",
              "sanitized": "name adjusted", "limitation": "preservation note", "excluded": "excluded"}

CATEGORY_LABEL = {"music": "Music", "tv": "TV", "movies": "Movie", "documents": "Document", "photos": "Photo",
                  "other": "Other"}
COLUMNS = ["Action", "Type", "Current location", "Proposed location", "Why"]


class PlanTableModel(QAbstractTableModel):
    PAGE = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store: PlanStore | None = None
        self.filter = "changes"
        self.text = ""
        self._count = 0
        self._pages: OrderedDict[int, list[dict]] = OrderedDict()
        self._base = ""

    def set_store(self, store: PlanStore | None):
        self.beginResetModel()
        self.store = store
        self._pages.clear()
        self._count = store.count(self.filter, self.text) if store else 0
        self._base = os.path.commonpath([*(store.header("sources") or {}).keys()]) if store and \
            (store.header("sources") or {}) else ""
        self.endResetModel()

    def set_filter(self, filt: str, text: str):
        self.filter, self.text = filt, text
        self.set_store(self.store)

    def refresh(self):
        self.set_store(self.store)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self._count

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def row(self, r: int) -> dict | None:
        if not self.store or r < 0 or r >= self._count:
            return None
        page = r // self.PAGE
        rows = self._pages.get(page)
        if rows is None:
            rows = self.store.page(self.filter, self.text, page * self.PAGE, self.PAGE)
            self._pages[page] = rows
            if len(self._pages) > 40:
                self._pages.popitem(last=False)
        i = r - page * self.PAGE
        return rows[i] if i < len(rows) else None

    def _short(self, p: str | None) -> str:
        if not p:
            return ""
        return p

    def data(self, index, role=Qt.DisplayRole):
        row = self.row(index.row())
        if row is None:
            return None
        c = index.column()
        flags = [f for f in (row.get("flags") or "").split(",") if f]
        if role in (Qt.DisplayRole, Qt.AccessibleTextRole):
            if c == 0:
                label = ACTION_LABEL.get(row["action"], row["action"])
                if flags:
                    label += " · " + ", ".join(FLAG_LABEL.get(f, f) for f in flags if f != "sanitized")
                return label.rstrip(" ·")
            if c == 1:
                name = CATEGORY_LABEL.get(row["category"], row["category"].capitalize())
                return name + (" · companion" if row["role"] == "companion" else "")
            if c == 2:
                return row["source"]
            if c == 3:
                if row["action"] in ("move", "quarantine") and row["dest"]:
                    return row["dest"]
                return "— stays where it is" if row["action"] != "none" else "— already in place"
            if c == 4:
                return row["reason"]
        if role == Qt.ToolTipRole:
            return row["reason"]
        if role == Qt.UserRole:
            return row["id"]
        return None


def build_tree(store: PlanStore, limit: int = 20000) -> QStandardItemModel:
    """Proposed destination folders with file counts (folders only; files counted)."""
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(["Proposed folder", "Files", "New"])
    rows = store.rows("SELECT dest_dir, COUNT(*) FROM items WHERE action IN ('move','none','quarantine') AND dest_dir "
                      "IS NOT NULL GROUP BY dest_dir ORDER BY dest_dir LIMIT ?", (limit,))
    new_dirs = set(store.header("new_dirs") or [])
    nodes: dict[str, QStandardItem] = {}
    roots = sorted({r for r in (store.header("dest_caps") or {}).keys()}, key=len, reverse=True)

    def node_for(path: str) -> QStandardItem:
        if path in nodes:
            return nodes[path]
        root = next((r for r in roots if path == r or path.startswith(r.rstrip(os.sep) + os.sep)), None)
        if root is None or path == root:
            it = QStandardItem(path)
            it.setEditable(False)
            c, n = QStandardItem(""), QStandardItem("new" if path in new_dirs else "")
            c.setEditable(False)
            n.setEditable(False)
            model.appendRow([it, c, n])
            nodes[path] = it
            return it
        parent = node_for(os.path.dirname(path))
        it = QStandardItem(os.path.basename(path))
        it.setEditable(False)
        c, n = QStandardItem(""), QStandardItem("new" if path in new_dirs else "")
        c.setEditable(False)
        n.setEditable(False)
        parent.appendRow([it, c, n])
        nodes[path] = it
        return it

    for d, n in rows:
        it = node_for(d)
        parent = it.parent() or model.invisibleRootItem()
        parent.child(it.row(), 1).setText(f"{n:,}")
    return model
