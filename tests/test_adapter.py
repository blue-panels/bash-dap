"""The tests of bash-dap: the adapter run as a client would run it, on the scripts beside."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(HERE, "scripts")
sys.path.insert(0, ROOT)

from bash_dap.adapter import parse_declare, unquote  # noqa: E402


def script(name):
    return os.path.realpath(os.path.join(SCRIPTS, name))


class Client:
    """A client of the adapter: requests, their responses, and the events in order."""

    def __init__(self, run_in_terminal=False):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        self.proc = subprocess.Popen([sys.executable, "-m", "bash_dap"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, env=env)
        self.seq = 0
        self.messages = queue.Queue()
        self.events = []
        self.terminal = None
        self.run_in_terminal = run_in_terminal
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        out = self.proc.stdout
        while True:
            length = None
            while True:
                line = out.readline()
                if not line:
                    self.messages.put(None)
                    return
                line = line.strip()
                if not line:
                    break
                name, _, value = line.partition(b":")
                if name.lower() == b"content-length":
                    length = int(value)
            self.messages.put(json.loads(out.read(length)))

    def send(self, message):
        self.seq += 1
        message["seq"] = self.seq
        body = json.dumps(message).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()
        return self.seq

    def handle(self, message):
        """A reverse request: runInTerminal runs the program as a client does, in a session of
        its own"""
        if message.get("type") == "request" and message["command"] == "runInTerminal":
            args = message["arguments"]
            env = dict(os.environ)
            env.update(args.get("env", {}))
            self.terminal = subprocess.Popen(args["args"], cwd=args["cwd"], env=env,
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             start_new_session=True)
            self.send({"type": "response", "request_seq": message["seq"],
                       "command": "runInTerminal", "success": True,
                       "body": {"processId": self.terminal.pid}})
            return True
        return False

    def next_message(self, timeout):
        message = self.messages.get(timeout=timeout)
        if message is None:
            raise EOFError("the adapter has gone")
        return message

    def request(self, command, arguments=None, timeout=10):
        seq = self.send({"type": "request", "command": command, "arguments": arguments or {}})
        end = time.monotonic() + timeout
        while True:
            message = self.next_message(end - time.monotonic())
            if self.handle(message):
                continue
            if message.get("type") == "event":
                self.events.append(message)
            elif message.get("request_seq") == seq:
                return message

    def wait_event(self, name, timeout=10):
        for i, event in enumerate(self.events):
            if event["event"] == name:
                return self.events.pop(i)
        end = time.monotonic() + timeout
        while True:
            message = self.next_message(end - time.monotonic())
            if self.handle(message):
                continue
            if message.get("type") == "event":
                if message["event"] == name:
                    return message
                self.events.append(message)

    def output(self):
        return "".join(e["body"]["output"] for e in self.events if e["event"] == "output")

    def start(self, program, breakpoints=(), args=None, console="internalConsole"):
        self.request("initialize", {"adapterID": "bash-dap", "linesStartAt1": True,
                                    "supportsRunInTerminalRequest": self.run_in_terminal})
        response = self.request("launch", {"program": program, "args": args or [],
                                           "cwd": SCRIPTS, "console": console})
        assert response["success"], response
        self.wait_event("initialized")
        by_file = {}
        for path, line in breakpoints:
            by_file.setdefault(path, []).append({"line": line})
        for path, lines in by_file.items():
            self.request("setBreakpoints", {"source": {"path": path}, "breakpoints": lines})
        self.request("configurationDone")

    def stop_place(self):
        frames = self.request("stackTrace", {"threadId": 1})["body"]["stackFrames"]
        return [(f["name"], os.path.basename(f["source"]["path"]), f["line"]) for f in frames]

    def variables(self, ref=1):
        body = self.request("variables", {"variablesReference": ref})["body"]
        return {v["name"]: v for v in body["variables"]}

    def evaluate(self, expression):
        return self.request("evaluate", {"expression": expression, "frameId": 1,
                                         "context": "watch"})

    def close(self):
        try:
            self.request("disconnect", {"terminateDebuggee": True}, timeout=5)
        except (EOFError, queue.Empty):
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class QuotingTest(unittest.TestCase):
    def test_unquote(self):
        self.assertEqual(unquote(r"a\ b\"c"), 'a b"c')
        self.assertEqual(unquote("$'line\\none'"), "line\none")
        self.assertEqual(unquote("'x y'"), "x y")
        self.assertEqual(unquote(r"declare\ --\ x=\"1\""), 'declare -- x="1"')

    def test_declare(self):
        self.assertEqual(parse_declare('declare -- x="a \\"b\\""'), ("-", 'a "b"', None))
        flags, _, elements = parse_declare('declare -a f=([0]="n.txt" [1]="p q")')
        self.assertEqual(elements, [("0", "n.txt"), ("1", "p q")])
        flags, _, elements = parse_declare('declare -A k=([jpg]="image" [txt]="text" )')
        self.assertEqual(dict(elements), {"jpg": "image", "txt": "text"})
        self.assertEqual(parse_declare("declare -- unset_one"), ("-", "", None))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close()

    def test_function_steps_and_exit(self):
        c = self.client = Client()
        c.start(script("backup.sh"), [(script("backup.sh"), 9)])
        self.assertEqual(c.wait_event("stopped")["body"]["reason"], "breakpoint")
        # in the function, though it runs in the subshell of $(...)
        self.assertEqual(c.stop_place(), [("size_of", "backup.sh", 9), ("main", "backup.sh", 15)])
        names = c.variables()
        self.assertEqual(names["name"]["value"], "notes.txt")
        self.assertEqual(names["size"]["value"], "900")
        self.assertEqual(names["files"]["value"], "(notes.txt photo.jpg report.pdf)")
        elements = c.variables(names["files"]["variablesReference"])
        self.assertEqual(elements["[1]"]["value"], "photo.jpg")
        self.assertEqual(c.evaluate("total")["body"]["result"], "0")
        self.assertEqual(c.evaluate("$(( 2 * 21 ))")["body"]["result"], "42")
        # a step out of the function of $(...) stops in its caller
        c.request("next", {"threadId": 1})
        c.wait_event("stopped")
        self.assertEqual(c.stop_place()[0], ("main", "backup.sh", 16))
        c.request("setBreakpoints", {"source": {"path": script("backup.sh")}, "breakpoints": []})
        c.request("continue", {"threadId": 1})
        self.assertEqual(c.wait_event("exited")["body"]["exitCode"], 3)
        c.wait_event("terminated")
        self.assertIn("3 files, 2800 bytes", c.output())

    def test_pipe_sourced_file_and_dollar_zero(self):
        c = self.client = Client()
        c.start(script("pipe.sh"), [(script("lib.sh"), 3), (script("pipe.sh"), 6)])
        c.wait_event("stopped")
        # lib.sh comes from "$(dirname "$0")": $0 is the script's own
        self.assertEqual(c.stop_place(), [("greet", "lib.sh", 3), ("main", "pipe.sh", 4)])
        c.request("continue", {"threadId": 1})
        c.wait_event("stopped")
        self.assertEqual(c.evaluate("$letter")["body"]["result"], "a")
        c.request("continue", {"threadId": 1})
        c.wait_event("stopped")
        self.assertEqual(c.evaluate("$letter")["body"]["result"], "b")
        # taken off in the subshell of the pipe, for the next round too
        c.request("setBreakpoints", {"source": {"path": script("pipe.sh")}, "breakpoints": []})
        c.request("continue", {"threadId": 1})
        self.assertEqual(c.wait_event("exited")["body"]["exitCode"], 0)
        self.assertIn("got c", c.output())
        self.assertIn("end", c.output())

    def test_pause_and_breakpoint_while_running(self):
        c = self.client = Client()
        c.start(script("loop.sh"))
        time.sleep(0.5)
        c.request("pause", {"threadId": 1})
        self.assertEqual(c.wait_event("stopped")["body"]["reason"], "pause")
        n = int(c.evaluate("$n")["body"]["result"])
        self.assertGreater(n, 0)
        c.request("continue", {"threadId": 1})
        time.sleep(0.3)
        # a breakpoint set while it runs is taken at its next command
        c.request("setBreakpoints", {"source": {"path": script("loop.sh")},
                                     "breakpoints": [{"line": 5}]})
        self.assertEqual(c.wait_event("stopped")["body"]["reason"], "breakpoint")
        self.assertGreater(int(c.evaluate("$n")["body"]["result"]), n)

    def test_terminal_of_the_client(self):
        c = self.client = Client(run_in_terminal=True)
        c.start(script("read.sh"), [(script("read.sh"), 3)], console="integratedTerminal")
        self.assertIsNotNone(c.terminal)
        # what is typed in the terminal is the script's
        c.terminal.stdin.write(b"yes\n")
        c.terminal.stdin.flush()
        c.wait_event("stopped")
        self.assertEqual(c.evaluate("answer")["body"]["result"], "yes")
        c.request("continue", {"threadId": 1})
        c.wait_event("exited")
        self.assertIn(b"answer=yes", c.terminal.stdout.read())

    def test_disconnect_ends_the_script(self):
        c = self.client = Client(run_in_terminal=True)
        c.start(script("loop.sh"), console="integratedTerminal")
        time.sleep(0.3)
        pid = c.terminal.pid
        c.close()
        self.client = None
        c.terminal.wait(5)
        self.assertIsNotNone(c.terminal.returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)


if __name__ == "__main__":
    unittest.main()
