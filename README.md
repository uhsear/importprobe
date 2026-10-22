# importprobe

Import your script's modules the way a scheduled task would, compare the result with your shell, and give a verdict. It finds the import that dies before logging exists, so the job leaves no log.

A nightly job copies a layer to a share. It ran by hand every day for a year. One Monday Task Scheduler shows `Last Run Result 0x1` and the folder has no log file. The script sets up logging on line 40. The crash is on line 3, inside `import fancylog`, a helper that calls `sys.stdout.isatty()` while it loads. The task has no console, so `sys.stdout` is `None`, and the helper raises `AttributeError: 'NoneType' object has no attribute 'isatty'`. Nothing can write that error anywhere.

You open a console and run the script. It works. The next guess is the wrong one, and then the one after it. Two other things differ between your console and the task. The task may set `PYTHONNOUSERSITE=1`, so a package that only lives in your user site-packages directory is gone. And the task runs as another account, where `import arcpy` can fail with `The Product License has not been initialized`. Nothing in a console shows any of the three.

importprobe runs each import twice, in two child processes of the interpreter you name. One child is your shell. The other child has `sys.stdin`, `sys.stdout` and `sys.stderr` set to `None`, the environment variables you give with `--env`, and the script directory first on `sys.path`, as `python script.py` puts it. It then compares the two and prints one verdict.

```
$ python importprobe.py --script nightly.py --env PYTHONNOUSERSITE=1 --task-xml nightly_task.xml
importprobe: C:\Python313\python.exe, 5 module(s)
  here: python 3.13.2, user site on, stdout a real stream
  task: python 3.13.2, user site off, stdout None, env set: PYTHONNOUSERSITE

module           here   task
json             ok     ok
sys              ok     ok
fancylog         ok     fail
tidy_helpers     ok     fail
optional_charts  fail   fail

task XML: LogonType=Password RunLevel=LeastPrivilege

FAIL  fancylog  STDOUT-NONE: AttributeError: 'NoneType' object has no attribute 'isatty' (at C:\ipdemo\lib\fancylog.py:2) in the task context (imports fine here)
FAIL  tidy_helpers  USER-SITE-ONLY: ModuleNotFoundError: No module named 'tidy_helpers' in the task context (imports fine here)
WARN  optional_charts  NOT-IMPORTABLE: ModuleNotFoundError: No module named 'optional_charts' in the task context (also fails here); the import is optional
OK    json  OK: found in stdlib: C:\Python313\Lib\json\__init__.py
OK    sys  OK: found in built-in: built-in
OK    <task>  TASK-LOGON: LogonType is Password: the task can run with no interactive session, and an arcpy licence is set per account

verdict: FAIL (2 fail, 1 warn)
```

The block above is a real run of the command shown, in a folder with a synthetic `nightly.py`, a synthetic `fancylog.py` and a synthetic task XML. The run exits 1.

## The existing tool

[pywho](https://github.com/AhsanSheraz/pywho) explains one interpreter and one environment. It shows which Python you run, why `import X` loaded one file, and which files in a project shadow a standard-library module or an installed package. It has zero dependencies, runs on Python 3.10 and later, and is MIT licensed. If you only need to know what is in front of you, use it first.

pywho looks at the environment it runs in. importprobe runs the import a second time under different conditions and shows the difference. It does not replace pywho. It answers a different question: why does this import work here and fail in the task. Python also ships `python -m site`, which prints the user site-packages path and whether the user site is enabled.

## Requirements

Python 3.9 or later, standard library only. Nothing to install.

```
git clone https://github.com/uhsear/importprobe.git
python importprobe.py --self-test
```

`--python` names the interpreter the task uses. It does not have to be the one that runs importprobe. For ArcGIS Pro:

```
python importprobe.py --python "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" --module arcpy
```

## Usage

```
python importprobe.py --module json --module arcpy
python importprobe.py --script nightly.py
python importprobe.py --script nightly.py --env PYTHONNOUSERSITE=1
python importprobe.py --task-xml nightly_task.xml --script nightly.py
python importprobe.py --script nightly.py --out report.json --apply
```

| Flag | What it does |
|---|---|
| `--module NAME` | Probe one module. Repeat for several. |
| `--script FILE` | Probe the modules the script imports when it loads. The script is parsed with `ast` and never run. |
| `--python EXE` | The interpreter the task uses. Default: the one running importprobe. |
| `--env NAME=VALUE` | Set a variable in the task context only. Repeat for several. The report prints the name, never the value. |
| `--task-xml FILE` | Read an exported Task Scheduler definition and report its `LogonType`. |
| `--timeout S` | Seconds one probe process may run. Default 120. |
| `--out FILE` | Where to write the report as JSON. |
| `--apply` | Write the `--out` file. Without it nothing is written. |
| `--self-test` | Run the offline assertions and exit. |

Only `--apply` writes a file. `--apply` without `--out` is a usage error. The parser sets `allow_abbrev=False`, so `--ap`, `--app` and `--appl` are refused and never read as `--apply`.

Imports that are guarded by `try` and `except ImportError` (or `except Exception`, or a bare `except`) are optional. A missing optional module is a warning. Imports inside a function are not probed, because they run after logging exists.

### Verdict and exit codes

| Exit | Verdict | Meaning |
|---|---|---|
| 0 | OK | Every import works in both contexts and nothing is shadowed. |
| 1 | FAIL | An import dies in the task context, or a probe process died. |
| 3 | WARN | Every import works, but something differs or is shadowed. |
| 2 | | A file could not be read or written, or the interpreter does not exist. |
| 64 | | Usage error. |
| 70 | | An unexpected crash in importprobe. It is never a finding. |

### Finding codes

| Code | Severity | Meaning |
|---|---|---|
| `STDOUT-NONE` | FAIL | The import works with a real stream and raises `AttributeError: 'NoneType' ...` when the streams are `None`. |
| `USER-SITE-ONLY` | FAIL | The module is missing in the task and lives in the user site-packages directory here. |
| `NOT-IMPORTABLE` | FAIL, or WARN if optional | `ModuleNotFoundError` in the task. |
| `LICENCE` | FAIL | `arcpy` raised the licence message on import, or `arcpy.ProductInfo()` returned `NotInitialized`. |
| `IMPORT-ERROR` | FAIL | Any other exception in the task. The text names the exception and the file and line that raised it. |
| `CRASH` | FAIL | The probe process exited without a result, timed out, or could not start. |
| `SHADOWS` | WARN | The winning copy is in the user site or beside the script, and another copy exists later on `sys.path`. |
| `ORIGIN-DIFFERS` | WARN | The two contexts load different files. |
| `PASSES-ONLY-IN-TASK` | WARN | The import works in the task and fails here. |
| `LICENCE-CHECK` | WARN | `arcpy.ProductInfo()` raised. |
| `UNPROBED` | OK | The probe loaded the module itself before the test, so the result is not a fresh import. It is a notice and never changes the verdict. |
| `TASK-LOGON` | OK, or WARN | The `LogonType` in the task XML. WARN when the script imports arcpy and the type is not `InteractiveToken`. |

## What each claim rests on

Every claim below was run, not only read. The self-test assertion or command is named.

| Claim | Where it was exercised |
|---|---|
| With `sys.stdout` `None`, `reconfigure()` and `isatty()` at import raise `AttributeError`. | Self-test, real child processes. Also run by hand under `pythonw.exe` (note 2). |
| `print()` and a default `logging` handler do not fail when the streams are `None`. They are silent, so a file handler is the only record. | Self-test, pinned. Also run by hand under `pythonw.exe` (note 2). |
| A package that is only in the user site disappears when `PYTHONNOUSERSITE=1`. | Self-test, using `PYTHONUSERBASE` to move the user site into a temporary folder. The CLI run above shows it. |
| A user-site copy that hides another copy is named, with the hidden path. | Self-test: a `.pth` file in the user site adds a second directory. |
| A file beside the script that shares a standard-library name wins. | Self-test, with a `colorsys.py` beside the script. |
| The current directory is not searched, as for `python script.py`. | Self-test, pinned. |
| The licence message and `NotInitialized` are classified. | Self-test, against a stub `arcpy` package. The real `arcpy` import and `ProductInfo()` were also run on ArcGIS Pro's Python 3.13.7 and returned a level (note 3). |
| An exported Task Scheduler XML is read, in UTF-16 and UTF-8, and a `DOCTYPE` is refused. | Self-test. Also parsed a real export from a Windows machine. |
| No file is written without `--apply`, and `--ap` is refused. | Self-test, pinned. |
| An env value never reaches the output or the JSON report. | Self-test, pinned, and a grep of the output and the JSON report after a CLI run that set `SECRET_TOKEN`. |
| The probe writes no `.pyc` file beside the script. | Self-test. |

Note 2: run once by hand with `Start-Process pythonw.exe`. `sys.stdin`, `sys.stdout`, `sys.stderr` and `sys.__stdout__` were all `None`. `reconfigure()` and `isatty()` raised the same two messages as in the emulation. `print()` and `logging.warning()` did nothing. This check is not part of the self-test, because it needs `pythonw.exe` and Windows. importprobe emulates the `None` streams on every platform instead of launching `pythonw.exe`.

Note 3: `python importprobe.py --python "<Pro python>" --module arcpy --module json --env PYTHONNOUSERSITE=1` returned OK for both and `ProductInfo() returned ArcInfo`. The failing licence path was only run against the stub, because producing the real failure needs a second Windows account with no licence.

### Checked against official documentation

- Standard streams can be `None`: [`sys.stdin`, `sys.stdout`, `sys.stderr`](https://docs.python.org/3.12/library/sys.html) say they "can be `None`. It is usually the case for Windows GUI apps that aren't connected to a console and Python apps started with pythonw."
- `python script.py` puts the script directory first on `sys.path`, and `python -c` puts an empty string. importprobe removes the `-c` entry and inserts the script directory, as described in the same [`sys.path`](https://docs.python.org/3.12/library/sys.html) section.
- User site: [`site.ENABLE_USER_SITE`](https://docs.python.org/3.12/library/site.html) is `False` when disabled "with `-s` or `PYTHONNOUSERSITE`". The Windows default is `%APPDATA%\Python\PythonXY\site-packages`, and `.pth` files in it are processed.
- [`arcpy.ProductInfo()`](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/productinfo.html) returns `NotInitialized` when "no license has been set", and otherwise `ArcView`, `ArcEditor`, `ArcInfo`, `Engine`, `EngineGeoDB` or `ArcServer`.
- [Esri knowledge base](https://support.esri.com/en-us/knowledge-base/running-schedule-python-task-under-a-gmsa-account-throw-000040955): a scheduled task under a group managed service account fails with "The Product License has not been initialized" because that account has no ArcGIS Pro licence configuration until Pro is launched under it. The words of that message are what importprobe matches. A different wording is reported as `IMPORT-ERROR`.
- [`LogonType`](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-logontype-principaltype-element): `InteractiveToken` means "the task will be run only in an existing interactive session". `Password` and `S4U` do not carry that promise.

Not verified, so not claimed: that an interactive-only licence type fails in a non-interactive session. The Esri page above is about per-account configuration. importprobe warns on the `LogonType` and leaves the cause to you.

## Self-test

```
$ python importprobe.py --self-test
importprobe self-test: temporary files and local child processes only, no network, no credentials, no arcpy
--------------------------------------------------------------------
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the exception it is looking for
...
PASS  a site directory that is the interpreter prefix does not turn every stdlib file into site-packages  <-- pinned defect
...
PASS  reconfigure() and isatty() at import work in a shell and die with stdout None  <-- pinned defect
PASS  print() and a default logging handler do not fail with stdout and stderr None, they are silent  <-- pinned defect
PASS  a package installed only in the user site vanishes when the task sets PYTHONNOUSERSITE  <-- pinned defect
...
PASS  the prefix --ap of --apply is refused by the parser  <-- pinned defect
...
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
132 assertions, 0 failed
```

The count is 132 on Windows Python 3.13.2, on Python 3.9.25, 3.10.20 and 3.12 on Windows, on ArcGIS Pro's Python 3.13.7, and on Linux Python 3.12.3. No assertion depends on the platform.

Branch coverage is 100 percent. Run it with `--include`, because the self-test imports fixture modules from a temporary folder that it deletes afterwards:

```
python -m coverage run --branch --include=importprobe.py importprobe.py --self-test
python -m coverage report -m
```

The probe child runs inside the target interpreter, so `coverage` cannot follow it into a subprocess. The self-test also calls the same function in-process, which is how its branches are measured. importprobe sends the function to the child with `inspect.getsource`, so the code measured is the code that runs.

## Limits

- It reproduces three conditions: the streams, the environment variables and the `sys.path` start. It does not reproduce a Windows logon session. A task that runs as another account, in session 0, with another profile, has a different user site directory and a different licence. `--env PYTHONUSERBASE=...` points the user site somewhere else, and `--task-xml` reads the logon type, but neither one runs the import as that account. Run importprobe as the task's account when you can.
- The task context starts from your environment, then applies `--env`. A real task has the environment of its account. List the variables you care about.
- `sys.stdout` is set to `None` in the child. That is what `pythonw.exe` gives Python, but importprobe does not launch `pythonw.exe`. Code that checks the process subsystem or console handles can still behave differently.
- Importing a module runs its top-level code. importprobe imports each module once per context, so two times. Do not probe a module whose import writes data.
- The probe needs `importlib` and `sysconfig`, and it loads them before the test when the interpreter has not. A script that imports either one gets an `UNPROBED` notice, and a file beside the script that shadows them is not found. On the Windows Python 3.13.2 used here both were notices. On Linux Python 3.12.3 only `sysconfig` was.
- It starts the script directory first on `sys.path` even when `-P` or `PYTHONSAFEPATH` would remove it, and it does not resolve a symbolic link to the script.
- Optional imports are found by `try` and `except` only. An import guarded by an `if` is treated as required.
- `--script` reads imports. It does not read what the script does with `sys.stdout` after loading. A script that calls `sys.stdout.reconfigure()` in its own code is not caught until a module it imports does the same.
- Only `LogonType` is judged in the task XML. importprobe does not read the triggers, the "run whether logged on or not" setting or the working directory.
- `--task-xml` is meaningful for Windows task exports only. The `None` stream and user-site checks run the same on Linux.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that look at what a scheduled job does when nobody is watching:

- [taskpulse](https://github.com/uhsear/taskpulse) - audit every Windows scheduled task and report which ones are silently failing
- [jobharness](https://github.com/uhsear/jobharness) - one import gives a scheduled script logging, retry and resume
- [logsift](https://github.com/uhsear/logsift) - mine a scheduled job's own log files into a metric series
- [stalled](https://github.com/uhsear/stalled) - count the days since a scheduled job last succeeded
