# Process Tracker

A simple desktop app for logging game project progress with projects, categories, dates, time spent, notes, and basic analytics.

## Features

- Create multiple projects and keep logs separated by project.
- Create, rename, and delete projects in the `Projects & Categories` tab.
- Create, rename, and delete categories in the `Projects & Categories` tab.
- Pick time spent from a dropdown instead of typing it manually.
- Log sessions retrospectively by setting a past `Work date`.
- Review your history with project, category, search, and date filters.
- See analytics for total time, recent time, active days, and time spent by category.

## Run

```powershell
python app.py
```

You can also open the app from the desktop shortcut named `Process Tracker`.

## Data Storage

Your entries are stored in a local SQLite database file named `process_tracking.db` in the same folder as the app.

## Git Sync

The app writes a `process_tracking_export.json` file and can push it to a separate repository URL that you set in the in-app `Settings` tab.
