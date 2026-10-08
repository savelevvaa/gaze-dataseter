# Распределение целей по осям и расчёт тепловой карты
import numpy as np


# Виды графиков, размеры точек и цветовая шкала
VIEWS = ('Точки', 'Плотность', 'Тепловая карта', 'По горизонтали', 'По вертикали')
POINT_SIZES = ('1 px', '2 px', '3 px', '4 px', '5 px')
HEAT_COLORS = np.array(((255, 255, 255), (255, 242, 170), (255, 174, 65),
                        (227, 68, 35), (150, 15, 35)), dtype=float)


# Считает распределение по равным интервалам выбранной оси
def axis_counts(targets, axis, bins=20, *, by_frames=False):
    counts = [0] * bins
    for target in targets:
        coordinate = target.x if axis == 'x' else target.y
        # Координата на правой границе остаётся в последнем интервале
        index = min(bins - 1, max(0, int(coordinate * bins)))
        counts[index] += target.frames if by_frames else 1
    return counts


# Считает каждую цель один раз и сглаживает фиксированную сетку
def heatmap_density(targets, bins=80):
    counts = np.zeros((bins, bins), dtype=float)
    for target in targets:
        col = min(bins - 1, max(0, int(target.x * bins)))
        row = min(bins - 1, max(0, int(target.y * bins)))
        counts[row, col] += 1
    # Ядро сглаживания распределяет каждую цель по соседним ячейкам
    offsets = np.arange(-4, 5)
    kernel = np.exp(-0.5 * (offsets / 1.2) ** 2)
    kernel /= kernel.sum()

    # Симметричное продолжение сохраняет суммарное количество у границ области
    for axis in (0, 1):
        padding = [(0, 0), (0, 0)]
        padding[axis] = (4, 4)
        counts = np.apply_along_axis(lambda values: np.convolve(values, kernel, mode='valid'),
                                     axis, np.pad(counts, padding, mode='symmetric'))
    return counts


# Переводит относительную плотность в цвета от белого к красному
def heat_colors(strength):
    levels = np.clip(np.asarray(strength), 0, 1) * (len(HEAT_COLORS) - 1)
    lower = np.minimum(levels.astype(int), len(HEAT_COLORS) - 2)
    # Промежуточные оттенки получаем смешением соседних цветов шкалы
    fraction = (levels - lower)[..., None]
    return np.rint(HEAT_COLORS[lower] * (1 - fraction) + HEAT_COLORS[lower + 1] * fraction).astype('uint8')
