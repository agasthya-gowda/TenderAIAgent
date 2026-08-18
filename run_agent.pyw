import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

subprocess.Popen(
    [sys.executable, "main.py"],
    creationflags=subprocess.CREATE_NO_WINDOW
)