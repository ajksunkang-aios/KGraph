"""Enable `python -m kgraph_eval --kernel ...`."""
import sys
from kgraph_eval.cli import main

if __name__ == "__main__":
    sys.exit(main())
