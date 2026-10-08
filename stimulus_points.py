# Общий генератор целей для сбора и симуляции
import random


# Выбирает независимые целочисленные координаты внутри заданных отступов
def generate_random_points(num_points, win_w, win_h, margin_ratio, *, rng=None):
    rng = random if rng is None else rng
    # Отступы в процентах переводим в пиксели окна
    margin_x = int(win_w * margin_ratio)
    margin_y = int(win_h * margin_ratio)
    # Если отступы не оставляют области для целей, используем центр окна
    if margin_x * 2 >= win_w or margin_y * 2 >= win_h:
        return [(win_w // 2, win_h // 2)] * num_points
    return [(rng.randint(margin_x, win_w - margin_x - 1),
             rng.randint(margin_y, win_h - margin_y - 1)) for _ in range(num_points)]
