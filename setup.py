import os
import re
import subprocess
import sys
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

        build_dir = os.path.join(ROOT_DIR, "build/nuitka")
        dist_dir = os.path.join(ROOT_DIR, "dist")
        exe_name = "pysocat"
        main_py_file = os.path.join(ROOT_DIR, "pysocat.py")
        os.makedirs(build_dir, exist_ok=True)
        os.makedirs(dist_dir, exist_ok=True)

        sub_args = dict(cwd=build_dir, env=dict(os.environ, PYTHONPATH=ROOT_DIR))

        def run_cmd(cmds):
            assert subprocess.run(cmds, **sub_args).returncode == 0

        run_cmd(
            [
                sys.executable,
                "-m",
                "nuitka",
                # "--static-libpython=yes",
                f"--output-filename={exe_name}",
                main_py_file,
            ]
        )
        run_cmd(
            [
                "bash",
                "-c",
                f"tar czvf {dist_dir}/{exe_name}-{PROJECT_VERSION}.tar.gz {exe_name}",
            ]
        )


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
