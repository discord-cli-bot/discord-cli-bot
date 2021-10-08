import asyncio
import json
import signal
import unittest


class Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reader, self.writer = await asyncio.open_connection(
            '127.0.0.1', 49813)

        await self.assert_simple_prompt()

    async def asyncTearDown(self):
        self.writer.close()
        await self.writer.wait_closed()

    async def send(self, message):
        self.writer.write(json.dumps(message).encode() + b'\n')
        await self.writer.drain()

    async def recv(self, timeout=2):
        data = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        return json.loads(data.strip().decode())

    async def assertResp(self, expected, timeout=2):
        message = await self.recv(timeout=timeout)
        self.assertEqual(message, expected)

    async def assert_simple_prompt(self):
        await self.assertResp({"type": "PROMPT", "payload": "root@NSJAIL:/# "})

    async def test_hello_world(self):
        await self.send({"type": "INPUT", "payload": "echo hello world\n"})
        await self.assertResp({"type": "DIRECT", "payload": "hello world\n"})
        await self.assert_simple_prompt()

    async def test_two_command(self):
        await self.send({"type": "INPUT", "payload": "echo hello\n"})
        await self.assertResp({"type": "DIRECT", "payload": "hello\n"})
        await self.assert_simple_prompt()

        await self.send({"type": "INPUT", "payload": "echo world\n"})
        await self.assertResp({"type": "DIRECT", "payload": "world\n"})
        await self.assert_simple_prompt()

    async def test_multiline_command(self):
        await self.send({"type": "INPUT", "payload": "echo hello &&\n"})
        await self.assertResp({"type": "PROMPT", "payload": "> "})
        await self.send({"type": "INPUT", "payload": "echo world\n"})
        await self.assertResp({"type": "DIRECT", "payload": "hello\nworld\n"})
        await self.assert_simple_prompt()

    async def test_input_is_echoed_line(self):
        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "INPUT", "payload": "hello world\n"})
        await self.assertResp({"type": "DIRECT",
                               "payload": "hello world\nhello world\n"})
        await self.send({"type": "INPUT", "payload": "hello\n"})
        await self.assertResp({"type": "DIRECT", "payload": "hello\nhello\n"})
        await self.send({"type": "INPUT", "payload": "world\n"})
        await self.assertResp({"type": "DIRECT", "payload": "world\nworld\n"})

    async def test_input_is_echoed_partial(self):
        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "INPUT", "payload": "hello "})
        await self.assertResp({"type": "DIRECT", "payload": "hello "})
        await self.send({"type": "INPUT", "payload": "world\n"})
        await self.assertResp({"type": "DIRECT",
                               "payload": "world\nhello world\n"})

    async def test_input_is_echoed_readline(self):
        await self.send({"type": "INPUT", "payload": "bash\n"})
        await self.assertResp({"type": "DIRECT", "payload": "root@NSJAIL:/# "})
        await self.send({"type": "INPUT", "payload": "echo hello "})
        await self.assertResp({"type": "DIRECT", "payload": "echo hello "})
        await self.send({"type": "INPUT", "payload": "world\n"})
        await self.assertResp({"type": "DIRECT",
                               "payload": "world\nhello world\n"})

    async def test_no_color(self):
        await self.send({"type": "INPUT", "payload": "ls --color\n"})
        await self.assertResp({"type": "DIRECT", "payload":
            "bin   dev  home  lib32  libx32  mnt  proc  run   srv  tmp  var\n"
            "boot  etc  lib   lib64  media   opt  root  sbin  sys  usr\n"})
        await self.assert_simple_prompt()

    async def test_render_screen(self):
        await self.send({"type": "INPUT", "payload": "head -c 1024 /dev/zero | xxd | less\n"})
        await self.assertResp({"type": "DISPLAY", "payload":
            "OOXOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO\n"
            "O00000000: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000010: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000020: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000030: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000040: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000050: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000060: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000070: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000080: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000090: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000a0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000b0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000c0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000d0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000e0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000f0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000100: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000110: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000120: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000130: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000140: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000150: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000160: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "X:\n"})
        await self.send({"type": "INPUT", "payload": "q"})
        await self.assertResp({"type": "DISPLAY", "payload":
            "OXOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO\n"
            "O00000000: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000010: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000020: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000030: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000040: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000050: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000060: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000070: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000080: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000090: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000a0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000b0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000c0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000d0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000e0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000f0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000100: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000110: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000120: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000130: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000140: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000150: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000160: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "X\n"})
        await self.assert_simple_prompt()

    async def test_input_flushed_on_prompt(self):
        # NOTE: was fixed by changing TCSADRAIN to TCSAFLUSH
        # in bash osaibot_prompt.
        # The idea is, in a program that reads from pts char by char,
        # and exits immediately after reading "q", if you type "q\n",
        # and then run cat upon next prompt, "\n" is still in the read buffer,
        # so cat will immediately output "\n"
        await self.send({"type": "INPUT", "payload": "head -c 1024 /dev/zero | xxd | less\n"})
        await self.assertResp({"type": "DISPLAY", "payload":
            "OOXOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO\n"
            "O00000000: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000010: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000020: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000030: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000040: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000050: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000060: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000070: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000080: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000090: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000a0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000b0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000c0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000d0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000e0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000f0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000100: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000110: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000120: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000130: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000140: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000150: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000160: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "X:\n"})
        await self.send({"type": "INPUT", "payload": "q\n"})
        await self.assertResp({"type": "DISPLAY", "payload":
            "OXOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO\n"
            "O00000000: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000010: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000020: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000030: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000040: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000050: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000060: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000070: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000080: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000090: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000a0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000b0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000c0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000d0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000e0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O000000f0: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000100: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000110: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000120: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000130: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000140: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000150: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "O00000160: 0000 0000 0000 0000 0000 0000 0000 0000  ................\n"
            "X\n"})
        await self.assert_simple_prompt()

        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "INPUT", "payload": "hello world\n"})
        await self.assertResp({"type": "DIRECT",
                               "payload": "hello world\nhello world\n"})

    async def test_tty_keys_job_ctrl(self):
        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "INPUT", "payload": "\u001a"})
        await self.assertResp({"type": "DIRECT", "payload":
                               "^Z\n[1]+  Stopped                 cat\n"})

        await self.assert_simple_prompt()
        await self.send({"type": "INPUT", "payload": "fg\n"})
        await asyncio.sleep(0.5)
        await self.assertResp({"type": "DIRECT", "payload": "cat\n"})
        await self.send({"type": "INPUT", "payload": "\u0003"})
        await self.assertResp({"type": "DIRECT", "payload": "^C\n"})
        await self.assert_simple_prompt()

    async def test_signal(self):
        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "SIGNAL", "signum": signal.SIGTSTP})
        await self.assertResp({"type": "DIRECT", "payload":
                               "\n[1]+  Stopped                 cat\n"})
        await self.assert_simple_prompt()

        await self.send({"type": "INPUT", "payload": "fg\n"})
        await asyncio.sleep(0.5)
        await self.assertResp({"type": "DIRECT", "payload": "cat\n"})
        await self.send({"type": "SIGNAL", "signum": signal.SIGINT})
        await self.assertResp({"type": "DIRECT", "payload": "\n"})
        await self.assert_simple_prompt()

        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "SIGNAL", "signum": signal.SIGTERM})
        await self.assertResp({"type": "DIRECT", "payload":
                               "Terminated\n"})
        await self.assert_simple_prompt()

        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "SIGNAL", "signum": signal.SIGKILL})
        await self.assertResp({"type": "DIRECT", "payload":
                               "Killed\n"})
        await self.assert_simple_prompt()

    async def test_no_background(self):
        # This is controlled by TOSTOP flag. Background processes
        # may out send output to TTY
        await self.send({"type": "INPUT", "payload": "xxd /dev/zero &\n"})
        msg = await self.recv()
        self.assertEqual(msg['type'], 'DIRECT')
        self.assertRegex(msg['payload'], r'^\[1\] +\d+\n$')
        await self.assert_simple_prompt()

        await asyncio.sleep(1)
        await self.send({"type": "INPUT", "payload": "cat\n"})
        await asyncio.sleep(0.5)
        await self.send({"type": "INPUT", "payload": "hello world\n"})
        await self.assertResp({"type": "DIRECT",
                               "payload": "hello world\nhello world\n"})

    async def test_conn_restart(self):
        await self.send({"type": "INPUT", "payload": "exit\n"})
        await self.assert_simple_prompt()
        await self.send({"type": "INPUT", "payload": "exec false\n"})
        await self.assert_simple_prompt()
        await self.send({"type": "INPUT", "payload": "exec true\n"})
        await self.assert_simple_prompt()
        await self.send({"type": "INPUT", "payload": "exec sleep infinity\n"})
        await self.assert_simple_prompt()

    async def test_no_env_pollution(self):
        await self.send({"type": "INPUT", "payload": "echo $SOCK_FD\n"})
        await self.assertResp({"type": "DIRECT", "payload": "\n"})
        await self.assert_simple_prompt()
        await self.send({"type": "INPUT", "payload": "echo $EXE_FD\n"})
        await self.assertResp({"type": "DIRECT", "payload": "\n"})
        await self.assert_simple_prompt()

    async def test_dev_discord(self):
        await self.send(
            {"type": "INPUT", "payload": "echo hello world > /dev/discord\n"})
        await self.assertResp(
            {"type": "UPLOAD", "payload": "aGVsbG8gd29ybGQK"})
        await self.assert_simple_prompt()


if __name__ == "__main__":
    unittest.main()
