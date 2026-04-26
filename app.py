from __future__ import annotations

import calendar
import json
import os
import shutil
import sqlite3
import subprocess
import tkinter as tk
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk


APP_TITLE = "Process Tracker"
APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
DB_PATH = APP_DIR / "process_tracking.db"
EXPORT_PATH = APP_DIR / "process_tracking_export.json"
SETTINGS_PATH = APP_DIR / "process_tracker_settings.json"
SYNC_REPO_DIR = APP_DIR / "process_tracker_sync_repo"
LOGO_PNG_PATH = ASSETS_DIR / "process_tracker_logo.png"
LOGO_ICO_PATH = ASSETS_DIR / "process_tracker_logo.ico"
DEFAULT_PROJECT_NAME = "Main Project"
DEFAULT_CATEGORY_NAME = "General"
FALLBACK_CATEGORY_NAME = "Uncategorized"
TIME_OPTIONS = [f"{value / 4:.2f}" for value in range(1, 49)]


def shorten_note(note: str, limit: int = 60) -> str:
    cleaned = " ".join(note.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


class ProcessTrackerDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, name),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )

            session_columns = self._get_table_columns(connection, "sessions")
            if not session_columns:
                self._create_sessions_table(connection)
            elif (
                "project_id" not in session_columns
                or "category_id" not in session_columns
                or "work_date" not in session_columns
            ):
                self._migrate_legacy_sessions(connection)

            self._ensure_default_data(connection)

    def _get_table_columns(self, connection: sqlite3.Connection, table_name: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in rows}

    def _create_sessions_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                focus TEXT NOT NULL,
                hours REAL NOT NULL CHECK(hours > 0),
                notes TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE RESTRICT
            )
            """
        )

    def _migrate_legacy_sessions(self, connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE sessions RENAME TO sessions_legacy")
        self._create_sessions_table(connection)

        project_id = self._ensure_project(connection, DEFAULT_PROJECT_NAME)
        category_id = self._ensure_category(connection, project_id, DEFAULT_CATEGORY_NAME)

        legacy_columns = self._get_table_columns(connection, "sessions_legacy")
        if {"entry_date", "focus", "hours", "notes"}.issubset(legacy_columns):
            connection.execute(
                """
                INSERT INTO sessions (
                    project_id,
                    category_id,
                    work_date,
                    created_at,
                    focus,
                    hours,
                    notes
                )
                SELECT
                    ?,
                    ?,
                    date(entry_date),
                    entry_date,
                    focus,
                    hours,
                    notes
                FROM sessions_legacy
                """,
                (project_id, category_id),
            )

        connection.execute("DROP TABLE sessions_legacy")

    def _ensure_default_data(self, connection: sqlite3.Connection) -> None:
        has_projects = connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone()
        if has_projects:
            return

        project_id = self._ensure_project(connection, DEFAULT_PROJECT_NAME)
        self._ensure_category(connection, project_id, DEFAULT_CATEGORY_NAME)

    def _ensure_project(self, connection: sqlite3.Connection, name: str) -> int:
        existing = connection.execute(
            "SELECT id FROM projects WHERE name = ?",
            (name.strip(),),
        ).fetchone()
        if existing:
            return int(existing["id"])

        cursor = connection.execute(
            "INSERT INTO projects (name, created_at) VALUES (?, ?)",
            (name.strip(), datetime.now().isoformat(timespec="seconds")),
        )
        return int(cursor.lastrowid)

    def _ensure_category(self, connection: sqlite3.Connection, project_id: int, name: str) -> int:
        existing = connection.execute(
            "SELECT id FROM categories WHERE project_id = ? AND name = ?",
            (project_id, name.strip()),
        ).fetchone()
        if existing:
            return int(existing["id"])

        cursor = connection.execute(
            """
            INSERT INTO categories (project_id, name, created_at)
            VALUES (?, ?, ?)
            """,
            (project_id, name.strip(), datetime.now().isoformat(timespec="seconds")),
        )
        return int(cursor.lastrowid)

    def get_projects(self) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT id, name, created_at
                FROM projects
                ORDER BY name COLLATE NOCASE ASC
                """
            ).fetchall()

    def add_project(self, name: str) -> int:
        with self._connection() as connection:
            project_id = self._ensure_project(connection, name)
            self._ensure_category(connection, project_id, DEFAULT_CATEGORY_NAME)
            return project_id

    def rename_project(self, project_id: int, new_name: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE projects SET name = ? WHERE id = ?",
                (new_name.strip(), project_id),
            )

    def delete_project(self, project_id: int) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def get_categories(self, project_id: int) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT id, project_id, name, created_at
                FROM categories
                WHERE project_id = ?
                ORDER BY name COLLATE NOCASE ASC
                """,
                (project_id,),
            ).fetchall()

    def add_category(self, project_id: int, name: str) -> int:
        with self._connection() as connection:
            return self._ensure_category(connection, project_id, name)

    def rename_category(self, category_id: int, new_name: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE categories SET name = ? WHERE id = ?",
                (new_name.strip(), category_id),
            )

    def delete_category(self, project_id: int, category_id: int) -> None:
        with self._connection() as connection:
            category = connection.execute(
                "SELECT name FROM categories WHERE id = ? AND project_id = ?",
                (category_id, project_id),
            ).fetchone()
            if category is None:
                return

            fallback_category = connection.execute(
                """
                SELECT id
                FROM categories
                WHERE project_id = ? AND id <> ?
                ORDER BY
                    CASE WHEN name = ? THEN 0 ELSE 1 END,
                    name COLLATE NOCASE ASC
                LIMIT 1
                """,
                (project_id, category_id, DEFAULT_CATEGORY_NAME),
            ).fetchone()

            if fallback_category is not None:
                fallback_category_id = int(fallback_category["id"])
            else:
                fallback_category_id = self._ensure_category(
                    connection,
                    project_id,
                    FALLBACK_CATEGORY_NAME,
                )

            connection.execute(
                """
                UPDATE sessions
                SET category_id = ?
                WHERE project_id = ? AND category_id = ?
                """,
                (fallback_category_id, project_id, category_id),
            )
            connection.execute("DELETE FROM categories WHERE id = ?", (category_id,))

    def add_session(
        self,
        project_id: int,
        category_id: int,
        work_date: str,
        focus: str,
        hours: float,
        notes: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    project_id,
                    category_id,
                    work_date,
                    created_at,
                    focus,
                    hours,
                    notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    category_id,
                    work_date,
                    datetime.now().isoformat(timespec="seconds"),
                    focus.strip(),
                    hours,
                    notes.strip(),
                ),
            )

    def delete_session(self, session_id: int) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def get_sessions(
        self,
        search: str = "",
        from_date: str = "",
        to_date: str = "",
        project_id: int | None = None,
        category_id: int | None = None,
    ) -> list[sqlite3.Row]:
        query = """
            SELECT
                s.id,
                s.work_date,
                s.created_at,
                p.name AS project_name,
                c.name AS category_name,
                s.focus,
                s.hours,
                s.notes
            FROM sessions s
            JOIN projects p ON p.id = s.project_id
            JOIN categories c ON c.id = s.category_id
            WHERE 1 = 1
        """
        params: list[str | int] = []

        if search.strip():
            like_value = f"%{search.strip()}%"
            query += """
                AND (
                    s.focus LIKE ?
                    OR s.notes LIKE ?
                    OR p.name LIKE ?
                    OR c.name LIKE ?
                )
            """
            params.extend([like_value, like_value, like_value, like_value])

        if from_date.strip():
            query += " AND date(s.work_date) >= date(?)"
            params.append(from_date.strip())

        if to_date.strip():
            query += " AND date(s.work_date) <= date(?)"
            params.append(to_date.strip())

        if project_id is not None:
            query += " AND s.project_id = ?"
            params.append(project_id)

        if category_id is not None:
            query += " AND s.category_id = ?"
            params.append(category_id)

        query += " ORDER BY s.work_date DESC, s.created_at DESC"

        with self._connection() as connection:
            return connection.execute(query, params).fetchall()

    def get_overview(self, project_id: int | None = None) -> sqlite3.Row:
        seven_days_ago = (datetime.now() - timedelta(days=6)).date().isoformat()
        today = datetime.now().date().isoformat()
        month_prefix = datetime.now().strftime("%Y-%m")
        params: list[str | int] = [seven_days_ago, today, month_prefix]
        project_filter = ""
        if project_id is not None:
            project_filter = "WHERE project_id = ?"
            params.append(project_id)

        with self._connection() as connection:
            return connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total_sessions,
                    COALESCE(SUM(hours), 0) AS total_hours,
                    COALESCE(AVG(hours), 0) AS average_hours,
                    COALESCE(SUM(CASE WHEN date(work_date) >= date(?) THEN hours END), 0) AS last_7_days,
                    COALESCE(SUM(CASE WHEN date(work_date) = date(?) THEN hours END), 0) AS today_hours,
                    COUNT(DISTINCT work_date) AS active_days,
                    COALESCE(SUM(CASE WHEN substr(work_date, 1, 7) = ? THEN hours END), 0) AS this_month_hours
                FROM sessions
                {project_filter}
                """,
                params,
            ).fetchone()

    def get_category_breakdown(
        self, project_id: int | None = None, limit: int = 10
    ) -> list[sqlite3.Row]:
        params: list[int] = []
        project_filter = ""
        if project_id is not None:
            project_filter = "WHERE s.project_id = ?"
            params.append(project_id)

        params.append(limit)
        with self._connection() as connection:
            return connection.execute(
                f"""
                SELECT
                    p.name AS project_name,
                    c.name AS category_name,
                    COUNT(*) AS session_count,
                    SUM(s.hours) AS total_hours
                FROM sessions s
                JOIN projects p ON p.id = s.project_id
                JOIN categories c ON c.id = s.category_id
                {project_filter}
                GROUP BY s.project_id, s.category_id
                ORDER BY total_hours DESC, p.name COLLATE NOCASE ASC, c.name COLLATE NOCASE ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

    def get_daily_totals(
        self, project_id: int | None = None, limit: int = 14
    ) -> list[sqlite3.Row]:
        params: list[int] = []
        project_filter = ""
        if project_id is not None:
            project_filter = "WHERE project_id = ?"
            params.append(project_id)

        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT work_date AS day, SUM(hours) AS total_hours
                FROM sessions
                {project_filter}
                GROUP BY work_date
                ORDER BY work_date DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return list(reversed(rows))

    def get_daily_totals_in_range(
        self,
        project_id: int | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> list[sqlite3.Row]:
        query = """
            SELECT work_date AS day, SUM(hours) AS total_hours
            FROM sessions
            WHERE 1 = 1
        """
        params: list[str | int] = []

        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)

        if start_date.strip():
            query += " AND date(work_date) >= date(?)"
            params.append(start_date.strip())

        if end_date.strip():
            query += " AND date(work_date) <= date(?)"
            params.append(end_date.strip())

        query += " GROUP BY work_date ORDER BY work_date ASC"

        with self._connection() as connection:
            return connection.execute(query, params).fetchall()

    def get_streaks(self, project_id: int | None = None) -> dict[str, int]:
        query = "SELECT DISTINCT work_date FROM sessions"
        params: list[int] = []
        if project_id is not None:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " ORDER BY work_date ASC"

        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()

        days = [parse_iso_date(str(row["work_date"])) for row in rows]
        if not days:
            return {"current_streak": 0, "longest_streak": 0}

        longest_streak = 1
        running_streak = 1
        for previous_day, current_day in zip(days, days[1:]):
            if current_day - previous_day == timedelta(days=1):
                running_streak += 1
            else:
                longest_streak = max(longest_streak, running_streak)
                running_streak = 1
        longest_streak = max(longest_streak, running_streak)

        current_streak = 0
        last_day = days[-1]
        if last_day >= date.today() - timedelta(days=1):
            current_streak = 1
            index = len(days) - 1
            while index > 0 and days[index] - days[index - 1] == timedelta(days=1):
                current_streak += 1
                index -= 1

        return {
            "current_streak": current_streak,
            "longest_streak": longest_streak,
        }

    def get_weekly_totals(
        self, project_id: int | None = None, weeks: int = 12
    ) -> list[dict[str, object]]:
        current_week_start = date.today() - timedelta(days=date.today().weekday())
        first_week_start = current_week_start - timedelta(weeks=max(weeks - 1, 0))
        last_week_end = current_week_start + timedelta(days=6)

        totals_by_day = {
            parse_iso_date(str(row["day"])): float(row["total_hours"])
            for row in self.get_daily_totals_in_range(
                project_id=project_id,
                start_date=first_week_start.isoformat(),
                end_date=last_week_end.isoformat(),
            )
        }

        weekly_rows: list[dict[str, object]] = []
        for week_index in range(weeks):
            week_start = first_week_start + timedelta(weeks=week_index)
            week_total = sum(
                totals_by_day.get(week_start + timedelta(days=offset), 0.0)
                for offset in range(7)
            )
            weekly_rows.append(
                {
                    "week_start": week_start.isoformat(),
                    "total_hours": week_total,
                    "is_current_week": week_start == current_week_start,
                }
            )

        return weekly_rows

    def export_snapshot(self) -> dict[str, object]:
        with self._connection() as connection:
            projects = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, name, created_at
                    FROM projects
                    ORDER BY name COLLATE NOCASE ASC
                    """
                ).fetchall()
            ]
            categories = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, project_id, name, created_at
                    FROM categories
                    ORDER BY project_id ASC, name COLLATE NOCASE ASC
                    """
                ).fetchall()
            ]
            sessions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        s.id,
                        s.project_id,
                        p.name AS project_name,
                        s.category_id,
                        c.name AS category_name,
                        s.work_date,
                        s.created_at,
                        s.focus,
                        s.hours,
                        s.notes
                    FROM sessions s
                    JOIN projects p ON p.id = s.project_id
                    JOIN categories c ON c.id = s.category_id
                    ORDER BY s.work_date DESC, s.created_at DESC, s.id DESC
                    """
                ).fetchall()
            ]

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "projects": projects,
            "categories": categories,
            "sessions": sessions,
        }


class ProcessTrackerApp:
    def _default_settings(self) -> dict[str, str]:
        return {
            "github_repo_url": "",
            "last_log_project": "",
        }

    def _load_settings(self) -> dict[str, str]:
        settings = self._default_settings()
        if SETTINGS_PATH.exists():
            try:
                loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    settings.update(
                        {
                            key: str(value)
                            for key, value in loaded.items()
                            if key in settings and value is not None
                        }
                    )
            except (OSError, json.JSONDecodeError):
                pass
        return settings

    def _save_settings(self) -> None:
        SETTINGS_PATH.write_text(
            json.dumps(self.settings, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.db = ProcessTrackerDB(DB_PATH)
        self.settings = self._load_settings()
        self.chart_rows: list[sqlite3.Row] = []
        self.weekly_rows: list[dict[str, object]] = []
        self.heatmap_totals: dict[date, float] = {}
        self.project_name_to_id: dict[str, int] = {}
        self.category_name_to_id_by_project: dict[int, dict[str, int]] = {}
        self.icon_image: tk.PhotoImage | None = None
        self.header_logo_image: tk.PhotoImage | None = None
        self.work_date_picker: tk.Toplevel | None = None
        self.work_date_picker_month = date.today().replace(day=1)
        self.work_date_picker_days_frame: ttk.Frame | None = None
        self.work_date_picker_header_var = tk.StringVar()

        self.log_project_var = tk.StringVar(value=str(self.settings.get("last_log_project", "")))
        self.log_category_var = tk.StringVar()
        self.focus_var = tk.StringVar()
        self.hours_var = tk.StringVar(value="1.00")
        self.work_date_var = tk.StringVar(value=date.today().isoformat())
        self.log_status_var = tk.StringVar()

        self.history_project_var = tk.StringVar(value="All Projects")
        self.history_category_var = tk.StringVar(value="All Categories")
        self.search_var = tk.StringVar()
        self.from_date_var = tk.StringVar()
        self.to_date_var = tk.StringVar()

        self.analytics_project_var = tk.StringVar(value="All Projects")
        self.manage_project_var = tk.StringVar()
        self.new_project_var = tk.StringVar()
        self.new_category_var = tk.StringVar()
        self.github_repo_url_var = tk.StringVar(value=str(self.settings.get("github_repo_url", "")))
        self.settings_status_var = tk.StringVar()

        self.auto_date_var = tk.StringVar()
        self.today_hours_var = tk.StringVar()
        self.total_sessions_var = tk.StringVar()
        self.total_hours_var = tk.StringVar()
        self.average_hours_var = tk.StringVar()
        self.last_7_days_var = tk.StringVar()
        self.active_days_var = tk.StringVar()
        self.avg_active_day_var = tk.StringVar()
        self.this_month_hours_var = tk.StringVar()
        self.current_streak_var = tk.StringVar()
        self.heatmap_summary_var = tk.StringVar()

        self.root.title(APP_TITLE)
        self.root.geometry("1260x820")
        self.root.minsize(1060, 680)

        self._configure_style()
        self._load_branding_assets()
        self._build_ui()
        self._load_projects(initial=True)
        self._update_auto_date()
        self.refresh_all()
        if not EXPORT_PATH.exists():
            self._export_data()

    def _configure_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        elif "vista" in style.theme_names():
            style.theme_use("vista")

        self.root.configure(bg="#f3f6fb")

        style.configure("TFrame", background="#f3f6fb")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("TLabel", background="#f3f6fb", foreground="#172033", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 10))
        style.configure("CardMuted.TLabel", background="#ffffff", foreground="#66758c", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#f3f6fb", foreground="#66758c")
        style.configure(
            "Header.TLabel",
            background="#f3f6fb",
            foreground="#172033",
            font=("Segoe UI Semibold", 17),
        )
        style.configure(
            "Metric.TLabel",
            background="#ffffff",
            foreground="#172033",
            font=("Segoe UI Semibold", 20),
        )
        style.configure(
            "MetricCaption.TLabel",
            background="#ffffff",
            foreground="#66758c",
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "TButton",
            background="#2d6cdf",
            foreground="#ffffff",
            padding=(14, 9),
            borderwidth=0,
            focusthickness=0,
            font=("Segoe UI Semibold", 10),
        )
        style.map("TButton", background=[("active", "#2358b7")])
        style.configure(
            "Secondary.TButton",
            background="#e7edf7",
            foreground="#172033",
            padding=(14, 9),
            borderwidth=0,
            font=("Segoe UI Semibold", 10),
        )
        style.map("Secondary.TButton", background=[("active", "#d6e1f1")])
        style.configure(
            "Danger.TButton",
            background="#d9534f",
            foreground="#ffffff",
            padding=(14, 9),
            borderwidth=0,
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#bf4340"), ("pressed", "#a93b38")],
            foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        )
        style.configure(
            "Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground="#172033",
            rowheight=34,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#eef3fb",
            foreground="#41506a",
            relief="flat",
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Treeview",
            background=[("selected", "#dce8fb")],
            foreground=[("selected", "#172033")],
        )
        style.configure(
            "TNotebook",
            background="#f3f6fb",
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background="#dfe6f2",
            foreground="#506179",
            padding=(18, 12),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff")],
            foreground=[("selected", "#172033")],
        )
        style.configure(
            "TEntry",
            fieldbackground="#fbfcfe",
            foreground="#172033",
            padding=8,
        )
        style.configure(
            "TCombobox",
            fieldbackground="#fbfcfe",
            foreground="#172033",
            padding=6,
            arrowsize=16,
        )

    def _load_branding_assets(self) -> None:
        if LOGO_PNG_PATH.exists():
            try:
                self.icon_image = tk.PhotoImage(file=str(LOGO_PNG_PATH))
                self.header_logo_image = self.icon_image.subsample(4, 4)
                self.root.iconphoto(True, self.icon_image)
            except tk.TclError:
                self.icon_image = None
                self.header_logo_image = None

        if LOGO_ICO_PATH.exists():
            try:
                self.root.iconbitmap(default=str(LOGO_ICO_PATH))
            except tk.TclError:
                pass

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)

        self.log_tab = ttk.Frame(notebook, padding=18)
        self.history_tab = ttk.Frame(notebook, padding=18)
        self.analytics_tab = ttk.Frame(notebook, padding=18)
        self.manage_tab = ttk.Frame(notebook, padding=18)
        self.settings_tab = ttk.Frame(notebook, padding=18)

        notebook.add(self.log_tab, text="Log Session")
        notebook.add(self.history_tab, text="History")
        notebook.add(self.analytics_tab, text="Analytics")
        notebook.add(self.manage_tab, text="Projects & Categories")
        notebook.add(self.settings_tab, text="Settings")

        self._build_log_tab()
        self._build_history_tab()
        self._build_analytics_tab()
        self._build_manage_tab()
        self._build_settings_tab()

    def _build_log_tab(self) -> None:
        self.log_tab.columnconfigure(0, weight=3)
        self.log_tab.columnconfigure(1, weight=2)
        self.log_tab.rowconfigure(1, weight=1)

        top_frame = ttk.Frame(self.log_tab)
        top_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)

        brand_frame = ttk.Frame(top_frame)
        brand_frame.grid(row=0, column=0, sticky="w")
        if self.header_logo_image is not None:
            ttk.Label(brand_frame, image=self.header_logo_image).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, 12)
            )
        ttk.Label(
            brand_frame,
            text=APP_TITLE,
            style="Header.TLabel",
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            brand_frame,
            text="Log what you worked on, when you worked on it, and how much time it took.",
            style="Muted.TLabel",
        ).grid(row=1, column=1, sticky="w", pady=(2, 0))
        ttk.Label(top_frame, textvariable=self.auto_date_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        form_card = ttk.Frame(self.log_tab, style="Card.TFrame", padding=18)
        form_card.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        form_card.columnconfigure(1, weight=1)
        form_card.rowconfigure(5, weight=1)

        ttk.Label(form_card, text="Project", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        self.log_project_combo = ttk.Combobox(
            form_card,
            textvariable=self.log_project_var,
            state="readonly",
        )
        self.log_project_combo.grid(row=0, column=1, sticky="ew", pady=(0, 10), padx=(12, 0))
        self.log_project_combo.bind("<<ComboboxSelected>>", self._on_log_project_change)

        ttk.Label(form_card, text="Category", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 10)
        )
        self.log_category_combo = ttk.Combobox(
            form_card,
            textvariable=self.log_category_var,
            state="readonly",
        )
        self.log_category_combo.grid(row=1, column=1, sticky="ew", pady=(0, 10), padx=(12, 0))

        ttk.Label(form_card, text="What did you work on?", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=(0, 10)
        )
        ttk.Entry(form_card, textvariable=self.focus_var).grid(
            row=2, column=1, sticky="ew", pady=(0, 10), padx=(12, 0)
        )

        ttk.Label(form_card, text="Time spent (hours)", style="Card.TLabel").grid(
            row=3, column=0, sticky="w", pady=(0, 10)
        )
        self.hours_combo = ttk.Combobox(
            form_card,
            textvariable=self.hours_var,
            values=TIME_OPTIONS,
            state="readonly",
        )
        self.hours_combo.grid(row=3, column=1, sticky="ew", pady=(0, 10), padx=(12, 0))

        date_row = ttk.Frame(form_card, style="Card.TFrame")
        date_row.grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=(0, 10))
        date_row.columnconfigure(0, weight=1)

        ttk.Label(form_card, text="Work date", style="Card.TLabel").grid(
            row=4, column=0, sticky="w", pady=(0, 10)
        )
        ttk.Entry(date_row, textvariable=self.work_date_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(date_row, text="Use Today", command=self._set_today_work_date).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )
        ttk.Button(
            date_row,
            text="Pick Date",
            command=self._open_work_date_picker,
            style="Secondary.TButton",
        ).grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Label(form_card, text="Notes", style="Card.TLabel").grid(row=5, column=0, sticky="nw")
        self.notes_text = tk.Text(
            form_card,
            height=15,
            wrap="word",
            background="#fbfcfe",
            foreground="#172033",
            insertbackground="#172033",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#d8e0ec",
            highlightcolor="#2d6cdf",
            font=("Segoe UI", 10),
        )
        self.notes_text.grid(row=5, column=1, sticky="nsew", padx=(12, 0))

        button_row = ttk.Frame(form_card, style="Card.TFrame")
        button_row.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        button_row.columnconfigure(0, weight=1)

        ttk.Button(button_row, text="Save Session", command=self.save_session).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            button_row,
            text="Clear Form",
            command=self.clear_form,
            style="Secondary.TButton",
        ).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )
        ttk.Label(button_row, textvariable=self.log_status_var, style="CardMuted.TLabel").grid(
            row=0, column=2, sticky="e"
        )

        sidebar = ttk.Frame(self.log_tab)
        sidebar.grid(row=1, column=1, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)

        today_card = ttk.Frame(sidebar, style="Card.TFrame", padding=18)
        today_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            today_card,
            text="Today's Logged Time",
            style="MetricCaption.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(today_card, textvariable=self.today_hours_var, style="Metric.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )

        tips_card = ttk.Frame(sidebar, style="Card.TFrame", padding=18)
        tips_card.grid(row=1, column=0, sticky="nsew")
        tips_card.columnconfigure(0, weight=1)
        ttk.Label(tips_card, text="Quick Tips", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        tips = (
            "Pick a project and category from the dropdowns before saving.\n\n"
            "Use the work date field to log retrospective entries in YYYY-MM-DD format.\n\n"
            "Projects and categories are managed in the last tab."
        )
        ttk.Label(tips_card, text=tips, justify="left", wraplength=280).grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )

        recent_card = ttk.Frame(sidebar, style="Card.TFrame", padding=18)
        recent_card.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        recent_card.columnconfigure(0, weight=1)
        recent_card.rowconfigure(1, weight=1)
        ttk.Label(recent_card, text="Recent Sessions", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        self.recent_tree = ttk.Treeview(
            recent_card,
            columns=("date", "focus", "hours"),
            show="headings",
            height=6,
        )
        self.recent_tree.grid(row=1, column=0, sticky="nsew")
        self.recent_tree.heading("date", text="Date")
        self.recent_tree.heading("focus", text="Worked On")
        self.recent_tree.heading("hours", text="Hours")
        self.recent_tree.column("date", width=90, anchor="center")
        self.recent_tree.column("focus", width=150, anchor="w")
        self.recent_tree.column("hours", width=70, anchor="center")

    def _build_history_tab(self) -> None:
        self.history_tab.columnconfigure(0, weight=1)
        self.history_tab.rowconfigure(1, weight=1)

        filter_row = ttk.Frame(self.history_tab)
        filter_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for column in range(11):
            filter_row.columnconfigure(column, weight=1 if column in (1, 5) else 0)

        ttk.Label(filter_row, text="Project").grid(row=0, column=0, sticky="w")
        self.history_project_combo = ttk.Combobox(
            filter_row,
            textvariable=self.history_project_var,
            state="readonly",
            width=20,
        )
        self.history_project_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12))
        self.history_project_combo.bind("<<ComboboxSelected>>", self._on_history_project_change)

        ttk.Label(filter_row, text="Category").grid(row=0, column=2, sticky="w")
        self.history_category_combo = ttk.Combobox(
            filter_row,
            textvariable=self.history_category_var,
            state="readonly",
            width=18,
        )
        self.history_category_combo.grid(row=0, column=3, sticky="w", padx=(8, 12))

        ttk.Label(filter_row, text="Search").grid(row=0, column=4, sticky="w")
        ttk.Entry(filter_row, textvariable=self.search_var).grid(
            row=0, column=5, sticky="ew", padx=(8, 12)
        )

        ttk.Label(filter_row, text="From").grid(row=0, column=6, sticky="w")
        ttk.Entry(filter_row, textvariable=self.from_date_var, width=12).grid(
            row=0, column=7, sticky="w", padx=(8, 12)
        )
        ttk.Label(filter_row, text="To").grid(row=0, column=8, sticky="w")
        ttk.Entry(filter_row, textvariable=self.to_date_var, width=12).grid(
            row=0, column=9, sticky="w", padx=(8, 12)
        )
        ttk.Button(filter_row, text="Apply Filters", command=self.refresh_history).grid(
            row=0, column=10, sticky="e"
        )

        table_card = ttk.Frame(self.history_tab, style="Card.TFrame", padding=12)
        table_card.grid(row=1, column=0, sticky="nsew")
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(0, weight=1)

        columns = (
            "id",
            "work_date",
            "logged_at",
            "project",
            "category",
            "focus",
            "hours",
            "notes",
        )
        self.history_tree = ttk.Treeview(
            table_card,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.history_tree.grid(row=0, column=0, sticky="nsew")

        self.history_tree.heading("id", text="ID")
        self.history_tree.heading("work_date", text="Work Date")
        self.history_tree.heading("logged_at", text="Saved At")
        self.history_tree.heading("project", text="Project")
        self.history_tree.heading("category", text="Category")
        self.history_tree.heading("focus", text="Worked On")
        self.history_tree.heading("hours", text="Hours")
        self.history_tree.heading("notes", text="Notes Preview")

        self.history_tree.column("id", width=0, stretch=False)
        self.history_tree.column("work_date", width=110, anchor="center")
        self.history_tree.column("logged_at", width=145, anchor="center")
        self.history_tree.column("project", width=170, anchor="w")
        self.history_tree.column("category", width=150, anchor="w")
        self.history_tree.column("focus", width=220, anchor="w")
        self.history_tree.column("hours", width=80, anchor="center")
        self.history_tree.column("notes", width=340, anchor="w")

        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=self.history_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=scrollbar.set)

        action_row = ttk.Frame(self.history_tab)
        action_row.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        action_row.columnconfigure(0, weight=1)

        ttk.Button(
            action_row,
            text="Refresh",
            command=self.refresh_history,
            style="Secondary.TButton",
        ).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            action_row,
            text="Delete Selected",
            command=self.delete_selected,
            style="Danger.TButton",
        ).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

    def _build_analytics_tab(self) -> None:
        self.analytics_tab.columnconfigure(0, weight=1)
        self.analytics_tab.rowconfigure(1, weight=0)
        self.analytics_tab.rowconfigure(2, weight=2)
        self.analytics_tab.rowconfigure(3, weight=1)
        self.analytics_tab.rowconfigure(4, weight=1)

        filter_row = ttk.Frame(self.analytics_tab)
        filter_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        filter_row.columnconfigure(1, weight=1)

        ttk.Label(filter_row, text="Project Scope").grid(row=0, column=0, sticky="w")
        self.analytics_project_combo = ttk.Combobox(
            filter_row,
            textvariable=self.analytics_project_var,
            state="readonly",
            width=24,
        )
        self.analytics_project_combo.grid(row=0, column=1, sticky="w", padx=(8, 12))
        self.analytics_project_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.refresh_analytics()
        )
        ttk.Button(filter_row, text="Refresh", command=self.refresh_analytics).grid(
            row=0, column=2, sticky="w"
        )

        summary_row = ttk.Frame(self.analytics_tab)
        summary_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for column in range(4):
            summary_row.columnconfigure(column, weight=1)

        metric_specs = (
            ("Total Sessions", self.total_sessions_var),
            ("Total Hours", self.total_hours_var),
            ("Average / Session", self.average_hours_var),
            ("Active Days", self.active_days_var),
            ("Last 7 Days", self.last_7_days_var),
            ("This Month", self.this_month_hours_var),
            ("Avg / Active Day", self.avg_active_day_var),
            ("Current Streak", self.current_streak_var),
        )

        for index, (label, variable) in enumerate(metric_specs):
            card = ttk.Frame(summary_row, style="Card.TFrame", padding=12)
            row_index = index // 4
            column_index = index % 4
            card.grid(
                row=row_index,
                column=column_index,
                sticky="nsew",
                padx=(0, 10 if column_index < 3 else 0),
                pady=(0, 10 if row_index == 0 else 0),
            )
            ttk.Label(card, text=label, style="MetricCaption.TLabel").grid(
                row=0, column=0, sticky="w"
            )
            ttk.Label(card, textvariable=variable, style="Metric.TLabel").grid(
                row=1, column=0, sticky="w", pady=(8, 0)
            )

        lower_row = ttk.Frame(self.analytics_tab)
        lower_row.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        lower_row.columnconfigure(0, weight=1)
        lower_row.columnconfigure(1, weight=2)
        lower_row.rowconfigure(0, weight=1)

        breakdown_card = ttk.Frame(lower_row, style="Card.TFrame", padding=12)
        breakdown_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        breakdown_card.columnconfigure(0, weight=1)
        breakdown_card.rowconfigure(1, weight=1)
        ttk.Label(
            breakdown_card,
            text="Time by Category",
            style="MetricCaption.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.breakdown_tree = ttk.Treeview(
            breakdown_card,
            columns=("category", "sessions", "hours"),
            show="headings",
            height=6,
        )
        self.breakdown_tree.grid(row=1, column=0, sticky="nsew")
        self.breakdown_tree.heading("category", text="Category")
        self.breakdown_tree.heading("sessions", text="Sessions")
        self.breakdown_tree.heading("hours", text="Hours")
        self.breakdown_tree.column("category", width=260, anchor="w")
        self.breakdown_tree.column("sessions", width=80, anchor="center")
        self.breakdown_tree.column("hours", width=90, anchor="center")

        chart_card = ttk.Frame(lower_row, style="Card.TFrame", padding=12)
        chart_card.grid(row=0, column=1, sticky="nsew")
        chart_card.columnconfigure(0, weight=1)
        chart_card.rowconfigure(1, weight=1)
        ttk.Label(chart_card, text="Category Breakdown Chart", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )

        self.chart_canvas = tk.Canvas(
            chart_card,
            background="#ffffff",
            highlightthickness=0,
            relief="flat",
            height=220,
        )
        self.chart_canvas.grid(row=1, column=0, sticky="nsew")
        self.chart_canvas.bind("<Configure>", lambda _event: self.draw_category_chart())

        bottom_row = ttk.Frame(self.analytics_tab)
        bottom_row.grid(row=3, column=0, sticky="nsew", pady=(0, 12))
        bottom_row.columnconfigure(0, weight=2)
        bottom_row.columnconfigure(1, weight=1)
        bottom_row.rowconfigure(0, weight=1)

        trend_card = ttk.Frame(bottom_row, style="Card.TFrame", padding=12)
        trend_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        trend_card.columnconfigure(0, weight=1)
        trend_card.rowconfigure(1, weight=1)
        ttk.Label(trend_card, text="Weekly Trend", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        self.weekly_trend_canvas = tk.Canvas(
            trend_card,
            background="#ffffff",
            highlightthickness=0,
            relief="flat",
            height=180,
        )
        self.weekly_trend_canvas.grid(row=1, column=0, sticky="nsew")
        self.weekly_trend_canvas.bind("<Configure>", lambda _event: self.draw_weekly_trend())

        daily_card = ttk.Frame(bottom_row, style="Card.TFrame", padding=12)
        daily_card.grid(row=0, column=1, sticky="nsew")
        daily_card.columnconfigure(0, weight=1)
        daily_card.rowconfigure(1, weight=1)
        ttk.Label(daily_card, text="Last 14 Work Dates", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )

        self.daily_tree = ttk.Treeview(
            daily_card,
            columns=("day", "hours"),
            show="headings",
            height=5,
        )
        self.daily_tree.grid(row=1, column=0, sticky="nsew")
        self.daily_tree.heading("day", text="Date")
        self.daily_tree.heading("hours", text="Logged Hours")
        self.daily_tree.column("day", width=180, anchor="w")
        self.daily_tree.column("hours", width=120, anchor="center")

        heatmap_card = ttk.Frame(self.analytics_tab, style="Card.TFrame", padding=12)
        heatmap_card.grid(row=4, column=0, sticky="nsew")
        heatmap_card.columnconfigure(0, weight=1)
        heatmap_card.rowconfigure(1, weight=1)
        ttk.Label(heatmap_card, text="26-Week Calendar Heatmap", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        self.heatmap_canvas = tk.Canvas(
            heatmap_card,
            background="#ffffff",
            highlightthickness=0,
            relief="flat",
            height=170,
        )
        self.heatmap_canvas.grid(row=1, column=0, sticky="nsew")
        self.heatmap_canvas.bind("<Configure>", lambda _event: self.draw_heatmap())
        ttk.Label(
            heatmap_card,
            textvariable=self.heatmap_summary_var,
            style="CardMuted.TLabel",
            justify="left",
            wraplength=1080,
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))

    def _build_manage_tab(self) -> None:
        self.manage_tab.columnconfigure(0, weight=1)
        self.manage_tab.columnconfigure(1, weight=1)
        self.manage_tab.rowconfigure(1, weight=1)

        ttk.Label(
            self.manage_tab,
            text="Create, rename, and remove projects and project-specific categories here.",
            style="Header.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))

        project_card = ttk.Frame(self.manage_tab, style="Card.TFrame", padding=18)
        project_card.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        project_card.columnconfigure(0, weight=1)
        project_card.rowconfigure(2, weight=1)

        ttk.Label(project_card, text="Projects", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        project_input_row = ttk.Frame(project_card, style="Card.TFrame")
        project_input_row.grid(row=1, column=0, sticky="ew", pady=(10, 14))
        project_input_row.columnconfigure(0, weight=1)

        ttk.Entry(project_input_row, textvariable=self.new_project_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(project_input_row, text="Add Project", command=self.add_project).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

        self.projects_tree = ttk.Treeview(
            project_card,
            columns=("name", "created"),
            show="headings",
            height=14,
        )
        self.projects_tree.grid(row=2, column=0, sticky="nsew")
        self.projects_tree.heading("name", text="Project")
        self.projects_tree.heading("created", text="Created")
        self.projects_tree.column("name", width=250, anchor="w")
        self.projects_tree.column("created", width=170, anchor="center")
        self.projects_tree.bind("<<TreeviewSelect>>", self._on_project_tree_select)

        project_actions = ttk.Frame(project_card, style="Card.TFrame")
        project_actions.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(
            project_actions,
            text="Rename Selected",
            command=self.rename_project,
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            project_actions,
            text="Delete Selected",
            command=self.delete_project,
            style="Danger.TButton",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        category_card = ttk.Frame(self.manage_tab, style="Card.TFrame", padding=18)
        category_card.grid(row=1, column=1, sticky="nsew")
        category_card.columnconfigure(0, weight=1)
        category_card.rowconfigure(3, weight=1)

        ttk.Label(category_card, text="Categories", style="MetricCaption.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.manage_project_combo = ttk.Combobox(
            category_card,
            textvariable=self.manage_project_var,
            state="readonly",
        )
        self.manage_project_combo.grid(row=1, column=0, sticky="ew", pady=(10, 14))
        self.manage_project_combo.bind("<<ComboboxSelected>>", self.refresh_manage_categories)

        category_input_row = ttk.Frame(category_card, style="Card.TFrame")
        category_input_row.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        category_input_row.columnconfigure(0, weight=1)

        ttk.Entry(category_input_row, textvariable=self.new_category_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(category_input_row, text="Add Category", command=self.add_category).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

        self.categories_tree = ttk.Treeview(
            category_card,
            columns=("name", "created"),
            show="headings",
            height=14,
        )
        self.categories_tree.grid(row=3, column=0, sticky="nsew")
        self.categories_tree.heading("name", text="Category")
        self.categories_tree.heading("created", text="Created")
        self.categories_tree.column("name", width=240, anchor="w")
        self.categories_tree.column("created", width=170, anchor="center")
        self.categories_tree.bind("<<TreeviewSelect>>", self._on_category_tree_select)

        category_actions = ttk.Frame(category_card, style="Card.TFrame")
        category_actions.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(
            category_actions,
            text="Rename Selected",
            command=self.rename_category,
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            category_actions,
            text="Delete Selected",
            command=self.delete_category,
            style="Danger.TButton",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

    def _build_settings_tab(self) -> None:
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.rowconfigure(1, weight=1)

        ttk.Label(
            self.settings_tab,
            text="Configure external sync for the exported tracker data.",
            style="Header.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 16))

        sync_card = ttk.Frame(self.settings_tab, style="Card.TFrame", padding=18)
        sync_card.grid(row=1, column=0, sticky="nsew")
        sync_card.columnconfigure(1, weight=1)

        ttk.Label(sync_card, text="GitHub Repo URL", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 12)
        )
        ttk.Entry(sync_card, textvariable=self.github_repo_url_var).grid(
            row=0, column=1, sticky="ew", pady=(0, 12), padx=(12, 0)
        )

        help_text = (
            "Set a repository URL like https://github.com/yourname/yourrepo.git.\n"
            "The app pushes the exported JSON file to that separate repo after data changes."
        )
        ttk.Label(sync_card, text=help_text, style="CardMuted.TLabel", justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 16)
        )

        actions = ttk.Frame(sync_card, style="Card.TFrame")
        actions.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(actions, text="Save Settings", command=self.save_settings).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            actions,
            text="Test Sync Now",
            command=self.test_sync_now,
            style="Secondary.TButton",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(actions, textvariable=self.settings_status_var, style="CardMuted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(16, 0)
        )

    def _update_auto_date(self) -> None:
        self.auto_date_var.set(
            f"Saved now: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.root.after(1000, self._update_auto_date)

    def _set_today_work_date(self) -> None:
        self.work_date_var.set(date.today().isoformat())

    def _close_work_date_picker(self) -> None:
        if self.work_date_picker is not None and self.work_date_picker.winfo_exists():
            self.work_date_picker.destroy()
        self.work_date_picker = None
        self.work_date_picker_days_frame = None

    def _select_work_date(self, selected_date: date) -> None:
        self.work_date_var.set(selected_date.isoformat())
        self._close_work_date_picker()

    def _change_work_date_picker_month(self, month_delta: int) -> None:
        year = self.work_date_picker_month.year
        month = self.work_date_picker_month.month + month_delta
        if month < 1:
            month = 12
            year -= 1
        elif month > 12:
            month = 1
            year += 1

        candidate = date(year, month, 1)
        if candidate > date.today().replace(day=1):
            return

        self.work_date_picker_month = candidate
        self._render_work_date_picker()

    def _render_work_date_picker(self) -> None:
        if self.work_date_picker_days_frame is None:
            return

        today = date.today()
        try:
            selected_date = parse_iso_date(self.work_date_var.get())
        except ValueError:
            selected_date = today

        if selected_date > today:
            selected_date = today

        self.work_date_picker_header_var.set(
            self.work_date_picker_month.strftime("%B %Y")
        )

        for child in self.work_date_picker_days_frame.winfo_children():
            child.destroy()

        for column, weekday in enumerate(calendar.day_abbr):
            ttk.Label(
                self.work_date_picker_days_frame,
                text=weekday,
                style="CardMuted.TLabel",
            ).grid(row=0, column=column, padx=3, pady=(0, 6))

        month_matrix = calendar.monthcalendar(
            self.work_date_picker_month.year,
            self.work_date_picker_month.month,
        )

        for row_index, week in enumerate(month_matrix, start=1):
            for column, day_number in enumerate(week):
                if day_number == 0:
                    ttk.Label(
                        self.work_date_picker_days_frame,
                        text="",
                        style="Card.TLabel",
                    ).grid(row=row_index, column=column, padx=3, pady=3)
                    continue

                cell_date = date(
                    self.work_date_picker_month.year,
                    self.work_date_picker_month.month,
                    day_number,
                )
                is_future = cell_date > today
                is_selected = cell_date == selected_date
                is_today = cell_date == today

                bg = "#fbfcfe"
                fg = "#172033"
                active_bg = "#eef3fb"
                disabled_fg = "#9aa8bb"
                relief = "flat"

                if is_selected:
                    bg = "#2d6cdf"
                    fg = "#ffffff"
                    active_bg = "#2358b7"
                elif is_today:
                    bg = "#dce8fb"

                button = tk.Button(
                    self.work_date_picker_days_frame,
                    text=str(day_number),
                    width=3,
                    font=("Segoe UI Semibold", 10),
                    bg=bg,
                    fg=fg,
                    activebackground=active_bg,
                    activeforeground=fg,
                    disabledforeground=disabled_fg,
                    relief=relief,
                    bd=0,
                    padx=8,
                    pady=7,
                    cursor="hand2" if not is_future else "arrow",
                    command=lambda chosen=cell_date: self._select_work_date(chosen),
                )
                if is_future:
                    button.configure(state="disabled")

                button.grid(row=row_index, column=column, padx=3, pady=3, sticky="nsew")

    def _open_work_date_picker(self) -> None:
        today = date.today()
        try:
            selected_date = parse_iso_date(self.work_date_var.get())
        except ValueError:
            selected_date = today

        if selected_date > today:
            selected_date = today

        self.work_date_picker_month = selected_date.replace(day=1)

        if self.work_date_picker is not None and self.work_date_picker.winfo_exists():
            self._render_work_date_picker()
            self.work_date_picker.deiconify()
            self.work_date_picker.lift()
            self.work_date_picker.focus_force()
            return

        popup = tk.Toplevel(self.root)
        popup.title("Pick Work Date")
        popup.configure(bg="#f3f6fb")
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.protocol("WM_DELETE_WINDOW", self._close_work_date_picker)
        self.work_date_picker = popup

        card = ttk.Frame(popup, style="Card.TFrame", padding=16)
        card.grid(row=0, column=0, padx=12, pady=12)
        card.columnconfigure(1, weight=1)

        ttk.Button(
            card,
            text="<",
            command=lambda: self._change_work_date_picker_month(-1),
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            card,
            textvariable=self.work_date_picker_header_var,
            style="Card.TLabel",
        ).grid(row=0, column=1, padx=14, sticky="ew")
        ttk.Button(
            card,
            text=">",
            command=lambda: self._change_work_date_picker_month(1),
            style="Secondary.TButton",
        ).grid(row=0, column=2, sticky="e")

        self.work_date_picker_days_frame = ttk.Frame(card, style="Card.TFrame")
        self.work_date_picker_days_frame.grid(row=1, column=0, columnspan=3, pady=(14, 10))

        footer = ttk.Frame(card, style="Card.TFrame")
        footer.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Button(
            footer,
            text="Today",
            command=lambda: self._select_work_date(today),
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            footer,
            text="Close",
            command=self._close_work_date_picker,
            style="Secondary.TButton",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        self._render_work_date_picker()
        popup.grab_set()
        popup.focus_force()

    def _get_selected_project_id(self, variable_value: str) -> int | None:
        return self.project_name_to_id.get(variable_value)

    def _get_selected_category_id(self, project_id: int | None, category_name: str) -> int | None:
        if project_id is None:
            return None
        return self.category_name_to_id_by_project.get(project_id, {}).get(category_name)

    def _load_projects(self, initial: bool = False, preferred_project: str | None = None) -> None:
        projects = self.db.get_projects()
        self.project_name_to_id = {row["name"]: int(row["id"]) for row in projects}
        project_names = list(self.project_name_to_id.keys())

        self.log_project_combo["values"] = project_names
        self.manage_project_combo["values"] = project_names

        history_values = ["All Projects", *project_names]
        analytics_values = ["All Projects", *project_names]
        self.history_project_combo["values"] = history_values
        self.analytics_project_combo["values"] = analytics_values

        if project_names:
            if preferred_project and preferred_project in project_names:
                chosen_project = preferred_project
            elif self.log_project_var.get() in project_names:
                chosen_project = self.log_project_var.get()
            else:
                chosen_project = project_names[0]

            self.log_project_var.set(chosen_project)

            if self.manage_project_var.get() not in project_names:
                self.manage_project_var.set(chosen_project)
            if self.history_project_var.get() not in history_values:
                self.history_project_var.set("All Projects")
            if self.analytics_project_var.get() not in analytics_values:
                self.analytics_project_var.set("All Projects")

        self._refresh_all_category_options()
        self._load_project_list()
        self._persist_last_log_project()

        if initial and not self.work_date_var.get().strip():
            self._set_today_work_date()

    def _refresh_all_category_options(self) -> None:
        for project_id in self.project_name_to_id.values():
            categories = self.db.get_categories(project_id)
            self.category_name_to_id_by_project[project_id] = {
                row["name"]: int(row["id"]) for row in categories
            }

        self._sync_log_categories()
        self._sync_history_categories()
        self.refresh_manage_categories()

    def _sync_log_categories(self) -> None:
        project_id = self._get_selected_project_id(self.log_project_var.get())
        category_map = self.category_name_to_id_by_project.get(project_id or -1, {})
        category_names = list(category_map.keys())
        self.log_category_combo["values"] = category_names
        if category_names:
            if self.log_category_var.get() not in category_names:
                self.log_category_var.set(category_names[0])
        else:
            self.log_category_var.set("")

        self.refresh_log_summary()

    def _sync_history_categories(self) -> None:
        project_id = self._get_selected_project_id(self.history_project_var.get())
        if project_id is None:
            self.history_category_combo["values"] = ["All Categories"]
            self.history_category_var.set("All Categories")
            self.history_category_combo.configure(state="disabled")
            return

        category_names = list(self.category_name_to_id_by_project.get(project_id, {}).keys())
        values = ["All Categories", *category_names]
        self.history_category_combo["values"] = values
        self.history_category_combo.configure(state="readonly")
        if self.history_category_var.get() not in values:
            self.history_category_var.set("All Categories")

    def _load_project_list(self) -> None:
        for item in self.projects_tree.get_children():
            self.projects_tree.delete(item)

        for project in self.db.get_projects():
            created = datetime.fromisoformat(project["created_at"]).strftime("%Y-%m-%d %H:%M")
            item = self.projects_tree.insert("", tk.END, values=(project["name"], created))
            if project["name"] == self.manage_project_var.get():
                self.projects_tree.selection_set(item)
                self.projects_tree.focus(item)

    def _on_log_project_change(self, _event=None) -> None:
        self._persist_last_log_project()
        self._sync_log_categories()

    def _on_history_project_change(self, _event=None) -> None:
        self._sync_history_categories()
        self.refresh_history()

    def _on_project_tree_select(self, _event=None) -> None:
        selection = self.projects_tree.selection()
        if not selection:
            return
        values = self.projects_tree.item(selection[0], "values")
        self.manage_project_var.set(values[0])
        self.refresh_manage_categories()

    def _on_category_tree_select(self, _event=None) -> None:
        selection = self.categories_tree.selection()
        if not selection:
            return
        values = self.categories_tree.item(selection[0], "values")
        self.new_category_var.set(values[0])

    def refresh_manage_categories(self, _event=None) -> None:
        for item in self.categories_tree.get_children():
            self.categories_tree.delete(item)

        project_id = self._get_selected_project_id(self.manage_project_var.get())
        if project_id is None:
            return

        for category in self.db.get_categories(project_id):
            created = datetime.fromisoformat(category["created_at"]).strftime("%Y-%m-%d %H:%M")
            self.categories_tree.insert("", tk.END, values=(category["name"], created))

    def _selected_project_name(self) -> str | None:
        selection = self.projects_tree.selection()
        if not selection:
            return None
        values = self.projects_tree.item(selection[0], "values")
        return values[0]

    def _selected_category_name(self) -> str | None:
        selection = self.categories_tree.selection()
        if not selection:
            return None
        values = self.categories_tree.item(selection[0], "values")
        return values[0]

    def _persist_last_log_project(self) -> None:
        self.settings["last_log_project"] = self.log_project_var.get().strip()
        self._save_settings()

    def _export_data(self) -> None:
        snapshot = self.db.export_snapshot()
        EXPORT_PATH.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def _git_run(self, args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return subprocess.run(
            args,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def _ensure_sync_repo(self) -> tuple[Path | None, str | None]:
        repo_url = self.github_repo_url_var.get().strip()
        if not repo_url:
            return None, "Saved locally. GitHub sync not configured."

        if not (SYNC_REPO_DIR / ".git").exists():
            if SYNC_REPO_DIR.exists():
                for child in SYNC_REPO_DIR.iterdir():
                    if child.is_dir():
                        return None, "Saved locally. Sync folder exists but is not a git repo."
                    child.unlink()
            else:
                SYNC_REPO_DIR.mkdir(parents=True, exist_ok=True)

            clone_result = self._git_run(
                ["git", "clone", repo_url, str(SYNC_REPO_DIR)],
                cwd=APP_DIR,
                check=False,
            )
            if clone_result.returncode != 0:
                if any(SYNC_REPO_DIR.iterdir()):
                    return None, "Saved locally. Could not clone the sync repo."
                self._git_run(["git", "init"], cwd=SYNC_REPO_DIR)
                self._git_run(["git", "remote", "add", "origin", repo_url], cwd=SYNC_REPO_DIR)

        remote_result = self._git_run(
            ["git", "remote", "get-url", "origin"],
            cwd=SYNC_REPO_DIR,
            check=False,
        )
        if remote_result.returncode != 0:
            self._git_run(["git", "remote", "add", "origin", repo_url], cwd=SYNC_REPO_DIR)
        elif remote_result.stdout.strip() != repo_url:
            self._git_run(["git", "remote", "set-url", "origin", repo_url], cwd=SYNC_REPO_DIR)

        email_result = self._git_run(
            ["git", "config", "--get", "user.email"],
            cwd=SYNC_REPO_DIR,
            check=False,
        )
        if email_result.returncode != 0 or not email_result.stdout.strip():
            self._git_run(
                ["git", "config", "user.email", "process-tracker@local"],
                cwd=SYNC_REPO_DIR,
            )

        name_result = self._git_run(
            ["git", "config", "--get", "user.name"],
            cwd=SYNC_REPO_DIR,
            check=False,
        )
        if name_result.returncode != 0 or not name_result.stdout.strip():
            self._git_run(
                ["git", "config", "user.name", "Process Tracker"],
                cwd=SYNC_REPO_DIR,
            )

        return SYNC_REPO_DIR, None

    def _sync_export_to_git(self, reason: str) -> str:
        sync_repo, skip_message = self._ensure_sync_repo()
        if sync_repo is None:
            return skip_message or "Saved locally. GitHub sync skipped."

        try:
            export_target = sync_repo / EXPORT_PATH.name
            export_target.write_text(EXPORT_PATH.read_text(encoding="utf-8"), encoding="utf-8")

            branch_result = self._git_run(
                ["git", "branch", "--show-current"],
                cwd=sync_repo,
                check=False,
            )
            branch_name = branch_result.stdout.strip() or "main"
            if not branch_result.stdout.strip():
                self._git_run(["git", "checkout", "-B", branch_name], cwd=sync_repo)

            self._git_run(["git", "add", "--", export_target.name], cwd=sync_repo)
            commit_result = self._git_run(
                ["git", "commit", "-m", f"Update process tracker data ({reason})"],
                cwd=sync_repo,
                check=False,
            )
            if commit_result.returncode != 0:
                commit_text = f"{commit_result.stdout}\n{commit_result.stderr}".lower()
                if "nothing to commit" in commit_text:
                    return "Saved locally. Git export already up to date."
                return "Saved locally. Git commit failed."

            push_result = self._git_run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=sync_repo,
                check=False,
            )
            if push_result.returncode != 0:
                return "Saved locally. Git commit worked, but push failed."
        except FileNotFoundError:
            return "Saved locally. Git is not installed."
        except subprocess.CalledProcessError:
            return "Saved locally. Git sync failed."

        return "Saved and pushed to GitHub."

    def _after_data_change(self, reason: str) -> None:
        self._export_data()
        sync_status = self._sync_export_to_git(reason)
        self.log_status_var.set(sync_status)
        self.settings_status_var.set(sync_status)

    def save_settings(self) -> None:
        old_repo_url = self.settings.get("github_repo_url", "").strip()
        new_repo_url = self.github_repo_url_var.get().strip()
        if old_repo_url != new_repo_url and SYNC_REPO_DIR.exists():
            shutil.rmtree(SYNC_REPO_DIR)

        self.settings["github_repo_url"] = new_repo_url
        self._persist_last_log_project()
        self._save_settings()
        self.settings_status_var.set("Settings saved.")

    def test_sync_now(self) -> None:
        self.save_settings()
        self._export_data()
        sync_status = self._sync_export_to_git("manual sync")
        self.settings_status_var.set(sync_status)
        self.log_status_var.set(sync_status)

    def clear_form(self, status_message: str = "Form cleared") -> None:
        self.focus_var.set("")
        self.hours_var.set("1.00")
        self.notes_text.delete("1.0", tk.END)
        self.work_date_var.set(date.today().isoformat())
        self.log_status_var.set(status_message)

    def add_project(self) -> None:
        project_name = self.new_project_var.get().strip()
        if not project_name:
            messagebox.showerror("Missing project", "Enter a project name.")
            return

        try:
            self.db.add_project(project_name)
        except sqlite3.IntegrityError:
            messagebox.showerror("Duplicate project", "That project name already exists.")
            return
        self.new_project_var.set("")
        self._load_projects(preferred_project=project_name)
        self.refresh_all()
        self._after_data_change("project added")

    def rename_project(self) -> None:
        project_name = self._selected_project_name()
        if not project_name:
            messagebox.showinfo("No selection", "Select a project first.")
            return

        project_id = self._get_selected_project_id(project_name)
        if project_id is None:
            return

        new_name = simpledialog.askstring(
            "Rename project",
            "New project name:",
            initialvalue=project_name,
            parent=self.root,
        )
        if not new_name or not new_name.strip():
            return

        try:
            self.db.rename_project(project_id, new_name)
        except sqlite3.IntegrityError:
            messagebox.showerror("Duplicate project", "That project name already exists.")
            return
        self._load_projects(preferred_project=new_name.strip())
        self.refresh_all()
        self._after_data_change("project renamed")

    def delete_project(self) -> None:
        project_name = self._selected_project_name()
        if not project_name:
            messagebox.showinfo("No selection", "Select a project first.")
            return

        if len(self.db.get_projects()) <= 1:
            messagebox.showerror("Cannot delete", "At least one project must remain.")
            return

        project_id = self._get_selected_project_id(project_name)
        if project_id is None:
            return

        should_delete = messagebox.askyesno(
            "Delete project",
            f"Delete project '{project_name}'?\n\nThis removes all logs and categories in that project.",
        )
        if not should_delete:
            return

        self.db.delete_project(project_id)
        self._load_projects()
        self.refresh_all()
        self._after_data_change("project deleted")

    def add_category(self) -> None:
        project_id = self._get_selected_project_id(self.manage_project_var.get())
        category_name = self.new_category_var.get().strip()

        if project_id is None:
            messagebox.showerror("Missing project", "Select a project first.")
            return

        if not category_name:
            messagebox.showerror("Missing category", "Enter a category name.")
            return

        try:
            self.db.add_category(project_id, category_name)
        except sqlite3.IntegrityError:
            messagebox.showerror("Duplicate category", "That category already exists in this project.")
            return
        self.new_category_var.set("")
        self._refresh_all_category_options()
        self.refresh_all()
        self._after_data_change("category added")

    def rename_category(self) -> None:
        project_id = self._get_selected_project_id(self.manage_project_var.get())
        category_name = self._selected_category_name()
        if project_id is None or not category_name:
            messagebox.showinfo("No selection", "Select a category first.")
            return

        category_id = self._get_selected_category_id(project_id, category_name)
        if category_id is None:
            return

        new_name = simpledialog.askstring(
            "Rename category",
            "New category name:",
            initialvalue=category_name,
            parent=self.root,
        )
        if not new_name or not new_name.strip():
            return

        try:
            self.db.rename_category(category_id, new_name)
        except sqlite3.IntegrityError:
            messagebox.showerror("Duplicate category", "That category already exists in this project.")
            return
        self._refresh_all_category_options()
        self.refresh_all()
        self._after_data_change("category renamed")

    def delete_category(self) -> None:
        project_id = self._get_selected_project_id(self.manage_project_var.get())
        category_name = self._selected_category_name()
        if project_id is None or not category_name:
            messagebox.showinfo("No selection", "Select a category first.")
            return

        category_id = self._get_selected_category_id(project_id, category_name)
        if category_id is None:
            return

        should_delete = messagebox.askyesno(
            "Delete category",
            f"Delete category '{category_name}'?\n\nAny existing logs in it will be moved automatically to another category.",
        )
        if not should_delete:
            return

        try:
            self.db.delete_category(project_id, category_id)
        except ValueError as error:
            messagebox.showerror("Cannot delete", str(error))
            return

        self.new_category_var.set("")
        self._refresh_all_category_options()
        self.refresh_all()
        self._after_data_change("category deleted")

    def _validate_work_date(self, work_date_text: str) -> str:
        try:
            work_day = parse_iso_date(work_date_text)
        except ValueError as error:
            raise ValueError("Work date must be in YYYY-MM-DD format.") from error

        if work_day > date.today():
            raise ValueError("Work date cannot be in the future.")

        return work_day.isoformat()

    def save_session(self) -> None:
        project_id = self._get_selected_project_id(self.log_project_var.get())
        category_id = self._get_selected_category_id(project_id, self.log_category_var.get())
        focus = self.focus_var.get().strip()
        notes = self.notes_text.get("1.0", tk.END).strip()
        hours_text = self.hours_var.get().strip()

        if project_id is None:
            messagebox.showerror("Missing info", "Select a project.")
            return

        if category_id is None:
            messagebox.showerror("Missing info", "Select a category.")
            return

        if not focus:
            messagebox.showerror("Missing info", "Enter what you worked on.")
            return

        try:
            hours = float(hours_text)
        except ValueError:
            messagebox.showerror("Invalid time", "Time spent must be selected from the dropdown.")
            return

        if hours <= 0:
            messagebox.showerror("Invalid time", "Time spent must be greater than zero.")
            return

        try:
            work_date = self._validate_work_date(self.work_date_var.get())
        except ValueError as error:
            messagebox.showerror("Invalid work date", str(error))
            return

        self.db.add_session(
            project_id=project_id,
            category_id=category_id,
            work_date=work_date,
            focus=focus,
            hours=hours,
            notes=notes,
        )
        self.clear_form()
        self.refresh_all()
        self._after_data_change("session saved")

    def refresh_all(self) -> None:
        self.refresh_history()
        self.refresh_analytics()
        self.refresh_log_summary()
        self.refresh_manage_categories()
        self.refresh_recent_sessions()

    def refresh_log_summary(self) -> None:
        project_id = self._get_selected_project_id(self.log_project_var.get())
        overview = self.db.get_overview(project_id=project_id)
        label = "selected project" if project_id is not None else "all projects"
        self.today_hours_var.set(f"{overview['today_hours']:.2f} hrs ({label})")

    def refresh_recent_sessions(self) -> None:
        for item in self.recent_tree.get_children():
            self.recent_tree.delete(item)

        project_id = self._get_selected_project_id(self.log_project_var.get())
        sessions = self.db.get_sessions(project_id=project_id)[:6]
        for session in sessions:
            self.recent_tree.insert(
                "",
                tk.END,
                values=(
                    session["work_date"],
                    shorten_note(session["focus"], 24),
                    f"{session['hours']:.2f}",
                ),
            )

    def refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)

        project_id = self._get_selected_project_id(self.history_project_var.get())
        category_id = self._get_selected_category_id(project_id, self.history_category_var.get())
        if self.history_category_var.get() == "All Categories":
            category_id = None

        sessions = self.db.get_sessions(
            search=self.search_var.get(),
            from_date=self.from_date_var.get(),
            to_date=self.to_date_var.get(),
            project_id=project_id,
            category_id=category_id,
        )

        for session in sessions:
            logged_at = datetime.fromisoformat(session["created_at"]).strftime("%Y-%m-%d %H:%M")
            self.history_tree.insert(
                "",
                tk.END,
                values=(
                    session["id"],
                    session["work_date"],
                    logged_at,
                    session["project_name"],
                    session["category_name"],
                    session["focus"],
                    f"{session['hours']:.2f}",
                    shorten_note(session["notes"]),
                ),
            )

    def delete_selected(self) -> None:
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("No selection", "Select a row in History first.")
            return

        item_id = selection[0]
        values = self.history_tree.item(item_id, "values")
        session_id = int(values[0])
        focus = values[5]

        should_delete = messagebox.askyesno(
            "Delete session",
            f"Delete the session for '{focus}'?",
        )
        if not should_delete:
            return

        self.db.delete_session(session_id)
        self.refresh_all()
        self._after_data_change("session deleted")

    def _analytics_project_id(self) -> int | None:
        return self._get_selected_project_id(self.analytics_project_var.get())

    def refresh_analytics(self) -> None:
        project_id = self._analytics_project_id()
        overview = self.db.get_overview(project_id=project_id)
        streaks = self.db.get_streaks(project_id=project_id)
        active_days = int(overview["active_days"])
        total_hours = float(overview["total_hours"])

        self.total_sessions_var.set(str(overview["total_sessions"]))
        self.total_hours_var.set(f"{total_hours:.2f} hrs")
        self.average_hours_var.set(f"{overview['average_hours']:.2f} hrs")
        self.last_7_days_var.set(f"{overview['last_7_days']:.2f} hrs")
        self.active_days_var.set(str(active_days))
        avg_active_day = total_hours / active_days if active_days else 0.0
        self.avg_active_day_var.set(f"{avg_active_day:.2f} hrs")
        self.this_month_hours_var.set(f"{overview['this_month_hours']:.2f} hrs")
        current_streak = int(streaks["current_streak"])
        self.current_streak_var.set(f"{current_streak} day" if current_streak == 1 else f"{current_streak} days")

        for item in self.breakdown_tree.get_children():
            self.breakdown_tree.delete(item)

        self.chart_rows = self.db.get_category_breakdown(project_id=project_id)
        for row in self.chart_rows:
            if project_id is None:
                category_label = f"{row['project_name']} / {row['category_name']}"
            else:
                category_label = row["category_name"]
            self.breakdown_tree.insert(
                "",
                tk.END,
                values=(category_label, row["session_count"], f"{row['total_hours']:.2f}"),
            )

        for item in self.daily_tree.get_children():
            self.daily_tree.delete(item)

        for row in self.db.get_daily_totals(project_id=project_id):
            self.daily_tree.insert("", tk.END, values=(row["day"], f"{row['total_hours']:.2f}"))

        self.weekly_rows = self.db.get_weekly_totals(project_id=project_id, weeks=12)

        current_week_start = date.today() - timedelta(days=date.today().weekday())
        heatmap_start = current_week_start - timedelta(weeks=25)
        heatmap_end = current_week_start + timedelta(days=6)
        heatmap_rows = self.db.get_daily_totals_in_range(
            project_id=project_id,
            start_date=heatmap_start.isoformat(),
            end_date=heatmap_end.isoformat(),
        )
        self.heatmap_totals = {
            parse_iso_date(str(row["day"])): float(row["total_hours"]) for row in heatmap_rows
        }
        best_day_hours = max(self.heatmap_totals.values(), default=0.0)
        if self.heatmap_totals:
            longest_streak = int(streaks["longest_streak"])
            self.heatmap_summary_var.set(
                (
                    f"{heatmap_start.strftime('%b %d')} to {heatmap_end.strftime('%b %d, %Y')}  "
                    f"|  Best day: {best_day_hours:.2f} hrs  "
                    f"|  Longest streak: {longest_streak} day"
                    f"{'' if longest_streak == 1 else 's'}  "
                    "|  Darker cells mean more logged hours."
                )
            )
        else:
            self.heatmap_summary_var.set("No activity in the selected scope yet.")

        self.draw_category_chart()
        self.draw_weekly_trend()
        self.draw_heatmap()

    def draw_category_chart(self) -> None:
        canvas = self.chart_canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        canvas.create_rectangle(0, 0, width, height, fill="#ffffff", outline="")

        if width < 160 or height < 120:
            return

        if not self.chart_rows:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Add a few sessions to see the chart.",
                fill="#66758c",
                font=("Segoe UI", 12),
            )
            return

        max_hours = max(float(row["total_hours"]) for row in self.chart_rows)
        left_margin = min(220, max(132, int(width * 0.34)))
        right_margin = 92
        top_margin = 18
        bar_gap = 10
        available_height = height - top_margin * 2
        bar_height = max(
            16,
            int((available_height - bar_gap * (len(self.chart_rows) - 1)) / len(self.chart_rows)),
        )
        bar_area_width = max(width - left_margin - right_margin, 40)

        colors = ["#2d6cdf", "#4db6ac", "#f28b50", "#8b7cf6", "#55b66a", "#f0c44c"]
        project_id = self._analytics_project_id()

        for index, row in enumerate(self.chart_rows):
            y0 = top_margin + index * (bar_height + bar_gap)
            y1 = y0 + bar_height
            bar_width = 0
            if max_hours > 0:
                bar_width = bar_area_width * (
                    float(row["total_hours"]) / max_hours
                )

            label = row["category_name"]
            if project_id is None:
                label = f"{row['project_name']} / {row['category_name']}"
            label = shorten_note(label, 28)

            canvas.create_text(
                left_margin - 10,
                (y0 + y1) / 2,
                text=label,
                fill="#172033",
                anchor="e",
                font=("Segoe UI", 10),
            )
            canvas.create_rectangle(
                left_margin,
                y0,
                left_margin + bar_width,
                y1,
                fill=colors[index % len(colors)],
                outline="",
            )
            value_text = f"{float(row['total_hours']):.2f} hrs"
            value_x = left_margin + bar_width + 8
            value_anchor = "w"
            value_fill = "#506179"
            if value_x + 60 > width - 8:
                value_x = max(left_margin + bar_width - 8, left_margin + 10)
                value_anchor = "e"
                value_fill = "#ffffff" if bar_width >= 78 else "#172033"
            canvas.create_text(
                value_x,
                (y0 + y1) / 2,
                text=value_text,
                fill=value_fill,
                anchor=value_anchor,
                font=("Segoe UI", 10),
            )

    def draw_weekly_trend(self) -> None:
        canvas = self.weekly_trend_canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        canvas.create_rectangle(0, 0, width, height, fill="#ffffff", outline="")

        if width < 220 or height < 120:
            return

        if not self.weekly_rows:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Add more sessions to see weekly trends.",
                fill="#66758c",
                font=("Segoe UI", 12),
            )
            return

        max_hours = max(float(row["total_hours"]) for row in self.weekly_rows)
        if max_hours <= 0:
            canvas.create_text(
                width / 2,
                height / 2,
                text="No logged hours in the last 12 weeks.",
                fill="#66758c",
                font=("Segoe UI", 12),
            )
            return

        left_margin = 42
        right_margin = 18
        top_margin = 24
        bottom_margin = 38
        chart_width = width - left_margin - right_margin
        chart_height = height - top_margin - bottom_margin
        slot_width = chart_width / max(len(self.weekly_rows), 1)
        bar_width = max(12, slot_width * 0.58)

        canvas.create_line(
            left_margin,
            height - bottom_margin,
            width - right_margin,
            height - bottom_margin,
            fill="#d8e0ec",
            width=1,
        )

        for guide_ratio in (0.25, 0.5, 0.75, 1.0):
            y = top_margin + chart_height * (1 - guide_ratio)
            canvas.create_line(
                left_margin,
                y,
                width - right_margin,
                y,
                fill="#eef3fb",
                width=1,
            )
            canvas.create_text(
                left_margin - 8,
                y,
                text=f"{max_hours * guide_ratio:.1f}",
                fill="#8191a8",
                anchor="e",
                font=("Segoe UI", 8),
            )

        for index, row in enumerate(self.weekly_rows):
            week_hours = float(row["total_hours"])
            week_start = parse_iso_date(str(row["week_start"]))
            x_center = left_margin + slot_width * index + slot_width / 2
            bar_height = chart_height * (week_hours / max_hours)
            x0 = x_center - bar_width / 2
            y0 = height - bottom_margin - bar_height
            x1 = x_center + bar_width / 2
            y1 = height - bottom_margin
            fill = "#2d6cdf" if bool(row["is_current_week"]) else "#9dbdff"

            canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")

            if week_hours > 0:
                canvas.create_text(
                    x_center,
                    y0 - 8,
                    text=f"{week_hours:.1f}",
                    fill="#506179",
                    font=("Segoe UI", 8),
                )

            if index % 2 == 0 or bool(row["is_current_week"]):
                canvas.create_text(
                    x_center,
                    height - bottom_margin + 16,
                    text=week_start.strftime("%b %d"),
                    fill="#66758c",
                    font=("Segoe UI", 8),
                )

    def _heatmap_color(self, hours: float, max_hours: float, day: date) -> str:
        if day > date.today():
            return "#f7f9fc"
        if hours <= 0:
            return "#e7edf7"
        if max_hours <= 0:
            return "#d6e6ff"

        ratio = hours / max_hours
        if ratio <= 0.25:
            return "#d6e6ff"
        if ratio <= 0.5:
            return "#a9c6ff"
        if ratio <= 0.75:
            return "#6f9ff5"
        return "#2d6cdf"

    def draw_heatmap(self) -> None:
        canvas = self.heatmap_canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        canvas.create_rectangle(0, 0, width, height, fill="#ffffff", outline="")

        if width < 220 or height < 110:
            return

        current_week_start = date.today() - timedelta(days=date.today().weekday())
        start_day = current_week_start - timedelta(weeks=25)
        total_weeks = 26
        max_hours = max(self.heatmap_totals.values(), default=0.0)

        left_margin = 34
        top_margin = 22
        right_margin = 12
        bottom_margin = 22
        grid_width = width - left_margin - right_margin
        grid_height = height - top_margin - bottom_margin
        cell_size = min(grid_width / total_weeks, grid_height / 7)
        cell_gap = max(1, int(cell_size * 0.12))
        cell_span = max(cell_size - cell_gap, 4)

        day_labels = ("M", "T", "W", "T", "F", "S", "S")
        for day_index, day_label in enumerate(day_labels):
            y = top_margin + day_index * cell_size + cell_span / 2
            canvas.create_text(
                left_margin - 10,
                y,
                text=day_label,
                fill="#8191a8",
                anchor="e",
                font=("Segoe UI", 8),
            )

        previous_month: int | None = None
        for week_index in range(total_weeks):
            week_start = start_day + timedelta(weeks=week_index)
            if previous_month != week_start.month:
                x = left_margin + week_index * cell_size
                canvas.create_text(
                    x,
                    top_margin - 10,
                    text=week_start.strftime("%b"),
                    fill="#66758c",
                    anchor="w",
                    font=("Segoe UI Semibold", 8),
                )
                previous_month = week_start.month

            for day_index in range(7):
                current_day = week_start + timedelta(days=day_index)
                hours = self.heatmap_totals.get(current_day, 0.0)
                x0 = left_margin + week_index * cell_size
                y0 = top_margin + day_index * cell_size
                x1 = x0 + cell_span
                y1 = y0 + cell_span
                canvas.create_rectangle(
                    x0,
                    y0,
                    x1,
                    y1,
                    fill=self._heatmap_color(hours, max_hours, current_day),
                    outline="",
                )

        legend_y = height - 10
        legend_x = max(left_margin, width - 118)
        canvas.create_text(
            legend_x - 12,
            legend_y,
            text="Less",
            fill="#8191a8",
            anchor="e",
            font=("Segoe UI", 8),
        )
        legend_colors = ["#e7edf7", "#d6e6ff", "#a9c6ff", "#6f9ff5", "#2d6cdf"]
        for index, color in enumerate(legend_colors):
            x0 = legend_x + index * 14
            canvas.create_rectangle(x0, legend_y - 5, x0 + 10, legend_y + 5, fill=color, outline="")
        canvas.create_text(
            legend_x + len(legend_colors) * 14 + 6,
            legend_y,
            text="More",
            fill="#8191a8",
            anchor="w",
            font=("Segoe UI", 8),
        )


def main() -> None:
    root = tk.Tk()
    ProcessTrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
