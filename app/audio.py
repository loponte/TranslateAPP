"""Captura do áudio que sai do PC (WASAPI loopback) -> chunks float32 mono 16 kHz de `block_ms`, linha do tempo contínua.

Threads: callback do PortAudio (só enfileira) -> `_pump` (mono, 16 kHz, chunks, `on_audio`, preenche silêncio pelo
relógio) e `_manage` (abre/reabre o dispositivo, acompanha a saída padrão do Windows).
Por app (`app="Discord.exe"`, Windows build 19041+): Process Loopback via ctypes (só a árvore de processos do app);
`_manage_app` acha/vigia o processo e uma thread leitora enfileira `(t, 48000, 2, bytes)` para o mesmo `_pump`.
Custo fixo medido: +32-38 ms em relação ao loopback da saída inteira.
Reamostragem: PyAV/swr (o mesmo caminho do faster-whisper) em vez de soxr: ~1 ms de atraso fixo contra 11-43 ms do soxr
(medido com blocos de 10 ms), resposta plana até ~6,5 kHz e > 60 dB de rejeição acima de 9 kHz.
"""
from __future__ import annotations

import ctypes
import queue
import struct
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Callable

import av
import numpy as np

if sys.platform == "win32":
    import pyaudiowpatch as pa

from app.events import SR

# ponytail: GRACE fixo; um dispositivo que entregue em rajadas > 32 ms geraria chunks de zeros falsos -> adaptar ao intervalo medido
GRACE = 0.032   # s: atraso tolerado na entrega do dado real antes de preencher o chunk com zeros
TOL = 0.064     # s: erro (relógio x dado recebido) acima do qual a linha do tempo salta em vez de seguir suavemente


@dataclass
class LoopbackDevice:
    index: int
    name: str
    is_default: bool


def _loopbacks(p: "pa.PyAudio") -> list[dict]:
    """Dispositivos loopback WASAPI (dict do PortAudio) com o padrão do Windows primeiro."""
    devs = list(p.get_loopback_device_info_generator())
    try:
        dflt = p.get_default_wasapi_loopback()["index"]
    except Exception:
        dflt = None
    for d in devs:
        d["is_default"] = d["index"] == dflt
    return sorted(devs, key=lambda d: not d["is_default"])  # sort estável


def list_loopback_devices() -> list[LoopbackDevice]:
    """Windows: saídas WASAPI (padrão primeiro). macOS: "Áudio do sistema" + entradas (ex.: BlackHole)."""
    if sys.platform == "darwin":
        from app import audio_mac
        try:
            ins = audio_mac.inputs()
        except Exception:   # sem PortAudio/sem dispositivo: o áudio do sistema ainda funciona
            ins = []
        return [LoopbackDevice(i, n, i == 0) for i, n in enumerate([audio_mac.SYSTEM, *ins])]
    p = pa.PyAudio()
    try:
        return [LoopbackDevice(d["index"], d["name"].removesuffix(" [Loopback]"), d["is_default"]) for d in _loopbacks(p)]
    finally:
        p.terminate()


@dataclass
class AudioApp:
    id: str       # Windows: exe ("Discord.exe"); mac: bundle id ("com.hnc.Discord")
    name: str     # FileDescription do exe ("Discord") / applicationName no mac
    active: bool  # tocando agora (Windows); mac: sempre False


# a doc oficial pede o build 20348; o mesmo caminho funciona desde o 19041 (proc-tap/OBS). Só testado no 26300.
APP_CAPTURE = sys.platform == "darwin" or (sys.platform == "win32" and sys.getwindowsversion().build >= 19041)


def list_audio_apps() -> list[AudioApp]:
    """Apps que dá para capturar, quem está tocando primeiro; [] se não suportado. Qualquer thread (inicializa COM sozinha).
    Windows: sessões de áudio de todas as saídas, agrupadas pelo exe do processo raiz (~10 ms). Mac: apps abertos (`helper --list`)."""
    if not APP_CAPTURE:
        return []
    if sys.platform == "darwin":
        from app import audio_mac
        return [AudioApp(b, n, False) for b, n in audio_mac.list_apps()]
    com = _com_init()
    try:
        procs = _procs()
        apps: dict[str, AudioApp] = {}
        for root, active in _sessions(procs).items():
            exe = procs[root][1]
            a = apps.get(exe.lower())
            if a is None:
                apps[exe.lower()] = AudioApp(exe, _app_name(root, exe), active)
            else:   # ponytail: dois exe iguais e independentes (2 python.exe) viram 1 item; a captura pega o que toca
                a.active |= active
        return sorted(apps.values(), key=lambda a: (not a.active, a.name.lower()))
    finally:
        if com:
            _ole.CoUninitialize()


if sys.platform == "win32":
    from ctypes import wintypes as W

    _ole = ctypes.OleDLL("ole32")
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ver = ctypes.WinDLL("version")
    for _f in ("CreateToolhelp32Snapshot", "OpenProcess", "CreateEventW"):
        getattr(_k32, _f).restype = W.HANDLE
    _k32.CloseHandle.argtypes = [W.HANDLE]
    _k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    _P = ctypes.c_void_p
    _WAIT_TIMEOUT = 0x102
    _LEAK: list = []

    def _guid(s: str):
        return ctypes.create_string_buffer(uuid.UUID(s).bytes_le, 16)

    def _call(obj, i: int, *args):
        """Método `i` da vtable de um objeto COM; HRESULT de falha vira OSError (S_FALSE = 1 volta como int)."""
        fn = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(_P)))[0][i]
        types = [type(a) if isinstance(a, ctypes._SimpleCData) else _P for a in args]
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, _P, *types)(fn)(obj, *args)

    def _release(o) -> None:
        if o and o.value:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, _P)(ctypes.cast(o, ctypes.POINTER(ctypes.POINTER(_P)))[0][2])(o)

    def _com_init() -> bool:
        """COM nesta thread (MTA). False = já estava inicializado em outro modelo (ex.: STA do Tk): serve igual, sem Uninitialize."""
        try:
            _ole.CoInitializeEx(None, 0)
            return True
        except OSError:
            return False

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                    ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD),
                    ("szExeFile", W.WCHAR * 260)]

    class _Handler(ctypes.Structure):   # IActivateAudioInterfaceCompletionHandler feito à mão: só o ponteiro da vtable
        _fields_ = [("vtbl", _P)]

    _IIDS_OK = {uuid.UUID(s).bytes_le for s in ("00000000-0000-0000-C000-000000000046",    # IUnknown
                                                 "94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90",    # IAgileObject
                                                 "41D949AB-9862-444A-80F6-C261334DA5EB")}   # o próprio handler
    _QI = ctypes.WINFUNCTYPE(ctypes.c_long, _P, _P, ctypes.POINTER(_P))
    _REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, _P)
    _DONE = ctypes.WINFUNCTYPE(ctypes.c_long, _P, _P)


def _procs() -> dict[int, tuple[int, str]]:
    """pid -> (pid do pai, exe)."""
    h = _k32.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
    e = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
    out = {}
    try:
        ok = _k32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = _k32.Process32NextW(h, ctypes.byref(e))
    finally:
        _k32.CloseHandle(h)
    return out


def _root(pid: int, procs) -> int:
    """Sobe enquanto o pai tem o mesmo exe (filhos do Discord/Chrome -> processo principal)."""
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        ppid = procs[pid][0]
        if ppid not in procs or procs[ppid][1].lower() != procs[pid][1].lower():
            break
        pid = ppid
    return pid


def _sessions(procs) -> dict[int, bool]:
    """Sessões de áudio de todas as saídas ativas (o Discord pode tocar num fone que não é o padrão), sem os sons do
    sistema: pid raiz -> tocando agora. A thread precisa de COM."""
    enum, col = _P(), _P()
    out: dict[int, bool] = {}
    try:
        _ole.CoCreateInstance(_guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), None, 23,
                              _guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), ctypes.byref(enum))   # MMDeviceEnumerator
        _call(enum, 3, ctypes.c_int(0), W.DWORD(1), ctypes.byref(col))   # EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)
        nd = W.UINT()
        _call(col, 3, ctypes.byref(nd))
        for d in range(nd.value):
            dev, mgr, sen, n = _P(), _P(), _P(), ctypes.c_int()
            try:
                _call(col, 4, W.UINT(d), ctypes.byref(dev))
                _call(dev, 3, _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), W.DWORD(23), _P(), ctypes.byref(mgr))  # IAudioSessionManager2
                _call(mgr, 5, ctypes.byref(sen))   # GetSessionEnumerator
                _call(sen, 3, ctypes.byref(n))
            except OSError:
                n.value = 0   # saída que sumiu no meio da listagem
            for i in range(n.value):
                ctl, ctl2 = _P(), _P()
                try:
                    _call(sen, 4, ctypes.c_int(i), ctypes.byref(ctl))
                    _call(ctl, 0, _guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"), ctypes.byref(ctl2))  # QI IAudioSessionControl2
                    if _call(ctl2, 15) == 0:   # IsSystemSoundsSession: S_OK = sons do sistema
                        continue
                    pid, st = W.DWORD(), ctypes.c_int()
                    _call(ctl2, 14, ctypes.byref(pid))   # GetProcessId
                    _call(ctl2, 3, ctypes.byref(st))     # GetState: 0 inativa, 1 ativa, 2 expirada
                    if pid.value in procs:
                        root = _root(pid.value, procs)
                        out[root] = out.get(root, False) or st.value == 1
                except OSError:
                    pass   # sessão que sumiu no meio da listagem
                finally:
                    _release(ctl2)
                    _release(ctl)
            for o in (sen, mgr, dev):
                _release(o)
    except OSError:
        pass   # sem saída de áudio
    finally:
        _release(col)
        _release(enum)
    return out


def _default_render_id() -> str | None:
    """ID do endpoint de saída padrão (a thread precisa ter COM inicializado). O PortAudio só reenumera ao reiniciar,
    então é isto que detecta a troca da saída padrão com a captura rodando."""
    enum, dev, wid = _P(), _P(), _P()
    try:
        _ole.CoCreateInstance(_guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), None, 23,
                              _guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), ctypes.byref(enum))   # MMDeviceEnumerator
        _call(enum, 4, ctypes.c_int(0), ctypes.c_int(0), ctypes.byref(dev))   # GetDefaultAudioEndpoint(eRender, eConsole)
        _call(dev, 5, ctypes.byref(wid))                                      # IMMDevice::GetId
        s = ctypes.wstring_at(wid.value)
        _ole.CoTaskMemFree(wid)
        return s
    except Exception:
        return None
    finally:
        _release(dev)
        _release(enum)


def _app_name(pid: int, exe: str) -> str:
    """FileDescription do exe ("Discord", "Google Chrome"); sem ela, o nome do exe sem ".exe"."""
    stem = exe[:-4] if exe.lower().endswith(".exe") else exe
    h = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return stem
    buf, n = ctypes.create_unicode_buffer(1024), W.DWORD(1024)
    ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    _k32.CloseHandle(h)
    size = _ver.GetFileVersionInfoSizeW(buf.value, None) if ok else 0
    info, p, ln = ctypes.create_string_buffer(size or 1), _P(), W.UINT()
    if not size or not _ver.GetFileVersionInfoW(buf.value, 0, size, info) or not _ver.VerQueryValueW(
            info, r"\VarFileInfo\Translation", ctypes.byref(p), ctypes.byref(ln)) or ln.value < 4:
        return stem
    w = ctypes.cast(p, ctypes.POINTER(W.WORD))
    if _ver.VerQueryValueW(info, rf"\StringFileInfo\{w[0]:04x}{w[1]:04x}\FileDescription", ctypes.byref(p),
                           ctypes.byref(ln)) and ln.value:
        return ctypes.wstring_at(p.value).strip() or stem
    return stem


def _find_app(exe: str) -> int | None:
    """Processo raiz desse exe: o que está tocando, senão o que tem sessão de áudio, senão qualquer um. None = não está aberto."""
    procs = _procs()
    roots = {_root(p, procs) for p, (_, e) in procs.items() if e.lower() == exe.lower()}
    if not roots:
        return None
    sess = _sessions(procs)
    return max(roots, key=lambda r: (sess.get(r, False), r in sess))


def _open_app_loopback(pid: int, timeout: float = 5.0):
    """IAudioClient do Process Loopback do `pid` + filhos, iniciado em float32 48 kHz estéreo.
    -> (IAudioClient, IAudioCaptureClient, evento). Em falha, libera tudo e levanta."""
    done = threading.Event()

    def qi(this, riid, ppv):
        if ctypes.string_at(riid, 16) in _IIDS_OK:
            ppv[0] = this
            return 0
        ppv[0] = None
        return 0x80004002 - (1 << 32)   # E_NOINTERFACE

    funcs = (_QI(qi), _REF(lambda this: 1), _REF(lambda this: 1), _DONE(lambda this, op: (done.set(), 0)[1]))
    vtbl = (_P * 4)(*[ctypes.cast(f, _P) for f in funcs])
    handler = _Handler(ctypes.cast(vtbl, _P))
    params = (ctypes.c_uint32 * 3)(1, pid, 0)   # AUDIOCLIENT_ACTIVATION_PARAMS: PROCESS_LOOPBACK, pid, INCLUDE_TARGET_PROCESS_TREE
    pv = (ctypes.c_byte * 24)()                 # PROPVARIANT VT_BLOB apontando para params
    ctypes.c_uint16.from_buffer(pv, 0).value = 65
    ctypes.c_uint32.from_buffer(pv, 8).value = ctypes.sizeof(params)
    _P.from_buffer(pv, 16).value = ctypes.addressof(params)
    op, client, cap, ev = _P(), _P(), _P(), None
    try:
        mm = ctypes.WinDLL("mmdevapi")
        mm.ActivateAudioInterfaceAsync.restype = ctypes.HRESULT
        mm.ActivateAudioInterfaceAsync(ctypes.c_wchar_p("VAD\\Process_Loopback"), _guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"),
                                       ctypes.byref(pv), ctypes.byref(handler), ctypes.byref(op))   # IAudioClient
        if not done.wait(timeout):   # o callback ainda pode chegar depois: o handler fica vivo de propósito (vaza ~200 B)
            _LEAK.append((funcs, vtbl, handler, params, pv))
            raise TimeoutError("o Windows não respondeu à ativação")
        hr = ctypes.c_long()
        _call(op, 3, ctypes.byref(hr), ctypes.byref(client))   # GetActivateResult
        if hr.value:
            raise OSError(f"0x{hr.value & 0xffffffff:08x}")
        wfx = (ctypes.c_byte * 18)()   # WAVEFORMATEX IEEE float
        struct.pack_into("<HHIIHHH", wfx, 0, 3, 2, 48000, 48000 * 8, 8, 32, 0)
        flags = 0x00020000 | 0x00040000 | 0x80000000 | 0x08000000   # LOOPBACK|EVENTCALLBACK|AUTOCONVERTPCM|SRC_DEFAULT_QUALITY
        _call(client, 3, ctypes.c_int(0), W.DWORD(flags), ctypes.c_longlong(200_000), ctypes.c_longlong(0),
              ctypes.byref(wfx), _P())   # Initialize(SHARED, flags, 20 ms, 0, fmt, NULL)
        ev = _k32.CreateEventW(None, False, False, None)
        _call(client, 13, W.HANDLE(ev))   # SetEventHandle
        _call(client, 14, _guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), ctypes.byref(cap))   # GetService(IAudioCaptureClient)
        _call(client, 10)   # Start
        return client, cap, ev
    except BaseException:
        _release(cap)
        _release(client)
        if ev:
            _k32.CloseHandle(ev)
        raise
    finally:
        _release(op)


def _alive(h) -> bool:
    return not h or _k32.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT   # sem handle (sem acesso): não dá para vigiar


class LoopbackCapture:
    def __init__(self, on_audio: Callable[[np.ndarray, float], None], device: str | None = None,
                 on_status: Callable[[str], None] | None = None, block_ms: int = 32, app: str | None = None):
        self.on_audio, self.device, self.on_status = on_audio, device, on_status
        self.app = app                   # exe (Windows) / bundle id (mac); None = saída inteira (`device`)
        self.device_name = ""            # preenchido ao abrir (~0,1 s após start) e a cada reabertura; com app = nome do app
        self._n = block_ms * SR // 1000  # amostras por chunk
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._q = queue.Queue()
        self._threads = [threading.Thread(target=f, name=n, daemon=True)
                         for n, f in (("audio-pump", self._pump), ("audio-dev", self._manage_mac if sys.platform == "darwin"
                                                                    else self._manage_app if self.app else self._manage))]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        if not self._threads:
            return
        self._stop.set()
        self._q.put(None)
        for t in self._threads:
            if t is not threading.current_thread():   # stop() chamado de dentro de on_audio/on_status
                t.join(5)
        self._threads = []

    def _status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                traceback.print_exc()

    # ---- dispositivo ----
    def _manage_mac(self) -> None:
        from app import audio_mac
        audio_mac.manage(self)

    def _open(self, p: "pa.PyAudio"):
        devs = _loopbacks(p)
        if self.device is None:
            d = devs[0] if devs else None
        else:
            s = self.device.lower()   # nome exato (o que a UI devolve) ganha da busca por substring
            d = next((x for x in devs if x["name"].removesuffix(" [Loopback]").lower() == s), None) \
                or next((x for x in devs if s in x["name"].lower()), None)
        if d is None:
            raise LookupError("Nenhuma saída de áudio encontrada — tentando de novo…" if self.device is None
                              else f"Dispositivo de áudio '{self.device}' não encontrado — tentando de novo…")
        ch, rate, q = int(d["maxInputChannels"]), int(d["defaultSampleRate"]), self._q

        def cb(data, frames, tinfo, flags):   # só enfileira; o resto é na thread _pump
            q.put_nowait((time.monotonic(), rate, ch, data))
            return None, pa.paContinue

        stream = p.open(format=pa.paFloat32, channels=ch, rate=rate, input=True, input_device_index=d["index"],
                        frames_per_buffer=rate // 100, stream_callback=cb)   # 10 ms = período do motor de áudio
        self.device_name = d["name"].removesuffix(" [Loopback]")
        return stream

    def _manage(self) -> None:
        com = _com_init()   # o PortAudio/WASAPI exige COM nesta thread (ele só o inicializa na thread do 1º PyAudio() do processo)
        p = stream = None
        dflt_id, last_msg = None, None

        def close():
            for obj, fn in ((stream, "stop_stream"), (stream, "close"), (p, "terminate")):
                try:
                    getattr(obj, fn)()
                except Exception:
                    pass

        while not self._stop.is_set():
            try:
                if stream is None:
                    p = pa.PyAudio()   # nova instância = dispositivos reenumerados (novo padrão incluso)
                    dflt_id = _default_render_id()
                    stream = self._open(p)
                    self._status(f"Capturando áudio de: {self.device_name}")
                    last_msg = None
                elif not stream.is_active():
                    raise RuntimeError("A captura de áudio parou — reabrindo…")
                elif self.device is None and dflt_id and _default_render_id() not in (None, dflt_id):
                    raise RuntimeError("A saída padrão do Windows mudou — reabrindo a captura…")
            except Exception as e:
                close()
                stream = p = None
                msg = str(e) if isinstance(e, (RuntimeError, LookupError)) else f"Falha ao abrir o áudio ({e}) — tentando de novo…"
                if msg != last_msg:   # não repete a mesma mensagem a cada tentativa
                    self._status(msg)
                last_msg = msg
            self._stop.wait(1.5 if stream is None else 0.5)
        close()
        if com:
            _ole.CoUninitialize()

    def _manage_app(self) -> None:
        """Windows, só um app: acha o processo raiz pelo exe, abre o Process Loopback e vigia o processo a cada 0,5 s;
        se ele fechar, procura de novo pelo nome (reaberto = outro pid). Fechado: a linha do tempo segue com zeros."""
        com = _com_init()
        name = self.app[:-4] if self.app.lower().endswith(".exe") else self.app
        rd = stop_rd = proc = None   # thread leitora, seu sinal de parada, handle do processo (SYNCHRONIZE)
        last_msg = None

        def close():
            nonlocal rd, proc
            if rd is not None:
                stop_rd.set()
                rd.join(2)
            if proc:
                _k32.CloseHandle(proc)
            rd = proc = None

        while not self._stop.is_set():
            try:
                if rd is not None and not _alive(proc):   # o app fechou: procura de novo já
                    close()
                if rd is None:
                    pid = _find_app(self.app)
                    if pid is None:
                        raise LookupError(f"{name} não está aberto — esperando…")
                    name = _app_name(pid, self.app)
                    proc = _k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
                    try:
                        client, cc, ev = _open_app_loopback(pid)
                    except Exception as e:
                        raise RuntimeError(f"Falha na captura por app ({name}: {e}) — use \"Saída inteira\"") from e
                    stop_rd = threading.Event()
                    rd = threading.Thread(target=self._read_app, args=(client, cc, ev, stop_rd), name="audio-app", daemon=True)
                    rd.start()
                    self.device_name = name
                    self._status(f"Capturando áudio de: {name}")
                    last_msg = None
                elif not rd.is_alive():
                    raise RuntimeError(f"A captura de {name} parou — reabrindo…")
            except Exception as e:
                close()
                msg = str(e) if isinstance(e, (RuntimeError, LookupError)) else f"Falha ao abrir o áudio de {name} ({e}) — tentando de novo…"
                if msg != last_msg:
                    self._status(msg)
                last_msg = msg
            self._stop.wait(1.5 if rd is None else 0.5)
        close()
        if com:
            _ole.CoUninitialize()

    def _read_app(self, client, cc, ev, stop: threading.Event) -> None:
        """Thread leitora do Process Loopback: pacotes de 10 ms -> `self._q` como o callback do PortAudio. Libera tudo ao sair."""
        com = _com_init()
        q = self._q
        try:
            while not stop.is_set():
                _k32.WaitForSingleObject(ev, 100)
                while True:
                    nxt = W.UINT()
                    _call(cc, 5, ctypes.byref(nxt))   # GetNextPacketSize
                    if not nxt.value:
                        break
                    data, nf, fl = _P(), W.UINT(), W.DWORD()
                    _call(cc, 3, ctypes.byref(data), ctypes.byref(nf), ctypes.byref(fl), _P(), _P())   # GetBuffer
                    t = time.monotonic()
                    # AUDCLNT_BUFFERFLAGS_SILENT: o buffer não vale nada, são zeros (app mudo ou já fechado)
                    raw = bytes(nf.value * 8) if fl.value & 2 else ctypes.string_at(data, nf.value * 8)
                    _call(cc, 4, W.UINT(nf.value))    # ReleaseBuffer
                    q.put_nowait((t, 48000, 2, raw))
        except Exception:
            traceback.print_exc()   # a _manage_app vê a thread morta e reabre
        finally:
            try:
                _call(client, 11)   # Stop
            except OSError:
                pass
            _release(cc)
            _release(client)
            _k32.CloseHandle(ev)
            if com:
                _ole.CoUninitialize()

    # ---- linha do tempo ----
    def _pump(self) -> None:
        """mono -> 16 kHz -> chunks. A linha do tempo é uma grade de `n` amostras ancorada no relógio: dado real entra
        nela; quando o chunk vence sem dado (loopback não entrega nada com tudo mudo) completa com zeros."""
        n, q = self._n, self._q
        anchor = time.monotonic()      # monotonic da amostra 0 da grade
        done, last = 0, 0.0            # amostras já entregues; último t_end entregue
        pend = np.empty(0, np.float32)
        fmt = rs = None
        realign = True                 # o próximo dado real realinha a grade (início ou depois de preencher zeros)

        def emit():
            nonlocal pend, done, last
            while len(pend) >= n:
                done += n
                last = max(anchor + done / SR, last + 1e-3)   # monotônico mesmo se a âncora recuar
                chunk, pend = pend[:n].copy(), pend[n:]
                try:
                    self.on_audio(chunk, last)
                except Exception:
                    traceback.print_exc()

        def due():   # quando o próximo chunk vence; já ocioso (realign) não há o que esperar: sem tolerância
            return anchor + (done + n) / SR + (0.0 if realign else GRACE)

        while True:
            items = []
            try:   # espera dado até o próximo chunk vencer (+ tolerância); depois drena o que já chegou
                items.append(q.get(timeout=max(0.0, due() - time.monotonic())))
                while True:
                    items.append(q.get_nowait())
            except queue.Empty:
                pass
            for it in items:
                if it is None:
                    return
                t_cb, rate, ch, data = it
                try:
                    if (rate, ch) != fmt:   # novo dispositivo/formato: resampler novo (o swr guarda estado)
                        fmt = (rate, ch)
                        rs = av.AudioResampler(format="flt", layout="mono", rate=SR) if rate != SR else None
                    d = np.frombuffer(data, np.float32).reshape(-1, ch)
                    # 5.1/7.1: o áudio de call costuma ir só em FL/FR (+ centro); a média simples atenuaria vários dB
                    x = d.mean(axis=1) if ch < 6 else d[:, :2].mean(axis=1) + 0.7 * d[:, 2]
                    if rs is not None:
                        f = av.AudioFrame.from_ndarray(x.reshape(1, -1), format="flt", layout="mono")
                        f.sample_rate = rate
                        x = np.concatenate([o.to_ndarray().reshape(-1) for o in rs.resample(f)] or [x[:0]])
                except Exception:
                    traceback.print_exc()
                    fmt = None
                    continue
                pend = np.concatenate((pend, x))
                d = t_cb - (anchor + (done + len(pend)) / SR)   # o fim deste dado aconteceu em ~t_cb
                if realign or abs(d) > TOL:
                    anchor, realign = anchor + d, False
                else:
                    anchor += 0.02 * d                            # segue a deriva entre o relógio do dispositivo e o do PC
                emit()
            if (lag := time.monotonic() - due()) > 1.0:   # salto do relógio (suspensão/hibernação): não despeja horas de zeros
                anchor, realign = anchor + lag, True
            while time.monotonic() >= due():   # chunk vencido sem dado: zeros
                pend = np.concatenate((pend, np.zeros(n - len(pend), np.float32)))
                realign = True
                emit()
