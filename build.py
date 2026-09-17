import PyInstaller.__main__
import os
import shutil
import sys

# Скрипт должен работать из папки проекта, откуда бы его ни запустили
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def pause():
    if sys.stdin is None or not sys.stdin.isatty():
        return
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass


def clean(folder):
    """Чистит содержимое папки, но не саму папку.

    Открытая в проводнике или в терминале папка не удаляется, хотя писать
    в неё Windows разрешает — из-за этого сборка падала на ровном месте.
    """
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except PermissionError:
            print(f"Cannot delete '{path}' — file is in use.")
            if name.lower().endswith('.exe'):
                print("Close AtlasConverter.exe and run the build again!")
            pause()
            sys.exit(1)


# Clean dist/build folders
for folder in ('dist', 'build', '__pycache__'):
    clean(folder)

# Remove old spec to force fresh generation with current icon
if os.path.exists('AtlasConverter.spec'):
    os.remove('AtlasConverter.spec')

# Define arguments
args = [
    'main.py',
    '--name=AtlasConverter',
    '--windowed',
    '--onefile',
    '--clean',
    '--noupx',
    '--icon=tear_icon.ico',
    # Add explicit imports if needed, e.g. for av
    '--hidden-import=av',
    '--hidden-import=av.logging',
    '--hidden-import=av.stream',
    '--hidden-import=numpy',
    '--hidden-import=cv2',
    '--collect-all=av',
    '--collect-all=cv2',
]

print("Starting build...")
try:
    PyInstaller.__main__.run(args)
    print("\nBuild finished. Check 'dist' folder.")
except Exception as e:
    print(f"\nBuild FAILED: {e}")

pause()
