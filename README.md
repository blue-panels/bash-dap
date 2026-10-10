# bash-dap

A debug adapter of the [Debug Adapter Protocol](https://microsoft.github.io/debug-adapter-protocol/)
for bash scripts: breakpoints, steps into, over and out of functions, the call stack, the
variables (arrays and associative arrays as trees), expressions, pause.

It needs nothing but bash 4.1 or newer and Python 3.8 or newer: no bashdb, no Node.js.  (The bash
of macOS is 3.2: a newer one, from Homebrew say, is given to the launch as *bash*.)  The debugger itself is a script
of bash, `harness.sh`, which the adapter runs the script under; it stops the script with the
trap DEBUG, in the subshells of `$(...)` and of pipes too.

## Install

```
pipx install bash-dap            # or: pip install --user bash-dap
pipx install .                   # from a clone of this repository
```

The command `bash-dap` speaks the protocol on its stdin and stdout.

## Editors

**coole**: a script `.sh` or `.bash` gets bash-dap by itself: F6 on a line, F5.

**Neovim** (nvim-dap):

```lua
local dap = require("dap")
dap.adapters.bashdap = { type = "executable", command = "bash-dap" }
dap.configurations.sh = {
  { type = "bashdap", request = "launch", name = "bash-dap",
    program = "${file}", cwd = "${workspaceFolder}", args = {} },
}
```

**Helix**, **Zed** and the others: an adapter of type `executable`, command `bash-dap`, a
request `launch`.

## Launch

```
program      the script
args         its arguments
cwd          the directory it runs in
env          variables added to its environment
console      integratedTerminal (the default, when the client has runInTerminal): the script
             runs in the terminal of the client, and reads what is typed there;
             internalConsole: what it writes comes as output, it reads nothing
stopOnEntry  stop before its first command
bash         the bash to run it with
```

## How it works

The adapter makes a directory of its own with two fifos: it writes the commands of the harness
to one (run, step, next, finish, print, vars, stack) and reads its replies from the other.  A
subshell is a process of its own, so what is said to one, a step, a breakpoint, goes to the
others through files of that directory, which every process looks at, at a test of two files a
command.  A script under the debugger runs some 25 times slower than by itself.

When the session ends the adapter ends the script by its process group, the session it has to
itself; never by a parent, never anything else.

## Not yet

Conditional breakpoints, breakpoints on functions, a script that another script runs with a
new bash.

## License

GPL-3.0-or-later.
