import os
import sys
import traceback
import tempfile


def show_startup_error(log_path):
    """Сообщение о падении старта средствами самой ОС — Qt может быть ещё не жив."""
    message = f"AtlasConverter failed to start.\nLog: {log_path}"
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "AtlasConverter", 0x10)
        elif sys.platform == "darwin":
            import subprocess
            script = (
                'display dialog "{}" with title "AtlasConverter" '
                'buttons {{"OK"}} default button "OK" with icon stop'
            ).format(message.replace('"', "'").replace("\n", "\\n"))
            subprocess.run(["osascript", "-e", script], check=False)
        else:
            print(message, file=sys.stderr)
    except Exception:
        pass


def main():
    try:
        from PySide6.QtWidgets import QApplication
        from gui import MainWindow
        from theme import apply_theme

        app = QApplication(sys.argv)
        apply_theme(app)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception:
        err = traceback.format_exc()
        log_path = os.path.join(tempfile.gettempdir(), "AtlasConverter_startup.log")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(err)
        except Exception:
            pass

        show_startup_error(log_path)
        raise


if __name__ == "__main__":
    main()
