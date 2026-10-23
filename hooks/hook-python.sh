# Sourced by hooks/py.sh and hooks/python-check.sh: sets hook_python, the
# interpreter every hook and plugin script runs with, and hook_data_dir,
# $CLAUDE_PLUGIN_DATA when it is absolute (by the rule below), else empty.
#
# A private interpreter under the plugin's data directory keeps the hooks'
# dependencies (requirements-hooks.txt) out of the project's Python and out of
# reach of a system Python upgrade. It is used when CLAUDE_PLUGIN_DATA
# is an absolute path, so a relative value can't make the current directory
# choose the interpreter, and when one of these is executable:
#
#   $CLAUDE_PLUGIN_DATA/hook-python/bin/python3        (a venv on macOS, Linux)
#   $CLAUDE_PLUGIN_DATA/hook-python/Scripts/python.exe (a venv on Windows)
#
# Absolute means /... (including Git Bash's /c/...) or, on Windows only (Git
# Bash, Cygwin), a drive path C:\... or C:/...: elsewhere C:\x is a
# relative name. Otherwise hook_python is python3 from PATH.
hook_python=python3
hook_data_dir=
_ork_data=${CLAUDE_PLUGIN_DATA:-}
case "${OSTYPE:-}" in
  msys* | cygwin*) case "$_ork_data" in [A-Za-z]:[\\/]*) _ork_data=/drive ;; esac ;;
esac
case "$_ork_data" in
  /*)
    hook_data_dir=$CLAUDE_PLUGIN_DATA
    for _ork_candidate in "$CLAUDE_PLUGIN_DATA/hook-python/bin/python3" \
                          "$CLAUDE_PLUGIN_DATA/hook-python/Scripts/python.exe"; do
      if [[ -f "$_ork_candidate" && -x "$_ork_candidate" ]]; then
        hook_python=$_ork_candidate
        break
      fi
    done
    unset _ork_candidate
    ;;
esac
unset _ork_data
