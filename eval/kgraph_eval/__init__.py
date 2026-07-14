"""KGraph eval — consumes an existing kgraph.db, runs the KBench retrieval A/B, renders a report.

This is a thin orchestration layer on top of KBench (https://github.com/ajksunkang-aios/KBench).
It does NOT reimplement the agent loop, scorers, or task set — those live in KBench.
The only integration point is KBench's B-arm, which imports KGraph's mcp/server.py
directly (see eval/DESIGN.md §3).
"""

__version__ = "0.1.0"
