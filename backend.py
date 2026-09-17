import av
import cv2
import numpy as np
import os
import re


# OpenCV на Windows открывает файлы через ANSI-API: кириллица, диакритика и иероглифы
# в пути превращаются в мусор. cv2.imread тогда молча отдаёт None, cv2.imwrite — False
# (или пишет файл с кракозябрами в имени). Поэтому файлы читаем и пишем средствами
# Python, а OpenCV только кодирует/декодирует байты в памяти.

def read_image(path):
    """Аналог cv2.imread(path, cv2.IMREAD_UNCHANGED) для любых путей. None, если не прочитать."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)


def write_image(path, image):
    """Аналог cv2.imwrite для любых путей, формат — по расширению. При ошибке бросает исключение."""
    ok, buf = cv2.imencode(os.path.splitext(path)[1], image)
    if not ok:
        raise RuntimeError(f"Failed to encode image: {path}")
    with open(path, "wb") as f:
        f.write(buf.tobytes())


class VideoLoader:
    def __init__(self):
        self.container = None
        self.stream = None
        self.is_sequence = False
        self.image_sequence = []
        self.is_image_list = False
        self.file_path = ""
        self.duration = 0.0
        self.fps = 0.0
        self.total_frames = 0
        self.width = 0
        self.height = 0
        self._frame_cache = {}

    @staticmethod
    def _frame_to_bgr(frame):
        # PyAV возвращает битый view (data-pointer вне буфера) для AVI rawvideo
        # с отрицательным line_size — Photoshop экспортирует так. cvtColor/copy
        # на таком массиве крашат процесс. format='rgb24' заставляет swscale
        # пересобрать буфер с положительным stride, дальше меняем каналы вручную.
        if any(p.line_size < 0 for p in frame.planes):
            rgb = frame.to_ndarray(format='rgb24')
            return np.ascontiguousarray(rgb[:, :, ::-1])
        return frame.to_ndarray(format='bgr24')

    def load_video(self, path):
        if self.container or self.is_sequence:
            self.close()
        
        self.file_path = path
        if os.path.splitext(path)[1].lower() == ".png":
            return self._load_png_sequence(path)

        try:
            self.is_sequence = False
            self.image_sequence = []
            self.container = av.open(path, metadata_errors='ignore')
            self.stream = self.container.streams.video[0]
            self.stream.thread_type = 'AUTO'
            
            self.width = self.stream.width
            self.height = self.stream.height
            
            if self.stream.duration:
                self.duration = float(self.stream.duration * self.stream.time_base)
            else:
                self.duration = 0.0
                
            self.fps = float(self.stream.average_rate)
            
            if self.stream.frames > 0:
                self.total_frames = self.stream.frames
            else:
                self.total_frames = int(self.duration * self.fps)
                
            self._frame_cache = {}
            return True
        except Exception as e:
            print(f"Error loading video: {e}")
            return False

    def load_image_list(self, paths, is_image_list=True):
        if self.container or self.is_sequence:
            self.close()

        cleaned = []
        for path in paths:
            if path and os.path.isfile(path) and path.lower().endswith(".png"):
                cleaned.append(path)

        if not cleaned:
            return False

        first = read_image(cleaned[0])
        if first is None:
            return False

        self.container = None
        self.stream = None
        self.is_sequence = True
        self.is_image_list = is_image_list
        self.image_sequence = cleaned
        self.file_path = cleaned[0]
        self._frame_cache = {}
        self.height, self.width = first.shape[:2]
        self.total_frames = len(self.image_sequence)
        self.fps = 30.0
        self.duration = self.total_frames / self.fps
        return True
    
    def _load_png_sequence(self, path):
        folder = os.path.dirname(path) or "."
        name = os.path.basename(path)
        match = re.match(r"^(.*?)(\d+)(\.[^.]+)$", name)

        sequence = []
        if match:
            prefix, _, suffix = match.groups()
            for fname in os.listdir(folder):
                m = re.match(rf"^{re.escape(prefix)}(\d+){re.escape(suffix)}$", fname, re.IGNORECASE)
                if m:
                    sequence.append((int(m.group(1)), os.path.join(folder, fname)))
            sequence.sort(key=lambda x: x[0])
            self.image_sequence = [p for _, p in sequence]
        else:
            self.image_sequence = sorted(
                [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".png")]
            )

        if not self.image_sequence:
            return False

        first = read_image(self.image_sequence[0])
        if first is None:
            return False

        self.container = None
        self.stream = None
        self.is_sequence = True
        self.is_image_list = False
        self._frame_cache = {}
        self.height, self.width = first.shape[:2]
        self.total_frames = len(self.image_sequence)
        self.fps = 30.0
        self.duration = self.total_frames / self.fps
        return True

    def get_frame(self, frame_index):
        if self.is_sequence:
            if frame_index < 0: frame_index = 0
            if self.total_frames > 0 and frame_index >= self.total_frames: frame_index = self.total_frames - 1
            if frame_index in self._frame_cache:
                return self._frame_cache[frame_index]
            img = read_image(self.image_sequence[frame_index])
            if img is not None:
                self._frame_cache[frame_index] = img
            return img

        if not self.container: return None
        if frame_index < 0: frame_index = 0
        if self.total_frames > 0 and frame_index >= self.total_frames: frame_index = self.total_frames - 1
        
        if frame_index in self._frame_cache:
            return self._frame_cache[frame_index]

        # Seek logic
        try:
            target_pts = int(frame_index / self.fps / self.stream.time_base)
            self.container.seek(target_pts, stream=self.stream, any_frame=False, backward=True)

            best_img = None
            best_index = -1

            for packet in self.container.demux(self.stream):
                if packet.dts is None:
                    continue
                for frame in packet.decode():
                    current_pts = frame.pts
                    if current_pts is None:
                        continue

                    current_time = current_pts * self.stream.time_base
                    current_index = int(round(current_time * self.fps))

                    img = self._frame_to_bgr(frame)
                    self._frame_cache[current_index] = img

                    if current_index == frame_index:
                        return img

                    # Track the closest frame we've seen so far
                    if best_img is None or abs(current_index - frame_index) < abs(best_index - frame_index):
                        best_img = img
                        best_index = current_index

                    if current_index > frame_index + 20:
                        break

            # Return closest decoded frame if exact match wasn't found
            if best_img is not None and abs(best_index - frame_index) <= 2:
                self._frame_cache[frame_index] = best_img
                return best_img

        except Exception as e:
            print(f"Seek error: {e}")

        # Fallback: sequential decode from start for stubborn frames
        try:
            self.container.seek(0, stream=self.stream)
            for packet in self.container.demux(self.stream):
                for frame in packet.decode():
                    if frame.pts is None:
                        continue
                    current_time = frame.pts * self.stream.time_base
                    current_index = int(round(current_time * self.fps))
                    img = self._frame_to_bgr(frame)
                    self._frame_cache[current_index] = img
                    if current_index >= frame_index:
                        self._frame_cache[frame_index] = img
                        return img
        except Exception as e:
            print(f"Sequential decode error: {e}")

        return None
        
    def get_metadata(self):
        source_type = "video"
        if self.is_sequence:
            source_type = "image_list" if self.is_image_list else "png_sequence"
        return {
            "filename": os.path.basename(self.file_path),
            "source_type": source_type,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "fps": self.fps,
            "total_frames": self.total_frames
        }

    def close(self):
        if self.container:
            self.container.close()
        self.container = None
        self.stream = None
        self.is_sequence = False
        self.is_image_list = False
        self.image_sequence = []
        self._frame_cache = {}

class ImageProcessor:
    @staticmethod
    def to_bgr(image):
        if image is None:
            return None
        if len(image.shape) == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        return image

    @staticmethod
    def source_to_rgba(image):
        if image is None:
            return None
        if len(image.shape) == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2RGBA)
        if image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGBA)

    @staticmethod
    def composite_on_background(image_rgba, bg_color):
        if image_rgba is None:
            return None

        if len(image_rgba.shape) < 3 or image_rgba.shape[2] != 4:
            return image_rgba

        r, g, b, a = cv2.split(image_rgba)
        alpha = a.astype(np.float32) / 255.0
        bg = np.array(bg_color, dtype=np.float32)
        fg_rgb = cv2.merge([r, g, b]).astype(np.float32)
        bg_rgb = np.full_like(fg_rgb, bg)
        out_rgb = (fg_rgb * alpha[:, :, np.newaxis] + bg_rgb * (1 - alpha[:, :, np.newaxis])).astype(np.uint8)
        return out_rgb

    @staticmethod
    def transform_rgba(image_rgba, scale=1.0, offset=(0.0, 0.0)):
        if image_rgba is None:
            return None
        if len(image_rgba.shape) < 3 or image_rgba.shape[2] != 4:
            return image_rgba

        h, w = image_rgba.shape[:2]
        tx, ty = offset
        s = max(0.05, float(scale))
        cx = (w - 1) * 0.5
        cy = (h - 1) * 0.5

        matrix = np.array(
            [
                [s, 0.0, tx + cx * (1.0 - s)],
                [0.0, s, ty + cy * (1.0 - s)],
            ],
            dtype=np.float32,
        )

        return cv2.warpAffine(
            image_rgba,
            matrix,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0, 0),
        )

    @staticmethod
    def transform_content(image, offset=(0.0, 0.0), scale=(1.0, 1.0), border=(0, 0, 0, 0)):
        """Transform секвенции: сдвиг и масштаб содержимого кадра вокруг центра, холст тот же.

        offset — в пикселях исходника, scale — отдельно по X и Y. Вылезшее за холст обрезается,
        освободившееся место заливается border (по умолчанию нули — в RGBA прозрачное).
        """
        if image is None:
            return None
        tx, ty = float(offset[0]), float(offset[1])
        sx, sy = max(0.01, float(scale[0])), max(0.01, float(scale[1]))
        if tx == 0.0 and ty == 0.0 and sx == 1.0 and sy == 1.0:
            return image

        h, w = image.shape[:2]
        cx = (w - 1) * 0.5
        cy = (h - 1) * 0.5

        if sx < 1.0 and sy < 1.0:
            # Сильное уменьшение через warpAffine даёт лесенку — ужимаем INTER_AREA, потом только сдвиг
            rw = max(1, int(round(w * sx)))
            rh = max(1, int(round(h * sy)))
            image = cv2.resize(image, (rw, rh), interpolation=cv2.INTER_AREA)
            matrix = np.array(
                [
                    [1.0, 0.0, cx + tx - (rw - 1) * 0.5],
                    [0.0, 1.0, cy + ty - (rh - 1) * 0.5],
                ],
                dtype=np.float32,
            )
        else:
            matrix = np.array(
                [
                    [sx, 0.0, tx + cx * (1.0 - sx)],
                    [0.0, sy, ty + cy * (1.0 - sy)],
                ],
                dtype=np.float32,
            )

        return cv2.warpAffine(
            image,
            matrix,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=tuple(border),
        )

    @staticmethod
    def apply_chromakey(image, key_color, gain, soften, sat_thresh=0.15, val_thresh=0.15, clip=0.0):
        """
        clip: float -1.0 to 1.0. 
              Controls Gamma (Midtones).
              < 0 (Left): Shift towards Black/Transparent (Gamma > 1)
              > 0 (Right): Shift towards White/Opaque (Gamma < 1)
        """
        if image is None: return None
        
        key_color_bgr = np.uint8([[[key_color[2], key_color[1], key_color[0]]]])
        key_color_hsv = cv2.cvtColor(key_color_bgr, cv2.COLOR_BGR2HSV)[0][0]
        
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        
        target_h = int(key_color_hsv[0])
        
        # Hue Distance
        diff_h = np.abs(h.astype(np.int16) - target_h)
        diff_h = np.minimum(diff_h, 180 - diff_h) 
        
        s_float = s.astype(np.float32) / 255.0
        v_float = v.astype(np.float32) / 255.0
        
        # Hue Tolerance
        hue_tolerance = 5 + (gain * 60) 
        
        dist_h = diff_h.astype(np.float32)
        
        lower_h = hue_tolerance
        upper_h = hue_tolerance + 1.0 # Sharp transition
        
        alpha_h = np.clip((dist_h - lower_h) / (upper_h - lower_h), 0, 1)
        
        # Protections
        sat_prot_lower = sat_thresh
        sat_prot_upper = sat_thresh + 0.1 
        sat_protection = 1.0 - np.clip((s_float - sat_prot_lower) / (sat_prot_upper - sat_prot_lower), 0, 1)
        
        val_prot_lower = val_thresh
        val_prot_upper = val_thresh + 0.1
        val_protection = 1.0 - np.clip((v_float - val_prot_lower) / (val_prot_upper - val_prot_lower), 0, 1)
        
        max_protection = np.maximum(sat_protection, val_protection)
        
        # Combine
        alpha_final = np.maximum(alpha_h, max_protection)
        
        # Apply Soften as Blur (First, to create gradient for Levels/Clip)
        if soften > 0:
            # Increased kernel multiplier to ensure gradient exists even at low soften
            k_size = int(soften * 30) 
            if k_size % 2 == 0: k_size += 1
            if k_size >= 3:
                alpha_final = cv2.GaussianBlur(alpha_final, (k_size, k_size), 0)

        # Apply Clip (Midtones / Gamma) AFTER Blur
        if clip != 0.0:
            # clip is -1.0 to 1.0
            # Behaves like Photoshop Levels Midtone Slider
            # Left (-1.0) -> Brightens/Expands Mask (Gamma < 1)
            # Right (1.0) -> Darkens/Contracts Mask (Gamma > 1)
            
            # Using base 10 for stronger reaction
            gamma = float(np.power(10.0, clip))
            
            alpha_final = np.power(alpha_final, gamma)
        
        b, g, r = cv2.split(image)
        alpha_channel = (alpha_final * 255).astype(np.uint8)
        
        return cv2.merge([r, g, b, alpha_channel])

    @staticmethod
    def refine_alpha(image_rgba, erode_px=0.0, blur_px=0.0):
        """
        Подрезает край альфа-канала и размывает его.
        erode_px: 0.0..5.0 — сколько пикселей "съесть" с края маски (поддерживает дробные значения).
        blur_px:  0.0..N  — радиус гауссова размытия альфы.
        Работает с RGBA (как у всех методов sprite-пайплайна).
        """
        if image_rgba is None:
            return None
        if len(image_rgba.shape) < 3 or image_rgba.shape[2] != 4:
            return image_rgba
        if erode_px <= 0.001 and blur_px <= 0.001:
            return image_rgba

        r, g, b, a = cv2.split(image_rgba)

        if erode_px > 0.001:
            # distance transform от границы прозрачности — даёт суб-пиксельную точность
            mask = (a > 0).astype(np.uint8) * 255
            dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
            # пиксели в полосе [erode_px-1 .. erode_px] плавно уходят в 0 — антиалиас на новой границе
            falloff = np.clip(dist - (erode_px - 1.0), 0.0, 1.0)
            a_float = a.astype(np.float32) / 255.0
            a_float = a_float * falloff
            a = np.clip(a_float * 255.0, 0, 255).astype(np.uint8)

        if blur_px > 0.001:
            k = int(round(blur_px) * 2) + 1
            if k < 3:
                k = 3
            a = cv2.GaussianBlur(a, (k, k), float(blur_px))

        return cv2.merge([r, g, b, a])

    @staticmethod
    def resize_frame(frame, target_size, fit_contain=False, crop_target=None):
        if frame is None: return None
        
        h, w = frame.shape[:2]
        
        # 1. Crop to aspect ratio
        if crop_target:
            tw, th = crop_target
            img_aspect = w / h
            target_aspect = tw / th
            
            if abs(img_aspect - target_aspect) > 0.001:
                if img_aspect > target_aspect:
                    # Too wide -> crop width
                    ch = h
                    cw = int(h * target_aspect)
                else:
                    # Too tall -> crop height
                    cw = w
                    ch = int(w / target_aspect)
                    
                cx = (w - cw) // 2
                cy = (h - ch) // 2
                
                frame = frame[cy:cy+ch, cx:cx+cw]
        
        # 2. Scale
        final_w, final_h = target_size
        resized = cv2.resize(frame, (final_w, final_h), interpolation=cv2.INTER_AREA)
        
        if len(resized.shape) == 2:
             pass 
        elif resized.shape[2] == 3:
             resized = cv2.cvtColor(resized, cv2.COLOR_BGR2BGRA)
             
        return resized

class AtlasBuilder:
    @staticmethod
    def create_atlas(frames, columns, rows, frame_width, frame_height):
        atlas_width = columns * frame_width
        atlas_height = rows * frame_height
        
        atlas = np.zeros((atlas_height, atlas_width, 4), dtype=np.uint8)
        
        for idx, frame in enumerate(frames):
            if idx >= columns * rows: break
            
            col = idx % columns
            row = idx // columns
            
            x = col * frame_width
            y = row * frame_height
            
            h, w = frame.shape[:2]
            h = min(h, frame_height)
            w = min(w, frame_width)
            
            atlas[y:y+h, x:x+w] = frame[:h, :w]
            
        return atlas
