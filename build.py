"""Build an emscripten-browser version of CPython."""
from configparser import ConfigParser
from argparse import ArgumentParser
from hashlib import md5
from itertools import chain
import os
from pathlib import Path
import re
import subprocess
from shutil import copy
import sys
from zipfile import ZipFile


CONFIG_PATH = Path.home() / ".python-wasm.ini"


def run(command, capture_output=False, **kwargs):
    """Run a shell command."""
    if capture_output:
        process = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            encoding="utf-8",
            **kwargs
        )
    else:
        process = subprocess.run(command, shell=True, **kwargs)
    try:
        process.check_returncode()
    except Exception:
        print(process.stderr)
        raise
    return process


def shell_source(emsdk_directory):
    """Emulate shell "source" command."""
    process = run(
        ". ./emsdk_env.sh && env",
        capture_output=True,
        cwd=emsdk_directory,
    )
    output = process.stdout
    env = dict(line.split("=", 1) for line in output.splitlines())
    os.environ.update(env)


def fingerprint_filename(path):
    """Return filename with first 12 digits of MD5 checksum."""
    checksum = md5(path.read_bytes()).hexdigest()[:12]
    new_path = path.with_stem(f"{path.stem}.{checksum}")
    return new_path


def read_config():
    """Read top-level options from the configuration file."""
    parser = ConfigParser()
    if CONFIG_PATH.is_file():
        parser.read(CONFIG_PATH)
    return dict(parser["wasm"]) if "wasm" in parser else {}


def parse_args():
    """Parse command-line arguments."""
    parser = ArgumentParser()
    parser.add_argument(
        "--cpython",
        type=Path,
    )
    parser.add_argument(
        "--emsdk",
        type=Path,
    )
    parser.add_argument(
        "--url-prefix",
        type=str,
        default="",
    )
    parser.add_argument(
        "--python-version",
        help="Git tag for a specific CPython version",
    )
    parser.add_argument(
        "--setup-emsdk-version",
        default=None,
        help="Setup given emsdk version (assume it's setup if not given)",
    )
    args = parser.parse_args()
    config = read_config()
    for option in ["cpython", "emsdk", "url_prefix"]:
        if not getattr(args, option):
            if option in config:
                setattr(args, option, config[option])
            else:
                sys.exit(
                    f"Must supply --{option.replace('_', '-')} directory "
                    f"or {option} option in ~/.python-wasm.ini"
                )
    return args


def check_build_dir(directory, human_name):
    """Prompt user if directory already exists."""
    if directory.exists():
        if input(f"{human_name} exists. Use existing (y/n)? ") != "y":
            sys.exit("Exiting")
    return directory


def build_cpython(python_build_dir, python_version):
    if python_version:
        run(f"git checkout {python_version}")
    python_build_dir.mkdir(parents=True)
    run("../../configure -C", cwd=python_build_dir)
    run("make -j$(nproc)", cwd=python_build_dir)


def build_wasm_browser(wasm_build_dir):
    wasm_build_dir.mkdir(parents=True)
    os.environ["CONFIG_SITE"] = (
        "../../Tools/wasm/config.site-wasm32-emscripten"
    )
    run(
        "emconfigure ../../configure -C"
        " --host=wasm32-unknown-emscripten"
        " --build=$(../../config.guess)"
        " --with-emscripten-target=browser"
        " --with-build-python=$(pwd)/../build/python",
        cwd=wasm_build_dir,
    )
    run("emmake make -j$(nproc)", cwd=wasm_build_dir)


def build_wasm_node(wasm_build_dir):
    wasm_build_dir.mkdir(parents=True)
    os.environ["CONFIG_SITE"] = (
        "../../Tools/wasm/config.site-wasm32-emscripten"
    )
    run(
        "emconfigure ../../configure -C"
        " --host=wasm32-unknown-emscripten"
        " --build=$(../../config.guess)"
        " --with-emscripten-target=node"
        " --with-build-python=$(pwd)/../build/python",
        cwd=wasm_build_dir,
    )
    run("emmake make -j$(nproc)", cwd=wasm_build_dir)


def prepare_browser_files(wasm_build_dir, url_prefix):
    """Fingerprint browser WASM files and add appropriate URL prefixes."""
    data_file = wasm_build_dir / "python.data"
    wasm_file = wasm_build_dir / "python.wasm"
    js_file = wasm_build_dir / "python.js"
    js_file_contents = js_file.read_text()

    for path in [data_file, wasm_file]:
        new_path = fingerprint_filename(path)
        copy(path, new_path)
        js_file_contents = re.sub(
            rf'"{path.name}"',
            rf'"{url_prefix}{new_path.name}"',
            js_file_contents
        )

        # Without this, the URL will become an absolute path
        js_file_contents = js_file_contents.replace(
            "wasmBinaryFile=locateFile(wasmBinaryFile)",
            "",
        )
        new_path = fingerprint_filename(js_file)
        new_path.write_text(js_file_contents)
    else:
        copy(wasm_file, fingerprint_filename(wasm_file))
        copy(js_file, fingerprint_filename(js_file))


def prepare_node_files(python_build_dir, wasm_build_dir):
    """Zip the essential WASM node files up."""
    os.chdir(python_build_dir)
    lib_dir = python_build_dir / "Lib"
    with ZipFile("wasm-node-build.zip", "w") as zip_file:
        for path in chain(lib_dir.rglob("*"), wasm_build_dir.rglob("*")):
            if path.is_file():
                zip_file.write(
                    os.fspath(path),
                    os.fspath(path.relative_to(python_build_dir)),
                )


def main():
    args = parse_args()
    cpython_path = Path(args.cpython)
    emsdk_path = Path(args.emsdk)
    url_prefix = args.url_prefix
    python_version = args.python_version
    setup_emsdk_version = args.setup_emsdk_version

    os.chdir(cpython_path)

    python_build_dir = check_build_dir(
        cpython_path / "builddir/build",
        "CPython build directory",
    )
    wasm_browser_build_dir = check_build_dir(
        cpython_path / "builddir/emscripten-browser",
        "WASM browser build",
    )
    wasm_node_build_dir = check_build_dir(
        cpython_path / "builddir/emscripten-node",
        "WASM Node build",
    )

    if setup_emsdk_version:
        run(f"./emsdk install {setup_emsdk_version}", cwd=emsdk_path)
        run(f"./emsdk activate {setup_emsdk_version}", cwd=emsdk_path)
    shell_source(emsdk_path)  # Source emsdk
    run("embuilder --help", capture_output=True)  # Make sure embuilder works
    run("embuilder build zlib bzip2")  # Build emscripted ports

    if not python_build_dir.exists():
        build_cpython(python_build_dir, python_version)

    if not wasm_browser_build_dir.exists():
        build_wasm_browser(wasm_browser_build_dir)

    if not wasm_node_build_dir.exists():
        build_wasm_node(wasm_node_build_dir)

    prepare_browser_files(wasm_browser_build_dir, url_prefix)
    prepare_node_files(cpython_path, wasm_node_build_dir)


if __name__ == "__main__":
    main()
