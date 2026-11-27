"""Windows credential UI and user-scoped storage for one VirtualBox guest login."""

import ctypes as c
from ctypes import wintypes as w


class Credential(c.Structure):
    _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR),
                ("Comment", w.LPWSTR), ("LastWritten", w.FILETIME),
                ("CredentialBlobSize", w.DWORD), ("CredentialBlob", c.c_void_p),
                ("Persist", w.DWORD), ("AttributeCount", w.DWORD),
                ("Attributes", c.c_void_p), ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR)]


class Dialog(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hwndParent", w.HWND), ("message", w.LPCWSTR),
                ("caption", w.LPCWSTR), ("banner", w.HBITMAP)]


vault = c.WinDLL("advapi32", use_last_error=True)
credptr = c.POINTER(Credential)
vault.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(credptr)]
vault.CredWriteW.argtypes = [credptr, w.DWORD]
vault.CredFree.argtypes = [c.c_void_p]
vault.CredFree.restype = None
prompt = c.WinDLL("credui").CredUIPromptForCredentialsW
prompt.argtypes = [c.POINTER(Dialog), w.LPCWSTR, c.c_void_p, w.DWORD, w.LPWSTR, w.ULONG,
                   w.LPWSTR, w.ULONG, c.POINTER(w.BOOL), w.DWORD]
prompt.restype = w.DWORD


def target(uuid, user):
    # Credential Manager treats target names case-insensitively, Debian logins may differ in case.
    return "Codex.VirtualBox/" + uuid + "/" + user.encode("utf-8").hex()


def saved(uuid, user):
    result = credptr()
    if not vault.CredReadW(target(uuid, user), 1, 0, c.byref(result)):
        error = c.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            return None
        raise c.WinError(error)
    try:
        entry = result.contents
        if entry.UserName != user:
            raise ValueError("Имя в сохранённой записи отличается от имени гостевой учётной записи.")
        return c.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode("utf-16-le")
    finally:
        vault.CredFree(result)


def save(uuid, user, password):
    data = password.encode("utf-16-le")
    buffer = c.create_string_buffer(data)
    entry = Credential(Type=1, TargetName=target(uuid, user), Persist=2, UserName=user,
                       CredentialBlob=c.addressof(buffer), CredentialBlobSize=len(data))
    try:
        if not vault.CredWriteW(c.byref(entry), 0):
            raise c.WinError(c.get_last_error())
    finally:
        c.memset(buffer, 0, c.sizeof(buffer))


def ask(info, user, remember_requested=False):
    caption = "VirtualBox · " + info["name"]
    policy = ("После успешной команды пароль будет сохранён для этой ВМ в вашем профиле Windows."
              if remember_requested else "Чтобы сохранить пароль после успешной команды, отметьте «Запомнить».")
    message = f"Гостевая учётная запись: {user}\nВведите пароль гостевой ОС. {policy}"
    ui = Dialog(c.sizeof(Dialog), None, message, caption, None)
    login = c.create_unicode_buffer(user, 514)
    password = c.create_unicode_buffer(257)
    remember = w.BOOL(False)
    # Generic login, always show UI, keep the supplied username, let the caller save after validation.
    flags = 0x40000 | 0x80 | 0x100000 | 0x2
    if not remember_requested:
        flags |= 0x40  # CREDUI_FLAGS_SHOW_SAVE_CHECK_BOX
    try:
        status = prompt(c.byref(ui), info["name"], None, 0, login, len(login),
                        password, len(password), c.byref(remember), flags)
        if status == 1223:
            raise ValueError("Пользователь отменил вход.")
        if status:
            raise c.WinError(status)
        if login.value != user:
            raise ValueError("Выбрано другое имя пользователя. Повторите запуск с нужным --user.")
        return password.value, remember_requested or bool(remember.value)
    finally:
        c.memset(password, 0, c.sizeof(password))
