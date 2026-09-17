# Atlas Converter

Desktop-приложение на PySide6 для сборки спрайт-атласов из видео/PNG-секвенций и пакетной обработки PNG-файлов.

## Стек

- PySide6 (GUI), PyAV (видео), OpenCV (обработка изображений), NumPy (альфа/маски/трансформации)
- PyInstaller (сборка в exe)

## Структура

- `main.py` — точка входа
- `gui.py` — UI + вся прикладная логика (режимы Atlas и Sprites, таймлайн, превью, экспорт, настройки)
- `backend.py` — данные и обработка: `VideoLoader`, `ImageProcessor`, `AtlasBuilder`
- `theme.py` — тёмно-серая тема (AE-style): палитра, QSS, цвета таймлайна и вьюпорта
- `build.py` / `AtlasConverter.spec` — сборка exe (Windows)
- `build_mac.py` — сборка `.app` (запускать на macOS)

## Две вкладки (работают параллельно)

- **Atlas** — загрузка видео/PNG-секвенции, chroma key, выбор диапазона кадров, сборка атласа, экспорт `atlas.png` + `atlas.json`
- **Sprites** — загрузка набора PNG, resize/crop/смещение/масштаб каждого спрайта, композиция на фон, пакетный экспорт PNG

Вкладки независимы: ассеты Atlas не попадают в Sprites и наоборот.

## Важные особенности

- Preview и export идут раздельными пайплайнами — при изменении одного обязательно проверяй второй
- Таймлайн только для Atlas: ключи (ромбы) можно перетаскивать мышью, поэтому `timeline.markers` не всегда равномерны. `Refresh Atlas` ключи НЕ трогает — только перерисовывает превью
- Ключи на таймлайне: `timeline.markers` — кадры по возрастанию, `timeline.selection` — индексы выбранных. Резиновая рамка в дорожке ключей выделяет несколько; рамку выделения двигают целиком или растягивают за края (`_drag_keys()` считает от снимка на начало жеста). ПКМ по ключу — меню Delete / Duplicate (на ближайший свободный кадр справа) / Start Here
- Порядок кадров в атласе — `timeline.atlas_order()`: Start Here (`timeline.start_key`) поворачивает секвенцию, ключи левее стыка уходят в конец. Превью, `export_atlas()`, `export_video()` и Play Frames берут `atlas_order()`, а не `markers`
- Transform (Atlas): сдвиг/масштаб содержимого внутри кадра — `atlas_transform()` → `ImageProcessor.transform_content()`. Применяется в `frame_to_rgba()` (превью и экспорт) и в `process_and_display()` (вьюпорт) — меняешь одно, проверь второе
- Рамка Transform во вьюпорте (Atlas): внутри — сдвиг, ручки — масштаб (Alt — от центра, Shift — переключить пропорции, при сдвиге — одна ось). Геометрия и жест — `ViewportWidget._drag_frame()`, значения уходят сигналом `transformDragged`; весь жест — один шаг undo (`begin_transform_drag()` / `end_transform_drag()`). Панорама в Atlas — средней кнопкой или перетаскиванием мимо рамки
- Вьюпорт с keying кэширует keying и композит на фон текущего кадра (`viewport_keyed_frame()`), иначе рамка тянется рывками. Новый параметр, влияющий на эту картинку, — добавь в ключ кэша
- Кнопки воспроизведения — `PlaybackBar` поверх вьюпорта (`ViewportWidget.set_overlay()`). Стиль самого вьюпорта задан с селектором `#viewport`: без селектора он каскадом перекрывает тему у панели
- Тексты интерфейса — только английские (UI приложения английский), комментарии в коде — русские
- Ключи заново раскладываются равномерно только при смене Frames (`on_frames_count_changed()`) и загрузке файла. Смена C/R выставляет Frames = C × R (`on_atlas_grid_changed()`) — дальше как при смене Frames; Frames можно поправить руками до следующей смены сетки
- Границы диапазона ключи НЕ раскладывают заново: при перетаскивании ручки ключи масштабируются вместе с диапазоном, ручная расстановка сохраняется (`TimelineWidget._scale_keys_to_range()`, от снимка на начало жеста). `on_crop_changed()` только планирует превью
- Undo/redo (Ctrl+Z / Ctrl+Shift+Z): свой `UndoStack` на вкладку. Atlas — снимок `(crop_start, crop_end, markers, start_key, transform)`, Sprites — трансформации. Жесты мыши открываются/закрываются сигналами `editStarted`/`editFinished` и `spriteEditStarted`/`spriteEditFinished`. Новое изменение ключей или трансформаций — не забудь положить снимок в стек
- Загрузить файлы можно тремя способами: кнопка, клик по пустому вьюпорту, drag&drop на вьюпорт
- Keying применяется только в режиме Atlas
- Один `VideoLoader` используется для обоих режимов
- Картинки с диска читать/писать только через `read_image()` / `write_image()` из `backend.py`, не `cv2.imread` / `cv2.imwrite`: на Windows OpenCV не понимает не-ASCII пути (кириллица в имени папки/юзера) — `imread` отдаёт None, `imwrite` молча возвращает False. Из-за этого у юзеров экспорт атласа оставлял только json
- В Sprites два режима трансформации: на текущий файл или на все файлы сразу

## Как менять

- Новый параметр keying: UI в `setup_ui()`, сигнал в `setup_connections()`, обработка в `ImageProcessor`, использование в `process_and_display()` (и ключ кэша `viewport_keyed_frame()`), `frame_to_rgba()`, `update_atlas_preview()`, `export_atlas()`
- Новый источник импорта: расширять `VideoLoader.load_video()` или `VideoLoader.load_image_list()`
- Изменение логики выбора кадров: `rebuild_markers()` (равномерно) и `TimelineWidget._drag_keys()` (перетаскивание и растяжение ключей), меню ключа — `_show_key_menu()`, `delete_keys()`, `duplicate_keys()`, `set_start_key()`
- Внешний вид таймлайна: `TimelineWidget` + константы `TL_*` в `theme.py`
- Интерактивное превью атласа: `AtlasViewer.set_cells()` (вызывается из `update_atlas_preview()`), клик — `cellClicked` → `on_atlas_cell_clicked()`
- Transform: UI в `setup_ui()` (`transform_group`), правка и undo — `on_transform_edited()` / `_apply_transform_change()`, пиксели — `ImageProcessor.transform_content()`, рамка во вьюпорте — `ViewportWidget.set_transform_frame()`, `_frame_part_at()`, `_drag_frame()`, `_paint_transform_frame()`
- Иконки кнопок рисуются кодом: `theme.make_icon()` / `theme.icon_pixmap()`. Курсор ⇹ — `theme.split_h_cursor()`: штатный `Qt.SplitHCursor` на Windows вдвое крупнее остальных, не использовать
- Тема и цвета: `theme.py` (`_QSS`, `VIEWPORT_*`, `DROPZONE_*`)
- Drag&drop и клик по вьюпорту: `ViewportWidget.dropEvent()`, `MainWindow.on_files_dropped()`
- Спрайтовые трансформации: `get_sprite_transform()`, `set_sprite_transform()`, `build_sprite_rgba()`
- Формат экспорта атласа: `export_atlas()`
- Пакетный экспорт PNG: `export_sprites()`
- Crop/resize: `ImageProcessor.resize_frame()` и `update_crop_rect()`
- Новый параметр настроек: добавить в `save_settings()` и `load_settings()`

## Подробная техкарта

См. `_utils/project.md` — полная документация по потокам данных, сущностям и слабым местам.
