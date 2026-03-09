import os
import sys

# Ensure the project root is in the path
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from orchestrator.api import run_local

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Universal Deployer LOCAL")
    parser.add_argument("--port", type=int, default=8000, help="Port to run on (default: 8000)")
    args = parser.parse_args()
    
    run_local(args.port)
