from __future__ import annotations
import os
import re
from pathlib import Path
from typing import IO, Callable
from threading import Condition, Thread
from dataclasses import dataclass, field
import json
import subprocess
import select
import pty
import time
import random
from uuid import uuid4

def CurrentTimeMillis():
    return round(time.time() * 1000)

# removes: colors, escape, control sequences
# https://stackoverflow.com/questions/14693701/how-can-i-remove-the-ansi-escape-sequences-from-a-string-in-python 
def StripANSI(s: str):
    return re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub('', s)

def RemoveTrailingNewline(s):
    while len(s) > 0 and s[-1] in {"\n", "\r"}:
        s = s[:-1]
    return s

def GenerateId():
    ascii_ranges = [(48, 57), (65, 90), (97, 122)]
    ascii_vocab = []
    for a, b in ascii_ranges:
        for i in range(a, b+1):
            ascii_vocab.append(chr(i))
    ID_LEN = 12
    buffer = []
    while True:
        _break = False
        x = uuid4().int
        while x > 0:
            x, r = divmod(x, len(ascii_vocab))
            buffer.append(ascii_vocab[r])
            if len(buffer) >= ID_LEN: _break=True; break
        if _break: break
    return ''.join(buffer)

@dataclass
class IpcModel:
    message_id: str = field(default_factory=GenerateId, kw_only=True)
    parse_error: str | None = field(default=None, kw_only=True)

    @classmethod
    def Parse(cls, raw: str):
        try:
            d = json.loads(raw)
            return cls(**d)
        except TypeError as e:
            emsg = str(e)
            emsg = emsg.replace("IpcModel.__init__()", "").strip()
            to_replace = {
                "missing 1 required positional argument:": "missing field",
                "got an unexpected keyword argument": "unknown field",
                "'": "[",
                "'": "]",
            }
            for k, v in to_replace.items():
                emsg = emsg.replace(k, v, 1)
            return IpcModel(parse_error=emsg)
        except json.JSONDecodeError as e:
            return IpcModel(parse_error="invalid json")

    def IsValid(self):
        return self.parse_error is None

    def Serialize(self):
        bl = {"parse_error"}
        should_serialize = lambda k, v: not k.startswith("_") and not callable(v) and k not in bl
        d = {k:v for k, v in self.__dict__.items() if should_serialize(k, v)}
        return json.dumps(d)

@dataclass
class IpcRequest(IpcModel):
    endpoint: str
    data: dict = field(default_factory=dict)

@dataclass
class IpcResponse(IpcModel):
    status: int
    data: dict = field(default_factory=dict)

class PipeServer:
    def __init__(self, io_dir: Path, callback: Callable[[PipeServer, str], None], overwrite: bool = False, id: str = None) -> None:
        success = False
        try:
            self._id = "main" if id is None else id
            self._server_path = io_dir/f"{self._id}.in"
            self._client_path = io_dir/f"{self._id}.out"
            if overwrite and self._server_path.exists(): os.remove(self._server_path)
            self._is_closing = False
            self._lock = Condition()
            
            os.mkfifo(self._server_path)
            def _open():
                return os.open(self._server_path, os.O_RDONLY|os.O_NONBLOCK)
            self._server_channel = _open()
            def _reset(reader: NonBlockingReader):
                if self._is_closing: return
                self._server_channel = _open()
                reader.Reset(self._server_channel)
            self.reader = NonBlockingReader(self._server_channel, on_close=_reset)
            self.reader.RegisterCallback(lambda x: callback(self, RemoveTrailingNewline(x.decode())))
            
            self._client_channel: int|None = None
            self._buffer: list[str] = []
            def prep_channel():
                if not self._client_path.exists(): 
                    if self._client_channel is not None:
                        os.close(self._client_channel)
                        self._client_channel = None
                    return False
                if self._client_channel is None:
                    self._client_channel = os.open(self._client_path, os.O_WRONLY)
                return True

            def try_send():
                if not prep_channel(): return
                retries = 2
                while len(self._buffer) > 0 and retries > 0:
                    msg = self._buffer.pop(0)
                    try:
                        os.write(self._client_channel, (msg+"\n").encode())
                    except OSError:
                        self._buffer.insert(0, msg)
                        # the channel is likely pointing to the previous client
                        # which is now closed, so we need to get a new fd for pipe
                        self._client_channel = None
                        prep_channel() 
                        retries -= 1
            
            def sender_process():
                while True:
                    with self._lock:
                        if self._is_closing: break
                        if len(self._buffer) == 0:
                            self._lock.wait(1)
                        else:
                            try_send()
            self._sender = Thread(target=sender_process)
            self._sender.start()

            success = True
        finally:
            if not success: self.Dispose()

    def Send(self, msg: str):
        with self._lock:
            self._buffer.append(msg)
            self._lock.notify_all()

    def IsOpen(self):
        return self._server_path.exists()

    def Dispose(self):
        def _try_close(fd):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            with self._lock:
                self._is_closing = True
                self._lock.notify_all()
            self.reader.Dispose()
            _try_close(self._server_channel)
            self._sender.join()
        except AttributeError:
            pass
        finally:
            if self._server_path.exists(): os.remove(self._server_path)

    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.Dispose()

class PipeClient:
    def __init__(self, server_pipe: Path, timeout: int|float = 15) -> None:
        success = False
        try:
            self._id = server_pipe.stem
            io_dir = server_pipe.parent
            self._server_path = io_dir/f"{self._id}.in"
            self._client_path = io_dir/f"{self._id}.out"
            self._lock = Condition()
            self._closed = False

            start = CurrentTimeMillis()
            random.seed(start)
            while self._client_path.exists():
                delay = random.random()*0.1
                time.sleep(delay)
                if CurrentTimeMillis() - start > timeout*1000:
                    raise TimeoutError("Failed to connect to server")

            def _on_close(reader: NonBlockingReader):
                with self._lock:
                    self._closed = True
            os.mkfifo(self._client_path)
            self._server_channel = os.open(self._server_path, os.O_WRONLY)
            self._client_channel = os.open(self._client_path, os.O_RDONLY|os.O_NONBLOCK)
            self._reader = NonBlockingReader(self._client_channel, on_close=_on_close)
            
            self._last_message_id: str = None
            self._last_response: IpcResponse = None
            def _on_response(x):
                res = IpcResponse.Parse(x.decode())
                if res.message_id != self._last_message_id: return
                self._last_response = res
            self._reader.RegisterCallback(_on_response)
            success = True
        finally:
            if not success: self.Dispose()

    def Send(self, msg: str):
        if self._closed: return
        os.write(self._server_channel, (msg+"\n").encode())

    def Transact(self, req: IpcRequest, timeout: int|float = 15) -> IpcResponse:
        self._last_response = None
        self._last_message_id = req.message_id
        msg = req.Serialize()
        self.Send(msg)
        start = CurrentTimeMillis()
        while self._last_response is None:
            if CurrentTimeMillis() - start > timeout*1000:
                raise TimeoutError("Failed to receive response")
            with self._lock:
                self._lock.wait(0.1)
                if self._closed: break
        return self._last_response

    def IsOpen(self):
        return self._client_path.exists()

    def Dispose(self):
        def _try_close(fd):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            with self._lock:
                self._closed = True
            self._reader.Dispose()
            _try_close(self._server_channel)
            _try_close(self._client_channel)
        except AttributeError:
            pass
        finally:
            if self._client_path.exists(): os.remove(self._client_path)

    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.Dispose()

class LiveShell:
    def __init__(self) -> None:
        self._shell = None
        self._MARK = "done"
        self._done = False
        self._silent = True

    def Exec(self, cmd: str, timeout: int|None = 15, silent=True, live_out: Callable[[str], None] = None, live_err: Callable[[str], None] = None):
        def _strip_indent(s):
            lines = s.split("\n")
            if len(lines) == 0: return s
            indent = 0
            for c in lines[0]:
                if c not in {" ", "\t"}: break
                indent += 1
            return "\n".join([l[indent:] for l in lines])
        
        def _await_done(await_timeout, delta):
            start = CurrentTimeMillis()
            while True:
                if self._done: break
                if CurrentTimeMillis() - start > await_timeout*1000: return False
                time.sleep(delta)
            self._done = False
            return True
        
        _out, _err = [], []
        def _decode(x):
            return RemoveTrailingNewline(self._shell.Decode(x))
        def _appender_out(x):
            msg = _decode(x)
            if msg == self._MARK: return
            _out.append(msg)
        def _appender_err(x):
            _err.append(_decode(x))
        self._shell.RegisterOnOut(_appender_out)
        self._shell.RegisterOnErr(_appender_err)
        if live_out is not None: self._shell.RegisterOnOut(lambda x: live_out(_decode(x)))
        if live_err is not None: self._shell.RegisterOnErr(lambda x: live_err(_decode(x)))

        self._silent = silent
        self._shell.Write(_strip_indent(cmd))
        start = CurrentTimeMillis()
        while True:
            try:
                self._shell.Write("echo done")
            except BrokenPipeError: break
            if _await_done(await_timeout=0.5, delta=0.1): break
            if timeout is not None and CurrentTimeMillis() - start > timeout*1000: break

        self._shell.RemoveOnOut(_appender_out)
        self._shell.RemoveOnErr(_appender_err)
        return _out, _err
    
    def __enter__(self):
        self._shell = TerminalProcess()
        def _check(x):
            if len(x) > len(self._MARK)+2: return
            x = RemoveTrailingNewline(self._shell.Decode(x))
            if x == self._MARK: self._done = True
        self._shell.RegisterOnOut(_check)
        def _tee(prefix):
            def _cb(x):
                if self._silent: return
                msg = RemoveTrailingNewline(self._shell.Decode(x))
                if len(msg) == 0: return
                if msg == self._MARK: return
                print(f"{prefix}{msg}")
            return _cb
        self._shell.RegisterOnErr(_tee("E: "))
        self._shell.RegisterOnOut(_tee("I: "))
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._shell.Dispose()
        self._shell = None

class TerminalProcess:
    class Pipe:
        def __init__(self, io:IO[bytes], lock: Condition = None) -> None:
            self.IO = io
            if lock is None: lock = Condition()
            self.Lock = lock

        def __enter__(self):
            self.Lock.acquire()

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.Lock.release()

    def __init__(self) -> None:
        # https://stackoverflow.com/questions/41542960/run-interactive-bash-with-popen-and-a-dedicated-tty-python
        out_master, out_slave = pty.openpty()
        err_master, err_slave = pty.openpty()
        self._fds = [out_master, err_master]

        console = subprocess.Popen(
            ["bash"],
            stdin=subprocess.PIPE,
            stdout=out_slave,
            stderr=err_slave,
            close_fds=True
        )

        self.ENCODING = "utf-8"
        self._console = console
        self._in = TerminalProcess.Pipe(console.stdin)
        self._onCloseLock = Condition()
        self._closed = False
        self.pid = console.pid
        self._err_reader = NonBlockingReader(err_master)
        self._out_reader = NonBlockingReader(out_master)

    def Send(self, payload: bytes):
        stdin = self._in
        with self._in:
            stdin.IO.write(payload)
            stdin.IO.flush()
    
    def Decode(self, payload: bytes):
        return payload.decode(encoding=self.ENCODING)

    def Write(self, msg: str):
        self.Send(bytes('%s\n' % (msg), encoding=self.ENCODING))

    def RegisterOnOut(self, callback: Callable[[bytes], None]):
        self._out_reader.RegisterCallback(callback)

    def RegisterOnErr(self, callback: Callable[[bytes], None]):
        self._err_reader.RegisterCallback(callback)

    def RemoveOnOut(self, callback: Callable[[bytes], None]):
        self._out_reader.RemoveCallback(callback)

    def RemoveOnErr(self, callback: Callable[[bytes], None]):
        self._err_reader.RemoveCallback(callback)
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.Dispose()
        return

    def Dispose(self):
        self._err_reader.Dispose()
        self._out_reader.Dispose()
        self._console.terminate()
        for i, fd in enumerate(self._fds):
            os.close(fd)

class NonBlockingReader:
    def __init__(self, io_handle: int, on_close: Callable[[NonBlockingReader], None] = None, sep: bytes = b"\n") -> None:
        self._callbacks = []
        self._lock = Condition()
        self._notify_in, self._notify_out = os.pipe() # https://stackoverflow.com/a/57341500/13690762
        self._on_close = on_close
        self._sep = sep
        self._worker = None
        self._is_closed = True
        self.Reset(io_handle)

    def Reset(self, io_handle: int):
        with self._lock:
            if not self._is_closed: return # only reset if closed
            self._is_closed = False

        def reader(fd: int, callbacks: list[Callable[[bytes], None]]):
            _buffer = []
            def _try_read():
                nonlocal _buffer
                changed = False
                while True:
                    # https://stackoverflow.com/a/21429655/13690762
                    r, _, _ = select.select([ fd, self._notify_out ], [], [], 60)
                    if fd not in r: break # notify_out triggered
                    chunk = os.read(fd, 4096)
                    if len(chunk) == 0:
                        with self._lock:
                            self._is_closed = True
                        return []
                    _buffer.append(chunk)
                    changed = True
                    if self._sep in chunk: break # line complete
                if not changed: return []

                complete_segments = []
                remainder = []
                lines = b''.join(_buffer).split(self._sep)
                for i, line in enumerate(lines):
                    if i < len(lines)-1:
                        complete_segments.append(line)
                    else: # last chunk
                        if len(line) > 0: remainder.append(line) # save for later if incomplete
                _buffer.clear()
                _buffer.extend(remainder)
                return complete_segments

            initial_wait, max_wait = 0.1, 600
            clear = True
            wait = 0.1
            def reset_wait():
                nonlocal wait, clear
                wait = initial_wait
                clear = True
            
            def scaling_wait():
                nonlocal wait, clear
                with self._lock:
                    self._lock.wait(wait)
                if not clear:
                    wait = min(wait*2, max_wait)
                clear = False

            while not self.IsClosed():
                try:
                    lines = list(_try_read())
                    for line in lines:
                        for cb in callbacks: cb(line)
                    reset_wait()
                except OSError as e: # fd closed
                    if e.errno == 9: # Bad file descriptor
                        break
                    else: # likely a race condition
                        scaling_wait()
            if callable(self._on_close): self._on_close(self)

        self._worker = Thread(target=reader, args=[io_handle, self._callbacks])
        self._worker.start()
        
    def RegisterCallback(self, callback: Callable[[bytes], None]):
        self._callbacks.append(callback)

    def RemoveCallback(self, callback: Callable[[bytes], None]):
        self._callbacks.remove(callback)

    def IsClosed(self):
        with self._lock:
            return self._is_closed

    def Dispose(self):
        with self._lock:
            self._is_closed = True
        try:
            with open(self._notify_in, "wb") as p:
                p.write(b"") # unblock reader
        except OSError:
            pass
        self._worker.join()

    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.Dispose()