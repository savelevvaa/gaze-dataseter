# Настройки приложения, проверка значений и сохранение конфигурации
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
import platform
from pathlib import Path
import tempfile

# Пути приложения и определение операционной системы при запуске
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "app_config.json"
SYSTEM_NAME = platform.system()
IS_WINDOWS = SYSTEM_NAME == "Windows"

# Стандартные параметры новой установки
DEFAULT_CONFIG = {
    "personal": {
        "output_dir": "data",
        "dataset": "test_dataset",
        "participant": "",
        "camera_index": 0,
    },
    "stimulus": {
        "total_points": 400,
        "dwell_ms": 500,
        "capture_start_ms": 300,
        "inter_point_ms": 100,
    },
    "distribution": {
        "margin_ratio": 0.01,
    },
    "images": {
        "save_face": True,
        "save_eyes": True,
        "face_size": 224,
        "eye_size": 112,
        "jpeg_quality": 95,
    },
    "window": {
        "width_percent": 0.9,
    },
    "saving": {
        "max_frames_per_fixation": 0,
    },
    "thresholds": {
        "min_detection_confidence": 0.5,
    },
    "prepare": {
        "countdown_seconds": 5,
    },
    "coverage": {
        "zones_rows": 3,
        "zones_cols": 3,
    },
}
if IS_WINDOWS:
    DEFAULT_CONFIG["camera"] = {"use_directshow": True}

# Рассчитывает длительность сессии и текст временных параметров
def protocol_summary(config):
    stimulus = config["stimulus"]
    count = stimulus["total_points"]
    # В расчёте времени нет паузы после последней цели
    duration = (count * stimulus["dwell_ms"] + max(0, count - 1) * stimulus["inter_point_ms"]) / 1000
    duration += config["prepare"]["countdown_seconds"]
    seconds = math.ceil(duration)
    capture = stimulus["dwell_ms"] - stimulus["capture_start_ms"]
    return (f"    Время до записи: {stimulus['capture_start_ms']} мс\n    Время записи: {capture} мс\n"
            f"Время между целями: {stimulus['inter_point_ms']} мс\nРасчетная продолжительность: ≈ {seconds // 60} мин {seconds % 60:02d} с")


# Описание поля формы: тип, границы значений и масштаб единиц
@dataclass(frozen=True)
class Field:
    section: str
    key: str
    label: str
    kind: type
    minimum: float = 0
    maximum: float = 1000000
    hint: str = ""
    scale: int = 1

    # Проверяет введённое значение и переводит его в единицы сборщика
    def parse(self, value):
        # Галочка должна передавать логическое значение, а не строку
        if self.kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"«{self.label}»: требуется включить или выключить опцию.")
            return value
        try:
            # Разрешаем дробные значения с точкой и с запятой
            text = str(value).strip().replace(",", ".")
            parsed = int(text) if self.kind is int else float(text)
        except (ValueError, TypeError):
            noun = "целое число" if self.kind is int else "число"
            raise ValueError(f"«{self.label}»: введите {noun}.") from None
        if not math.isfinite(parsed) or not self.minimum <= parsed <= self.maximum:
            raise ValueError(f"«{self.label}»: допустимо от {self.minimum:g} до {self.maximum:g}.")
        # Проценты из формы переводим в доли для сборщика
        return parsed / self.scale if self.scale != 1 else parsed

# Группы полей, их подписи и границы допустимых значений
FIELD_GROUPS = (
    ("Показ точек", (
        Field("stimulus", "total_points", "Количество точек за сессию (шт)", int, 1, 1000000),
        Field("stimulus", "dwell_ms", "Время показа одной точки (мс)", int, 1, 60000),
        Field("stimulus", "capture_start_ms", "Задержка до начала записи (мс)", int, 0, 59999,
              "Должна быть меньше времени показа точки."),
        Field("stimulus", "inter_point_ms", "Пауза между точками (мс)", int, 0, 60000),
    )),
    ("Распределение и окно", (
        Field("distribution", "margin_ratio", "Отступ от краёв окна (%)", float, 0, 49,
              "Точки выбираются случайно внутри области с этим отступом.", 100),
        Field("window", "width_percent", "Ширина окна от ширины монитора (%)", float, 10, 100,
              "Высота определяется по соотношению сторон камеры.", 100),
    )),
    ("Сохранение изображений", (
        Field("images", "save_face", "Сохранять изображение лица", bool),
        Field("images", "face_size", "Размер изображения лица (px)", int, 16, 2048),
        Field("images", "save_eyes", "Сохранять изображения обоих глаз", bool),
        Field("images", "eye_size", "Размер изображения каждого глаза (px)", int, 16, 2048),
        Field("images", "jpeg_quality", "Качество JPEG (1 – 100)", int, 1, 100),
    )),
    ("Количество кадров", (
        Field("saving", "max_frames_per_fixation", "Максимум кадров на точку (0 — все)", int, 0, 1000,
              "0 — все подходящие кадры после задержки. При N > 0 — до N кадров."),
    )),
    ("Обнаружение лица", (
        Field("thresholds", "min_detection_confidence", "Минимальная уверенность (0 – 1)", float, 0.01, 1),
    )),
    ("Подготовка", (
        Field("prepare", "countdown_seconds", "Обратный отсчёт перед сбором (сек)", int, 0, 300),
    )),
    ("Отчёт о покрытии", (
        Field("coverage", "zones_rows", "Количество строк зон", int, 1, 100),
        Field("coverage", "zones_cols", "Количество столбцов зон", int, 1, 100,
              "Разбиение для отчёта; на генерацию точек не влияет."),
    )),
)


# Добавляет настройку DirectShow только на Windows
def algorithm_fields():
    if IS_WINDOWS:
        return (("Подключение камеры", (
            Field("camera", "use_directshow", "Использовать DirectShow", bool),
        )),) + FIELD_GROUPS
    return FIELD_GROUPS


# Приводит настройки камеры к текущей операционной системе
def platform_config(config):
    result = deepcopy(config)
    if IS_WINDOWS:
        # При первом запуске на Windows DirectShow включён
        result.setdefault("camera", {}).setdefault("use_directshow", True)
    else:
        # Убираем Windows-параметр, если настройки перенесены на другую систему
        result.pop("camera", None)
    return result

# Проверяет, что имя обозначает одну папку, а не произвольный путь
def _folder_name(value, label):
    name = str(value).strip()
    if (not name or name in (".", "..") or any(c in name for c in '/\\')
            or any(ord(c) < 32 for c in name)):
        raise ValueError(f"«{label}»: укажите имя папки без символов / и \\.")
    return name


# Проверяет пути, имя участника и индекс камеры
def validate_personal(personal):
    participant = _folder_name(personal.get("participant", ""), "Испытуемый")
    output_dir = str(personal.get("output_dir", "")).strip()
    if not output_dir or "\x00" in output_dir:
        raise ValueError("Укажите папку для сохранения данных.")
    dataset = str(personal.get("dataset", "")).strip()
    if "\x00" in dataset or not dataset:
        raise ValueError("Укажите собираемый датасет.")
    # Имя нового датасета задаёт одну подпапку внутри папки данных
    if not Path(dataset).expanduser().is_absolute():
        dataset = _folder_name(dataset, "Собираемый датасет")
    try:
        camera_index = int(str(personal.get("camera_index", "")).strip())
    except ValueError:
        raise ValueError("Индекс камеры должен быть целым числом от 0.") from None
    if camera_index < 0:
        raise ValueError("Индекс камеры должен быть целым числом от 0.")
    return {**personal, "participant": participant, "dataset": dataset,
            "output_dir": output_dir, "camera_index": camera_index}


# Разрешает пути корня данных, датасета и участника
def storage_paths(personal):
    output = Path(personal["output_dir"]).expanduser()
    if not output.is_absolute():
        output = BASE_DIR / output
    output = output.resolve()
    dataset = Path(personal["dataset"]).expanduser()
    if not dataset.is_absolute():
        # Абсолютный путь датасета остаётся независимым от выбранной папки данных
        dataset = output / dataset
    dataset = dataset.resolve()
    return output, dataset, dataset / personal["participant"]

# Проверяет поля и совместимость параметров сбора
def validate_algorithm(config):
    result = platform_config(config)
    for _, fields in algorithm_fields():
        for field in fields:
            value = result[field.section][field.key]
            result[field.section][field.key] = field.parse(value * field.scale if field.scale != 1 else value)
    # После переключения взгляда должен оставаться интервал для записи
    if result["stimulus"]["capture_start_ms"] >= result["stimulus"]["dwell_ms"]:
        raise ValueError("Задержка записи должна быть меньше времени показа точки.")
    if not (result["images"]["save_face"] or result["images"]["save_eyes"]):
        raise ValueError("Включите сохранение лица или глаз, чтобы собрать изображения.")
    return result

# Загружает сохранённые значения поверх стандартных настроек
def load_config(path=CONFIG_PATH):
    config = deepcopy(DEFAULT_CONFIG)
    if Path(path).exists():
        with open(path, encoding="utf-8") as stream:
            saved = json.load(stream)
        if not isinstance(saved, dict):
            raise ValueError("Конфигурация должна содержать объект настроек.")
        # Из файла берём сохранённые значения, отсутствующие оставляем стандартными
        for section, values in saved.items():
            if section in config and not isinstance(values, dict):
                raise ValueError(f"Раздел «{section}» имеет неверный формат.")
            if isinstance(values, dict):
                config.setdefault(section, {}).update(values)

        # Сохраняет имя папки участника из прежней конфигурации
        old_personal = saved.get("personal", {})
        if "participant" not in old_personal and "person_id" in old_personal:
            old_id = str(old_personal["person_id"]).strip()
            config["personal"]["participant"] = f"person_{old_id}" if old_id else ""
        config["personal"].pop("person_id", None)
    return platform_config(config)

# Сохраняет файл через временную копию и атомарную замену
def save_config(config, path=CONFIG_PATH):
    path = Path(path)
    # Снимок сессии сохраняется как есть; рабочие настройки учитывают текущую систему
    if "schema_version" not in config:
        config = platform_config(config)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".app_config-", suffix=".json", delete=False) as stream:
            temporary = stream.name
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        # Готовый файл заменяет старый целиком
        os.replace(temporary, path)
    finally:
        # При неудачной записи удаляем оставшийся временный файл
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)

# Готовит проверенные настройки с абсолютным путём к данным
def collection_config(config):
    result = validate_algorithm(config)
    result["personal"] = validate_personal(result["personal"])
    output = Path(result["personal"]["output_dir"]).expanduser()
    if not output.is_absolute():
        output = BASE_DIR / output
    result["personal"]["output_dir"] = str(output.resolve())
    return result
