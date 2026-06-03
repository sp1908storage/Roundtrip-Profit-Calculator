import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

from cost_bot.desktop_dialog import DesktopDialogSession, parse_yes_no


class DesktopCalculatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Roundtrip Profit Calculator")
        self.root.geometry("900x650")
        self.root.minsize(720, 520)

        self.session = DesktopDialogSession()
        self._build_ui()
        self._add_bot_message(self.session.start())

    def _build_ui(self) -> None:
        self.transcript = ScrolledText(self.root, wrap=tk.WORD, height=28, font=("Segoe UI", 10))
        self.transcript.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 8))
        self.transcript.configure(state=tk.DISABLED)

        quick_frame = tk.Frame(self.root)
        quick_frame.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Button(quick_frame, text="Да", width=10, command=lambda: self._send_quick("да")).pack(side=tk.LEFT)
        tk.Button(quick_frame, text="Нет", width=10, command=lambda: self._send_quick("нет")).pack(side=tk.LEFT, padx=6)
        tk.Button(quick_frame, text="Новый расчет", command=self._reset).pack(side=tk.RIGHT)

        input_frame = tk.Frame(self.root)
        input_frame.pack(fill=tk.X, padx=12, pady=(0, 12))
        self.entry = tk.Entry(input_frame, font=("Segoe UI", 11))
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", lambda _event: self._send())
        tk.Button(input_frame, text="Отправить", command=self._send).pack(side=tk.RIGHT, padx=(8, 0))
        self.entry.focus_set()

    def _send_quick(self, text: str) -> None:
        self.entry.delete(0, tk.END)
        self.entry.insert(0, text)
        self._send()

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text and not self.session.current_prompt:
            return
        self.entry.delete(0, tk.END)
        self._add_user_message(text or "(пусто)")

        try:
            if self.session.stage in {"another_forward", "has_backhaul", "another_backhaul"}:
                messages = self.session.handle_control_prompt(parse_yes_no(text, default=False))
            else:
                messages = self.session.handle(text)
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return

        for message in messages:
            self._add_bot_message(message)

    def _reset(self) -> None:
        self.session = DesktopDialogSession()
        self.transcript.configure(state=tk.NORMAL)
        self.transcript.delete("1.0", tk.END)
        self.transcript.configure(state=tk.DISABLED)
        self._add_bot_message(self.session.start())
        self.entry.focus_set()

    def _add_user_message(self, message: str) -> None:
        self._append(f"Вы:\n{message}\n\n")

    def _add_bot_message(self, message: str) -> None:
        self._append(f"Калькулятор:\n{message}\n\n")

    def _append(self, text: str) -> None:
        self.transcript.configure(state=tk.NORMAL)
        self.transcript.insert(tk.END, text)
        self.transcript.see(tk.END)
        self.transcript.configure(state=tk.DISABLED)


def main() -> None:
    root = tk.Tk()
    DesktopCalculatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

