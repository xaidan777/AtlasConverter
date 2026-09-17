"""Сборка macOS-версии AtlasConverter.

Запускать НА МАКЕ — PyInstaller не умеет кросс-компиляцию, из Windows
.app не собрать.

    python3 build_mac.py --check              # проверить окружение, ничего не собирая
    python3 build_mac.py                      # dist/AtlasConverter.app
    python3 build_mac.py --onefile            # ещё и одиночный бинарник внутри .app
    python3 build_mac.py --universal2         # если Python и все колёса universal2
    python3 build_mac.py --sign "Developer ID Application: Имя (TEAMID)"
    python3 build_mac.py --dmg                # плюс AtlasConverter.dmg для раздачи

Что делает:
1. чистит содержимое dist/ и build/;
2. делает tear_icon.icns из tear_icon.png (sips + iconutil, оба есть в macOS);
3. собирает .app через PyInstaller;
4. дописывает ключи в Info.plist (версия, Retina, минимальная версия macOS);
5. переподписывает бандл — правка Info.plist ломает подпись PyInstaller,
   а без валидной подписи на Apple Silicon приложение не запускается;
6. по флагу собирает DMG.
"""

import os
import plistlib
import shutil
import subprocess
import sys

APP_NAME = "AtlasConverter"
BUNDLE_ID = "com.atlasconverter.app"
VERSION = "1.0.0"
MIN_MACOS = "11.0"
ICON_SOURCE = "tear_icon.png"

# Стандартный набор размеров для .iconset, который принимает iconutil
ICONSET_SIZES = (16, 32, 128, 256, 512)


def fail(message):
    print(f"\nBuild FAILED: {message}")
    sys.exit(1)


def run(cmd, critical=True, **kwargs):
    """Запуск внешней команды. critical=False — сообщаем и живём дальше."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    except FileNotFoundError:
        message = f"'{cmd[0]}' not found"
        if critical:
            fail(message)
        print(f"WARNING: {message}")
        return None

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        message = f"{' '.join(cmd)}\n{details}"
        if critical:
            fail(message)
        print(f"WARNING: {message}")
        return None
    return result


def clean(folder):
    """Чистим содержимое, а не саму папку: открытую в Finder папку macOS
    удалить даст, но привычка из Windows-версии никому не мешает."""
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
            fail(f"cannot delete '{path}' — file is in use. Quit {APP_NAME}.app and try again.")


def make_icns():
    """PNG -> ICNS через штатные sips и iconutil. Ничего доустанавливать не нужно.

    Иконка — косметика, поэтому любая осечка здесь не валит сборку: приложение
    просто получит стандартную иконку Python.
    """
    if not os.path.exists(ICON_SOURCE):
        print(f"WARNING: '{ICON_SOURCE}' not found — building without a custom icon.")
        return None

    iconset = f"build/{APP_NAME}.iconset"
    icns = f"build/{APP_NAME}.icns"
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset, exist_ok=True)

    for size in ICONSET_SIZES:
        for scale, suffix in ((1, ""), (2, "@2x")):
            px = size * scale
            out = os.path.join(iconset, f"icon_{size}x{size}{suffix}.png")
            if run(["sips", "-z", str(px), str(px), ICON_SOURCE, "--out", out],
                   critical=False) is None:
                print("WARNING: icon generation failed — building without a custom icon.")
                return None

    if run(["iconutil", "--convert", "icns", iconset, "--output", icns],
           critical=False) is None:
        print("WARNING: iconutil failed — building without a custom icon.")
        return None

    print(f"Icon: {icns}")
    return icns


def patch_info_plist(app_path):
    """Правка плиста — тоже не повод убивать уже собранный бандл."""
    plist_path = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.exists(plist_path):
        print(f"WARNING: Info.plist not found in {app_path} — skipping plist keys.")
        return False

    try:
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)

        plist.update({
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": MIN_MACOS,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSApplicationCategoryType": "public.app-category.graphics-design",
        })

        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)
    except Exception as e:
        print(f"WARNING: cannot patch Info.plist ({e}) — bundle still usable.")
        return False

    print("Info.plist patched")
    return True


def codesign(app_path, identity):
    """Подпись: правка Info.plist ломает подпись PyInstaller, поэтому переподписываем.
    Ad-hoc ('-') хватает для запуска на своей машине; для раздачи нужен Developer ID.

    Бандл к этому моменту уже собран, поэтому осечка подписи — предупреждение,
    а не смерть сборки. Но на Apple Silicon без валидной подписи .app не стартует.
    """
    cmd = ["codesign", "--force", "--deep", "--sign", identity]
    if identity == "-":
        cmd.append("--timestamp=none")
    else:
        # Для нотаризации нужны защищённая среда выполнения и доверенная метка времени
        cmd += ["--options=runtime", "--timestamp"]

    if run(cmd + [app_path], critical=False) is None:
        print("WARNING: codesign failed. On Apple Silicon macOS may refuse to launch "
              "the app — sign it manually:")
        print(f"  codesign --force --deep --sign - '{app_path}'")
        return False

    verify = subprocess.run(["codesign", "--verify", "--deep", "--strict", app_path],
                            capture_output=True, text=True)
    state = "ok" if verify.returncode == 0 else (verify.stderr or "").strip()
    print(f"Codesign ({'ad-hoc' if identity == '-' else identity}): {state}")
    return verify.returncode == 0


def make_dmg(app_path):
    dmg_path = f"dist/{APP_NAME}.dmg"
    staging = "build/dmg"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    shutil.copytree(app_path, os.path.join(staging, os.path.basename(app_path)), symlinks=True)
    os.symlink("/Applications", os.path.join(staging, "Applications"))

    if os.path.exists(dmg_path):
        os.remove(dmg_path)
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", staging,
         "-ov", "-format", "UDZO", dmg_path])
    print(f"DMG: {dmg_path}")


def preflight():
    """Проверка окружения без сборки: python3 build_mac.py --check

    Отвечает на вопрос «а вообще взлетит?» до того, как ждать сборку.
    """
    import platform

    print("=== Environment ===")
    print(f"macOS      : {platform.mac_ver()[0] or '?'}")
    print(f"CPU arch   : {platform.machine()}")
    print(f"Python     : {sys.version.split()[0]} ({platform.architecture()[0]})")

    ok = True

    print("\n=== Python packages ===")
    for module, label in (("PySide6", "PySide6"), ("av", "PyAV"),
                          ("cv2", "OpenCV"), ("numpy", "NumPy"),
                          ("PyInstaller", "PyInstaller")):
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "?")
            print(f"  OK   {label:<12} {version}")
        except Exception as e:
            print(f"  FAIL {label:<12} {e}")
            ok = False

    print("\n=== macOS tools ===")
    for tool, why in (("sips", "icon resize"), ("iconutil", "icns"),
                      ("codesign", "signing"), ("hdiutil", "dmg")):
        path = shutil.which(tool)
        print(f"  {'OK  ' if path else 'MISS'} {tool:<9} {path or '(not found) - ' + why}")

    print("\n=== Project files ===")
    for name in ("main.py", "gui.py", "backend.py", "theme.py", ICON_SOURCE):
        exists = os.path.exists(name)
        print(f"  {'OK  ' if exists else 'MISS'} {name}")
        if not exists and name != ICON_SOURCE:
            ok = False

    print("\n=== App import test ===")
    try:
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        code = (
            "from PySide6.QtWidgets import QApplication;"
            "from theme import apply_theme;"
            "from gui import MainWindow;"
            "app = QApplication([]);"
            "apply_theme(app);"
            "w = MainWindow();"
            "print('window created:', w.windowTitle())"
        )
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            print(f"  OK   {result.stdout.strip()}")
        else:
            print(f"  FAIL {(result.stderr or '').strip()[-800:]}")
            ok = False
    except Exception as e:
        print(f"  FAIL {e}")
        ok = False

    print("\nReady to build." if ok else "\nFix the FAIL lines above before building.")
    return 0 if ok else 1


def main():
    if "--check" in sys.argv and sys.platform != "darwin":
        # Проверку окружения имеет смысл гонять только на маке, но пусть
        # хотя бы честно скажет, где её запускать.
        print(f"Run --check on macOS (current platform: {sys.platform}).")
        sys.exit(1)

    if sys.platform != "darwin":
        fail(f"macOS build must run on macOS (current platform: {sys.platform}). "
             "PyInstaller cannot cross-compile - use build.py for Windows.")

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if "--check" in sys.argv:
        sys.exit(preflight())

    try:
        import PyInstaller.__main__
    except ImportError:
        fail("PyInstaller is not installed. Run: pip3 install -r requirements.txt")

    onefile = "--onefile" in sys.argv
    universal = "--universal2" in sys.argv
    want_dmg = "--dmg" in sys.argv
    identity = "-"
    if "--sign" in sys.argv:
        index = sys.argv.index("--sign") + 1
        if index >= len(sys.argv):
            fail("--sign requires an identity, e.g. --sign \"Developer ID Application: Name (TEAMID)\"")
        identity = sys.argv[index]

    for folder in ("dist", "build", "__pycache__"):
        clean(folder)

    icns = make_icns()

    args = [
        "main.py",
        f"--name={APP_NAME}",
        "--windowed",              # .app-бандл вместо консольного бинарника
        "--noconfirm",
        "--clean",
        "--noupx",
        f"--osx-bundle-identifier={BUNDLE_ID}",
        "--hidden-import=av",
        "--hidden-import=av.logging",
        "--hidden-import=av.stream",
        "--hidden-import=numpy",
        "--hidden-import=cv2",
        "--collect-all=av",
        "--collect-all=cv2",
    ]
    # По умолчанию onedir: .app стартует сразу, onefile каждый раз распаковывает
    # ~150 МБ во временную папку.
    args.append("--onefile" if onefile else "--onedir")
    if icns:
        args.append(f"--icon={icns}")
    if universal:
        args.append("--target-arch=universal2")

    print("Starting macOS build...")
    try:
        PyInstaller.__main__.run(args)
    except SystemExit as e:
        if e.code:
            fail(f"PyInstaller exited with code {e.code}")
    except Exception as e:
        fail(str(e))

    app_path = os.path.abspath(f"dist/{APP_NAME}.app")
    if not os.path.isdir(app_path):
        fail(f"{app_path} was not created")

    patch_info_plist(app_path)
    codesign(app_path, identity)

    if want_dmg:
        make_dmg(app_path)

    print(f"\nBuild finished: {app_path}")
    print("Run it:  open dist/AtlasConverter.app")
    if identity == "-":
        print("Ad-hoc signature: fine on this machine. For distribution use "
              "--sign \"Developer ID Application: ...\" and notarize with notarytool.")
        print("If macOS blocks a copied build: xattr -dr com.apple.quarantine dist/AtlasConverter.app")


if __name__ == "__main__":
    main()
