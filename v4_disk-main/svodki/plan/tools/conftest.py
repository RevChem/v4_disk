"""Скрипты tools/test_*.py с sys.exit() на уровне модуля pytest напрямую не собирает:
SystemExit при импорте обрывает сбор. Их запускает test_script_runner.py как подпроцессы.
"""
SCRIPT_STYLE = (
    "test_changes_feed.py",
    "test_daily_facts.py",
    "test_daily_facts_area.py",
    "test_ref_cache.py",
    "test_season_contrib_cache.py",
    "test_task_links_cache.py",
    "test_task_store.py",
    "test_task_store_sqlite.py",
)

collect_ignore = list(SCRIPT_STYLE)
