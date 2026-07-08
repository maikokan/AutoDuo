# Make `autoduo` importable when running `pytest` from the project root.
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))