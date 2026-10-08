# Общее оформление, переходы между окнами и прокрутка форм
import sys
import tkinter as tk
from tkinter import ttk


# Задаёт общий шрифт, сохраняя системную тему элементов
def setup_style(root):
    root.option_add("*Font", "Helvetica 13")
    style = ttk.Style(root)
    style.configure("TEntry", font=("Helvetica", 13))
    style.configure("TSpinbox", font=("Helvetica", 13))


# Добавляет горизонтальный разделитель
def separator(parent):
    line = tk.Frame(parent, height=1, background="black")
    line.pack(fill="x", pady=(10, 10))
    return line


# Размещает окно по центру с учётом доступного размера экрана
def center_window(window, width, height, minimum=(420, 320)):
    window.update_idletasks()
    screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
    # Ограничиваем размер окна, чтобы оно помещалось на экране
    width = min(width, max(320, screen_w - 80))
    height = min(height, max(300, screen_h - 100))
    window.minsize(min(minimum[0], width), min(minimum[1], height))
    x, y = max(0, (screen_w - width) // 2), max(25, (screen_h - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


# Скрывает родителя на время работы дочернего окна
class SingleWindow(tk.Toplevel):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self._parent_hidden = False
        self._previous_grab = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # Показывает окно и переводит на него фокус и обработку ввода
    def present(self, width, height, minimum=(420, 320)):
        center_window(self, width, height, minimum)
        # Сохраняем окно, которому надо вернуть управление при закрытии
        self._previous_grab = self.grab_current()
        self.master.withdraw()
        self._parent_hidden = True
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_set()

    # Закрывает окно и восстанавливает родителя и его фокус
    def destroy(self):
        parent = self.master
        restore = self._parent_hidden
        self._parent_hidden = False
        if self.grab_current() is self:
            self.grab_release()
        super().destroy()
        # Возвращаем только родительское окно, если оно ещё существует
        if restore and parent.winfo_exists():
            parent.deiconify()
            parent.lift()
            if self._previous_grab is not None and self._previous_grab.winfo_exists():
                self._previous_grab.grab_set()
            parent.focus_set()


# Прокручиваемая форма с фиксированной шириной содержимого
class ScrollForm(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=self.cget("background"), yscrollincrement=20)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = tk.Frame(self.canvas)
        # Содержимое формы размещается внутри области прокрутки
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", self._resize_content)
        self.top = self.winfo_toplevel()
        self.bindings = {sequence: self.top.bind(sequence, self._wheel, add="+")
                         for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>")}
        self.bind("<Destroy>", self._unbind, add="+")

    # Обновляет границы прокрутки после изменения содержимого
    def _update_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    # Подгоняет ширину формы под видимую область
    def _resize_content(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    # Направляет колесо мыши на прокрутку, не меняя значения числовых полей
    def bind_scrolling(self):
        def bind_tree(widget):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, self._wheel, add="+")
            # Назначаем обработку колеса также вложенным полям и кнопкам
            for child in widget.winfo_children():
                bind_tree(child)
        bind_tree(self.content)

    # Обрабатывает колесо мыши только внутри этой формы
    def _wheel(self, event):
        widget = event.widget
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        if widget is None:
            return
        # Мышь в Linux, macOS и Windows передаёт прокрутку разными событиями
        if getattr(event, "num", None) == 4:
            steps = -1
        elif getattr(event, "num", None) == 5:
            steps = 1
        elif not event.delta:
            return
        elif sys.platform == "darwin":
            steps = -int(event.delta)
        else:
            steps = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(steps, "units")
        return "break"

    # Прокручивает форму к полю, получившему фокус
    def reveal(self, widget):
        self.update_idletasks()
        y = widget.winfo_rooty() - self.content.winfo_rooty()
        height = max(1, self.content.winfo_height())
        # Сравниваем положение поля с видимой частью формы
        visible_start = self.canvas.canvasy(0)
        visible_end = visible_start + self.canvas.winfo_height()
        if y < visible_start or y + widget.winfo_height() > visible_end:
            self.canvas.yview_moveto(max(0, y - 25) / height)

    # Удаляет обработчики прокрутки при закрытии формы
    def _unbind(self, event):
        if event.widget is self:
            for sequence, binding in self.bindings.items():
                self.top.unbind(sequence, binding)


# Текст справки, который показывается в приложении
HELP_TEXT = """Подготовка
В разделе 'Настройки участника' укажите папку для данных, название собираемого датасета, имя испытуемого и индекс камеры. Обычно встроенная камера имеет индекс 0. Название датасета создаёт подпапку в папке данных; Выбрать существующую папку датасета можно при нажатии на кнопку 'Обзор'.

Параметры сбора
Все действующие параметры доступны в разделе 'Настройки сбора'. Значения параметров необходимо вводить только в виде чисел. Для дробных чисел можно использовать точку или запятую.

Для сбора данных можно использовать следующие диапазоны параметров: 200-400 точек, 500-800 мс суммарного времени показа целей, где первые 300-600 мс на отводятся на переключение взгляда испытуемого на появившуюся цель, а следующие 200 мс отводятся на запись (200 мс достаточно). Пауза между двумя целями может быть какая угодно, но для быстрого и комфортного сбора данных ее можно установить в диапазоне 100-300 мс. Введя выбранные параметры на странице главного меню а так же во время режима остановки специальный счетчик отобразит расчетное время сбора данных (продолжительность сессии).

Значение 0 в количестве кадров означает запись всех подходящих кадров после задержки; положительное значение задаёт максимум кадров на одну точку.

Сохранение
Нажмите 'Сохранить настройки'. Они сохраняются между запусками приложения, редактировать YAML не требуется. Кнопка 'Назад', Esc и закрытие окна предлагают сохранить несохранённые изменения в модальном окне: 'Да' сохраняет, 'Нет' возвращает главное меню без сохранения введенных параметров.

Запуск
Нажмите 'Пуск'. После обратного отсчёта смотрите на появляющиеся цели - качество сбора датасета зависит от аккуратности и целеустремленности испытуемого, в собранном датасете предполагается что все отображенные цели сохранены при условии что на них смотрел пользователь. Пробел вызывает остановку сбора данных после текущей отображаемой цели. Во время остановки видна следующая цель. Повторный пробел запускает отсчёт 3 секунды, сбор данных продолжается. Q или Esc завершают сбор - находитесь вы непосредственно во время сбора или в паузе.

Результат
Каждый запуск создаёт новую папку <датасет>/<испытуемый>/sN в выбранной папке данных, например data/test_dataset/person_111/s1. Имя испытуемого используется без изменения. В сессии находятся изображения, annotations.csv, coverage_report.json и session_config.json со всеми настройками запуска и размерами камеры и окна.

Количество сохранённых кадров может быть меньше выбранного максимума в том случае, еслси установленно коротное время записи (за 100 мс успевается взять примерно 3 кадра).

Датасеты
Кнопка 'Датасеты' открывает окно-проводник, в котором удобно можно оценить какие датасеты у вас есть. Выбрать можно как сам датасет, так и конкретного пользователя из датасета или же конкретную сессию и оценить их карту покрытия и другие статистические данные. Цели без записанных кадров не добавляются на карту. Можно переключаться между точками и плотностью, учитывать цели или кадры, менять сетку зон. Просмотр в этом окне никак не изменяет файлы датасетов или настройки записи.
"""


# Открывает справку и повторно использует уже открытое окно
def show_help(parent):
    existing = getattr(parent, "_help_window", None)
    # Повторное нажатие возвращает уже открытую справку
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return existing
    window = SingleWindow(parent, "GazeDataSeter")
    parent._help_window = window
    frame = tk.Frame(window)
    frame.pack(fill="both", expand=True, padx=25, pady=20)
    tk.Label(frame, text="Система сбора датасетов 2D Gaze Estimation",
             font=("Helvetica", 18)).pack(pady=(0, 12))
    body = tk.Frame(frame)
    body.pack(fill="both", expand=True)
    text = tk.Text(body, wrap="word", relief="flat", font=("Helvetica", 13),
                   padx=12, pady=12, width=45, height=12)
    scroll = ttk.Scrollbar(body, command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    text.pack(fill="both", expand=True)
    text.insert("1.0", HELP_TEXT)
    text.configure(state="disabled")
    tk.Button(frame, text="Назад", command=window.destroy).pack(pady=(15, 0))
    window.present(620, 650)
    window.bind("<Escape>", lambda _event: window.destroy())
    return window
