# harness.sh: the debugger of bash-dap, in the script it debugs.
#
#   BASH_DAP_DIR=DIRECTORY bash -c '. harness.sh' SCRIPT [ARGUMENTS...]
#
# The script runs as it would by itself: its $0, its arguments, its stdin, stdout and stderr.
# The trap DEBUG stops it before a command, in the subshells of $(...) and of pipes too.
#
# DIRECTORY has the fifos "commands", which the adapter writes a command a line to, and
# "replies", which it reads what the debugger says from, a line each:
#
#   commands:  run | step | next | finish | print EXPRESSION | vars | stack
#   replies:   stopped REASON LINE FUNCTION FILE      (REASON: entry, breakpoint, function, step)
#              value TEXT | var NAME TEXT | frame LEVEL FUNCTION LINE FILE | error TEXT
#              done, after the reply to a command | exited CODE
#
# TEXT is quoted as printf %q does, so that a reply is one line.  A command is read only while
# the script is stopped.  At any time the adapter may write the breakpoints to the file "breaks",
# FILE:LINE a line, then make the empty file "gen.N" of the next generation N; and stop the
# script at its next command by writing "step 0" to the file "state".  A line NAME() of
# "breaks" is a breakpoint on the function NAME: the script stops at its first command.
#
# A subshell is a process of its own: what it is told, a step, a breakpoint, goes to the others
# through these files.  The names of the debugger begin with __bdap_.

__bdap_self=${BASH_SOURCE[0]}
__bdap_dir=$BASH_DAP_DIR
unset BASH_DAP_DIR BASH_DAP_HARNESS
exec {__bdap_in}<"$__bdap_dir/commands" {__bdap_out}>"$__bdap_dir/replies" || exit 125

# how the script goes on: run, step, next or finish, with the depth of the stop; in the file
# "state" while it is not "run", so that a subshell and its parent go on as one: a step out of a
# function of $(...) stops in the line that called it
__bdap_state=$__bdap_dir/state
__bdap_mode=run
__bdap_depth=0
# the breakpoints by FILE:LINE, and the generation of the file they came from; __bdap_names has
# NAME:LINE, the name of the file only, that the trap tests at each command; __bdap_funcs has
# NAME() of the functions to stop in
declare -A __bdap_breaks=() __bdap_names=() __bdap_funcs=()
__bdap_gen=0
__bdap_inside=
__bdap_prev=  # the place of the command before: a line stops when it is come to, not again
__bdap_fdepth=0  # the depth of the calls at the command before: more is a function entered
__bdap_fenter=  # a function to stop in is entered: its first command is next

# the variables of bash itself, and those it makes for a function: not shown
__bdap_known=" $(compgen -v | tr '\n' ' ') FUNCNAME BASH_ARGC BASH_ARGV BASH_LINENO BASH_SOURCE COLUMNS LINES "

__bdap_say () {
    printf '%s\n' "$*" >&"$__bdap_out"
}

__bdap_vars () {
    local __bdap_name
    for __bdap_name in $(compgen -v); do
        [[ $__bdap_name == __bdap_* || $__bdap_known == *" $__bdap_name "* ]] && continue
        __bdap_say "var $__bdap_name $(printf '%q' "$(declare -p "$__bdap_name" 2>/dev/null)")"
    done
}

# The frames of the script: those of the debugger left out, the top of the script is main
__bdap_stack () {
    local __bdap_i __bdap_level=0 __bdap_func
    for ((__bdap_i = 1; __bdap_i < ${#FUNCNAME[@]}; __bdap_i++)); do
        [[ ${FUNCNAME[__bdap_i]} == __bdap_* || ${BASH_SOURCE[__bdap_i]} == "$__bdap_self" ]] && continue
        __bdap_func=${FUNCNAME[__bdap_i]}
        [[ $__bdap_func == source || $__bdap_func == main ]] && __bdap_func=main
        # the line a frame is at is the line its callee was called from
        __bdap_say "frame $__bdap_level $__bdap_func ${__bdap_lines[__bdap_level]} $(__bdap_path "${BASH_SOURCE[__bdap_i]}")"
        ((__bdap_level++))
    done
}

# A file as the adapter names it: whole
__bdap_path () {
    if [[ $1 == /* ]]; then
        printf '%s' "$1"
    else
        printf '%s' "$PWD/${1#./}"
    fi
}

__bdap_resume () {
    __bdap_mode=$1
    __bdap_depth=$2
    if [[ $__bdap_mode == run ]]; then
        rm -f "$__bdap_state"
    else
        printf '%s %s\n' "$__bdap_mode" "$__bdap_depth" >"$__bdap_state"
    fi
}

__bdap_breaks_load () {
    local __bdap_place
    while [[ -e $__bdap_dir/gen.$((__bdap_gen + 1)) ]]; do
        ((__bdap_gen++))
    done
    __bdap_breaks=()
    __bdap_names=()
    __bdap_funcs=()
    while read -r __bdap_place; do
        [[ -z $__bdap_place ]] && continue
        if [[ $__bdap_place == *"()" ]]; then
            __bdap_funcs["$__bdap_place"]=1
            continue
        fi
        __bdap_breaks["$__bdap_place"]=1
        __bdap_names["${__bdap_place##*/}"]=1
    done <"$__bdap_dir/breaks"
}

# Wait for the commands of the adapter, until one runs the script on
__bdap_wait () {
    local __bdap_command __bdap_argument
    while read -r __bdap_command __bdap_argument <&"$__bdap_in"; do
        case $__bdap_command in
        run) __bdap_resume run 0; return ;;
        step) __bdap_resume step 0; return ;;
        next | finish) __bdap_resume "$__bdap_command" "$1"; return ;;
        print)
            # a name is a variable; anything else is a word of bash, expanded
            if [[ $__bdap_argument =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
                __bdap_say "value $(printf '%q' "$(declare -p "$__bdap_argument" 2>&1)")"
            else
                __bdap_say "value $(printf '%q' "$(eval "printf '%s' $__bdap_argument" 2>&1)")"
            fi
            ;;
        vars) __bdap_vars ;;
        stack) __bdap_stack ;;
        *) __bdap_say "error $(printf '%q' "unknown command $__bdap_command")" ;;
        esac
        __bdap_say done
    done
    # the adapter has gone: the script goes on by itself
    __bdap_resume run 0
    __bdap_breaks=()
    __bdap_names=()
    __bdap_funcs=()
    trap - DEBUG
}

# Whether the command about to run is on its line: bash gives a command of several lines, a
# pipe into a loop, the line of a command in it, where it has not got to yet
__bdap_on_line () {
    local -a __bdap_text
    local __bdap_word=${__bdap_command%%[[:space:]]*}
    mapfile -t -s $((__bdap_line - 1)) -n 1 __bdap_text <"$__bdap_file" 2>/dev/null || return 0
    [[ -z $__bdap_word || ${__bdap_text[0]-} == *"$__bdap_word"* ]]
}

# Before a command of the script that may stop: what the script has set, -e, -u, -x, is not
# the debugger's, which runs without
__bdap_hook () {
    { local __bdap_opts=$- __bdap_command=$1; set +eux; } 2>/dev/null
    __bdap_check
    [[ $__bdap_opts == *e* ]] && set -e
    [[ $__bdap_opts == *u* ]] && set -u
    [[ $__bdap_opts == *x* ]] && set -x
    return 0
}

__bdap_check () {
    [[ -n $__bdap_inside ]] && return 0
    # 0 is this function, 1 the hook, 2 the frame of the script
    local __bdap_file=${BASH_SOURCE[2]} __bdap_line=${BASH_LINENO[1]} __bdap_d=$((${#FUNCNAME[@]} - 1))
    local __bdap_reason= __bdap_lines __bdap_func
    [[ $__bdap_file == "$__bdap_self" ]] && return 0
    [[ $__bdap_file == /* ]] || __bdap_file=$PWD/${__bdap_file#./}
    # what another process of the script has said: a subshell has stepped, or the adapter has
    # changed the breakpoints, or wants a stop
    [[ -e $__bdap_dir/gen.$((__bdap_gen + 1)) ]] && __bdap_breaks_load
    if [[ -e $__bdap_state ]]; then
        read -r __bdap_mode __bdap_depth <"$__bdap_state"
    elif [[ $__bdap_mode != run ]]; then
        __bdap_mode=run
    fi
    case $__bdap_mode in
    step) __bdap_reason=step ;;
    next) ((__bdap_d <= __bdap_depth)) && __bdap_reason=step ;;
    finish) ((__bdap_d < __bdap_depth)) && __bdap_reason=step ;;
    esac
    if [[ -z $__bdap_reason && -n ${__bdap_breaks["$__bdap_file:$__bdap_line"]-} ]] && __bdap_on_line; then
        __bdap_reason=breakpoint
    fi
    # the first command of a function to stop in: the calls are deeper than at the command
    # before; bash comes first to the line that defines it, with the call as the command
    if [[ -z $__bdap_reason && -n ${__bdap_funcs["${FUNCNAME[2]-}()"]-} ]]; then
        if ((__bdap_d - 1 > __bdap_fdepth)) && [[ $__bdap_command == "${FUNCNAME[2]}"* ]]; then
            __bdap_fenter=1
        elif ((__bdap_d - 1 > __bdap_fdepth)) || [[ -n $__bdap_fenter ]]; then
            __bdap_reason=function
        fi
    fi
    [[ -n $__bdap_reason ]] && __bdap_fenter=
    # the next command of a line is no new place to stop at
    [[ -z $__bdap_reason || $__bdap_prev == "$__bdap_line:${BASH_SOURCE[2]}" ]] && return 0
    # the lines of the frames, the one stopped in first
    __bdap_lines=("$__bdap_line" "${BASH_LINENO[@]:2}")
    __bdap_func=${FUNCNAME[2]:-main}
    [[ $__bdap_func == source || $__bdap_func == main ]] && __bdap_func=main
    __bdap_inside=1
    __bdap_say "stopped $__bdap_reason $__bdap_line $__bdap_func $__bdap_file"
    __bdap_wait "$__bdap_d"
    __bdap_inside=
    return 0
}

__bdap_say "stopped entry 0 main $(__bdap_path "$0")"
__bdap_inside=1
__bdap_wait 0
__bdap_inside=

# the end, by an exit of the script too
trap '__bdap_say "exited $?"' EXIT
shopt -s extdebug
set -o functrace
# the hook only when there may be something to do: a step, a breakpoint in a file of that name
# on the line or on the function, what another process has said; else a command costs a test
# of two files.  One line: a second one would have LINENO one more
# (with set -u of the script it runs too: nothing unset is read, BASH_SOURCE is empty at the end,
# FUNCNAME unset out of any function)
__bdap_trap='[[ -n $__bdap_inside || ( $__bdap_mode == run'
__bdap_trap+=' && -z ${__bdap_names["${BASH_SOURCE[0]:+${BASH_SOURCE[0]##*/}}:$LINENO"]-}'
__bdap_trap+=' && -z ${__bdap_funcs["${FUNCNAME[0]-}()"]-}'
__bdap_trap+=' && ! -e $__bdap_state && ! -e $__bdap_dir/gen.$((__bdap_gen + 1)) ) ]] || __bdap_hook "$BASH_COMMAND";'
__bdap_trap+=' __bdap_prev=$LINENO:${BASH_SOURCE[0]-} __bdap_fdepth=${FUNCNAME[0]+${#FUNCNAME[@]}}'
trap "$__bdap_trap" DEBUG
. "$0" "$@"
