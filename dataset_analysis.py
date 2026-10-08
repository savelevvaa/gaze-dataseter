# Фактическая статистика по разметке и сохранённым изображениям
from collections import Counter
import csv
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re


# Сигнал отмены фонового чтения данных
class ScanCancelled(Exception):
    pass


# Нормализованные координаты цели и число её сохранённых кадров
@dataclass
class Target:
    x: float
    y: float
    width: int
    height: int
    frames: int = 0


# Статистика одной сессии и ошибки чтения данных
@dataclass
class SessionStats:
    path: Path
    participant: str
    rows: int = 0
    frames: int = 0
    missing_images: int = 0
    invalid_rows: int = 0
    targets: dict = field(default_factory=dict)
    planned: int | None = None
    issues: list = field(default_factory=list)

    # Возвращает число целей с пригодными записями
    @property
    def points(self):
        return len(self.targets)


# Папка датасета и найденные в ней сессии
@dataclass
class DatasetStats:
    path: Path
    name: str
    sessions: list = field(default_factory=list)


# Прерывает чтение, если запрошена отмена
def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ScanCancelled


# Перечисляет видимые подпапки в естественном порядке имён
def _directories(path):
    return sorted((child for child in path.iterdir() if child.is_dir() and not child.name.startswith('.')),
                  key=lambda child: natural_key(child.name))


# Разделяет текст и числа для порядка s1, s2, s10
def natural_key(name):
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', name))


# Распознаёт сессии по файлам и структуре папок
def session_directories(participant):
    markers = ('annotations.csv', 'session_config.json', 'coverage_report.json')
    def has_marker(child):
        return any((child / marker).is_file() for marker in markers)
    # Отличает пустую сессию от участника с похожим именем
    def is_session(child):
        if has_marker(child):
            return True


        return bool(re.fullmatch(r's\d+', child.name)) and not any(
            has_marker(nested) or re.fullmatch(r's\d+', nested.name) for nested in _directories(child))
    return [child for child in _directories(participant) if is_session(child)]


# Проверяет конечное целое значение и нижнюю границу
def _integer(value, minimum=0):
    number = float(value)
    if not math.isfinite(number) or not number.is_integer() or number < minimum:
        raise ValueError
    return int(number)


# Проверяет непустой файл внутри сессии и запоминает результат
def _image_exists(session, value, cache):
    if not value:
        return False
    if value not in cache:
        try:
            path = (session / value).resolve()
            cache[value] = path.is_relative_to(session) and path.is_file() and path.stat().st_size > 0
        except (OSError, ValueError):
            cache[value] = False
    return cache[value]


# Читает разметку и учитывает только строки с корректными данными и файлами
def analyze_session(path, participant=None, *, cancel=None):
    path = Path(path).resolve()
    result = SessionStats(path, participant or path.parent.name)
    # Из конфигурации сессии нужны план целей и выбранные виды изображений
    config = {}
    snapshot = path / 'session_config.json'
    if snapshot.exists():
        try:
            saved = json.loads(snapshot.read_text(encoding='utf-8'))
            saved_config = saved['configuration']
            if not isinstance(saved_config, dict):
                raise ValueError('configuration не является объектом')
            config = saved_config
            try:
                # План берём из снимка запуска, а число записанных целей — из CSV
                result.planned = _integer(config['stimulus']['total_points'], 1)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        except (OSError, ValueError, KeyError, TypeError) as error:
            result.issues.append(f'Не удалось прочитать конфигурацию: {error}')
    images = config.get('images', {})
    # Состав обязательных изображений определяется параметрами сохранения
    required_images = []
    if isinstance(images, dict):
        if images.get('save_face') is True:
            required_images.append('image_face')
        if images.get('save_eyes') is True:
            required_images.extend(('image_left_eye', 'image_right_eye'))
    csv_path = path / 'annotations.csv'
    if not csv_path.is_file():
        result.issues.append('Нет annotations.csv: записанных примеров не найдено.')
        return result
    # Запоминаем проверенные файлы и координаты каждого индекса цели
    cache = {}
    coordinates = {}
    try:
        with csv_path.open(newline='', encoding='utf-8-sig') as stream:
            reader = csv.DictReader(stream)
            required = {'fixation_idx', 'target_x_px', 'target_y_px', 'screen_w', 'screen_h'}
            # Без индекса и координат строки нельзя использовать для карты
            if not required.issubset(reader.fieldnames or []):
                result.issues.append('CSV не содержит индекса цели, координат или размеров холста.')
                for _ in reader:
                    _check_cancel(cancel)
                    result.rows += 1
                result.invalid_rows = result.rows
                return result
            # Каждая строка проверяется до включения в статистику
            for row in reader:
                _check_cancel(cancel)
                result.rows += 1
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError
                    idx = _integer(row['fixation_idx'])
                    width, height = _integer(row['screen_w'], 2), _integer(row['screen_h'], 2)
                    px, py = _integer(row['target_x_px']), _integer(row['target_y_px'])
                    if px >= width or py >= height:
                        raise ValueError
                    coordinate = (px, py, width, height)
                    # Один индекс внутри сессии должен обозначать одну и ту же цель
                    if idx in coordinates and coordinates[idx] != coordinate:
                        raise ValueError
                    coordinates[idx] = coordinate
                except (ValueError, TypeError, OverflowError):
                    result.invalid_rows += 1
                    continue
                # Требуются включённые изображения и все дополнительные пути из строки
                declared = [key for key in ('image_face', 'image_left_eye', 'image_right_eye') if row.get(key)]
                needed = set(required_images) | set(declared)
                # Исключаем кадр, если нет хотя бы одного обязательного изображения
                if not needed or not all(_image_exists(path, row.get(key, ''), cache) for key in needed):
                    result.missing_images += 1
                    continue
                # Повторные кадры одной цели объединяются только внутри этой сессии
                target = result.targets.setdefault(idx, Target(px / (width - 1), py / (height - 1), width, height))
                target.frames += 1
                result.frames += 1
    except (OSError, UnicodeError, csv.Error) as error:
        result.issues.append(f'Ошибка чтения CSV; учтены только прочитанные строки: {error}')
    if result.invalid_rows:
        result.issues.append(f'Некорректные строки или координаты: {result.invalid_rows}.')
    if result.missing_images:
        result.issues.append(f'Строки без полного набора указанных изображений: {result.missing_images}.')
    return result


# Ищет датасеты новой и прежней структуры хранения
def scan_library(root, extra_datasets=(), *, cancel=None, progress=None):
    root = Path(root).expanduser().resolve()
    candidates = []
    if root.is_dir():
        children = _directories(root)
        legacy = [child for child in children if session_directories(child)]
        # Старые данные без папки датасета показываем отдельным узлом
        if legacy:
            candidates.append((root, 'Прежние данные', legacy))
        for child in children:
            if child not in legacy:
                candidates.append((child, child.name, _directories(child)))
    # Добавляем выбранный внешний датасет, если его ещё нет в списке
    for extra in extra_datasets:
        extra = Path(extra).expanduser().resolve()
        if extra.is_dir() and all(candidate[0] != extra for candidate in candidates):
            candidates.append((extra, extra.name, _directories(extra)))
    datasets = []
    # Проходим найденные датасеты, испытуемых и их сессии
    for path, name, participants in candidates:
        _check_cancel(cancel)
        dataset = DatasetStats(path, name)
        for participant in participants:
            for session in session_directories(participant):
                _check_cancel(cancel)
                if progress:
                    progress(f'Чтение: {name} / {participant.name} / {session.name}')
                dataset.sessions.append(analyze_session(session, participant.name, cancel=cancel))
        datasets.append(dataset)
    return datasets


# Объединяет статистику, сохраняя отдельные цели каждой сессии
def aggregate(sessions):
    targets = [target for session in sessions for target in session.targets.values()]
    frames = sum(session.frames for session in sessions)
    # Размер карты выбирается по самому частому размеру окна в записях
    resolutions = Counter((target.width, target.height) for target in targets)
    return {'sessions': len(sessions), 'participants': len({session.participant for session in sessions}),
            'rows': sum(session.rows for session in sessions), 'frames': frames, 'points': len(targets),
            'excluded': sum(session.missing_images + session.invalid_rows for session in sessions),
            'empty_sessions': sum(session.frames == 0 for session in sessions),
            'planned': sum(session.planned for session in sessions if session.planned is not None),
            'planned_sessions': sum(session.planned is not None for session in sessions),
            'mean_frames': frames / len(targets) if targets else 0,
            'targets': targets, 'resolutions': resolutions}


# Считает цели или кадры по ячейкам нормализованной области
def zone_counts(targets, size=3, *, by_frames=False):
    counts = [[0] * size for _ in range(size)]
    for target in targets:
        col = min(size - 1, int(target.x * size))
        row = min(size - 1, int(target.y * size))
        counts[row][col] += target.frames if by_frames else 1
    return counts
