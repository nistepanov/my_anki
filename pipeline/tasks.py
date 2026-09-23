"""Prose every task-file stage hands its worker, kept in one place because it is the same prose.

The stages differ in what they ask for, not in how the asking works: a directory of numbered
task files, one answer file each, answered by whoever is available. Rules about that mechanism
belong to the mechanism, and a copy per stage is a correction that lands in only one of them.
"""

# Appended to every stage's instructions. The cost of losing a half-finished answer file is the
# whole file, and that has happened often enough to be worth a paragraph.
INCREMENTAL_SAVING = """
## Saving as you go

Write the answer file as you work rather than once at the end: every ten rows or so, rewrite it
whole with everything finished so far. Rewriting keeps it valid JSON at every moment, so a run
that stops early loses the rows in hand instead of all of them.

Read the answer file first if it already exists. The rows it names are done and must not be
answered again — start from the first row of the task file it does not mention, and keep the
rows already there exactly as they are.
"""
