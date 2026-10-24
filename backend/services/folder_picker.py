"""Native folder picker shared by Settings and Open Folder.

Runs off the request thread. The dialog blocks until the user closes it.
On macOS, tkinter from a non-main thread is unreliable, so Darwin uses
AppleScript. Linux falls back to tkinter.
"""
import platform
import subprocess


def open_directory_dialog(result: dict, prompt: str = "Select a folder") -> None:
    """Fill result['path'] or result['error']. Never raises to the caller thread."""
    try:
        if platform.system() == "Darwin":
            proc = subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'POSIX path of (choose folder with prompt "{prompt}")',
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            stdout = (proc.stdout or "").strip().rstrip("/")
            stderr = (proc.stderr or "").strip()
            if proc.returncode != 0:
                if "User canceled" in stderr or "user cancelled" in stderr.lower() or not stderr:
                    result["path"] = ""
                else:
                    result["error"] = stderr or f"osascript failed ({proc.returncode})"
                return
            result["path"] = stdout
            return

        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        result["root"] = root
        path = filedialog.askdirectory(title=prompt)
        root.destroy()
        result["path"] = path or ""
    except Exception as exc:
        result["error"] = str(exc)
