from __future__ import print_function
from __future__ import unicode_literals

import contextlib
import io
import os
import pipes
import shutil
import subprocess
import sys
import tempfile
import stat
import platform

from distutils.sysconfig import get_config_var
from setuptools.command.build_ext import build_ext as _build_ext


@contextlib.contextmanager
def _tmpdir():
    tempdir = tempfile.mkdtemp()
    try:
        yield tempdir
    finally:
        def del_with_escalation(action, name, exc):
            os.chmod(name, stat.S_IWRITE)
            os.remove(name)
        shutil.rmtree(tempdir, onerror=del_with_escalation)


def _get_cflags(compiler):
    return ' '.join('-I{}'.format(p) for p in compiler.include_dirs)


LFLAG_CLANG = '-Wl,-undefined,dynamic_lookup'
LFLAG_GCC = '-Wl,--unresolved-symbols=ignore-all'
LFLAG_WINDOWS = '-L{bin_dir}\libs -lpython{lib_version} '.format(bin_dir=get_config_var("BINDIR"),
                                                               lib_version=get_config_var("VERSION"))
LFLAGS = (LFLAG_CLANG, LFLAG_GCC, LFLAG_WINDOWS)


def _get_ldflags():
    """Determine the correct link flags.  This attempts dummy compiles similar
    to how autotools does feature detection.
    """
    cc = subprocess.check_output(('go', 'env', 'CC')).decode('UTF-8').strip()

    with _tmpdir() as tmpdir:
        testf = os.path.join(tmpdir, 'test.c')
        with io.open(testf, 'w') as f:
            f.write('int f(int); int main(void) { return f(0); }\n')

        if platform.system() == "Windows":
            return LFLAG_WINDOWS

        for lflag in LFLAGS:  # pragma: no cover (platform specific)
            try:
                subprocess.check_call((cc, testf, lflag), cwd=tmpdir)
                return lflag
            except subprocess.CalledProcessError:
                pass
        else:  # pragma: no cover (platform specific)
            # wellp, none of them worked, fall back to gcc and they'll get a
            # hopefully reasonable error message
            return LFLAG_GCC


def _check_call(cmd, cwd, env):
    envparts = [
        '{}={}'.format(k, pipes.quote(v))
        for k, v in sorted(tuple(env.items()))
    ]
    print(
        '$ {}'.format(' '.join(envparts + [pipes.quote(p) for p in cmd])),
        file=sys.stderr,
    )
    env = {key.encode("utf-8", "ignore"): value.encode("utf-8", "ignore") for key, value in env.items()}
    subprocess.check_call(cmd, cwd=cwd, env=dict(os.environ, **env))


def _get_build_extension_method(base, root):
    def build_extension(self, ext):
        def _raise_error(msg):
            raise IOError(
                'Error building extension `{}`: '.format(ext.name) + msg,
            )

        # If there are no .go files then the parent should handle this
        if not any(source.endswith('.go') for source in ext.sources):
            return base.build_extension(self, ext)

        if len(ext.sources) != 1:
            _raise_error(
                'sources must be a single file in the `main` package.\n'
                'Recieved: {!r}'.format(ext.sources)
            )

        main_file, = ext.sources
        if not os.path.exists(main_file):
            _raise_error('{} does not exist'.format(main_file))
        main_dir = os.path.dirname(main_file)

        # Copy the package into a temporary GOPATH environment
        with _tmpdir() as tempdir:
            root_path = os.path.join(tempdir, 'src', root)
            # Make everything but the last directory (copytree interface)
            os.makedirs(os.path.dirname(root_path))
            shutil.copytree('.', root_path)
            pkg_path = os.path.join(root_path, main_dir)

            env = {'GOPATH': tempdir}
            cmd_get = ('go', 'get', '-d')
            _check_call(cmd_get, cwd=pkg_path, env=env)

            env.update({
                'CGO_CFLAGS': _get_cflags(self.compiler),
                'CGO_LDFLAGS': _get_ldflags(),
            })
            cmd_build = (
                'go', 'build', '-buildmode=c-shared',
                '-o', os.path.abspath(self.get_ext_fullpath(ext.name)),
            )
            _check_call(cmd_build, cwd=pkg_path, env=env)

    return build_extension


def _get_build_ext_cls(base, root):
    class build_ext(base):
        build_extension = _get_build_extension_method(base, root)

    return build_ext


def set_build_ext(dist, attr, value):
    root = value['root']
    base = dist.cmdclass.get('build_ext', _build_ext)
    dist.cmdclass['build_ext'] = _get_build_ext_cls(base, root)


GOLANG = 'https://storage.googleapis.com/golang/go1.7.5.linux-amd64.tar.gz'
WHEEL_ARGS = '--no-deps --wheel-dir /tmp /dist/*.tar.gz'


def build_manylinux_wheels(argv=None):  # pragma: no cover
    assert os.path.exists('setup.py')
    shutil.rmtree('dist')
    os.makedirs('dist')
    _check_call(('python', 'setup.py', 'sdist'), cwd='.', env={})
    _check_call(
        (
            'docker', 'run',
            '--volume', '{}:/dist:rw'.format(os.path.abspath('dist')),
            # I'd use --user, but this breaks git:
            # http://stackoverflow.com/a/20272540/812183
            '--env', 'UID={}'.format(os.getuid()),
            '--env', 'GID={}'.format(os.getgid()),
            'quay.io/pypa/manylinux1_x86_64:latest',
            'bash', '-exc',
            'cd /tmp\n'
            'wget {golang} -q --no-check-certificate -O /tmp/golang.tar.gz\n'
            'tar -xf /tmp/golang.tar.gz\n'
            'export GOROOT=/tmp/go\n'
            'export PATH="$GOROOT/bin:$PATH"\n'
            '/opt/python/cp27-cp27mu/bin/pip wheel {wheel_args}\n'
            '/opt/python/cp34-cp34m/bin/pip wheel {wheel_args}\n'
            '/opt/python/cp35-cp35m/bin/pip wheel {wheel_args}\n'
            '/opt/python/cp36-cp36m/bin/pip wheel {wheel_args}\n'
            'mkdir /tmp/whls\n'
            'ls *.whl | xargs -n1 --verbose auditwheel repair -w /tmp/whls\n'
            'cp /tmp/whls/* /dist\n'
            'chown "$UID:$GID" /dist/*\n'
            'ls /dist -al\n'.format(golang=GOLANG, wheel_args=WHEEL_ARGS),
        ),
        cwd='.', env={},
    )
    print('*' * 79)
    print('Your wheels have been built into ./dist')
    print('*' * 79)
