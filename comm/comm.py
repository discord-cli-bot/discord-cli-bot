# SPDX-License-Identifier: Apache-2.0
import asyncio
import base64
import contextlib
import ctypes
import enum
import errno
import fcntl
import functools
import itertools
import json
import os
import pty
import re
import signal
import socket
import stat
import struct
import threading
import time
import traceback
import uuid

import fuse
import pyte

DISCORD_UPLOAD_MOUNT = '/run/discord-upload-fuse'
CONTAINER_RUN = '/run/container-run'
BOT_TCP_ADDR = '0.0.0.0', 49813

THIS_PID = os.getpid()

fuse.fuse_python_api = (0, 2)

discord_upload_callbacks_lock = threading.Lock()
discord_upload_callbacks = {}

lib = ctypes.cdll.LoadLibrary(None)

PyErr_SetFromErrno = ctypes.pythonapi.PyErr_SetFromErrno
PyErr_SetFromErrno.argtypes = [ctypes.py_object]
PyErr_SetFromErrno.restype = ctypes.py_object


def libc_errno_wrapper(func, errorreturn=-1):
    @functools.wraps(func)
    def wrapped(*args):
        res = func(*args)
        if res == errorreturn:
            PyErr_SetFromErrno(OSError)
            assert False

        return res

    return wrapped


mount = lib.mount
mount.restype = ctypes.c_int
mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                  ctypes.c_ulong, ctypes.c_char_p]
mount = libc_errno_wrapper(mount)

umount2 = lib.umount2
umount2.restype = ctypes.c_int
umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
umount2 = libc_errno_wrapper(umount2)

MNT_DETACH = 2


def path_to_uuid(path):
    if not path or path[0] != '/':
        return None
    path = path[1:]
    if '/' in path:
        return None
    return path


class Stat(fuse.Stat):
    def __init__(self):
        now = time.time()

        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = now
        self.st_mtime = now
        self.st_ctime = now


class DiscordUploaderFile:
    def __new__(cls, path, flags):
        with discord_upload_callbacks_lock:
            if path_to_uuid(path) not in discord_upload_callbacks:
                return -errno.ENOENT
        accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if (flags & accmode) != os.O_WRONLY:
            return -errno.EACCES

        return super().__new__(cls)

    def __init__(self, path, flags):
        self.uuid = path_to_uuid(path)
        self.iolock = threading.Lock()
        self.data = b''
        self.efbig_hit = False

    def read(self, size, offset):
        return -errno.EPERM

    def write(self, buf, offset):
        with discord_upload_callbacks_lock:
            if self.uuid not in discord_upload_callbacks:
                return -errno.EIO

        with self.iolock:
            if offset != len(self.data):
                return -errno.EINVAL

            # Limit 8MiB Upload
            if len(self.data) + len(buf) > (8 << 20):
                self.efbig_hit = True
                return -errno.EFBIG

            self.data += buf
            return len(buf)

    def release(self, flags):
        if self.data and not self.efbig_hit:
            with discord_upload_callbacks_lock:
                if self.uuid in discord_upload_callbacks:
                    discord_upload_callbacks[self.uuid](self.data)


class DiscordUploaderFS(fuse.Fuse):
    def _valid_path(self, path):
        with discord_upload_callbacks_lock:
            return path_to_uuid(path) in discord_upload_callbacks

    def getattr(self, path):
        st = Stat()
        if path == '/':
            st.st_mode = stat.S_IFDIR | 0o755
            st.st_nlink = 2
        elif self._valid_path(path):
            st.st_mode = stat.S_IFREG | 0o222
            st.st_nlink = 1
            st.st_size = 0
        else:
            return -errno.ENOENT
        return st

    def readdir(self, path, offset):
        for r in ('.', '..'):
            yield fuse.Direntry(r)

    def truncate(self, path, size):
        if not self._valid_path(path):
            return -errno.ENOENT

        if size:
            return -errno.EINVAL

        return 0

    def chmod(self, path, mode):
        return -errno.EPERM

    def chown(self, path, user, group):
        return -errno.EPERM

    def main(self, *args, **kwrgs):
        self.file_class = DiscordUploaderFile
        return super().main(*args, **kwrgs)


def set_cloexec(*fds):
    for fd in fds:
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


def unset_cloexec(*fds):
    for fd in fds:
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)


def recv_fd(sock):
    msg, ancdata, flags, addr = sock.recvmsg(
        1, socket.CMSG_LEN(struct.calcsize('=i')))
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            fd = struct.unpack('=i', cmsg_data)[0]
            set_cloexec(fd)
            return fd

    raise RuntimeError('no fd received')


def read_one_pkt(sock):
    # Normally I'd write C, but Python's recv seems to resize the buffer to
    # the number returned by the recv syscall (if number passed into recv is
    # positive), so... cool
    buf = sock.recv(1, socket.MSG_PEEK | socket.MSG_TRUNC)
    if not buf:
        return b''

    return sock.recv(len(buf))


class RestartException(Exception):
    pass


class BotClosedException(Exception):
    pass


@enum.unique
class osaibot_command(enum.IntEnum):
    CMD_INPUT = 1
    CMD_SIGNAL = 2


@enum.unique
class osaibot_response(enum.IntEnum):
    RESP_PROMPT = 1
    RESP_BEGIN = 2


class TermState(enum.Enum):
    BAD = enum.auto()
    IN_PROMPT = enum.auto()
    IN_EXEC_DIRECT = enum.auto()  # Outputs are directly sent
    IN_EXEC_TERMEMU = enum.auto()  # Outputs are interpreted


class FlushType(enum.Enum):
    IF_NECESSARY = enum.auto()
    HIT_TIMER = enum.auto()
    FORCED = enum.auto()


def make_rate_limiter(min_delay):
    last_ts = 0

    async def rate_limiter():
        nonlocal last_ts

        now = time.monotonic()
        least = last_ts + min_delay
        if now < least:
            await asyncio.sleep(least - now)

        last_ts = time.monotonic()

    return rate_limiter


def check_sgr_osc(data, ind):
    if data[ind + 1] == b'['[0]:
        # CSI

        # Bash uses bracketed paste mode. Ignore this too
        if (
            data[ind + 2] == b'?'[0] and
            data[ind + 3] == b'2'[0] and
            data[ind + 4] == b'0'[0] and
            data[ind + 5] == b'0'[0] and
            data[ind + 6] == b'4'[0] and
            data[ind + 7] in (b'h'[0], b'l'[0])
        ):
            return True, ind + 8

        for i in itertools.count(ind + 2):
            if data[i] == b'm'[0]:  # SGR
                return True, i + 1
            elif data[i] == b';'[0]:
                continue
            elif b'0'[0] <= data[i] <= b'9'[0]:
                continue
            else:
                return False, None
    elif data[ind + 1] == b']'[0]:
        # OSC
        if data[ind + 2] == b'P'[0]:
            # set palette
            data[ind + 8]
            return True, ind + 9
        elif data[ind + 2] == b'R'[0]:
            # reset palette
            return True, ind + 3
        else:
            for i in itertools.count(ind + 3):
                if data[i] == 7:  # BEL termination
                    return True, i + 1
                elif (
                    data[i] == 0x1b and
                    data[i + 1] == b'\\'[0]
                ):  # ST termination
                    return True, i + 2
                else:
                    continue
    else:
        return False, None


def trim_sgr_osc(data, assert_is):
    last_ind = -1
    while True:
        last_ind = data.find(b'\x1b', last_ind + 1)
        if last_ind < 0:
            break

        is_sgr_osc, end_ind = check_sgr_osc(data, last_ind)
        assert not assert_is or is_sgr_osc

        if is_sgr_osc:
            data = (data[:last_ind] + data[end_ind:])

            diff = end_ind - last_ind
            last_ind = end_ind - diff - 1

    return data


async def reader_wait_cb(fd, cb):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def reader_cb():
        if fut.done():
            return
        try:
            data = cb(fd)
        except (BlockingIOError, InterruptedError):
            return
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(data)

    loop.add_reader(fd, reader_cb)
    fut.add_done_callback(lambda fut: loop.remove_reader(fd))
    return await fut


async def writeall(fd, data):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    view = memoryview(data)
    pos = 0

    def writer_cb():
        nonlocal pos

        if fut.done():
            return

        start = pos
        try:
            n = os.write(fd, view[start:])
        except (BlockingIOError, InterruptedError):
            return
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            return

        start += n

        if start == len(view):
            fut.set_result(None)
        else:
            pos = start

    loop.add_writer(fd, writer_cb)
    fut.add_done_callback(lambda fut: loop.remove_writer(fd))
    return await fut


# If something other than sigchld_handler is holding this lock, PIDs of
# children are okay to be accessed directly and won't vanish into thin air.
# If you start a child process and get a PID out of it, hold this lock to
# convert it into a PIDFD, where we have full control over their lifetime.
reaper_lock = asyncio.Lock()


def sigchld_handler():
    async def _inner():
        async with reaper_lock:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except OSError:
                    break

                if not pid:
                    break

    asyncio.create_task(_inner())


class Comm():
    def __init__(self, reader, writer):
        self.uuid = uuid.uuid4()
        self.bot_reader = reader
        self.bot_writer = writer

        self.loop = asyncio.get_running_loop()
        self.killer = self.loop.create_future()
        self.rate_limit = make_rate_limiter(1.2)
        self.bot_response_lock = asyncio.Lock()

        self.exec_drain = set()
        self.write_tasks = set()
        self.next_cmd_ptm = set()

    @staticmethod
    def raise_restart(*args):
        raise RestartException

    async def on_exit_kill(self, pidfd):
        await reader_wait_cb(pidfd, self.raise_restart)

    async def init_conn(self):
        self.bash_pidfd = await reader_wait_cb(
            self.cmdsock.fileno(),
            lambda fd: recv_fd(self.cmdsock))

        self.netnsfd = await reader_wait_cb(
            self.cmdsock.fileno(),
            lambda fd: recv_fd(self.cmdsock))

    async def bot_response(self, data):
        async with self.bot_response_lock:
            await self.rate_limit()

            self.bot_writer.write(json.dumps(data).encode() + b'\n')
            await self.bot_writer.drain()

    async def handle_ptm(self, data, flushtype):
        data = data.replace(b'\x00', b'')

        if data and self.te_stream:
            self.te_stream.feed(data.decode(errors='replace'))

        flush_wait = False

        while True:
            data, self.pending_out = self.pending_out + data, b''

            if self.term_state == TermState.IN_EXEC_DIRECT:
                # Figure out if we should switch to IN_EXEC_TERMEMU
                # We end this part at a erase character, or
                # an ANSI escape code that isn't SGR.
                should_switch = False

                # Erase character, either ^H or ^?
                if b'\b' in data or b'\x7f' in data:
                    should_switch = True

                if not should_switch:
                    # Check for SGR & OSC
                    last_ind = -1
                    while True:
                        last_ind = data.find(b'\x1b', last_ind + 1)
                        if last_ind < 0:
                            break

                        try:
                            is_sgr_osc, ind_end = check_sgr_osc(
                                data, last_ind)
                        except IndexError:
                            # Not enough data to determine
                            # SGR & OSC
                            data, self.pending_out = (
                                data[:last_ind],
                                data[last_ind:] + self.pending_out)
                            break

                        if is_sgr_osc:
                            last_ind = ind_end - 1
                            continue

                        # Not SGR or OSC, switch
                        should_switch = True
                        break

                if should_switch:
                    data = b''
                    self.term_state = TermState.IN_EXEC_TERMEMU
                    continue

                # Trim SGR & OSC
                data = trim_sgr_osc(data, True)

                while data and data[-1] == b'\r':
                    data, self.pending_out = \
                        data[:-1], data[-1] + self.pending_out

                while b'\r\n' in data:
                    data = data.replace(b'\r\n', b'\n')

                has_pending_from_limit = False
                if data:
                    # Operation:
                    # IF_NECESSARY =>
                    # Flush if we reach character limit
                    # HIT_TIMER =>
                    # Always flush at least something, but stop
                    # at last linebreak if there is one
                    # FORCED =>
                    # Flush literally everything
                    should_flush = (
                        flushtype != FlushType.IF_NECESSARY)

                    # Flush if we are over 2000 chars, because of
                    # discord limit, and it is necessary to flush
                    if len(data) > 2000:
                        should_flush = True
                        data, self.pending_out = (
                            data[:2000],
                            data[2000:] + self.pending_out)

                        # cause another iteration
                        has_pending_from_limit = True

                    # try to split on last linebreak if there
                    # is one, if we are not forced to flush
                    if flushtype != FlushType.FORCED:
                        nr_ind = data.rfind(b'\n')
                        if nr_ind >= 0 and nr_ind != len(data) - 1:
                            data, self.pending_out = (
                                data[:nr_ind + 1],
                                data[nr_ind + 1:] + self.pending_out)
                            flush_wait = True

                    if should_flush:
                        await self.bot_response({
                            'type': 'DIRECT',
                            'payload': data.decode(errors='replace'),
                        })
                        self.last_ptm_flush = time.monotonic()
                    else:
                        data, self.pending_out = (
                            b'', data + self.pending_out)
                        flush_wait = True
                        should_switch = False

                if has_pending_from_limit:
                    data = b''
                    continue
                else:
                    break
            elif self.term_state == TermState.IN_EXEC_TERMEMU:
                if flushtype != FlushType.IF_NECESSARY:
                    display = ' ' * (self.te_screen.cursor.x + 1)
                    display += '|\n'

                    for i, line in enumerate(self.te_screen.display):
                        display += (
                            '-' if self.te_screen.cursor.y == i
                            else ' ')
                        display += line.rstrip() + '\n'

                    await self.bot_response({
                        'type': 'DISPLAY',
                        'payload': display,
                    })
                    self.last_ptm_flush = time.monotonic()
                else:
                    flush_wait = True
                break
            else:
                self.pending_out = b''
                break

        self.has_flush_wait = flush_wait

    async def handle_cmd(self, cmd, payload):
        if cmd == osaibot_response.RESP_PROMPT:
            # We return to prompt, flush whatever is left
            await self.handle_ptm(b'', FlushType.FORCED)
            assert not self.has_flush_wait

            self.term_state = TermState.IN_PROMPT

            # Kill all pending PTM inputs
            for task in self.exec_drain:
                if not task.done():
                    task.cancel()

            prompt = payload

            # Trim SGR & OSC
            with contextlib.suppress(IndexError):
                prompt = trim_sgr_osc(prompt, False)

            # Trim anything ANSI in prompt
            # This doesn't Trim OSC properly
            # regex from
            # https://stackoverflow.com/a/14693789
            prompt = re.sub(
                br'(?:\x1B[@-Z\\-_]|[\x80-\x9A\x9C-\x9F]|'
                br'(?:\x1B\[|\x9B)[0-?]*[ -/]*[@-~])',
                b'', prompt)

            # If still unsupported stuffs left, delete them
            prompt.translate(None, b'\x1b\r\b\x7f')

            await self.bot_response({
                'type': 'PROMPT',
                'payload': prompt.decode(errors='replace'),
            })
        elif cmd == osaibot_response.RESP_BEGIN:
            self.term_state = TermState.IN_EXEC_DIRECT

            self.te_screen = pyte.Screen(80, 24)
            self.te_screen.write_process_input = (
                lambda data: os.write(
                    self.ptm_fd, data.encode()))
            self.te_stream = pyte.Stream(self.te_screen)
        else:
            raise AssertionError

    async def on_cmd_ptm(self):
        self.pending_out = b''
        self.te_screen = None
        self.te_stream = None

        def gen_ptm_task():
            return asyncio.create_task(reader_wait_cb(
                self.ptm_fd,
                lambda fd: ('ptm', os.read(fd, 1024))))

        def gen_cmd_task():
            def decode_cmd(data):
                if not data:
                    raise RestartException

                return data[0], data[1:]

            return asyncio.create_task(reader_wait_cb(
                self.cmdsock.fileno(),
                lambda fd: ('cmd', decode_cmd(
                    read_one_pkt(self.cmdsock)))))

        self.last_ptm_flush = time.monotonic()
        self.has_flush_wait = False
        self.next_cmd_ptm = {gen_ptm_task(), gen_cmd_task()}

        while True:
            if self.has_flush_wait:
                # Aggregate output, throttle to flush
                # when 0.5 secs timeout
                timeout = self.last_ptm_flush + 0.5 - time.monotonic()
                timeout = max(0, timeout)
            else:
                timeout = None

            if timeout is not None and timeout <= 0:
                await self.handle_ptm(b'', FlushType.HIT_TIMER)
                continue

            done, self.next_cmd_ptm = await asyncio.wait(
                self.next_cmd_ptm, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)

            if not done:
                # Timeout hit, flush
                await self.handle_ptm(b'', FlushType.HIT_TIMER)
                continue

            if self.next_cmd_ptm:
                # Try to get more resolved, else if one task is already
                # resolved (due to race tiebreaker), asyncio.wait will
                # immediately return
                done_2nd, self.next_cmd_ptm = await asyncio.wait(
                    self.next_cmd_ptm, timeout=0,
                    return_when=asyncio.FIRST_COMPLETED)
                done |= done_2nd

            # If we don't have timeout, we have potentially waited long time
            # start last_ptm_flush from now so the next timeout won't be zero
            if timeout is None:
                self.last_ptm_flush = time.monotonic()

            done_dict = {}
            for task in done:
                src, _ = await task
                assert src not in done_dict
                done_dict[src] = task

            # Race tiebreaker
            if 'ptm' in done_dict and 'cmd' in done_dict:
                _, (cmd, _) = await done_dict['cmd']
                if (
                    self.term_state == TermState.IN_PROMPT and
                    cmd == osaibot_response.RESP_BEGIN
                ):
                    # Transition: Prompt -> Exec, cmd gets priorty
                    drop = 'ptm'
                elif cmd == osaibot_response.RESP_PROMPT:
                    # Transition: Exec -> Prompt, ptm gets priorty
                    drop = 'cmd'
                else:
                    # Misc. cmd, cmd gets priorty
                    drop = 'ptm'

                drop_task = done_dict[drop]
                done.remove(drop_task)
                self.next_cmd_ptm.add(drop_task)

            for task in done:
                src, data = await task
                if src == 'ptm':
                    if not data:
                        raise RestartException

                    self.next_cmd_ptm.add(gen_ptm_task())

                    await self.handle_ptm(data, FlushType.IF_NECESSARY)
                elif src == 'cmd':
                    cmd, payload = data

                    self.next_cmd_ptm.add(gen_cmd_task())

                    await self.handle_cmd(cmd, payload)

    async def on_botmsg(self):
        cmd_lock = asyncio.Lock()
        ptm_lock = asyncio.Lock()

        def make_write_task(lock, exec_drain, coroutine):
            async def inner():
                try:
                    # Without lock, if a second task attempt to write
                    # the first is blocked waiting for poll, the second
                    # will cancel the first (in loop.add_writer).
                    async with lock:
                        await coroutine
                except asyncio.CancelledError:
                    pass
                except BaseException as e:
                    self.killer.set_exception(e)

            task = asyncio.create_task(inner())
            self.write_tasks.add(task)
            if exec_drain:
                self.exec_drain.add(task)

            def done_cb(arg):
                self.write_tasks.discard(task)
                if exec_drain:
                    self.exec_drain.discard(task)

            task.add_done_callback(done_cb)

        while True:
            data = (await self.bot_reader.readline()).strip()
            if not data:
                raise BotClosedException

            data = json.loads(data)

            if data['type'] == 'INPUT':
                payload = data['payload']
                if self.term_state == TermState.IN_PROMPT:
                    payload = (struct.pack('=b', osaibot_command.CMD_INPUT)
                               + payload.encode())
                    make_write_task(
                        cmd_lock, False,
                        self.loop.sock_sendall(self.cmdsock, payload))
                elif self.term_state in [
                    TermState.IN_EXEC_DIRECT,
                    TermState.IN_EXEC_TERMEMU,
                ]:
                    # For some reason, Enter is CR not NL
                    payload = payload.replace('\n', '\r').encode()
                    make_write_task(
                        ptm_lock, True,
                        writeall(self.ptm_fd, payload))
            elif data['type'] == 'SIGNAL':
                if self.term_state in [
                    TermState.IN_EXEC_DIRECT,
                    TermState.IN_EXEC_TERMEMU,
                ]:
                    signum = data['signum']
                    payload = struct.pack(
                        '=bi', osaibot_command.CMD_SIGNAL, signum)
                    make_write_task(
                        cmd_lock, True,
                        self.loop.sock_sendall(self.cmdsock, payload))
            else:
                raise AssertionError

    def upload_cb(self, data):
        async def _upload_cb_inner():
            try:
                await self.bot_response({
                    'type': 'UPLOAD',
                    'payload': base64.b64encode(data).decode(),
                })
            except asyncio.CancelledError:
                pass
            except BaseException as e:
                self.killer.set_exception(e)

        def _upload_cb():
            task = asyncio.create_task(_upload_cb_inner())
            self.write_tasks.add(task)

            def done_cb(arg):
                self.write_tasks.discard(task)

            task.add_done_callback(done_cb)

        self.loop.call_soon_threadsafe(_upload_cb)

    async def run(self):
        discord_upload_callbacks[str(self.uuid)] = self.upload_cb

        try:
            await self._run()
        finally:
            del discord_upload_callbacks[str(self.uuid)]

    async def _run(self):
        idname = None
        reinit = False

        try:
            async def read_idname():
                data = (await self.bot_reader.readline()).strip()
                if not data:
                    raise BotClosedException

                data = json.loads(data)
                assert data['type'] == 'INIT'

                nonlocal idname, reinit
                idname = data['idname']
                reinit = data['reinit']

                assert re.match(r'^[a-zA-Z0-9]{1,30}$', idname)
            await asyncio.wait_for(read_idname(), 1)
        except Exception:
            traceback.print_exc()
            raise BotClosedException

        run = os.path.join(CONTAINER_RUN, idname)
        run_exists = os.path.exists(run)
        rootdir = os.path.join(run, 'root')

        if reinit or not run_exists:
            try:
                umount2(run.encode(), MNT_DETACH)
            except OSError:
                pass

            try:
                try:
                    os.mkdir(run)
                except FileExistsError:
                    pass

                mount(b'tmpfs', run.encode(), b'tmpfs', 0, None)

                os.mkdir(os.path.join(run, 'upper'))
                os.mkdir(os.path.join(run, 'work'))
                os.mkdir(rootdir)

                mount(b'overlay', rootdir.encode(), b'overlay', 0,
                      f'lowerdir=/jailroot,'
                      f'upperdir={os.path.join(run, "upper")},'
                      f'workdir={os.path.join(run, "work")}'.encode())
            except OSError:
                umount2(run.encode(), MNT_DETACH)
                raise

        assert os.path.exists(rootdir)

        cmdsockjail = None
        exe_fd = None

        try:
            exe_fd = os.memfd_create('osaibot-bash')

            with open('/home/user/bash') as exe_base:
                exe_base.seek(0, os.SEEK_END)
                exe_size = exe_base.tell()
                offset = 0

                while offset < exe_size:
                    offset += os.sendfile(exe_fd, exe_base.fileno(),
                                          offset, exe_size - offset)

            self.cmdsock, cmdsockjail = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK,
                0)

            async with reaper_lock:
                bwrap_pid, self.ptm_fd = pty.fork()

                if not bwrap_pid:
                    unset_cloexec(cmdsockjail.fileno(), exe_fd)
                    os.environ['SOCK_FD'] = str(cmdsockjail.fileno())
                    os.environ['EXE_FD'] = str(exe_fd)
                    os.environ['ROOTDIR'] = rootdir
                    os.environ['DISCORD_UPLOAD_UUID'] = str(self.uuid)

                    os.execlp('/home/user/jail.sh', '/home/user/jail.sh')
                    os._exit(1)

                # Set PTM non-blocking
                flags = fcntl.fcntl(self.ptm_fd, fcntl.F_GETFL)
                fcntl.fcntl(self.ptm_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                set_cloexec(self.ptm_fd)

                self.bwrap_pidfd = os.pidfd_open(bwrap_pid, 0)
        finally:
            if cmdsockjail is not None:
                cmdsockjail.close()

            if exe_fd is not None:
                os.close(exe_fd)

        tasks = {self.killer}

        self.term_state = TermState.BAD
        self.bash_pidfd = None
        self.netnsfd = None
        self.slirp4netns_pidfd = None

        try:
            tasks.add(asyncio.create_task(
                self.on_exit_kill(self.bwrap_pidfd)))

            await asyncio.wait_for(self.init_conn(), 1)

            async with reaper_lock:
                # Normally slirp4netns takes a PID, but we don't have it.
                # We have the wrapper's PID, but it's outside the ns.
                # We also have PIDFDs, but IDK a way to translate them to PIDs.
                # Only way I know to to look at process tree :(
                slirp4netns_pid = os.spawnlp(
                    os.P_NOWAIT, 'slirp4netns',
                    'slirp4netns',
                    '--configure',
                    '--mtu=65520',
                    '--disable-host-loopback',
                    '--enable-sandbox',
                    '--enable-seccomp',
                    '--netns-type=path',
                    f'/proc/{THIS_PID}/fd/{self.netnsfd}',
                    'tap0')

                self.slirp4netns_pidfd = os.pidfd_open(slirp4netns_pid)

            tasks.add(asyncio.create_task(
                self.on_exit_kill(self.slirp4netns_pidfd)))

            tasks.add(asyncio.create_task(self.on_cmd_ptm()))
            tasks.add(asyncio.create_task(self.on_botmsg()))

            await asyncio.gather(*tasks)
        except RestartException:
            pass
        finally:
            # This is not awaited. The callback mechanism from
            # make_write_task will handle it.
            for task in self.write_tasks:
                if not task.done():
                    task.cancel()

            for task in tasks | self.next_cmd_ptm:
                if not task.done():
                    task.cancel()

                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    traceback.print_exc()

            if self.bwrap_pidfd is not None:
                with contextlib.suppress(OSError):
                    signal.pidfd_send_signal(
                        self.bwrap_pidfd, signal.SIGKILL, None, 0)
                os.close(self.bwrap_pidfd)
            if self.bash_pidfd is not None:
                with contextlib.suppress(OSError):
                    signal.pidfd_send_signal(
                        self.bash_pidfd, signal.SIGKILL, None, 0)
                os.close(self.bash_pidfd)
            if self.slirp4netns_pidfd is not None:
                with contextlib.suppress(OSError):
                    signal.pidfd_send_signal(
                        self.slirp4netns_pidfd, signal.SIGTERM, None, 0)
                os.close(self.slirp4netns_pidfd)

            if self.netnsfd is not None:
                os.close(self.netnsfd)

            if self.cmdsock is not None:
                self.cmdsock.close()

            os.close(self.ptm_fd)


async def main():
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGCHLD, sigchld_handler)

    with open('/proc/sys/net/ipv4/ping_group_range', 'w') as f:
        f.write('0 65535')

    def fuse_thread():
        fuse_server = DiscordUploaderFS(
            version='%prog ' + fuse.__version__,
            usage=fuse.Fuse.fusage,
            dash_s_do='setsingle')

        fuse_server.parse([
            '-f',
            '-o', 'allow_other',
            '-o', 'fsname=discord',
            '-o', 'subtype=osaibot',
            DISCORD_UPLOAD_MOUNT], errex=1)
        fuse_server.main()

    threading.Thread(target=fuse_thread, daemon=True).start()

    async def handle_connection(reader, writer):
        try:
            while True:
                comm = Comm(reader, writer)
                await comm.run()
        except BotClosedException:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle_connection, *BOT_TCP_ADDR)

    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
