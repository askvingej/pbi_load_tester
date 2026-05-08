"""
Startskript för PBI Load Tester.
Sätter PYTHONNET_RUNTIME=coreclr innan Streamlit startar,
så att pythonnet alltid initieras med .NET 6+ (CoreCLR).

Kör med: python run.py
"""
import os
import sys
import subprocess

# Tvinga CoreCLR — måste sättas innan pythonnet/clr importeras första gången
os.environ["PYTHONNET_RUNTIME"] = "coreclr"

# Starta Streamlit i samma process-miljö
result = subprocess.run(
    [sys.executable, "-m", "streamlit", "run", "app.py"] + sys.argv[1:],
    env=os.environ,
)
sys.exit(result.returncode)