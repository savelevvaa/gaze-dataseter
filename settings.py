# Окна настроек участника и сбора
from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from configuration import BASE_DIR, algorithm_fields, platform_config, validate_algorithm, validate_personal
from ui import ScrollForm, SingleWindow, separator, show_help


# Показывает вопрос о сохранении с кнопками «Да» и «Нет»
def ask_save_changes(parent, question):
    window = tk.Toplevel(parent)
    window.withdraw()
    window.title("Сохранение настроек")
    window.transient(parent)
    window.resizable(False, False)
    result = False
    # Запоминаем, какое окно принимало ввод до вопроса о сохранении
    previous_grab = parent.grab_current()

    def finish(save):
        nonlocal result
        result = save
        window.destroy()

    tk.Label(window, text=question, wraplength=380, justify="center").pack(padx=25, pady=(25, 20))
    buttons = tk.Frame(window)
    buttons.pack(pady=(0, 20))
    yes = tk.Button(buttons, text="Да", width=10, command=lambda: finish(True))
    yes.pack(side="left", padx=5)
    tk.Button(buttons, text="Нет", width=10, command=lambda: finish(False)).pack(side="left", padx=5)
    # Закрытие вопроса и Esc означают отказ от сохранения
    window.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    window.bind("<Escape>", lambda _event: finish(False))
    window.bind("<Return>", lambda _event: finish(True))
    parent.update_idletasks()
    x = max(0, parent.winfo_rootx() + (parent.winfo_width() - 430) // 2)
    y = max(25, parent.winfo_rooty() + (parent.winfo_height() - 160) // 2)
    window.geometry(f"430x160+{x}+{y}")
    window.deiconify()
    window.grab_set()
    yes.focus_set()
    parent.wait_window(window)
    if previous_grab is not None and previous_grab.winfo_exists():
        previous_grab.grab_set()
    return result


# Общие элементы окон настроек и проверка изменений при выходе
class _BaseDialog(SingleWindow):
    def __init__(self, parent, title, *, on_save=None, scrollable=True):
        super().__init__(parent, title)
        self.result = None
        self._save_callback = on_save
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = tk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", padx=25, pady=(20, 0))
        tk.Label(header, text=title, font=("Helvetica", 18)).pack(pady=(0, 5))
        separator(header)
        # В форме участника четыре поля, прокрутка нужна только параметрам сбора
        self.form = ScrollForm(self) if scrollable else tk.Frame(self)
        self.form.grid(row=1, column=0, sticky="nsew", padx=25)
        self.content = self.form.content if scrollable else self.form
        # Кнопки внизу не прокручиваются вместе с полями
        footer = tk.Frame(self)
        footer.grid(row=2, column=0, sticky="ew", padx=25, pady=(0, 20))
        separator(footer)
        tk.Button(footer, text="Сохранить настройки", command=self._on_ok).pack(fill="x")
        navigation = tk.Frame(footer)
        navigation.pack(fill="x", pady=(10, 0))
        tk.Button(navigation, text="Назад", command=self._on_back).pack(side="left")
        tk.Button(navigation, text="Справка", command=lambda: show_help(self)).pack(side="right")
        self.footer = footer
        self.bind("<Escape>", lambda _event: self._on_back())
        self.protocol("WM_DELETE_WINDOW", self._on_back)

    # Запоминает исходные значения и показывает окно
    def _show(self, width, height, wait):
        self._initial_values = self._current_values()
        if isinstance(self.form, ScrollForm):
            self.form.bind_scrolling()
        self.present(width, height, minimum=(460, 430 if isinstance(self.form, ScrollForm) else 480))
        if wait:
            self.wait_window()

    # Читает текущие значения из полей формы
    def _current_values(self):
        return {key: variable.get() for key, variable in self._vars.items()}

    # Предлагает сохранить изменения и возвращает предыдущее окно
    def _on_back(self):
        # Вопрос задаётся только при изменении значений за это посещение формы
        if self._current_values() != self._initial_values:
            if ask_save_changes(self, self.save_question):
                self._on_ok()
                return
        self.destroy()

    # Проверяет и сохраняет параметры; при ошибке оставляет форму открытой
    def _on_ok(self):
        try:
            result = self._get_result()
        except (ValueError, tk.TclError) as error:
            messagebox.showwarning("Проверьте настройки", str(error), parent=self)
            return
        try:
            if self._save_callback is not None:
                # Сначала записываем настройки; закрываем окно только после успешного сохранения
                self._save_callback(result)
        except OSError as error:
            messagebox.showerror("Не удалось сохранить настройки", str(error), parent=self)
            return
        self.result = result
        self.destroy()

    # Добавляет подпись с переносом длинного текста
    def _label(self, parent, text):
        label = tk.Label(parent, text=text, anchor="w", justify="left")
        label.pack(fill="x", pady=(10, 3))
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(100, event.width - 4)))


# Папки хранения, имя участника и индекс камеры
class PersonalSettingsDialog(_BaseDialog):
    save_question = "Вы хотите сохранить новые данные пользователя?"

    def __init__(self, parent, personal_config, *, wait=True, on_save=None):
        super().__init__(parent, "Настройки участника", on_save=on_save, scrollable=False)
        self._cfg = deepcopy(personal_config)
        self._vars = {}
        self.widgets = {}
        fields = (
            ("output_dir", "Папка для данных"),
            ("dataset", "Собираемый датасет"),
            ("participant", "Испытуемый"),
            ("camera_index", "Индекс камеры"),
        )
        # Создаём поля участника в том порядке, в котором они показаны в меню
        for key, label in fields:
            self._label(self.content, label)
            row = tk.Frame(self.content)
            row.pack(fill="x", pady=(0, 5))
            row.columnconfigure(0, weight=1)
            variable = tk.StringVar(self, value=str(self._cfg.get(key, "test_dataset" if key == "dataset" else "")))
            self._vars[key] = variable
            entry = ttk.Entry(row, textvariable=variable)
            entry.grid(row=0, column=0, sticky="ew")
            self.widgets[key] = entry
            if key in ("output_dir", "dataset"):
                tk.Button(row, text="Обзор…", command=lambda field=key: self._browse(field)).grid(
                    row=0, column=1, padx=(8, 0))
        self._show(580, 520, wait)

    # Выбирает папку; для датасета внутри корня сохраняет только её имя
    def _browse(self, field):
        path = Path(self._vars["output_dir"].get() or "data").expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        output = path.resolve()
        if field == "dataset":
            dataset = Path(self._vars["dataset"].get() or "test_dataset").expanduser()
            path = dataset if dataset.is_absolute() else output / dataset

        # Для диалога выбора ищем ближайшую существующую папку
        while not path.is_dir() and path != path.parent:
            path = path.parent
        selected = filedialog.askdirectory(initialdir=str(path), parent=self,
                                           title="Собираемый датасет" if field == "dataset" else "Папка для данных")
        if selected:
            selected = Path(selected).resolve()
            # Для датасета внутри папки данных достаточно названия, снаружи нужен полный путь
            value = selected.name if field == "dataset" and selected.parent == output else str(selected)
            self._vars[field].set(value)

    # Собирает и проверяет параметры участника
    def _get_result(self):
        return validate_personal({**self._cfg, **self._current_values()})


# Форма параметров сбора по описаниям полей конфигурации
class AlgorithmSettingsDialog(_BaseDialog):
    save_question = "Вы хотите сохранить новые настройки сбора?"

    def __init__(self, parent, config, *, wait=True, on_save=None):
        super().__init__(parent, "Настройки сбора", on_save=on_save)
        self._cfg = platform_config(config)
        self._vars = {}
        self.widgets = {}
        # Форма строится по тем же описаниям полей, которые используются при проверке
        for index, (title, fields) in enumerate(algorithm_fields()):
            if index:
                separator(self.content)
            tk.Label(self.content, text=title, anchor="w",
                     font=("Helvetica", 14, "bold")).pack(fill="x", pady=(4, 0))
            for field in fields:
                value = self._cfg[field.section][field.key]
                # Для переключателей создаём галочки, для чисел — поля ввода
                if field.kind is bool:
                    variable = tk.BooleanVar(self, value=value)
                    widget = tk.Checkbutton(self.content, text=field.label, variable=variable,
                                            command=self._toggle_images, anchor="w")
                    widget.pack(fill="x", pady=(10, 3))
                else:
                    self._label(self.content, field.label)
                    # Проценты показываем в привычном виде, например 90 вместо 0.9
                    shown = value * field.scale
                    variable = tk.StringVar(self, value=f"{shown:g}")
                    if field.kind is int:
                        widget = ttk.Spinbox(self.content, textvariable=variable,
                                             from_=field.minimum, to=field.maximum)
                    else:
                        widget = ttk.Entry(self.content, textvariable=variable)
                    widget.pack(fill="x", pady=(0, 5))
                    widget.bind("<FocusIn>", lambda _event, w=widget: self.form.reveal(w))
                self._vars[field.key] = variable
                self.widgets[field.key] = widget
        self._toggle_images()
        self._show(620, 740, wait)

    # Отключает размер изображения, если его сохранение выключено
    def _toggle_images(self):
        for switch, dependent in (("save_face", "face_size"), ("save_eyes", "eye_size")):
            if switch in self._vars and dependent in self.widgets:
                self.widgets[dependent].configure(state="normal" if self._vars[switch].get() else "disabled")

    # Переводит поля формы в конфигурацию сборщика
    def _get_result(self):
        result = deepcopy(self._cfg)
        for _, fields in algorithm_fields():
            for field in fields:
                result[field.section][field.key] = field.parse(self._vars[field.key].get())
        return validate_algorithm(result)
