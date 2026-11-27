"""Local VirtualBox console for a Windows host. Python 3.10+."""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def executable():
    for key in ("VBOX_MSI_INSTALL_PATH", "VBOX_INSTALL_PATH", "ProgramW6432", "ProgramFiles"):
        if root := os.environ.get(key):
            folder = Path(root)
            if key.startswith("Program"):
                folder = folder / "Oracle" / "VirtualBox"
            candidate = folder / "VBoxManage.exe"
            if candidate.is_file():
                return str(candidate)
    if candidate := shutil.which("VBoxManage.exe"):
        return candidate
    raise ValueError("Windows не нашла VBoxManage.exe. Проверьте установку VirtualBox.")


def call(*arguments, data=b"", timeout=20, checked=True):
    result = subprocess.run([executable(), *arguments], input=data, capture_output=True,
                            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    if checked and result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or
                         f"VBoxManage завершился с кодом {result.returncode}")
    return result


def inventory(running=False):
    raw = call("list", "runningvms" if running else "vms").stdout.decode("utf-8")
    return {match[2].lower(): match[1] for match in
            re.finditer(r'^"(.*)" \{([0-9a-fA-F-]{36})\}\r?$', raw, re.MULTILINE)}


def machine(selector):
    machines = inventory()
    uuid = selector.strip("{}").lower()
    if uuid not in machines:
        uuid = next((key for key, name in machines.items() if name == selector), None)
    if uuid is None:
        raise ValueError(f"ВМ «{selector}» не найдена. Проверьте имя через list.")
    raw = call("showvminfo", uuid, "--machinereadable").stdout.decode("utf-8")
    properties = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def value(key):
        return properties.get(key, "").strip('"')

    return dict(name=machines[uuid], uuid=uuid, state=value("VMState"), os=value("ostype"),
                additions=value("GuestAdditionsVersion"), additions_level=value("GuestAdditionsRunLevel"))


def read_script(path):
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip() or "\x00" in text:
        raise ValueError("Нужен непустой текстовый файл UTF-8 без нулевых байтов.")
    return text


def capture(uuid, path):
    path = path.resolve()
    if path.suffix.lower() != ".png":
        raise ValueError("Укажите путь с расширением .png.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
        temp = Path(scratch) / "screen.png"
        call("controlvm", uuid, "screenshotpng", str(temp), "0")
        with temp.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                raise ValueError("VirtualBox не вернул изображение PNG.")
        temp.replace(path)
    return str(path)


def type_line(uuid, path):
    line = read_script(path).removesuffix("\n")
    if any(not 32 <= ord(char) <= 126 for char in line):
        raise ValueError("Для клавиатуры нужна одна строка печатного ASCII и раскладка English (US) в ВМ. Для Unicode используйте run.")
    if len(line) > 1024:
        raise ValueError("Для строки длиннее 1024 символов используйте run.")
    # keyboardputfile bypasses host argument quoting and never appends Enter.
    with tempfile.TemporaryDirectory() as scratch:
        payload = Path(scratch) / "line.txt"
        payload.write_bytes(line.encode("ascii"))
        call("controlvm", uuid, "keyboardputfile", str(payload), timeout=90)


def guest_args(shell, script):
    if shell == "bash":
        return ["/bin/bash", "-c", script]
    # Windows PowerShell accepts UTF-16LE for -EncodedCommand on all supported versions.
    script = "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoLogo",
            "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def guest_run(info, opts):
    import guest_credentials
    if not info["additions_level"].isdigit() or int(info["additions_level"]) < 2:
        raise ValueError("Guest Additions пока не сообщают о готовности службы. Проверьте info или используйте screen и type.")
    shell = opts.shell or ("powershell" if "windows" in info["os"].lower() else "bash")
    exe, *args = guest_args(shell, read_script(opts.file))
    command = ["guestcontrol", info["uuid"], "run", "--username", opts.user,
               "--passwordfile", "stdin", "--wait-stdout", "--wait-stderr",
               "--timeout", str(opts.timeout * 1000), "--exe", exe, "--", *args]
    if len(subprocess.list2cmdline([executable(), *command]).encode("utf-16-le")) >= 65534:
        raise ValueError("Команда превышает предел Windows. Разбейте сценарий на части.")
    secret = None if opts.ask or opts.remember else guest_credentials.saved(info["uuid"], opts.user)
    remember = False
    if secret is None:
        secret, remember = guest_credentials.ask(info, opts.user, opts.remember)
    result = call(*command, data=secret.encode("utf-8"),
                  timeout=opts.timeout + 20, checked=False)
    output = {"vbox_exit": result.returncode, "stdout": result.stdout.decode("utf-8", "replace"),
              "stderr": result.stderr.decode("utf-8", "replace")}
    if result.returncode == 0 and remember:
        try:
            guest_credentials.save(info["uuid"], opts.user, secret)
            output["credential_saved"] = True
        except OSError as error:
            output["credential_saved"] = False
            output["credential_error"] = str(error)
    return output, int(result.returncode != 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="Зарегистрированные машины и признак запуска")
    for action in ("info", "screen", "type", "enter", "run"):
        sub = commands.add_parser(action)
        sub.add_argument("vm", help="Точное имя или UUID")
        if action == "screen":
            sub.add_argument("png", type=Path)
        if action in ("type", "run"):
            sub.add_argument("--file", type=Path, required=True, help="Текст команды в UTF-8")
        if action == "run":
            sub.add_argument("--user", required=True)
            sub.add_argument("--ask", action="store_true", help="Запросить гостевой пароль в окне Windows")
            sub.add_argument("--remember", action="store_true", help="Запросить пароль и сохранить после успешной команды")
            sub.add_argument("--shell", choices=["bash", "powershell"])
            sub.add_argument("--timeout", type=int, default=30, metavar="SECONDS")
    opts = parser.parse_args()
    if opts.action == "list":
        running = inventory(running=True)
        return [dict(name=name, uuid=uuid, running=uuid in running)
                for uuid, name in inventory().items()], 0
    info = machine(opts.vm)
    if opts.action == "info":
        return info, 0
    if info["state"] != "running":
        raise ValueError(f"ВМ «{info['name']}» сейчас в состоянии {info['state']}.")
    uuid = info["uuid"]
    if opts.action == "run":
        if not 1 <= opts.timeout <= 3600:
            raise ValueError("Тайм-аут должен составлять от 1 до 3600 секунд.")
        output, code = guest_run(info, opts)
        return dict(vm=info["name"], **output), code
    if opts.action == "screen":
        return dict(vm=info["name"], png=capture(uuid, opts.png)), 0
    if opts.action == "type":
        type_line(uuid, opts.file)
    else:
        call("controlvm", uuid, "keyboardputscancode", "1c", "9c")
    return dict(vm=info["name"], sent=opts.action), 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    try:
        output, code = main()
        print(json.dumps(output, ensure_ascii=False, indent=2))
        sys.exit(code)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
