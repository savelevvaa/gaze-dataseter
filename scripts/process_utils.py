# Обнаружение лица и глаз, оценка углов головы и подготовка изображений
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


import mediapipe as mp


# Углы головы и матрицы, полученные при оценке положения
@dataclass
class HeadPose:
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    rotation_vector: np.ndarray
    translation_vector: np.ndarray
    rotation_matrix: np.ndarray
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray


# Прямоугольники лица и глаз и точки лица в пикселях кадра
@dataclass
class Detections:
    face_bbox_xywh: Tuple[int, int, int, int]
    left_eye_bbox_xywh: Tuple[int, int, int, int]
    right_eye_bbox_xywh: Tuple[int, int, int, int]
    landmarks_px: np.ndarray


# Индексы точек вокруг глаз для устойчивых прямоугольников
LEFT_EYE_IDX: List[int] = [33, 133, 159, 145, 153, 154, 155, 173, 157, 158]
RIGHT_EYE_IDX: List[int] = [362, 263, 386, 374, 380, 381, 382, 373, 390, 249]


# Соответствия носа, подбородка, углов глаз и рта для оценки поворота
PNP_LANDMARK_IDX: List[int] = [1, 152, 33, 263, 61, 291]
# Приближённая модель лица в миллиметрах; порядок совпадает с индексами выше
MODEL_POINTS_3D: np.ndarray = np.array([
    [0.0, 0.0, 0.0],
    [0.0, -63.6, -12.5],
    [-43.3, 32.7, -26.0],
    [43.3, 32.7, -26.0],
    [-28.9, -28.9, -24.1],
    [28.9, -28.9, -24.1],
], dtype=np.float32)


# Получает углы для последовательности поворотов вокруг осей Z, Y, X
def _rotation_matrix_to_euler_angles(R: np.ndarray) -> Tuple[float, float, float]:
    sy = math.sqrt(R[0, 0] * R[0, 0] + R[1, 0] * R[1, 0])
    singular = sy < 1e-6

    # Обычный расчёт углов; при особом положении матрицы используем отдельную ветку
    if not singular:
        yaw = math.degrees(math.atan2(R[1, 0], R[0, 0]))
        pitch = math.degrees(math.atan2(-R[2, 0], sy))
        roll = math.degrees(math.atan2(R[2, 1], R[2, 2]))
    else:
        yaw = math.degrees(math.atan2(-R[1, 2], R[1, 1]))
        pitch = math.degrees(math.atan2(-R[2, 0], sy))
        roll = 0.0

    return yaw, pitch, roll


# Обработка одного лица через MediaPipe и подготовка признаков для записи
class FaceProcessor:

    def __init__(
        self,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        refine_landmarks: bool = True,
    ) -> None:
        self._mp_face_mesh = mp.solutions.face_mesh
        # Обрабатываем одно лицо, координаты отслеживаются между кадрами
        self._face_mesh = self._mp_face_mesh.FaceMesh(
            static_image_mode=False,
            refine_landmarks=refine_landmarks,
            max_num_faces=1,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )

    # Освобождает обработчик точек лица
    def close(self) -> None:
        self._face_mesh.close()

    # Переводит координаты точек лица в пиксели изображения
    @staticmethod
    def _landmarks_to_pixels(landmarks, width: int, height: int) -> np.ndarray:
        points = []
        # MediaPipe возвращает доли размера кадра, здесь переводим их в пиксели
        for lm in landmarks:
            x_px = int(lm.x * width)
            y_px = int(lm.y * height)
            points.append([x_px, y_px])
        return np.array(points, dtype=np.int32)

    # Строит прямоугольник с отступом и ограничивает его границами кадра
    @staticmethod
    def _bbox_from_points(points: np.ndarray, padding: float, image_shape: Tuple[int, int, int]) -> Tuple[int, int, int, int]:
        h, w = image_shape[:2]
        # Обрезаем границы области, чтобы вырезка не выходила за изображение
        x_min = int(np.clip(points[:, 0].min() - padding, 0, w - 1))
        y_min = int(np.clip(points[:, 1].min() - padding, 0, h - 1))
        x_max = int(np.clip(points[:, 0].max() + padding, 0, w - 1))
        y_max = int(np.clip(points[:, 1].max() + padding, 0, h - 1))
        return x_min, y_min, max(1, x_max - x_min), max(1, y_max - y_min)

    # Находит лицо и области глаз; возвращает пустой результат при отсутствии лица
    def detect(self, image_bgr: np.ndarray) -> Optional[Detections]:
        height, width = image_bgr.shape[:2]
        # Для MediaPipe переводим BGR из OpenCV в RGB
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        results = self._face_mesh.process(image_rgb)
        if not results.multi_face_landmarks:
            return None

        face_landmarks = results.multi_face_landmarks[0]
        landmarks_px = self._landmarks_to_pixels(face_landmarks.landmark, width, height)


        # Лицо берём по всем точкам, глаза — по соответствующим группам индексов
        face_bbox = self._bbox_from_points(landmarks_px, padding=20, image_shape=image_bgr.shape)


        left_eye_pts = landmarks_px[LEFT_EYE_IDX]
        right_eye_pts = landmarks_px[RIGHT_EYE_IDX]
        left_eye_bbox = self._bbox_from_points(left_eye_pts, padding=10, image_shape=image_bgr.shape)
        right_eye_bbox = self._bbox_from_points(right_eye_pts, padding=10, image_shape=image_bgr.shape)

        return Detections(
            face_bbox_xywh=face_bbox,
            left_eye_bbox_xywh=left_eye_bbox,
            right_eye_bbox_xywh=right_eye_bbox,
            landmarks_px=landmarks_px,
        )

    # Вырезает область и приводит её к квадратному размеру
    @staticmethod
    def crop_and_resize(image_bgr: np.ndarray, bbox_xywh: Tuple[int, int, int, int], out_size: int) -> np.ndarray:
        x, y, w, h = bbox_xywh
        crop = image_bgr[y:y + h, x:x + w]
        # Пустая область заменяется чёрным изображением нужного размера
        if crop.size == 0:

            return np.zeros((out_size, out_size, 3), dtype=np.uint8)
        return cv2.resize(crop, (out_size, out_size), interpolation=cv2.INTER_AREA)

    # Оценивает поворот по шести точкам лица и приближённой модели камеры
    @staticmethod
    def estimate_head_pose(
        landmarks_px: np.ndarray,
        image_size: Tuple[int, int],
    ) -> Optional[HeadPose]:
        width, height = image_size

        try:
            points_2d = landmarks_px[PNP_LANDMARK_IDX].astype(np.float32)
        except Exception:
            return None


        # Приближённая камера: фокус равен ширине кадра, центр — середине изображения
        focal_length = width
        center = (width / 2.0, height / 2.0)
        camera_matrix = np.array(
            [[focal_length, 0, center[0]], [0, focal_length, center[1]], [0, 0, 1]],
            dtype=np.float64,
        )
        # Искажения объектива не калибруются и принимаются нулевыми
        dist_coeffs = np.zeros((4, 1), dtype=np.float64)

        # Сопоставляем шесть точек кадра с приближённой трёхмерной моделью лица
        success, rotation_vector, translation_vector = cv2.solvePnP(
            MODEL_POINTS_3D,
            points_2d,
            camera_matrix,
            dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        if not success:
            return None

        # Вектор поворота переводим в матрицу, затем получаем три угла
        rotation_matrix, _ = cv2.Rodrigues(rotation_vector)
        yaw, pitch, roll = _rotation_matrix_to_euler_angles(rotation_matrix)

        return HeadPose(
            yaw_deg=float(yaw),
            pitch_deg=float(pitch),
            roll_deg=float(roll),
            rotation_vector=rotation_vector,
            translation_vector=translation_vector,
            rotation_matrix=rotation_matrix,
            camera_matrix=camera_matrix,
            dist_coeffs=dist_coeffs,
        )


# Переводит координаты цели в диапазоны от 0 до 1 и от −1 до 1
def normalize_screen_coords(
    x_px: float,
    y_px: float,
    screen_w: int,
    screen_h: int,
) -> Tuple[float, float, float, float]:
    x01 = np.clip(x_px / max(1, screen_w - 1), 0.0, 1.0)
    y01 = np.clip(y_px / max(1, screen_h - 1), 0.0, 1.0)
    # В центрированном диапазоне середина окна имеет координаты 0, 0
    x_centered = x01 * 2.0 - 1.0
    y_centered = y01 * 2.0 - 1.0
    return float(x01), float(y01), float(x_centered), float(y_centered)


# Рисует области лица и глаз и контрольные точки на копии кадра
def draw_landmarks_preview(
    frame_bgr: np.ndarray,
    detections: Optional[Detections],
) -> np.ndarray:
    vis = frame_bgr.copy()
    if detections is None:
        return vis

    x, y, w, h = detections.face_bbox_xywh
    cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 0), 2)

    lx, ly, lw, lh = detections.left_eye_bbox_xywh
    rx, ry, rw, rh = detections.right_eye_bbox_xywh
    cv2.rectangle(vis, (lx, ly), (lx + lw, ly + lh), (255, 0, 0), 2)
    cv2.rectangle(vis, (rx, ry), (rx + rw, ry + rh), (0, 0, 255), 2)


    for idx in LEFT_EYE_IDX + RIGHT_EYE_IDX:
        px, py = detections.landmarks_px[idx]
        cv2.circle(vis, (int(px), int(py)), 1, (0, 255, 255), -1)

    return vis


