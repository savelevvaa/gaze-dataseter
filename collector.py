# Предъявление целей, обработка камеры и запись сессии
import csv
import json
import math
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from screeninfo import get_monitors
from configuration import IS_WINDOWS, collection_config, save_config, storage_paths
from stimulus_points import generate_random_points as _generate_random_points


# Подключение общих функций обработки из папки скриптов
_scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from process_utils import (
    FaceProcessor,
    normalize_screen_coords,
    draw_landmarks_preview,
)


# Создаёт папку, если её ещё нет
def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# Выбирает следующий номер сессии без перезаписи существующих папок
def _create_session_dir(participant_dir: Path) -> Path:
    participant_dir.mkdir(parents=True, exist_ok=True)
    indices = [int(path.name[1:]) for path in participant_dir.iterdir()
               if path.name.startswith("s") and path.name[1:].isdigit()]
    index = max(indices, default=0) + 1
    while True:
        session_dir = participant_dir / f"s{index}"
        try:
            session_dir.mkdir()
            return session_dir
        # Если номер успел занять другой запуск, пробуем следующий
        except FileExistsError:
            index += 1


# Определяет строку и столбец зоны для координат цели
def _get_zone_index(
    x: int, y: int, w: int, h: int, zones_rows: int, zones_cols: int
) -> Tuple[int, int]:
    col = min(zones_cols - 1, max(0, int(x / max(1, w) * zones_cols)))
    row = min(zones_rows - 1, max(0, int(y / max(1, h) * zones_rows)))
    return row, col


# Рисует цель; красная кайма обозначает интервал записи
def _draw_stimulus(
    frame: np.ndarray, point_xy: Tuple[int, int], capturing: bool
) -> np.ndarray:
    vis = np.zeros_like(frame)
    r = 10
    cv2.circle(vis, point_xy, r, (255, 255, 255), -1, lineType=cv2.LINE_AA)
    color = (0, 0, 255) if capturing else (255, 255, 255)
    cv2.circle(vis, point_xy, r + 6, color, 2, lineType=cv2.LINE_AA)
    return vis


# Получает размеры монитора для окна предъявления
def _get_primary_screen_size() -> Tuple[int, int]:
    m = get_monitors()[0]
    return int(m.width), int(m.height)


# Рисует частоту обработки и число оставшихся целей
def _draw_status(canvas: np.ndarray, fps: float, targets_left: int) -> None:
    h, w = canvas.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.5
    thick = 1
    margin = 10
    t1 = f"FPS: {fps:.1f}"
    t2 = f"targets left: {max(0, int(targets_left))}"
    (tw1, th1), _ = cv2.getTextSize(t1, font, scale, thick)
    (tw2, th2), _ = cv2.getTextSize(t2, font, scale, thick)
    cv2.putText(canvas, t1, (max(margin, w - margin - tw1), margin + th1),
                font, scale, (0, 255, 0), thick, cv2.LINE_AA)
    cv2.putText(canvas, t2, (max(margin, w - margin - tw2), margin + th1 + th2 + 6),
                font, scale, (0, 255, 0), thick, cv2.LINE_AA)


# Читает клавишу и завершает сбор по Q или Esc
def _read_key(delay: int = 1) -> int:
    key = cv2.waitKey(delay) & 0xFF
    if key in (27, ord('q'), ord('Q')):
        raise KeyboardInterrupt
    return key


# Хранит запрос паузы до завершения текущей цели
class CollectionControls:
    def __init__(self):
        self.pause_requested = False

    # Повторный пробел не отменяет уже запрошенную паузу
    def poll(self):
        if _read_key() == ord(' '):
            self.pause_requested = True


# Рисует число обратного отсчёта
def _draw_countdown_digit(canvas, remaining, center=None):
    h, w = canvas.shape[:2]
    center = center or (w // 2, h // 2)
    radius = max(40, min(w, h) // 10)
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = radius / 60.0 * 2.0
    digit = str(remaining)
    (tw, th), _ = cv2.getTextSize(digit, font, scale, 5)
    cv2.circle(canvas, center, radius, (255, 255, 255), 8, cv2.LINE_AA)
    cv2.putText(canvas, digit, (center[0] - tw // 2, center[1] + th // 2),
                font, scale, (255, 255, 255), 5, cv2.LINE_AA)


# Показывает статус паузы, сохраняя следующую цель видимой
def _draw_pause_screen(width, height, point, fps, targets_left, remaining_s):
    canvas = _draw_stimulus(np.zeros((height, width, 3), dtype=np.uint8), point, False)
    seconds = max(0, math.ceil(remaining_s))
    lines = [
        ("PAUSED", 0.8, (0, 255, 0)),
        (f"FPS: {fps:.1f}  |  Targets left: {targets_left}", 0.55, (230, 230, 230)),
        (f"Remaining: ~{seconds // 60:02d}:{seconds % 60:02d}", 0.55, (230, 230, 230)),
        ("SPACE - resume (3s)", 0.5, (180, 180, 180)),
        ("Q - finish", 0.5, (180, 180, 180)),
    ]
    font = cv2.FONT_HERSHEY_SIMPLEX
    fit = min(1.0, (width - 32) / max(cv2.getTextSize(text, font, scale, 1)[0][0]
                                     for text, scale, _ in lines))
    line_height = max(22, int(height * 0.035))
    block_height = len(lines) * line_height
    top = (height - block_height) // 2
    block_width = max(cv2.getTextSize(text, font, scale * fit, 1)[0][0]
                      for text, scale, _ in lines)

    # Сдвигаем текст остановки, если следующая цель оказалась под ним
    if (abs(point[0] - width // 2) < block_width / 2 + 24
            and top - 24 < point[1] < top + block_height + 24):
        choices = [point[1] - 28 - block_height, point[1] + 28]
        choices = [value for value in choices if 16 <= value <= height - block_height - 16]
        if choices:
            top = min(choices, key=lambda value: abs(value - top))
    for index, (text, scale, color) in enumerate(lines):
        (tw, th), _ = cv2.getTextSize(text, font, scale * fit, 1)
        y = top + index * line_height + (line_height + th) // 2
        cv2.putText(canvas, text, ((width - tw) // 2, y), font,
                    scale * fit, color, 1, cv2.LINE_AA)

    cv2.circle(canvas, point, 10, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.circle(canvas, point, 16, (255, 255, 255), 2, cv2.LINE_AA)
    return canvas


# Рисует отсчёт возобновления, не перекрывая цель
def _draw_resume_screen(width, height, point, remaining):
    canvas = _draw_stimulus(np.zeros((height, width, 3), dtype=np.uint8), point, False)
    center = (width // 2, height // 2)
    radius = max(40, min(width, height) // 10)
    # Для отсчёта выбираем место подальше от следующей цели
    if math.dist(center, point) < radius + 28:
        candidates = [(width // 2, radius + 30), (width // 2, height - radius - 30),
                      (radius + 30, height // 2), (width - radius - 30, height // 2)]
        center = max(candidates, key=lambda candidate: math.dist(candidate, point))
    _draw_countdown_digit(canvas, remaining, center)
    font = cv2.FONT_HERSHEY_SIMPLEX
    for text, y in (("RESUMING", 28), ("Q - finish", height - 20)):
        (tw, _), _ = cv2.getTextSize(text, font, 0.5, 1)
        cv2.putText(canvas, text, ((width - tw) // 2, y), font, 0.5,
                    (200, 200, 200), 1, cv2.LINE_AA)
    cv2.circle(canvas, point, 10, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.circle(canvas, point, 16, (255, 255, 255), 2, cv2.LINE_AA)
    return canvas


# Ждёт пробела и выдерживает три секунды перед следующим интервалом
def _wait_for_resume(window, point, width, height, fps, targets_left,
                     dwell_ms, inter_point_ms, controls):
    controls.pause_requested = False
    remaining_s = (targets_left * dwell_ms + max(0, targets_left - 1) * inter_point_ms) / 1000 + 3
    paused = _draw_pause_screen(width, height, point, fps, targets_left, remaining_s)
    # Во время остановки ждём пробел, Q или Esc
    while True:
        cv2.imshow(window, paused)
        if _read_key(20) == ord(' '):
            break

    # На паузе и при отсчёте возобновления обработка лица и запись не выполняются
    deadline = time.monotonic() + 3.0
    while True:
        remaining = math.ceil(deadline - time.monotonic())
        if remaining <= 0:
            break
        cv2.imshow(window, _draw_resume_screen(width, height, point, remaining))
        _read_key(20)


# Читает камеру в отдельном потоке и хранит только последний кадр
class CameraReader:
    def __init__(self, camera_index: int, width: int = 1280, height: int = 720,
                 flip_horizontal: bool = True, use_directshow: bool = True) -> None:
        # DirectShow выбирается только на Windows; иначе способ подключения определяет OpenCV
        backend = cv2.CAP_DSHOW if IS_WINDOWS and use_directshow else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(camera_index, backend)
        if not self.cap.isOpened():
            raise RuntimeError(f"Не удалось открыть камеру index={camera_index}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        try:
            self.cap.set(cv2.CAP_PROP_FPS, 60)
        except Exception:
            pass
        self.flip_horizontal = flip_horizontal
        self.lock = threading.Lock()
        # Последний кадр и его номер читаются и обновляются под одной блокировкой
        self.latest_frame: Optional[np.ndarray] = None
        self.latest_seq = 0
        self.stopped = False
        self.thread = threading.Thread(target=self._loop, daemon=True)

    # Запускает поток чтения камеры
    def start(self) -> None:
        self.thread.start()

    # Обновляет последний кадр и его порядковый номер под блокировкой
    def _loop(self) -> None:
        # Поток постоянно заменяет кадр; очередь старых изображений не накапливается
        while not self.stopped:
            ret, frame = self.cap.read()
            if not ret or frame is None:
                time.sleep(0.001)
                continue
            if self.flip_horizontal:
                # Изображения сохраняются зеркальными, как в предварительном просмотре
                frame = cv2.flip(frame, 1)
            with self.lock:
                self.latest_frame = frame
                self.latest_seq += 1

    # Возвращает копию последнего кадра и его номер
    def get_frame(self) -> Tuple[Optional[np.ndarray], int]:
        with self.lock:
            if self.latest_frame is None:
                return None, self.latest_seq
            # Возвращаем копию, чтобы обработка не меняла кадр в потоке камеры
            return self.latest_frame.copy(), self.latest_seq

    # Читает разрешение камеры; использует запрошенный размер при нулевых значениях
    def frame_size(self) -> Tuple[int, int]:
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
        return w, h

    # Останавливает поток и освобождает камеру
    def stop(self) -> None:
        self.stopped = True
        try:
            if self.thread.is_alive():
                self.thread.join(timeout=0.5)
        except Exception:
            pass
        try:
            self.cap.release()
        except Exception:
            pass


# Выполняет подготовку, предъявление целей и запись сессии
def run_collection(config: dict) -> None:
    config = collection_config(config)
    personal = config["personal"]
    person_id = personal["participant"]
    camera_index = int(personal.get("camera_index", 0))

    # Параметры предъявления, сохранения изображений и обработки лица
    total_points = int(config["stimulus"].get("total_points", 400))
    dwell_ms = config["stimulus"].get("dwell_ms", 500)
    capture_start_ms = config["stimulus"].get("capture_start_ms", 300)
    inter_point_ms = config["stimulus"].get("inter_point_ms", 100)

    margin_ratio = float(config["distribution"].get("margin_ratio", 0.01))

    save_face = config["images"].get("save_face", True)
    save_eyes = config["images"].get("save_eyes", True)
    face_size = config["images"].get("face_size", 224)
    eye_size = config["images"].get("eye_size", 112)
    jpeg_quality = int(config["images"].get("jpeg_quality", 95))

    max_frames_per_fix = int(config["saving"].get("max_frames_per_fixation", 0))
    prepare_countdown_s = int(config["prepare"].get("countdown_seconds", 5))
    zones_rows = int(config["coverage"].get("zones_rows", 3))
    zones_cols = int(config["coverage"].get("zones_cols", 3))
    window_width_percent = float(config["window"].get("width_percent", 0.9))
    min_det_conf = float(config["thresholds"].get("min_detection_confidence", 0.5))

    screen_w, screen_h = _get_primary_screen_size()


    output_root, dataset_dir, person_dir = storage_paths(personal)
    # Каждый запуск получает отдельную папку сессии
    session_dir = _create_session_dir(person_dir)
    session_id = session_dir.name
    # Снимок запуска сохраняется до подключения камеры
    session_settings = {
        "schema_version": 1,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "configuration": config,
        "session": {"dataset_name": dataset_dir.name, "participant": person_id,
                    "session_id": session_id, "directory": str(session_dir)},
        "paths": {"output_root": str(output_root), "dataset_dir": str(dataset_dir),
                  "participant_dir": str(person_dir)},
        "software": {"python": sys.version.split()[0], "opencv": cv2.__version__,
                     "numpy": np.__version__, "mediapipe": version("mediapipe")},
    }
    snapshot_path = session_dir / "session_config.json"

    save_config(session_settings, snapshot_path)
    images_face_dir = os.path.join(session_dir, "face_images")
    images_left_dir = os.path.join(session_dir, "left_eye_images")
    images_right_dir = os.path.join(session_dir, "right_eye_images")

    _ensure_dir(session_dir)
    # Создаём только папки для включённых видов изображений
    if save_face:
        _ensure_dir(images_face_dir)
    if save_eyes:
        _ensure_dir(images_left_dir)
        _ensure_dir(images_right_dir)

    annotations_csv = os.path.join(session_dir, "annotations.csv")


    use_directshow = config.get("camera", {}).get("use_directshow", True)
    cam = CameraReader(camera_index, width=1280, height=720, flip_horizontal=True,
                       use_directshow=use_directshow)
    cam.start()
    time.sleep(0.05)

    # Соотношение сторон окна соответствует разрешению, сообщённому камерой
    cam_w, cam_h = cam.frame_size()
    cam_aspect = cam_w / cam_h
    win_w = int(screen_w * window_width_percent)
    win_h = int(win_w / cam_aspect)

    # Дополняет снимок фактическими размерами камеры и окна
    session_settings["runtime"] = {
        "camera": {"index": camera_index, "requested_resolution": [1280, 720],
                   "reported_resolution": [cam_w, cam_h], "requested_fps": 60,
                   "mirror_horizontal": True,
                   "backend_preference": "DirectShow" if IS_WINDOWS and use_directshow else "Auto"},
        "screen": {"width": screen_w, "height": screen_h},
        "stimulus": {"width": win_w, "height": win_h},
    }
    try:
        save_config(session_settings, snapshot_path)
    except OSError:
        cam.stop()
        raise

    print(f"\nКамера: {cam_w}×{cam_h}")
    print(f"Экран: {screen_w}×{screen_h}")
    print(f"Окно стимула: {win_w}×{win_h}")
    print(f"Датасет: {dataset_dir.name}  Испытуемый: {person_id}  Сессия: {session_id}")
    print(f"Точек: {total_points}  Dwell: {dwell_ms}ms")


    # Окна времени для частоты получения и обработки кадров
    frame_times: deque = deque(maxlen=300)
    processed_times: deque = deque(maxlen=300)
    proc_fps = 0.0

    # Считает число событий в скользящем интервале длиной одну секунду
    def update_fps(dq: deque) -> float:
        now_t = time.monotonic()
        dq.append(now_t)
        while dq and (now_t - dq[0]) > 1.0:
            # Старые отметки времени больше не участвуют в текущем FPS
            dq.popleft()
        return float(len(dq))


    processor = FaceProcessor(
        min_detection_confidence=min_det_conf,
        min_tracking_confidence=min_det_conf,
        refine_landmarks=True,
    )


    STIM = "stimulus"
    cv2.namedWindow(STIM, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(STIM, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
    try:
        cv2.resizeWindow(STIM, win_w, win_h)
        m = get_monitors()[0]
        pos_x = int(m.x + (m.width - win_w) / 2)
        pos_y = int(m.y + (m.height - win_h) / 2)
        cv2.moveWindow(STIM, pos_x, pos_y)
    except Exception:
        pass


    # Подготовительный отсчёт показывает изображение камеры без записи примеров
    if prepare_countdown_s > 0:
        t0 = time.monotonic()
        last_seq = -1
        while True:
            frame, seq = cam.get_frame()
            # До нового кадра продолжаем проверять клавишу завершения
            if frame is None or seq == last_seq:
                if (cv2.waitKey(1) & 0xFF) in (27, ord('q'), ord('Q')):
                    cam.stop()
                    processor.close()
                    cv2.destroyAllWindows()
                    return
                continue
            last_seq = seq
            update_fps(frame_times)
            det = processor.detect(frame)
            if det is not None:
                hp = FaceProcessor.estimate_head_pose(
                    det.landmarks_px, (frame.shape[1], frame.shape[0])
                )
                if hp is not None:
                    proc_fps = update_fps(processed_times)

            elapsed = time.monotonic() - t0
            remaining = int(max(0, math.ceil(prepare_countdown_s - elapsed)))

            canvas = cv2.resize(frame, (win_w, win_h))
            _draw_countdown_digit(canvas, max(0, remaining))
            _draw_status(canvas, proc_fps, total_points)
            cv2.imshow(STIM, canvas)

            if (cv2.waitKey(1) & 0xFF) in (27, ord('q'), ord('Q')):
                cam.stop()
                processor.close()
                cv2.destroyAllWindows()
                return
            if elapsed >= prepare_countdown_s:
                break


    # Полный список целей формируется до предъявления и перемешивается
    points = _generate_random_points(total_points, win_w, win_h, margin_ratio)
    random.shuffle(points)

    # Поля разметки одной записи: изображения, цель, углы, области и идентификаторы
    csv_fieldnames = [
        "image_face", "image_left_eye", "image_right_eye",
        "screen_w", "screen_h",
        "target_x_px", "target_y_px",
        "target_x_norm", "target_y_norm",
        "target_x_centered", "target_y_centered",
        "yaw_deg", "pitch_deg", "roll_deg",
        "face_x", "face_y", "face_w", "face_h",
        "left_eye_x", "left_eye_y", "left_eye_w", "left_eye_h",
        "right_eye_x", "right_eye_y", "right_eye_w", "right_eye_h",
        "camera_matrix", "dist_coeffs",
        "person_id", "session_id", "timestamp", "frame_seq", "fixation_idx",
    ]


    controls = CollectionControls()
    with open(annotations_csv, "w", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=csv_fieldnames)
        writer.writeheader()

        try:
            # Каждая цель получает собственные интервалы ожидания и записи
            for idx, (tx, ty) in enumerate(points):
                # Текущая цель уже закончена; показываем следующую и ждём продолжения
                if controls.pause_requested:
                    f_csv.flush()
                    _wait_for_resume(STIM, (int(tx), int(ty)), win_w, win_h, proc_fps,
                                     total_points - idx, dwell_ms, inter_point_ms, controls)


                # Показывает цель до ожидания нового кадра; таймер начинается после обработки событий окна
                initial = np.zeros((win_h, win_w, 3), dtype=np.uint8)
                initial = _draw_stimulus(initial, (int(tx), int(ty)), False)
                _draw_status(initial, proc_fps, max(0, total_points - (idx + 1)))
                cv2.imshow(STIM, initial)
                controls.poll()

                # Задержка записи и конец показа отсчитываются от одного момента
                point_start = time.monotonic()
                capture_start_t = point_start + capture_start_ms / 1000.0
                point_end_t = point_start + dwell_ms / 1000.0

                # При ограничении кадров моменты сохранения равномерно распределяются внутри интервала
                schedule_times: List[float] = []
                if max_frames_per_fix > 0:
                    window = max(0.0, point_end_t - capture_start_t)
                    if window > 0:
                        for k in range(max_frames_per_fix):
                            schedule_times.append(
                                capture_start_t + (k + 0.5) * (window / max_frames_per_fix)
                            )
                schedule_idx = 0

                # Номера кадров позволяют не обрабатывать одно изображение повторно
                last_proc_seq = -1
                last_saved_seq = -1

                # Обрабатывает новые кадры до окончания времени показа текущей цели
                while True:
                    now = time.monotonic()
                    if now >= point_end_t:
                        break

                    frame, seq = cam.get_frame()
                    # Если камера ещё не дала новый кадр, проверяем управление и ждём дальше
                    if frame is None or seq == last_proc_seq:
                        controls.poll()
                        continue
                    last_proc_seq = seq
                    update_fps(frame_times)

                    is_capturing = now >= capture_start_t
                    stim = np.zeros((win_h, win_w, 3), dtype=np.uint8)
                    stim = _draw_stimulus(stim, (int(tx), int(ty)), is_capturing)
                    _draw_status(stim, proc_fps, max(0, total_points - (idx + 1)))
                    cv2.imshow(STIM, stim)

                    det = processor.detect(frame)
                    # Кадры без обнаруженного лица не попадают в датасет
                    if det is None:
                        controls.poll()
                        continue

                    hp = FaceProcessor.estimate_head_pose(
                        det.landmarks_px, (frame.shape[1], frame.shape[0])
                    )
                    # Для сохранения нужен также результат оценки положения головы
                    if hp is None:
                        controls.poll()
                        continue

                    proc_fps = update_fps(processed_times)

                    # При нулевом лимите сохраняем все подходящие кадры после задержки
                    can_save = is_capturing and (
                        max_frames_per_fix == 0
                        or (schedule_idx < len(schedule_times)
                            and now >= schedule_times[schedule_idx])
                    )
                    if not can_save:
                        controls.poll()
                        continue


                    # В разметке хранится время Unix; интервалы измеряются монотонными часами
                    timestamp_ms = int(time.time() * 1000)
                    image_face_rel = image_left_rel = image_right_rel = ""

                    # Вырезаем лицо из того же кадра, для которого рассчитаны признаки
                    if save_face and seq != last_saved_seq:
                        face_img = FaceProcessor.crop_and_resize(
                            frame, det.face_bbox_xywh, face_size
                        )
                        face_path = os.path.join(
                            images_face_dir, f"face_{idx}_{timestamp_ms}.jpg"
                        )
                        cv2.imwrite(face_path, face_img,
                                    [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                        image_face_rel = os.path.relpath(face_path, session_dir)

                    # Левый и правый глаз сохраняем отдельно
                    if save_eyes and seq != last_saved_seq:
                        left_img = FaceProcessor.crop_and_resize(
                            frame, det.left_eye_bbox_xywh, eye_size
                        )
                        right_img = FaceProcessor.crop_and_resize(
                            frame, det.right_eye_bbox_xywh, eye_size
                        )
                        left_path = os.path.join(
                            images_left_dir, f"left_{idx}_{timestamp_ms}.jpg"
                        )
                        right_path = os.path.join(
                            images_right_dir, f"right_{idx}_{timestamp_ms}.jpg"
                        )
                        cv2.imwrite(left_path, left_img,
                                    [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                        cv2.imwrite(right_path, right_img,
                                    [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                        image_left_rel = os.path.relpath(left_path, session_dir)
                        image_right_rel = os.path.relpath(right_path, session_dir)

                    last_saved_seq = seq

                    # Записываем координаты цели также в нормированных диапазонах
                    x01, y01, xcc, ycc = normalize_screen_coords(tx, ty, win_w, win_h)

                    # Связывает сохранённые изображения с целью и признаками того же кадра
                    writer.writerow({
                        "image_face": image_face_rel,
                        "image_left_eye": image_left_rel,
                        "image_right_eye": image_right_rel,
                        "screen_w": win_w,
                        "screen_h": win_h,
                        "target_x_px": tx,
                        "target_y_px": ty,
                        "target_x_norm": x01,
                        "target_y_norm": y01,
                        "target_x_centered": xcc,
                        "target_y_centered": ycc,
                        "yaw_deg": hp.yaw_deg,
                        "pitch_deg": hp.pitch_deg,
                        "roll_deg": hp.roll_deg,
                        "face_x": det.face_bbox_xywh[0],
                        "face_y": det.face_bbox_xywh[1],
                        "face_w": det.face_bbox_xywh[2],
                        "face_h": det.face_bbox_xywh[3],
                        "left_eye_x": det.left_eye_bbox_xywh[0],
                        "left_eye_y": det.left_eye_bbox_xywh[1],
                        "left_eye_w": det.left_eye_bbox_xywh[2],
                        "left_eye_h": det.left_eye_bbox_xywh[3],
                        "right_eye_x": det.right_eye_bbox_xywh[0],
                        "right_eye_y": det.right_eye_bbox_xywh[1],
                        "right_eye_w": det.right_eye_bbox_xywh[2],
                        "right_eye_h": det.right_eye_bbox_xywh[3],
                        "camera_matrix": ",".join(
                            f"{v:.6f}" for v in hp.camera_matrix.flatten()
                        ),
                        "dist_coeffs": ",".join(
                            f"{v:.6f}" for v in hp.dist_coeffs.flatten()
                        ),
                        "person_id": person_id,
                        "session_id": session_id,
                        "timestamp": timestamp_ms,
                        "frame_seq": seq,
                        "fixation_idx": idx,
                    })

                    # Один сохранённый кадр занимает одно место в расписании
                    if schedule_idx < len(schedule_times):
                        schedule_idx += 1

                    controls.poll()


                # После последней цели или при запросе остановки межцелевая пауза не нужна
                if idx == len(points) - 1 or controls.pause_requested:
                    continue
                pause_dur = max(0.0, inter_point_ms / 1000.0)
                if pause_dur:
                    # В обычной паузе между целями показываем пустой фон
                    blank = np.zeros((win_h, win_w, 3), dtype=np.uint8)
                    _draw_status(blank, proc_fps, max(0, total_points - (idx + 1)))
                    cv2.imshow(STIM, blank)
                    controls.poll()
                    pause_start = time.monotonic()
                    while (time.monotonic() - pause_start) < pause_dur:
                        controls.poll()
                        if controls.pause_requested:
                            break

        except KeyboardInterrupt:
            print("\nСбор прерван пользователем")

        finally:

            try:
                # Отчёт описывает весь план целей, включая не записанные при раннем выходе
                zone_counts = [[0] * zones_cols for _ in range(zones_rows)]
                for px, py in points:
                    r, c = _get_zone_index(px, py, win_w, win_h, zones_rows, zones_cols)
                    zone_counts[r][c] += 1
                report = {
                    "zones_rows": zones_rows,
                    "zones_cols": zones_cols,
                    "counts": zone_counts,
                    "total_fixations": len(points),
                    "algorithm": "uniform_random",
                }
                with open(os.path.join(session_dir, "coverage_report.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
                print(f"Сессия сохранена: {session_dir}")
            except Exception as e:
                print(f"Ошибка при сохранении отчета: {e}")

            # Освобождаем камеру и обработчик лица после выхода из сессии
            cam.stop()
            processor.close()
            cv2.destroyAllWindows()
