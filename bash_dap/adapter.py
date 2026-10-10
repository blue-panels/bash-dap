"""bash-dap: a debug adapter of the Debug Adapter Protocol for bash scripts.

The adapter speaks DAP on its stdin and stdout.  It runs the script under harness.sh, a debugger
of bash written in bash with the trap DEBUG, and talks to it through two fifos and a few files
of a directory of its own (see harness.sh).  Nothing but bash and Python is needed.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness.sh")
VARIABLES_REF = 1  # the reference of the variables of a stop


def unquote(text):
    """The string that printf %q of bash has quoted: \\x, '...' and $'...' (C escapes)."""
    out = []
    i, n = 0, len(text)
    escapes = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
               "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            out.append(text[i + 1:j])
            i = j + 1
        elif c == "$" and text.startswith("$'", i):
            i += 2
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    e = text[i + 1]
                    if e in escapes:
                        out.append(escapes[e])
                        i += 2
                    elif e == "x":
                        m = re.match(r"[0-9a-fA-F]{1,2}", text[i + 2:])
                        out.append(chr(int(m.group(0), 16)) if m else "x")
                        i += 2 + (len(m.group(0)) if m else 0)
                    elif e in "01234567":
                        m = re.match(r"[0-7]{1,3}", text[i + 1:])
                        out.append(chr(int(m.group(0), 8)))
                        i += 1 + len(m.group(0))
                    else:
                        out.append(e)
                        i += 2
                else:
                    out.append(text[i])
                    i += 1
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def unquote_double(text):
    """A word of bash in double quotes, as declare -p writes a value: "a \\"b\\" \\$c"."""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return re.sub(r'\\(["\\$`])', r"\1", text[1:-1])
    if text.startswith("$'"):
        return unquote(text)
    return text


def parse_declare(text):
    """What declare -p says of a variable: its flags, and a value or the elements of an array
    as (key, value) pairs; None for something else, an error say."""
    m = re.match(r"declare -(\S+) ([^=\s]+)(?:=(.*))?$", text, re.S)
    if not m:
        return None
    flags, value = m.group(1), m.group(3)
    if value is None:
        return flags, "", None
    if ("a" in flags or "A" in flags) and value.startswith("("):
        pairs = re.findall(r'\[((?:[^\]\\]|\\.)*)\]=("(?:[^"\\]|\\.)*"|\$\'(?:[^\'\\]|\\.)*\'|[^\s)]*)',
                           value[1:])
        return flags, value, [(unquote_double(k) if k.startswith('"') else k, unquote_double(v))
                              for k, v in pairs]
    return flags, unquote_double(value), None


class Adapter:
    def __init__(self, rfile, wfile):
        self.rfile = rfile
        self.wfile = wfile
        self.write_lock = threading.Lock()
        self.seq = 0
        self.client = {}
        self.dir = None
        self.proc = None
        self.pid = None  # of the script run in the terminal of the client
        self.commands = None
        self.cv = threading.Condition()
        self.collect = []
        self.done = False
        self.stopped = False
        self.alive = False
        self.pause_asked = False
        self.breaks = {}
        self.gen = 0
        self.refs = {}
        self.next_ref = VARIABLES_REF + 1
        self.callbacks = {}
        self.stop_on_entry = False
        self.configured = False
        self.entered = False

    # --- the protocol ------------------------------------------------------------------------

    def send(self, message):
        with self.write_lock:
            self.seq += 1
            message["seq"] = self.seq
            body = json.dumps(message).encode()
            self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.wfile.flush()
            return self.seq

    def event(self, name, body=None):
        message = {"type": "event", "event": name}
        if body is not None:
            message["body"] = body
        self.send(message)

    def respond(self, request, body=None, success=True, message=None):
        response = {"type": "response", "request_seq": request["seq"],
                    "command": request["command"], "success": success}
        if body is not None:
            response["body"] = body
        if message is not None:
            response["message"] = message
        self.send(response)

    def output(self, text, category="console"):
        self.event("output", {"category": category, "output": text})

    def read_message(self):
        length = None
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                if length is not None:
                    break
                continue
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value)
        return json.loads(self.rfile.read(length))

    # --- the harness -------------------------------------------------------------------------

    def tell(self, command):
        os.write(self.commands, (command + "\n").encode())

    def ask(self, command, timeout=10):
        """A command of the harness while the script is stopped: the lines of its reply."""
        if not self.stopped or self.commands is None:
            return None
        with self.cv:
            self.collect = []
            self.done = False
            self.tell(command)
            end = time.monotonic() + timeout
            while not self.done and self.alive and time.monotonic() < end:
                self.cv.wait(0.2)
            return list(self.collect) if self.done else None

    def resume(self, command):
        if not self.stopped:
            return False
        self.stopped = False
        self.refs = {}
        self.tell(command)
        return True

    def save_breaks(self):
        """The breakpoints for the harness: the processes of the script take them at their next
        command, whether the script runs or is stopped."""
        if self.dir is None:
            return
        with open(os.path.join(self.dir, "breaks"), "w") as f:
            for path, lines in self.breaks.items():
                for line in lines:
                    f.write("%s:%d\n" % (path, line))
        self.gen += 1
        open(os.path.join(self.dir, "gen.%d" % self.gen), "w").close()

    def open_fifos(self):
        """The fifos once the harness has opened its ends; False when it never does."""
        path = os.path.join(self.dir, "commands")
        end = time.monotonic() + 15
        while True:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError:
                if time.monotonic() > end or (self.proc is not None and self.proc.poll() is not None):
                    return None
                time.sleep(0.02)
        os.set_blocking(fd, True)
        self.commands = fd
        return open(os.path.join(self.dir, "replies"), "r")

    def harness_loop(self):
        replies = self.open_fifos()
        if replies is None:
            self.output("bash-dap: the script did not start\n", "stderr")
            self.alive = False
            self.event("terminated")
            return
        self.alive = True
        for line in replies:
            line = line.rstrip("\n")
            kind, _, rest = line.partition(" ")
            if kind == "stopped":
                self.on_stopped(rest)
            elif kind == "exited":
                self.alive = False
                self.event("exited", {"exitCode": int(rest or 0)})
                break
            elif kind == "done":
                with self.cv:
                    self.done = True
                    self.cv.notify_all()
            else:
                with self.cv:
                    self.collect.append((kind, rest))
        self.alive = False
        with self.cv:
            self.cv.notify_all()
        self.event("terminated")

    def on_stopped(self, rest):
        reason, line, func, path = (rest.split(" ", 3) + ["", "", "", ""])[:4]
        self.stopped = True
        self.refs = {}
        if reason == "entry":
            # the breakpoints, then configurationDone runs it
            self.entered = True
            self.event("initialized")
            return
        if self.pause_asked and reason == "step":
            reason = "pause"
        self.pause_asked = False
        self.event("stopped", {"reason": reason, "threadId": 1, "allThreadsStopped": True})

    # --- the variables -----------------------------------------------------------------------

    def variable(self, name, text, evaluate_name):
        parsed = parse_declare(text)
        if parsed is None:
            return {"name": name, "value": text, "variablesReference": 0}
        flags, value, elements = parsed
        item = {"name": name, "value": value, "evaluateName": evaluate_name,
                "type": "array" if "a" in flags else "associative array" if "A" in flags
                else "integer" if "i" in flags else "string", "variablesReference": 0}
        if elements is not None:
            ref = self.next_ref
            self.next_ref += 1
            assoc = "A" in flags
            self.refs[ref] = [
                {"name": "[%s]" % k, "value": v, "variablesReference": 0,
                 "evaluateName": '"${%s[%s]}"' % (name, ('"%s"' % k) if assoc else k)}
                for k, v in elements]
            item["value"] = "(%s)" % " ".join(v for _, v in elements)
            item["variablesReference"] = ref
            item["indexedVariables" if not assoc else "namedVariables"] = len(elements)
        return item

    # --- the requests ------------------------------------------------------------------------

    def on_initialize(self, request):
        self.client = request.get("arguments", {})
        self.respond(request, {
            "supportsConfigurationDoneRequest": True,
            "supportsEvaluateForHovers": True,
            "supportsTerminateRequest": True,
        })

    def on_launch(self, request):
        args = request.get("arguments", {})
        cwd = os.path.realpath(args.get("cwd") or os.getcwd())
        program = args.get("program")
        if not program:
            self.respond(request, success=False, message="no program to debug")
            return
        program = os.path.realpath(os.path.join(cwd, program))
        self.stop_on_entry = bool(args.get("stopOnEntry", False))
        self.dir = tempfile.mkdtemp(prefix="bash-dap-")
        os.mkfifo(os.path.join(self.dir, "commands"))
        os.mkfifo(os.path.join(self.dir, "replies"))
        self.save_breaks()
        env = {"BASH_DAP_DIR": self.dir, "BASH_DAP_HARNESS": HARNESS}
        env.update({k: v for k, v in (args.get("env") or {}).items() if v is not None})
        argv = [args.get("bash") or "bash", "-c", '. "$BASH_DAP_HARNESS"', program]
        argv += [str(a) for a in args.get("args") or []]
        console = args.get("console", "integratedTerminal")
        if console != "internalConsole" and self.client.get("supportsRunInTerminalRequest"):
            # in the terminal of the client: the script reads what is typed there
            def ran(response):
                if not response.get("success"):
                    self.respond(request, success=False,
                                 message=response.get("message", "runInTerminal failed"))
                    return
                self.pid = (response.get("body") or {}).get("processId")
                self.respond(request)

            seq = self.send({"type": "request", "command": "runInTerminal", "arguments": {
                "kind": "external" if console == "externalTerminal" else "integrated",
                "title": "bash-dap", "cwd": cwd, "args": argv, "env": env}})
            self.callbacks[seq] = ran
        else:
            full = dict(os.environ)
            full.update(env)
            try:
                self.proc = subprocess.Popen(argv, cwd=cwd, env=full, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             start_new_session=True)
            except OSError as e:
                self.respond(request, success=False, message=str(e))
                return
            for stream, category in ((self.proc.stdout, "stdout"), (self.proc.stderr, "stderr")):
                threading.Thread(target=self.pump, args=(stream, category), daemon=True).start()
            self.respond(request)
        threading.Thread(target=self.harness_loop, daemon=True).start()

    def pump(self, stream, category):
        for chunk in iter(lambda: stream.read1(4096), b""):
            self.output(chunk.decode(errors="replace"), category)

    def on_setBreakpoints(self, request):
        args = request.get("arguments", {})
        path = os.path.realpath(args.get("source", {}).get("path", ""))
        lines = [b["line"] for b in args.get("breakpoints", []) if "line" in b]
        self.breaks[path] = lines
        self.save_breaks()
        self.respond(request, {"breakpoints": [
            {"id": i + 1, "verified": True, "line": line, "source": {"path": path}}
            for i, line in enumerate(lines)]})

    def on_setFunctionBreakpoints(self, request):
        self.respond(request, {"breakpoints": [
            {"verified": False, "message": "bash-dap has breakpoints on lines only"}
            for _ in request.get("arguments", {}).get("breakpoints", [])]})

    def on_setExceptionBreakpoints(self, request):
        self.respond(request, {"breakpoints": []})

    def on_configurationDone(self, request):
        self.configured = True
        self.respond(request)
        if self.stop_on_entry:
            self.event("stopped", {"reason": "entry", "threadId": 1, "allThreadsStopped": True})
        else:
            self.resume("run")

    def on_threads(self, request):
        self.respond(request, {"threads": [{"id": 1, "name": "bash"}]})

    def on_stackTrace(self, request):
        lines = self.ask("stack") or []
        frames = []
        for kind, rest in lines:
            if kind != "frame":
                continue
            level, func, line, path = (rest.split(" ", 3) + ["", "", "", ""])[:4]
            frames.append({"id": int(level) + 1, "name": func, "line": int(line or 0),
                           "column": 1, "source": {"path": path, "name": os.path.basename(path)}})
        args = request.get("arguments", {})
        start = args.get("startFrame", 0)
        levels = args.get("levels") or len(frames)
        self.respond(request, {"stackFrames": frames[start:start + levels],
                               "totalFrames": len(frames)})

    def on_scopes(self, request):
        # bash has dynamic scope: a frame sees the variables of those under it, all at once
        self.respond(request, {"scopes": [
            {"name": "Variables", "presentationHint": "locals",
             "variablesReference": VARIABLES_REF, "expensive": False}]})

    def on_variables(self, request):
        ref = request.get("arguments", {}).get("variablesReference")
        if ref == VARIABLES_REF:
            items = []
            for kind, rest in self.ask("vars") or []:
                if kind == "var":
                    name, _, text = rest.partition(" ")
                    items.append(self.variable(name, unquote(text), "$" + name))
            self.respond(request, {"variables": items})
        else:
            self.respond(request, {"variables": self.refs.get(ref, [])})

    def on_evaluate(self, request):
        args = request.get("arguments", {})
        expression = args.get("expression", "").strip()
        lines = self.ask("print " + expression)
        if lines is None:
            self.respond(request, success=False, message="the script is not stopped")
            return
        values = [unquote(rest) for kind, rest in lines if kind == "value"]
        text = values[0] if values else ""
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expression):
            if text.startswith("bash:") or "not found" in text:
                self.respond(request, success=False, message="%s is not set" % expression)
                return
            item = self.variable(expression, text, expression)
            self.respond(request, {"result": item["value"],
                                   "variablesReference": item["variablesReference"]})
            return
        self.respond(request, {"result": text, "variablesReference": 0})

    def on_continue(self, request):
        self.resume("run")
        self.respond(request, {"allThreadsContinued": True})

    def on_next(self, request):
        self.resume("next")
        self.respond(request)

    def on_stepIn(self, request):
        self.resume("step")
        self.respond(request)

    def on_stepOut(self, request):
        self.resume("finish")
        self.respond(request)

    def on_pause(self, request):
        # every process of the script stops at its next command
        if self.dir is not None and not self.stopped:
            self.pause_asked = True
            with open(os.path.join(self.dir, "state"), "w") as f:
                f.write("step 0\n")
        self.respond(request)

    def on_disconnect(self, request):
        self.terminate()
        self.respond(request)
        return False

    def on_terminate(self, request):
        self.terminate()
        self.respond(request)
        self.event("terminated")

    def terminate(self):
        """The script ended: its group, the session it has to itself.  Never by its parent, and
        never the group of the adapter or of the client."""
        group = None
        if self.proc is not None and self.proc.poll() is None:
            group = self.proc.pid  # start_new_session: it leads its group
        elif self.pid:
            try:
                group = os.getpgid(self.pid)
            except OSError:
                group = None
        if group and group > 1 and group != os.getpgid(0):
            try:
                os.killpg(group, signal.SIGTERM)
            except OSError:
                pass
        elif self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.commands is not None:
            try:
                os.close(self.commands)
            except OSError:
                pass
            self.commands = None
        if self.dir is not None:
            for name in os.listdir(self.dir):
                try:
                    os.unlink(os.path.join(self.dir, name))
                except OSError:
                    pass
            try:
                os.rmdir(self.dir)
            except OSError:
                pass
            self.dir = None

    def run(self):
        while True:
            message = self.read_message()
            if message is None:
                break
            if message.get("type") == "response":
                callback = self.callbacks.pop(message.get("request_seq"), None)
                if callback is not None:
                    callback(message)
                continue
            if message.get("type") != "request":
                continue
            handler = getattr(self, "on_" + message.get("command", ""), None)
            if handler is None:
                self.respond(message, success=False,
                             message="bash-dap: %s is not supported" % message.get("command"))
                continue
            try:
                if handler(message) is False:
                    break
            except Exception as e:  # a request that fails is told, the session goes on
                self.respond(message, success=False, message="bash-dap: %s" % e)
        self.terminate()


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("bash-dap: a debug adapter for bash scripts; it speaks the Debug Adapter Protocol "
              "on stdin and stdout")
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print("bash-dap 0.1")
        return 0
    Adapter(sys.stdin.buffer, sys.stdout.buffer).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
