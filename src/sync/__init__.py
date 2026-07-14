"""
KGraph sync module — incremental index update (lazy-indexing).

  change_detector.py   P2: parse compile_commands → .o mtime → find rebuilt TUs → filtered compdb
  incremental.py       P4-P5: localized scip-clang + transactional per-file ingestion (future)
  git_status.py        P3: git status wrapper for stability filtering (future)
"""
