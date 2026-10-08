#!/usr/bin/env python3
# Главное меню и запуск сбора в отдельном процессе
from copy import deepcopy
import multiprocessing as mp
import tkinter as tk
from tkinter import messagebox
from configuration import (BASE_DIR, DEFAULT_CONFIG, load_config, save_config,
                           collection_config, protocol_summary)
from ui import center_window, separator, setup_style, show_help


# Выполняет сбор в дочернем процессе и передаёт ошибку главному окну
def _collection_worker(config, connection):
    try:
        # Сборщик загружается в дочернем процессе, когда начинается запись
        from collector import run_collection
        run_collection(config)
    except Exception as error:
        connection.send(f"{type(error).__name__}: {error}")
        raise
    finally:
        connection.close()


# Главное окно и переходы к настройкам, датасетам и сбору
class MainApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("GazeDataSeter")
        setup_style(self.root)
        self.config_error = None
        try:
            # При запуске восстанавливаем последние сохранённые параметры
            self.config = load_config()
        except (OSError, ValueError) as error:
            # Если файл не читается, показываем стандартные значения и предупреждение
            self.config = deepcopy(DEFAULT_CONFIG)
            self.config_error = str(error)
        # Состояние запущенной сессии и открытого окна настроек
        self._process = None
        self._settings_open = False
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self._build_ui()
        self._refresh_summary()
        center_window(self.root, 540, 580, minimum=(460, 580))
        if self.config_error:
            self.root.after(100, lambda: messagebox.showwarning(
                "Не удалось прочитать настройки",
                f"{self.config_error}\n\nПоказаны стандартные значения. Проверьте настройки перед запуском.",
                parent=self.root))

    # Создаёт элементы главного меню
    def _build_ui(self):
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=25, pady=25)
        from PIL import Image, ImageTk
        with Image.open(BASE_DIR / "assets" / "icon.png") as image:
            self._icon = ImageTk.PhotoImage(image.resize((88, 88)))
        tk.Label(frame, image=self._icon).pack()
        tk.Label(frame, text="Система сбора датасетов 2D Gaze Estimation", font=("Helvetica", 18)).pack(pady=(15, 5))
        separator(frame)
        tk.Button(frame, text="Настройки участника", command=self._open_personal).pack(fill="x", pady=5)
        tk.Button(frame, text="Настройки сбора", command=self._open_algorithm).pack(fill="x", pady=5)
        tk.Button(frame, text="Датасеты", command=self._open_datasets).pack(fill="x", pady=5)
        separator(frame)
        # Сводка показывает параметры, с которыми начнётся следующая сессия
        self.summary = tk.Label(frame, text="", justify="left", anchor="w", foreground="#555555")
        self.summary.pack(fill="x", pady=(0, 10))
        self.summary.bind("<Configure>", lambda event: self.summary.configure(wraplength=max(100, event.width)))
        tk.Button(frame, text="Пуск", command=self._start_collection).pack(fill="x", pady=5)
        separator(frame)
        bottom = tk.Frame(frame)
        bottom.pack(fill="x")
        tk.Button(bottom, text="Завершить работу", command=self.root.destroy).pack(side="left")
        tk.Button(bottom, text="Справка", command=lambda: show_help(self.root)).pack(side="right")

    # Обновляет сводку по текущим настройкам
    def _refresh_summary(self):
        personal = self.config["personal"]
        stimulus = self.config["stimulus"]
        self.summary.configure(text=(
            f"Датасет: {personal['dataset']}\n"
            f"Испытуемый: {personal['participant'] or 'не указан'}\n"
            f"Камера: {personal['camera_index']}\nКоличество целей: {stimulus['total_points']} шт\n"
            f"Показ: {stimulus['dwell_ms']} мс\n"
            f"{protocol_summary(self.config)}"))

    # Сначала сохраняет настройки, затем обновляет главное окно
    def _apply_settings(self, updated):
        save_config(updated)
        self.config = updated
        self._refresh_summary()

    # Открывает настройки участника с сохранением выбранных значений
    def _open_personal(self):
        from settings import PersonalSettingsDialog
        # Меняем только данные участника, остальные параметры сохраняем
        def save_personal(values):
            updated = deepcopy(self.config)
            updated["personal"] = values
            self._apply_settings(updated)
        self._open_settings(PersonalSettingsDialog, self.config["personal"], save_personal)

    # Открывает параметры сбора
    def _open_algorithm(self):
        from settings import AlgorithmSettingsDialog
        self._open_settings(AlgorithmSettingsDialog, self.config, self._apply_settings)

    # Открывает просмотр датасетов; повторное открытие блокируется
    def _open_datasets(self):
        from dataset_browser import DatasetBrowser
        if self._settings_open:
            return
        self._settings_open = True
        try:
            DatasetBrowser(self.root, self.config["personal"], collection_settings=self.config)
        finally:
            self._settings_open = False

    # Передаёт управление одному окну настроек
    def _open_settings(self, dialog_class, config, on_save):
        if self._settings_open:
            return None
        self._settings_open = True
        try:
            return dialog_class(self.root, config, on_save=on_save)
        finally:
            self._settings_open = False

    # Проверяет настройки и запускает отдельный процесс сбора
    def _start_collection(self):
        try:
            config = collection_config(self.config)
            save_config(self.config)
        except (ValueError, OSError) as error:
            messagebox.showwarning("Проверьте настройки", str(error), parent=self.root)
            return
        # На всех системах создаётся новый процесс, без наследования окон и камеры
        context = mp.get_context("spawn")
        # По этому соединению дочерний процесс передаёт сообщение об ошибке
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_collection_worker, args=(config, send), daemon=False)
        try:
            process.start()
        except Exception as error:
            receive.close()
            send.close()
            messagebox.showerror("Не удалось начать сбор", str(error), parent=self.root)
            return
        send.close()
        self._process = process
        # На время сбора главное меню скрывается
        self.root.withdraw()
        self.root.after(300, lambda: self._poll_collection(process, receive))

    # Ждёт завершения процесса и возвращает главное окно
    def _poll_collection(self, process, connection):
        if process.is_alive():
            self.root.after(300, lambda: self._poll_collection(process, connection))
            return
        process.join()
        detail = None
        try:
            # Читаем причину ошибки, если сборщик успел её отправить
            if connection.poll():
                detail = connection.recv()
        except EOFError:
            pass
        finally:
            connection.close()
        self._process = None
        self.root.deiconify()
        self.root.lift()
        if detail or process.exitcode not in (0, -2, -15):
            messagebox.showerror("Сбор завершён с ошибкой",
                                 detail or f"Код завершения: {process.exitcode}", parent=self.root)

    # Запускает обработку событий интерфейса
    def run(self):
        self.root.mainloop()


# Поддержка запуска дочернего процесса и сборок для Windows
if __name__ == "__main__":
    mp.freeze_support()
    MainApp().run()
