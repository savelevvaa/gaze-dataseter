# Просмотр датасетов, карты покрытия и симуляция целей
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from configuration import BASE_DIR, DEFAULT_CONFIG
from dataset_analysis import ScanCancelled, aggregate, scan_library, zone_counts
from dataset_plots import POINT_SIZES, VIEWS, axis_counts, heat_colors, heatmap_density
from dataset_simulation import simulate_targets, simulation_size
from ui import SingleWindow


# Разделяет разряды числа пробелами
def number(value):
    return f'{value:,}'.replace(',', ' ')


# Рисует карту в одном растровом изображении вместо множества элементов окна
def render_coverage(targets, width, height, grid=3, *, density=False, by_frames=False,
                    color=(67, 116, 159), point_size=2, heatmap=False):
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    # Количество по зонам всегда считается по исходным координатам
    counts = zone_counts(targets, grid, by_frames=by_frames and not heatmap)
    if heatmap:
        values = heatmap_density(targets)
        maximum = values.max()
        image = Image.fromarray(heat_colors(values / maximum if maximum else values)).resize(
            (width, height), Image.Resampling.BILINEAR)
        draw = ImageDraw.Draw(image)
    elif density:
        # Насыщенность зоны зависит от её количества относительно самой заполненной зоны
        maximum = max((count for row in counts for count in row), default=0)
        for row in range(grid):
            for col in range(grid):
                strength = (counts[row][col] / maximum) ** 0.5 if maximum else 0
                shade = tuple(round(255 + (channel - 255) * strength) for channel in color)
                draw.rectangle((round(col * width / grid), round(row * height / grid),
                                round((col + 1) * width / grid), round((row + 1) * height / grid)), fill=shade)
    else:
        maximum = max((target.frames for target in targets), default=1)
        # Каждое записанное предъявление рисуется своей точкой
        for target in targets:
            x, y = round(target.x * (width - 1)), round(target.y * (height - 1))
            size = point_size + (round(4 * (target.frames / maximum) ** 0.5) if by_frames else 0)
            if size == 1:
                draw.point((x, y), fill=color)
            else:
                x0, y0 = x - size // 2, y - size // 2
                draw.ellipse((x0, y0, x0 + size - 1, y0 + size - 1), fill=color)
    # Наносим линии выбранной сетки поверх карты
    for index in range(1, grid):
        x, y = round(index * width / grid), round(index * height / grid)
        draw.line((x, 0, x, height), fill=(208, 211, 214))
        draw.line((0, y, width, y), fill=(208, 211, 214))
    draw.rectangle((0, 0, width - 1, height - 1), outline=(130, 135, 140))
    return image, counts


# Окно фактической статистики, карт покрытия и симуляции
class DatasetBrowser(SingleWindow):
    def __init__(self, parent, personal_config, *, collection_settings=None, wait=True):
        super().__init__(parent, 'Датасеты')
        output = Path(personal_config.get('output_dir', 'data')).expanduser()
        self.output = output.resolve() if output.is_absolute() else (BASE_DIR / output).resolve()
        dataset = Path(personal_config.get('dataset', 'test_dataset')).expanduser()
        self.extra_datasets = [dataset.resolve()] if dataset.is_absolute() else []
        # Состояние фонового чтения папок и текущего выбора в дереве
        self._messages = queue.Queue()
        self._generation = 0
        self._cancel = threading.Event()
        self._poll_id = self._redraw_id = None
        self._nodes = {}
        self._stats = aggregate([])
        self._photo = None
        self._plot = None
        self._simulation_config = deepcopy(collection_settings or DEFAULT_CONFIG)
        # У симуляции отдельная очередь, чтобы она не мешала чтению датасетов
        self._simulation_messages = queue.Queue()
        self._simulation_generation = 0
        self._simulation_cancel = threading.Event()
        self._simulation_redraw_id = None
        self._simulation_started = False
        self._simulation_targets = []
        self._simulation_photo = self._simulation_plot = None
        self._simulation_reference = (1280, 720)
        self._simulation_zone_summary = ''
        self._build_ui()
        self.bind('<Escape>', lambda _event: self.destroy())
        self.present(1180, 860, minimum=(920, 660))
        self.refresh()
        self._poll_id = self.after(100, self._poll)
        if wait:
            self.wait_window()

    # Создаёт дерево, сводку и вкладки анализа
    def _build_ui(self):
        frame = tk.Frame(self)
        frame.pack(fill='both', expand=True, padx=25, pady=20)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        def line(row):
            tk.Frame(frame, height=1, background='black').grid(row=row, column=0, sticky='ew', pady=10)
        tk.Label(frame, text='Статистика наборов данных', font=('Helvetica', 18)).grid(row=0, column=0, pady=(0, 5))
        line(1)
        source = tk.Frame(frame)
        source.grid(row=2, column=0, sticky='ew')
        tk.Label(source, text='Папка данных').pack(side='left', padx=(0, 10))
        self.folder_var = tk.StringVar(self, value=str(self.output))
        ttk.Entry(source, textvariable=self.folder_var, state='readonly').pack(side='left', fill='x', expand=True)
        tk.Button(source, text='Обзор…', command=self._browse_root).pack(side='left', padx=(8, 0))
        line(3)
        body = tk.Frame(frame)
        body.grid(row=4, column=0, sticky='nsew')
        body.columnconfigure(0, weight=1, minsize=260)
        body.columnconfigure(1, weight=6)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 18))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        self.library_title = tk.Label(left, text='Датасетов: 0; Сессий: 0', anchor='w')
        self.library_title.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        self.tree = ttk.Treeview(left, columns=('points', 'frames'), selectmode='browse')
        self.tree.heading('#0', text='Название')
        self.tree.heading('points', text='Цели')
        self.tree.heading('frames', text='Кадры')
        self.tree.column('#0', width=135, minwidth=100)
        for column in ('points', 'frames'):
            self.tree.column(column, width=60, minwidth=50, stretch=False, anchor='e')
        self.tree.grid(row=1, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<<TreeviewSelect>>', self._select)
        right = tk.Frame(body)
        right.grid(row=0, column=1, sticky='nsew')
        self.selection_title = tk.Label(right, text='Выберите датасет, испытуемого или сессию',
                                        anchor='w', justify='left', font=('Helvetica', 14, 'bold'))
        self.selection_title.pack(fill='x', pady=(0, 8))
        self.summary = tk.Frame(right)
        self.summary.pack(fill='x', pady=(0, 10))
        self.summary_values = {}
        # План и фактическую запись ставим рядом в первой строке таблицы
        metrics = (
            (('planned', 'Целей по плану'), ('points', 'Целей записано')),
            (('participants', 'Испытуемых'), ('sessions', 'Сессий')),
            (('frames', 'Кадров с файлами'), ('mean_frames', 'Кадров на цель, ср.')),
            (('rows', 'Строк CSV'), ('excluded', 'Исключённых строк')),
            (('empty_sessions', 'Сессий без кадров'),),
        )
        for row, pairs in enumerate(metrics):
            for pair, (key, label) in enumerate(pairs):
                column = pair * 2
                tk.Label(self.summary, text=label, anchor='w', foreground='#555555').grid(
                    row=row, column=column, sticky='w', padx=(0 if pair == 0 else 18, 8), pady=1)
                variable = self.summary_values[key] = tk.StringVar(self, value='—')
                tk.Label(self.summary, textvariable=variable, anchor='e').grid(
                    row=row, column=column + 1, sticky='e', pady=1)
        self.summary.columnconfigure(1, weight=1)
        self.summary.columnconfigure(3, weight=1)
        self.selection_title.bind('<Configure>', lambda event: self.selection_title.configure(wraplength=max(100, event.width)))
        # Под таблицей остаются две вкладки: записанные данные и симуляция
        notebook = self.notebook = ttk.Notebook(right)
        notebook.pack(fill='both', expand=True)
        map_tab = tk.Frame(notebook)
        map_tab.columnconfigure(0, weight=1)
        map_tab.rowconfigure(1, weight=1)
        notebook.add(map_tab, text='Карта покрытия')
        self.simulation_tab = tk.Frame(notebook)
        notebook.add(self.simulation_tab, text='Симуляция')
        self._build_simulation_ui(self.simulation_tab)
        notebook.bind('<<NotebookTabChanged>>', self._tab_changed)
        controls = tk.Frame(map_tab)
        controls.grid(row=0, column=0, sticky='ew', padx=10, pady=(12, 8))
        self.view_var = tk.StringVar(self, value='Точки')
        self.weight_var = tk.StringVar(self, value='Цели')
        self.grid_var = tk.StringVar(self, value='3 × 3')
        for label, variable, values, width in (
                ('Вид', self.view_var, VIEWS, 14),
                ('Вес', self.weight_var, ('Цели', 'Кадры'), 8),
                ('Сетка', self.grid_var, ('3 × 3', '5 × 5', '10 × 10'), 7)):
            tk.Label(controls, text=label).pack(side='left', padx=(0, 5))
            choice = ttk.Combobox(controls, textvariable=variable, values=values, state='readonly', width=width)
            choice.pack(side='left', padx=(0, 10))
            choice.bind('<<ComboboxSelected>>', self._request_draw)
            if variable is self.weight_var:
                self.weight_choice = choice
            elif variable is self.grid_var:
                self.grid_choice = choice
        self.canvas = tk.Canvas(map_tab, background='white', highlightthickness=0, height=260)
        self.canvas.grid(row=1, column=0, sticky='nsew', padx=10, pady=10)
        self.canvas.bind('<Configure>', self._request_draw)
        self.canvas.bind('<Motion>', self._hover)
        self.canvas.bind('<Leave>', lambda _event: self.zone_label.configure(text=self._zone_summary))
        self.zone_label = tk.Label(map_tab, text='', anchor='w', justify='left', foreground='#555555', height=2)
        self._zone_summary = ''
        self.zone_label.grid(row=2, column=0, sticky='ew', padx=10, pady=(0, 10))
        self.zone_label.bind('<Configure>', lambda event: self.zone_label.configure(wraplength=max(100, event.width)))
        line(5)
        bottom = tk.Frame(frame)
        bottom.grid(row=6, column=0, sticky='ew')
        tk.Button(bottom, text='Назад', command=self.destroy).pack(side='left')
        self.refresh_button = tk.Button(bottom, text='Обновить', command=self.refresh)
        self.refresh_button.pack(side='right')

    # Создаёт параметры виртуальных сессий и область их карты
    def _build_simulation_ui(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        inputs = tk.Frame(tab)
        inputs.grid(row=0, column=0, sticky='ew', padx=10, pady=(12, 8))
        inputs.columnconfigure(6, weight=1)
        self.simulation_mode = tk.StringVar(self, value='total')
        self.simulation_total = tk.StringVar(self, value='5000')
        self.simulation_sessions = tk.StringVar(self, value='5')
        self.simulation_per_session = tk.StringVar(self, value='1000')
        tk.Radiobutton(inputs, text='Всего точек', variable=self.simulation_mode, value='total',
                       command=self._simulation_mode_changed).grid(row=0, column=0, sticky='w')
        self.simulation_total_entry = ttk.Entry(inputs, textvariable=self.simulation_total, width=7)
        self.simulation_total_entry.grid(row=0, column=1, sticky='w', padx=(5, 12), pady=2)
        tk.Radiobutton(inputs, text='Сессий', variable=self.simulation_mode, value='sessions',
                       command=self._simulation_mode_changed).grid(row=0, column=2, sticky='w')
        self.simulation_sessions_entry = ttk.Entry(inputs, textvariable=self.simulation_sessions, width=5)
        self.simulation_sessions_entry.grid(row=0, column=3, sticky='w', padx=(5, 12), pady=2)
        tk.Label(inputs, text='Точек в сессии').grid(row=0, column=4, sticky='w', padx=(0, 5))
        self.simulation_per_session_entry = ttk.Entry(inputs, textvariable=self.simulation_per_session, width=7)
        self.simulation_per_session_entry.grid(row=0, column=5, sticky='w', pady=2)
        controls = tk.Frame(inputs)
        controls.grid(row=1, column=0, columnspan=8, sticky='ew', pady=(8, 0))
        self.simulation_view = tk.StringVar(self, value='Точки')
        self.simulation_grid = tk.StringVar(self, value='3 × 3')
        self.simulation_point_size = tk.StringVar(self, value='2 px')
        for label, variable, values, width in (
                ('Вид', self.simulation_view, VIEWS, 14),
                ('Сетка', self.simulation_grid, ('3 × 3', '5 × 5', '10 × 10'), 7),
                ('Размер цели', self.simulation_point_size, POINT_SIZES, 5)):
            tk.Label(controls, text=label).pack(side='left', padx=(0, 5))
            choice = ttk.Combobox(controls, textvariable=variable, values=values, width=width, state='readonly')
            choice.pack(side='left', padx=(0, 10))
            choice.bind('<<ComboboxSelected>>', self._request_simulation_draw)
            if variable is self.simulation_grid:
                self.simulation_grid_choice = choice
            elif variable is self.simulation_point_size:
                self.simulation_size_choice = choice
        self.simulation_button = tk.Button(inputs, text='Сгенерировать', command=self._generate_simulation)
        self.simulation_button.grid(row=0, column=7, sticky='e', padx=(12, 0))
        for entry in (self.simulation_total_entry, self.simulation_sessions_entry, self.simulation_per_session_entry):
            entry.bind('<Return>', lambda _event: self._generate_simulation())
        self._simulation_mode_changed()
        self.simulation_canvas = tk.Canvas(tab, background='white', highlightthickness=0, height=200)
        self.simulation_canvas.grid(row=1, column=0, sticky='nsew', padx=10, pady=(4, 10))
        self.simulation_canvas.bind('<Configure>', self._request_simulation_draw)
        self.simulation_canvas.bind('<Motion>', self._simulation_hover)
        self.simulation_canvas.bind('<Leave>', lambda _event: self.simulation_zone_label.configure(
            text=self._simulation_zone_summary))
        self.simulation_zone_label = tk.Label(tab, text='', anchor='w', justify='left',
                                              foreground='#555555', height=2)
        self.simulation_zone_label.grid(row=2, column=0, sticky='ew', padx=10, pady=(0, 10))
        self.simulation_zone_label.bind('<Configure>', lambda event: self.simulation_zone_label.configure(
            wraplength=max(100, event.width)))

    # Оставляет активными поля выбранного способа задания количества
    def _simulation_mode_changed(self):
        total = self.simulation_mode.get() == 'total'
        self.simulation_total_entry.configure(state='normal' if total else 'disabled')
        for entry in (self.simulation_sessions_entry, self.simulation_per_session_entry):
            entry.configure(state='disabled' if total else 'normal')

    # Запускает первый расчёт симуляции при открытии её вкладки
    def _tab_changed(self, _event=None):
        if self.notebook.select() == str(self.simulation_tab):
            if not self._simulation_started:
                self._generate_simulation()
            else:
                self._request_simulation_draw()

    # Проверяет объём и запускает генерацию в фоновом потоке
    def _generate_simulation(self):
        try:
            _, sessions, per_session = simulation_size(
                self.simulation_mode.get(), self.simulation_total.get(),
                self.simulation_sessions.get(), self.simulation_per_session.get())
        except ValueError as error:
            messagebox.showerror('Параметры симуляции', str(error), parent=self)
            return
        resolutions = self._stats['resolutions']
        # Для симуляции берём размер окна из выбранных данных, иначе рассчитываем без камеры
        if resolutions:
            reference = resolutions.most_common(1)[0][0]
        else:
            width = max(2, int(self.winfo_screenwidth() * self._simulation_config['window']['width_percent']))
            reference = (width, max(2, int(width / (1280 / 720))))
        margin = self._simulation_config['distribution']['margin_ratio']
        # Новый расчёт отменяет предыдущую генерацию
        self._simulation_cancel.set()
        self._simulation_cancel = threading.Event()
        self._simulation_generation += 1
        generation, cancel = self._simulation_generation, self._simulation_cancel
        self._simulation_started = True
        self.simulation_button.configure(state='disabled', text='Генерация…')

        def worker():
            try:
                targets = simulate_targets(sessions, per_session, *reference, margin, cancel=cancel)
                # Поток передаёт готовые координаты через очередь, элементы Tk он не меняет
                self._simulation_messages.put((generation, 'done', (targets, reference)))
            except ScanCancelled:
                pass
            except Exception as error:
                self._simulation_messages.put((generation, 'error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    # Применяет результат последнего расчёта в основном потоке
    def _poll_simulation(self):
        while True:
            try:
                generation, kind, value = self._simulation_messages.get_nowait()
            except queue.Empty:
                break
            # Применяется только последний запрошенный расчёт
            if generation != self._simulation_generation:
                continue
            self.simulation_button.configure(state='normal', text='Сгенерировать')
            if kind == 'done':
                self._simulation_targets, self._simulation_reference = value
                self._request_simulation_draw()
            else:
                messagebox.showerror('Ошибка генерации', value, parent=self)

    # Объединяет частые запросы перерисовки симуляции
    def _request_simulation_draw(self, _event=None):
        if self._simulation_redraw_id is not None:
            self.after_cancel(self._simulation_redraw_id)
        self._simulation_redraw_id = self.after(100, self._draw_simulation)

    # Перерисовывает выбранный вид, не меняя сгенерированные координаты
    def _draw_simulation(self):
        if self._simulation_redraw_id is not None:
            self.after_cancel(self._simulation_redraw_id)
        self._simulation_redraw_id = None
        view = self.simulation_view.get()
        self.simulation_size_choice.configure(state='readonly' if view == 'Точки' else 'disabled')
        self.simulation_grid_choice.configure(state='disabled' if view in VIEWS[-2:] else 'readonly')
        self._simulation_photo, self._simulation_plot, self._simulation_zone_summary = self._paint_map(
            self.simulation_canvas, self._simulation_targets, self._simulation_reference,
            int(self.simulation_grid.get().split()[0]), view=view,
            point_size=int(self.simulation_point_size.get().split()[0]),
            color=(48, 140, 78), empty_text='Нет сгенерированных точек')
        self.simulation_zone_label.configure(text=self._simulation_zone_summary)

    # Показывает число целей под курсором
    def _simulation_hover(self, event):
        self._show_zone(event, self._simulation_plot, self._simulation_zone_summary,
                        self.simulation_zone_label, 'целей')

    # Меняет корень просмотра без изменения настроек сбора
    def _browse_root(self):
        selected = filedialog.askdirectory(parent=self, initialdir=str(self.output if self.output.is_dir() else BASE_DIR),
                                           title='Папка с датасетами')
        if selected:
            self.output = Path(selected).resolve()
            self.folder_var.set(str(self.output))
            self.extra_datasets = []
            self.refresh()

    # Отменяет прежнее чтение и запускает новое сканирование
    def refresh(self):
        selection = self.tree.selection()
        selected_path = self._nodes[selection[0]][2] if selection and selection[0] in self._nodes else None
        # После перечитывания стараемся вернуть выбор к той же папке
        self._restore_path = selected_path
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        generation, cancel = self._generation, self._cancel
        output, extras = self.output, tuple(self.extra_datasets)
        self.library_title.configure(text='Чтение данных…')
        self.refresh_button.configure(state='disabled')

        def worker():
            last_progress = 0
            def progress(value):
                nonlocal last_progress
                if time.monotonic() - last_progress >= 0.15:
                    self._messages.put((generation, 'progress', value))
                    last_progress = time.monotonic()
            try:
                datasets = scan_library(output, extras, cancel=cancel, progress=progress)
                self._messages.put((generation, 'done', datasets))
            except ScanCancelled:
                pass
            except Exception as error:
                self._messages.put((generation, 'error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    # Обрабатывает сообщения сканирования и симуляции в потоке интерфейса
    def _poll(self):
        while True:
            try:
                generation, kind, value = self._messages.get_nowait()
            except queue.Empty:
                break
            # Результаты отменённого или устаревшего чтения не применяются
            if generation != self._generation:
                continue
            if kind == 'progress':
                self.library_title.configure(text='Чтение данных…')
            else:
                self.refresh_button.configure(state='normal')
                if kind == 'done':
                    self._populate(value)
                else:
                    self._populate([])
                    self.library_title.configure(text='Ошибка чтения данных')
                    messagebox.showerror('Ошибка чтения данных', value, parent=self)
        self._poll_simulation()
        self._poll_id = self.after(100, self._poll)

    # Заполняет дерево и восстанавливает выбранную папку
    def _populate(self, datasets):
        self.tree.delete(*self.tree.get_children())
        self._nodes.clear()
        selected = None
        # Добавляет узел дерева с суммарными целями и кадрами
        def insert(parent, label, sessions, path, title):
            nonlocal selected
            stats = aggregate(sessions)
            iid = self.tree.insert(parent, 'end', text=label, open=True,
                                   values=(number(stats['points']), number(stats['frames'])))
            self._nodes[iid] = (sessions, title, path)
            if path == self._restore_path:
                selected = iid
            return iid
        # Строим дерево от датасета к испытуемому и отдельной сессии
        for dataset in datasets:
            dataset_id = insert('', dataset.name, dataset.sessions, dataset.path, dataset.name)
            participants = defaultdict(list)
            for session in dataset.sessions:
                participants[session.participant].append(session)
            for participant, sessions in participants.items():
                title = f'{dataset.name} / {participant}'
                person_id = insert(dataset_id, participant, sessions, sessions[0].path.parent, title)
                for session in sessions:
                    insert(person_id, session.path.name, [session], session.path, f'{title} / {session.path.name}')
        roots = self.tree.get_children()
        # Выбираем прежнюю папку или первый датасет; пустая папка очищает таблицу и карту
        if roots:
            self.tree.selection_set(selected or roots[0])
            self.tree.see(selected or roots[0])
            self._select()
        else:
            self._stats = aggregate([])
            self.selection_title.configure(text='Датасеты не найдены')
            for variable in self.summary_values.values():
                variable.set('—')
            self._request_draw()
        sessions = [session for dataset in datasets for session in dataset.sessions]
        self.library_title.configure(text=f'Датасетов: {len(datasets)}; Сессий: {len(sessions)}')

    # Обновляет таблицу и карту для выбранного датасета, испытуемого или сессии
    def _select(self, _event=None):
        selection = self.tree.selection()
        if not selection or selection[0] not in self._nodes:
            return
        sessions, title, _path = self._nodes[selection[0]]
        stats = self._stats = aggregate(sessions)
        self.selection_title.configure(text=title)
        # Показываем среднее отдельно, остальные значения — целыми числами
        for key, variable in self.summary_values.items():
            if key == 'mean_frames':
                value = f"{stats[key]:.1f}" if stats['points'] else '—'
            elif key == 'planned' and not stats['planned_sessions']:
                value = '—'
            # При неполных снимках запуска сумма плана отмечается как частичная
            elif key == 'planned' and stats['planned_sessions'] < stats['sessions']:
                value = f"{number(stats[key])} (частично)"
            else:
                value = number(stats[key])
            variable.set(value)
        self._request_draw()

    # Откладывает перерисовку до завершения серии изменений размера
    def _request_draw(self, _event=None):
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
        self._redraw_id = self.after(100, self._draw)

    # Выбирает данные и параметры отображения фактической карты
    def _draw(self):
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
        self._redraw_id = None
        grid = int(self.grid_var.get().split()[0])
        targets = self._stats['targets']
        view = self.view_var.get()
        by_frames = self.weight_var.get() == 'Кадры' and view != 'Тепловая карта'
        self.weight_choice.configure(state='disabled' if view == 'Тепловая карта' else 'readonly')
        self.grid_choice.configure(state='disabled' if view in VIEWS[-2:] else 'readonly')
        reference = self._stats['resolutions'].most_common(1)[0][0] if targets else (1280, 720)
        self._photo, self._plot, self._zone_summary = self._paint_map(
            self.canvas, targets, reference, grid, view=view, by_frames=by_frames,
            empty_text='Нет записанных целей с файлами')
        if not targets:
            self._zone_summary = 'Карта строится только по фактически сохранённым данным.'
        self.zone_label.configure(text=self._zone_summary)

    # Рисует выбранный вид и сохраняет границы для подсказки под курсором
    def _paint_map(self, canvas, targets, reference, grid, *, view='Точки', by_frames=False,
                   color=(67, 116, 159), point_size=2, empty_text=''):
        canvas.delete('all')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not targets:
            canvas.create_text(width / 2, height / 2, text=empty_text, fill='#666666', font=('Helvetica', 13))
            return None, None, ''
        if width < 100 or height < 80:
            return None, None, ''
        if view in VIEWS[-2:]:
            axis = 'x' if view == 'По горизонтали' else 'y'
            return self._paint_histogram(canvas, targets, axis, by_frames=by_frames, color=color)
        density = view == 'Плотность'
        heatmap = view == 'Тепловая карта'
        # Тепловая карта всегда учитывает цели, независимо от выбранного веса
        if heatmap:
            by_frames = False
        # Сохраняем пропорции окна сбора при изменении размера карты
        aspect = reference[0] / reference[1]
        available_w, available_h = width - (90 if heatmap else 55), height - 40
        plot_w = max(1, int(min(available_w, available_h * aspect)))
        plot_h = max(1, int(plot_w / aspect))
        x0, y0 = (width - plot_w) // 2 + 10, (height - plot_h) // 2 - 8
        image, counts = render_coverage(targets, plot_w, plot_h, grid, density=density,
                                        by_frames=by_frames, color=color, point_size=point_size, heatmap=heatmap)
        # Ссылку на изображение храним до следующей перерисовки, иначе Tk его удалит
        photo = ImageTk.PhotoImage(image, master=self)
        canvas.create_image(x0, y0, image=photo, anchor='nw')
        for value in (0, 0.5, 1):
            canvas.create_text(x0 + value * (plot_w - 1), y0 + plot_h + 12, text=f'{value:g}',
                               font=('Helvetica', 11), fill='#666666')
            canvas.create_text(x0 - 12, y0 + value * (plot_h - 1), text=f'{value:g}',
                               font=('Helvetica', 11), fill='#666666')
        flat = [count for row in counts for count in row]
        summary = (f'Зоны {grid} × {grid}; заполнено: {sum(value > 0 for value in flat)}/{grid * grid}'
                   f'; мин.: {number(min(flat))}; макс.: {number(max(flat))}')
        if heatmap:
            summary = 'Цели; ' + summary
            bar_x = x0 + plot_w + 10
            # Цветовая шкала показывает плотность относительно максимума этой карты
            for index in range(40):
                shade = '#%02x%02x%02x' % tuple(heat_colors(1 - index / 39))
                canvas.create_rectangle(bar_x, y0 + index * plot_h / 40,
                                        bar_x + 8, y0 + (index + 1) * plot_h / 40,
                                        fill=shade, outline='')
            canvas.create_text(bar_x + 4, y0 - 8, text='1', font=('Helvetica', 10), fill='#666666')
            canvas.create_text(bar_x + 4, y0 + plot_h + 8, text='0', font=('Helvetica', 10), fill='#666666')
        plot = (x0, y0, plot_w, plot_h, counts)
        if density:
            maximum = max(flat)
            for row in range(grid):
                for col in range(grid):
                    count = counts[row][col]
                    # Числа рисуем только в достаточно крупных ячейках; при наведении они доступны всегда
                    if plot_w / grid < max(25, len(number(count)) * 7) or plot_h / grid < 15:
                        continue
                    canvas.create_text(x0 + (col + 0.5) * plot_w / grid, y0 + (row + 0.5) * plot_h / grid,
                                       text=number(count), font=('Helvetica', 11),
                                       fill='white' if maximum and count / maximum > 0.45 else '#333333')
        return photo, plot, summary

    # Показывает распределение по оси и среднее количество на интервал
    def _paint_histogram(self, canvas, targets, axis, *, by_frames=False, color=(67, 116, 159)):
        counts = axis_counts(targets, axis, by_frames=by_frames)
        total, maximum = sum(counts), max(counts)
        width, height = canvas.winfo_width(), canvas.winfo_height()
        x0, y0, plot_w, plot_h = 55, 25, max(1, width - 75), max(1, height - 65)
        scale = maximum or 1
        shade = '#%02x%02x%02x' % color
        for value in (0, scale / 2, scale):
            y = y0 + plot_h * (1 - value / scale)
            canvas.create_line(x0, y, x0 + plot_w, y, fill='#e2e4e6')
            canvas.create_text(x0 - 8, y, text=f'{value:g}', anchor='e',
                               font=('Helvetica', 11), fill='#666666')
        # Высота столбца показывает количество целей или кадров в интервале
        for index, count in enumerate(counts):
            left = x0 + index * plot_w / len(counts) + 1
            right = x0 + (index + 1) * plot_w / len(counts) - 1
            top = y0 + plot_h * (1 - count / scale)
            if count:
                canvas.create_rectangle(left, top, right, y0 + plot_h, fill=shade, outline='')
        # Пунктиром отмечаем среднее количество на один интервал
        mean = total / len(counts)
        mean_y = y0 + plot_h * (1 - mean / scale)
        canvas.create_line(x0, mean_y, x0 + plot_w, mean_y, fill='#555555', dash=(4, 4))
        canvas.create_text(x0 + plot_w - 3, mean_y - 8, text='Среднее', anchor='e',
                           font=('Helvetica', 10), fill='#555555')
        canvas.create_line(x0, y0, x0, y0 + plot_h, x0 + plot_w, y0 + plot_h, fill='#888888')
        for value in (0, 0.25, 0.5, 0.75, 1):
            canvas.create_text(x0 + value * plot_w, y0 + plot_h + 12, text=f'{value:g}',
                               font=('Helvetica', 11), fill='#666666')
        unit = 'Кадров' if by_frames else 'Целей'
        canvas.create_text(x0, 10, text=unit, anchor='w', font=('Helvetica', 11), fill='#666666')
        caption = 'X: слева направо' if axis == 'x' else 'Y: сверху вниз'
        canvas.create_text(x0 + plot_w / 2, y0 + plot_h + 30, text=caption,
                           font=('Helvetica', 11), fill='#666666')
        summary = (f'{unit}: {number(total)}; интервалов: {len(counts)}; '
                   f'мин.: {number(min(counts))}; макс.: {number(maximum)}')
        plot = {'axis': axis, 'bounds': (x0, y0, plot_w, plot_h), 'counts': counts}
        return None, plot, summary

    # Выбирает единицы подсказки для текущего вида
    def _hover(self, event):
        unit = 'кадров' if self.weight_var.get() == 'Кадры' and self.view_var.get() != 'Тепловая карта' else 'целей'
        self._show_zone(event, self._plot, self._zone_summary, self.zone_label, unit)

    # Показывает точное количество и долю для зоны или интервала
    def _show_zone(self, event, plot, summary, label, unit):
        if plot is None:
            return
        # Гистограмма определяет интервал по X курсора, карта — ячейку сетки
        if isinstance(plot, dict):
            x, y, width, height = plot['bounds']
            if x <= event.x < x + width and y <= event.y < y + height:
                counts = plot['counts']
                index = min(len(counts) - 1, int((event.x - x) / width * len(counts)))
                count, total = counts[index], sum(counts)
                share = count / total * 100 if total else 0
                label.configure(text=f"{summary}\n{plot['axis'].upper()}: {index / len(counts):.2f}–"
                                f"{(index + 1) / len(counts):.2f}: {number(count)} {unit} ({share:.1f}%)")
            else:
                label.configure(text=summary)
            return
        x, y, width, height, counts = plot
        if x <= event.x < x + width and y <= event.y < y + height:
            size = len(counts)
            col = min(size - 1, int((event.x - x) / width * size))
            row = min(size - 1, int((event.y - y) / height * size))
            label.configure(text=f'{summary}\nЗона {row + 1}, {col + 1}: {number(counts[row][col])} {unit}')
        else:
            label.configure(text=summary)

    # Отменяет фоновые задачи и отложенные вызовы перед закрытием окна
    def destroy(self):
        self._cancel.set()
        self._simulation_cancel.set()
        for after_id in (self._poll_id, self._redraw_id, self._simulation_redraw_id):
            if after_id is not None:
                self.after_cancel(after_id)
        super().destroy()
