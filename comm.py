# SPDX-License-Identifier: Apache-2.0
import asyncio
import contextlib
import enum
import fcntl
import itertools
import json
import os
import pty
import re
import signal
import socket
import struct
import time
import traceback

import pyte


CMD_UNIX_PATH = '/tmp/socket'
BOT_TCP_ADDR = '0.0.0.0', 49813


def recv_fd(sock):
    msg, ancdata, flags, addr = sock.recvmsg(
        1, socket.CMSG_LEN(struct.calcsize('=i')))
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            return struct.unpack('=i', cmsg_data)[0]

    raise RuntimeError('no fd received')


def read_one_pkt(sock):
    # Normally I'd write C, but Python's recv seems to resize the buffer to
    # the Nnumber returned by the recv syscall (if number passed into recv is
    # positive), so... cool
    buf = sock.recv(1, socket.MSG_PEEK | socket.MSG_TRUNC)
    if not buf:
        return b''

    return sock.recv(len(buf))


class RestartException(Exception):
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

    loop._add_writer(fd, writer_cb)
    fut.add_done_callback(lambda fut: loop.remove_writer(fd))
    return await fut


class Comm():
    def __init__(self, reader, writer):
        self.bot_reader = reader
        self.bot_writer = writer

        self.loop = asyncio.get_running_loop()
        self.killer = self.loop.create_future()
        self.rate_limit = make_rate_limiter(1.2)

        self.ongoing_writes = {
            'cmd': set(),
            'ptm': set(),
        }

    @staticmethod
    def raise_restart(*args):
        raise RestartException

    async def on_exit_kill(self):
        await reader_wait_cb(self.bwrap_pidfd, self.raise_restart)

    async def init_conn(self):
        self.cmdsock, addr = await self.loop.sock_accept(self.cmdmainsock)

        self.cmdmainsock.close()
        self.cmdmainsock = None
        with contextlib.suppress(OSError):
            os.unlink(CMD_UNIX_PATH)

        self.bash_pidfd = await reader_wait_cb(
            self.cmdsock.fileno(),
            lambda fd: recv_fd(self.cmdsock))

    async def bot_response(self, data):
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
                # We end this part at a backspace character, or
                # an ANSI escape code that isn't SGR.
                should_switch = False
                if b'\b' in data:
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
                    display = 'O' * (self.te_screen.cursor.x + 1)
                    display += 'X'
                    display += 'O' * (80 - self.te_screen.cursor.x - 1)
                    display += '\n'

                    for i, line in enumerate(self.te_screen.display):
                        display += (
                            'X' if self.te_screen.cursor.y == i
                            else 'O')
                        display += line.rstrip() + '\n'

                    await self.bot_response({
                        'type': 'DISPLAY',
                        'payload': display,
                    })
                else:
                    flush_wait = True
                break
            else:
                self.pending_out = b''
                break

        self.last_ptm_handle = time.monotonic()
        self.has_flush_wait = flush_wait

    async def handle_cmd(self, cmd, payload):
        if cmd == osaibot_response.RESP_PROMPT:
            # We return to prompt, flush whatever is left
            await self.handle_ptm(b'', FlushType.FORCED)
            assert not self.has_flush_wait

            self.term_state = TermState.IN_PROMPT

            # Kill all pending PTM inputs
            for task in self.ongoing_writes['ptm']:
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
            prompt.translate(None, b'\x1B\r\b')

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

        self.last_ptm_handle = time.monotonic()
        self.has_flush_wait = False
        next_tasks = {gen_ptm_task(), gen_cmd_task()}

        while True:
            if self.has_flush_wait:
                # Aggregate output, throttle to flush
                # when 0.5 secs timeout
                timeout = self.last_ptm_handle + 0.5 - time.monotonic()
                timeout = max(0, timeout)
            else:
                timeout = None

            done, next_tasks = await asyncio.wait(
                next_tasks, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)

            if not done:
                # Timeout hit, flush
                await self.handle_ptm(b'', FlushType.HIT_TIMER)
                continue

            if next_tasks:
                # Try to get more resolved, else if one task is already
                # resolved (due to race tiebreaker), asyncio.wait will
                # immediately return
                done_2nd, next_tasks = await asyncio.wait(
                    next_tasks, timeout=0,
                    return_when=asyncio.FIRST_COMPLETED)
                done |= done_2nd

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
                next_tasks.add(drop_task)

            for task in done:
                src, data = await task
                if src == 'ptm':
                    if not data:
                        raise RestartException

                    next_tasks.add(gen_ptm_task())

                    await self.handle_ptm(data, FlushType.IF_NECESSARY)
                elif src == 'cmd':
                    cmd, payload = data

                    next_tasks.add(gen_cmd_task())

                    await self.handle_cmd(cmd, payload)

    async def on_botmsg(self):
        def make_write_task(typ, coroutine):
            async def inner():
                try:
                    await coroutine
                except asyncio.CancelledError:
                    pass
                except BaseException as e:
                    self.killer.set_exception(e)

            task = asyncio.create_task(coroutine)
            self.ongoing_writes[typ].add(task)
            task.add_done_callback(
                lambda arg: self.ongoing_writes[typ].discard(task))

        while True:
            data = (await self.bot_reader.readline()).strip()
            if not data:
                raise RestartException

            data = json.loads(data)

            if data['type'] == 'INPUT':
                payload = data['payload']
                if self.term_state == TermState.IN_PROMPT:
                    payload = (struct.pack('=b', osaibot_command.CMD_INPUT)
                               + payload.encode())
                    make_write_task(
                        'cmd', self.loop.sock_sendall(self.cmdsock, payload))
                elif self.term_state in [
                    TermState.IN_EXEC_DIRECT,
                    TermState.IN_EXEC_TERMEMU,
                ]:
                    # For some reason, Enter is CR not NL
                    payload = payload.replace('\n', '\r').encode()
                    make_write_task('ptm', writeall(self.ptm_fd, payload))
            else:
                raise AssertionError

    async def run(self):
        self.cmdmainsock = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK,
            0)
        with contextlib.suppress(OSError):
            os.unlink(CMD_UNIX_PATH)
        self.cmdmainsock.bind(CMD_UNIX_PATH)
        self.cmdmainsock.listen(1)

        bwrap_pid, self.ptm_fd = pty.fork()
        if not bwrap_pid:
            os.execlp('./bwrap.sh', './bwrap.sh')
            os._exit(1)

        # Set PTM non-blocking
        flags = fcntl.fcntl(self.ptm_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.ptm_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self.bwrap_pidfd = os.pidfd_open(bwrap_pid, 0)
        tasks = {self.killer}

        self.term_state = TermState.BAD
        self.cmdsock = None
        self.bash_pidfd = None

        try:
            tasks.add(asyncio.create_task(self.on_exit_kill()))

            await asyncio.wait_for(self.init_conn(), 1)

            tasks.add(asyncio.create_task(self.on_cmd_ptm()))

            tasks.add(asyncio.create_task(self.on_botmsg()))

            await asyncio.gather(*tasks)
        except Exception:
            pass
        finally:
            # This is not awaited. The callback mechanism from
            # make_write_task will handle it.
            for task in self.ongoing_writes['ptm']:
                if not task.done():
                    task.cancel()
            for task in self.ongoing_writes['cmd']:
                if not task.done():
                    task.cancel()

            for task in tasks:
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

            if self.cmdsock is not None:
                self.cmdsock.close()

            if self.cmdmainsock is not None:
                self.cmdmainsock.close()

            os.waitid(os.P_PID, bwrap_pid, os.WEXITED)
            os.close(self.ptm_fd)


async def main():
    async def handle_connection(reader, writer):
        comm = Comm(reader, writer)

        try:
            while True:
                await comm.run()
        finally:
            writer.close()

    server = await asyncio.start_server(handle_connection, *BOT_TCP_ADDR)

    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
