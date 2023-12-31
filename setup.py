import os
import platform
import re
import subprocess
import sys
import tarfile
from distutils.command.clean import clean
from distutils.core import Command
from distutils.dir_util import remove_tree

from pkg_resources import parse_requirements
from setuptools import find_packages, setup

PROJECT_NAME = "pysocat"
PROJECT_VERSION = "0.0.1"
PROJECT_DESC = "Socat for python"
PROJECT_URL = "https://github.com/jmsun0/pysocat"
PROJECT_AUTHOR = "sunjm"
PROJECT_EMAIL = "sunjm1996@gmail.com"
PROJECT_LICENSE = "Apache License 2.0"

EXCLUDE_PACKAGES = []
PY_MODULES = ["pysocat"]
EXT_MODULES = []
CONSOLE_SCRIPTS = ["pysocat = pysocat:main"]


def include_package(package_name):
    is_exclude = False
    for exclude_package in EXCLUDE_PACKAGES:
        if re.match(exclude_package, package_name):
            is_exclude = True
            break
    return not is_exclude


PACKAGES = list(filter(include_package, find_packages()))


with open("requirements.txt", "r") as f:
    INSTALL_REQUIRES = [str(requirement) for requirement in parse_requirements(f)]

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))

PLATFORM_OS = platform.system().lower()
PLATFORM_ARCH = platform.machine().lower().replace("amd64", "x86_64")


class MyClean(clean):
    def run(self):
        # clean.run(self)

        egg_dir = list(filter(lambda x: x.endswith(".egg-info"), os.listdir(ROOT_DIR)))
        for directory in [self.build_base, self.bdist_base, *egg_dir]:
            if os.path.exists(directory):
                remove_tree(directory, dry_run=self.dry_run)
        for package_name in PACKAGES + [""]:
            dir_path = os.path.join(ROOT_DIR, package_name.replace(".", "/"))
            for filename in os.listdir(dir_path):
                file_path = os.path.join(dir_path, filename)
                if filename == "__pycache__":
                    remove_tree(file_path, dry_run=self.dry_run)
                elif (
                    filename.endswith(".c")
                    or filename.endswith(".pyd")
                    or filename.endswith(".so")
                ):
                    os.remove(file_path)


class MyBdistEXE(Command):
    description = "Build executable"
    user_options = []
    boolean_options = []
    sub_commands = []

    def initialize_options(self):
        ...

    def finalize_options(self):
        ...

    def run(self):
        for cmd_name in self.get_sub_commands():
            self.run_command(cmd_name)

        build_dir = os.path.join(ROOT_DIR, "build/exe")
        dist_dir = os.path.join(ROOT_DIR, "dist")
        exe_name = "pysocat"

        exe_suffix = ""
        if PLATFORM_OS == "windows":
            exe_suffix = ".exe"
        exe_full_name = f"{exe_name}{exe_suffix}"

        main_py_file = os.path.join(ROOT_DIR, "pysocat.py")

        os.makedirs(build_dir, exist_ok=True)
        os.makedirs(dist_dir, exist_ok=True)

        sub_args = dict(cwd=build_dir, env=dict(os.environ, PYTHONPATH=ROOT_DIR))

        def run_cmd(cmds):
            assert subprocess.run(cmds, **sub_args).returncode == 0

        package_program = os.environ.get("EXE_PACKAGE_PROGRAM", "pyinstaller")

        if package_program == "pyinstaller":
            run_cmd(
                [
                    sys.executable,
                    "-m",
                    "PyInstaller",
                    "--noconfirm",
                    "-F",
                    "--clean",
                    main_py_file,
                ]
            )
            build_dist_dir = os.path.join(build_dir, "dist")
            for file in os.listdir(build_dist_dir):
                output_file = os.path.join(build_dist_dir, file)
                break
        elif package_program == "nuitka":
            run_cmd(
                [
                    sys.executable,
                    "-m",
                    "nuitka",
                    "--standalone",
                    "--onefile",
                    "--follow-imports",
                    # "--static-libpython=yes",
                    # "--output-dir=.",
                    f"--output-filename={exe_full_name}",
                    "--show-scons",
                    "--assume-yes-for-downloads",
                    main_py_file,
                ]
            )
            output_file = f"{build_dir}/{exe_name}.dist/{exe_full_name}"
        else:
            raise

        with tarfile.open(
            name=f"{dist_dir}/{exe_name}-{PROJECT_VERSION}-{PLATFORM_OS}-{PLATFORM_ARCH}.tar.gz",
            mode='w:gz',
            format=tarfile.GNU_FORMAT,
        ) as tar:
            tar.add(output_file, arcname=exe_full_name)


if __name__ == "__main__":
    ...
    setup(
        name=PROJECT_NAME,
        version=PROJECT_VERSION,
        description=PROJECT_DESC,
        long_description=PROJECT_DESC,
        url=PROJECT_URL,
        author=PROJECT_AUTHOR,
        author_email=PROJECT_EMAIL,
        license=PROJECT_LICENSE,
        classifiers=[
            f"License :: OSI Approved :: {PROJECT_LICENSE}",
            f"Topic :: {PROJECT_NAME}",
            "Development Status :: 5 - Production/Stable",
            "Environment :: Console :: Curses",
            "Operating System :: MacOS",
            "Operating System :: POSIX",
            "Operating System :: Microsoft :: Windows",
            "Programming Language :: Python :: 3 :: Only",
            "Programming Language :: Python :: 3.6",
            "Programming Language :: Python :: 3.7",
            "Programming Language :: Python :: 3.8",
            "Programming Language :: Python :: 3.9",
            "Programming Language :: Python :: 3.10",
            "Programming Language :: Python :: 3.11",
            "Programming Language :: Python :: 3.12",
            "Programming Language :: Python :: Implementation :: CPython",
            "Programming Language :: Python :: Implementation :: PyPy",
        ],
        project_urls={
            "Documentation": PROJECT_URL,
            "Source": PROJECT_URL,
            "Tracker": PROJECT_URL,
        },
        packages=PACKAGES,
        py_modules=PY_MODULES,
        include_package_data=True,
        package_data={"": ["*"]},
        entry_points={"console_scripts": CONSOLE_SCRIPTS},
        install_requires=INSTALL_REQUIRES,
        extras_require={"dev": ["wheel", "cython", "pyinstaller"]},
        ext_modules=EXT_MODULES,
        cmdclass={
            # "build": MyBuild,
            # "build_ext": MyBuildExt,
            "clean": MyClean,
            "bdist_exe": MyBdistEXE,
        },
    )
