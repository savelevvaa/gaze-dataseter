# Генерация виртуальных сессий без камеры и записи файлов
import random

from dataset_analysis import ScanCancelled, Target
from stimulus_points import generate_random_points


# Проверяет активные поля и общий объём виртуальных сессий
def simulation_size(mode, total, sessions, points_per_session):
    def positive(value, label):
        try:
            count = int(str(value).strip())
        except (ValueError, TypeError):
            raise ValueError(f'{label}: введите целое число.') from None
        if count < 1:
            raise ValueError(f'{label}: число должно быть больше нуля.')
        return count

    # Читаем только поля выбранного способа расчёта
    if mode == 'total':
        count = positive(total, 'Всего точек')
        sessions, points_per_session = 1, count
    elif mode == 'sessions':
        sessions = positive(sessions, 'Сессий')
        points_per_session = positive(points_per_session, 'Точек в сессии')
        count = sessions * points_per_session
    else:
        raise ValueError('Выберите способ расчёта.')
    # Ограничиваем объём одной генерации, чтобы не занимать слишком много памяти
    if count > 1_000_000:
        raise ValueError('За один расчёт можно сгенерировать до 1 000 000 точек.')
    return count, sessions, points_per_session


# Генерирует и перемешивает каждую сессию общим со сборщиком алгоритмом
def simulate_targets(sessions, points_per_session, width, height, margin_ratio, *, rng=None, cancel=None):
    rng = random if rng is None else rng
    targets = []
    # Каждая виртуальная сессия генерируется и перемешивается отдельно
    for _ in range(sessions):
        if cancel is not None and cancel.is_set():
            raise ScanCancelled
        points = generate_random_points(points_per_session, width, height, margin_ratio, rng=rng)
        rng.shuffle(points)
        for index, (x, y) in enumerate(points):
            # При большом количестве точек периодически проверяем отмену расчёта
            if index % 1024 == 0 and cancel is not None and cancel.is_set():
                raise ScanCancelled
            targets.append(Target(x / (width - 1), y / (height - 1), width, height))
    return targets
