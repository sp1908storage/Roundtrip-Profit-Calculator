from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

from cost_bot.desktop_dialog import DesktopDialogSession, parse_yes_no


LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo_es_trans.png"

COLORS = {
    "red": "#d71920",
    "red_dark": "#a91218",
    "ink": "#20242a",
    "graphite": "#2b3138",
    "line": "#d9dee5",
    "muted": "#66717f",
    "paper": "#ffffff",
    "soft": "#f3f5f8",
    "logo_bg": "#eef1f4",
}


class DesktopCalculatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Roundtrip Profit Calculator")
        self.root.geometry("980x720")
        self.root.minsize(820, 560)
        self.root.configure(bg=COLORS["soft"])

        self.session = DesktopDialogSession()
        self.logo_image = None
        self._build_ui()
        self._add_bot_message(self.session.start())

    def _build_ui(self) -> None:
        self.header = tk.Frame(self.root, bg=COLORS["logo_bg"], height=86)
        self.header.pack(fill=tk.X)
        self.header.pack_propagate(False)

        brand_block = tk.Frame(self.header, bg=COLORS["logo_bg"])
        brand_block.pack(side=tk.LEFT, fill=tk.Y, padx=(18, 0), pady=0)
        if LOGO_PATH.exists():
            self.logo_image = tk.PhotoImage(file=str(LOGO_PATH)).subsample(6, 6)
            tk.Label(
                brand_block,
                image=self.logo_image,
                bg=COLORS["logo_bg"],
                bd=0,
            ).pack(anchor="w", padx=18, pady=(11, 0))
        else:
            tk.Label(
                brand_block,
                text="ЕС Транс",
                bg=COLORS["logo_bg"],
                fg=COLORS["graphite"],
                font=("Segoe UI Semibold", 20),
            ).pack(anchor="w", padx=18, pady=(11, 0))
        tk.Label(
            brand_block,
            text="Калькулятор себестоимости кругорейса",
            bg=COLORS["logo_bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=18, pady=(2, 0))

        status_block = tk.Frame(self.header, bg=COLORS["logo_bg"])
        status_block.pack(side=tk.RIGHT, padx=22)
        tk.Label(
            status_block,
            text="Локальная отладка",
            bg=COLORS["red"],
            fg=COLORS["paper"],
            font=("Segoe UI Semibold", 10),
            padx=12,
            pady=6,
        ).pack(anchor="e")
        tk.Label(
            status_block,
            text="Расчет + Google Sheets",
            bg=COLORS["logo_bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="e", pady=(6, 0))

        body = tk.Frame(self.root, bg=COLORS["soft"])
        body.pack(fill=tk.BOTH, expand=True, padx=18, pady=18)

        self.transcript = ScrolledText(
            body,
            wrap=tk.WORD,
            height=28,
            font=("Segoe UI", 10),
            bg=COLORS["paper"],
            fg=COLORS["ink"],
            relief=tk.FLAT,
            bd=0,
            padx=14,
            pady=14,
            insertbackground=COLORS["red"],
        )
        self.transcript.pack(fill=tk.BOTH, expand=True)
        self.transcript.configure(state=tk.DISABLED)
        self.transcript.tag_configure("speaker_bot", foreground=COLORS["red"], font=("Segoe UI Semibold", 10))
        self.transcript.tag_configure("speaker_user", foreground=COLORS["graphite"], font=("Segoe UI Semibold", 10))
        self.transcript.tag_configure("message", foreground=COLORS["ink"], spacing3=10)
        self.transcript.tag_configure("divider", foreground=COLORS["line"])

        controls = tk.Frame(self.root, bg=COLORS["soft"])
        controls.pack(fill=tk.X, padx=18, pady=(0, 10))
        self._button(controls, "Да", lambda: self._send_quick("да"), primary=False, width=9).pack(side=tk.LEFT)
        self._button(controls, "Нет", lambda: self._send_quick("нет"), primary=False, width=9).pack(side=tk.LEFT, padx=8)
        self._button(controls, "Новый расчет", self._reset, primary=False, width=15).pack(side=tk.RIGHT)

        input_frame = tk.Frame(self.root, bg=COLORS["soft"])
        input_frame.pack(fill=tk.X, padx=18, pady=(0, 18))
        self.entry = tk.Entry(
            input_frame,
            font=("Segoe UI", 11),
            bg=COLORS["paper"],
            fg=COLORS["ink"],
            insertbackground=COLORS["red"],
            relief=tk.SOLID,
            bd=1,
        )
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=9)
        self.entry.bind("<Return>", lambda _event: self._send())
        self._button(input_frame, "Отправить", self._send, primary=True, width=13).pack(side=tk.RIGHT, padx=(10, 0))
        self.entry.focus_set()

        footer = tk.Frame(self.root, bg=COLORS["graphite"], height=26)
        footer.pack(fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(
            footer,
            text="ЕС Транс • Надежность вашего бизнеса",
            bg=COLORS["graphite"],
            fg="#cbd2da",
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=18)

    def _button(self, parent: tk.Widget, text: str, command, primary: bool, width: int) -> tk.Button:
        bg = COLORS["red"] if primary else COLORS["paper"]
        fg = COLORS["paper"] if primary else COLORS["graphite"]
        active_bg = COLORS["red_dark"] if primary else "#edf0f4"
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=9,
            font=("Segoe UI Semibold", 10),
            cursor="hand2",
        )

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
        self._append("Вы:\n", "speaker_user")
        self._append(f"{message}\n", "message")
        self._append("─" * 82 + "\n\n", "divider")

    def _add_bot_message(self, message: str) -> None:
        self._append("Калькулятор:\n", "speaker_bot")
        self._append(f"{message}\n\n", "message")

    def _append(self, text: str, tag: str | None = None) -> None:
        self.transcript.configure(state=tk.NORMAL)
        self.transcript.insert(tk.END, text, tag)
        self.transcript.see(tk.END)
        self.transcript.configure(state=tk.DISABLED)


def main() -> None:
    root = tk.Tk()
    DesktopCalculatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
