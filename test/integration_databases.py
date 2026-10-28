#!/usr/bin/env python3
"""Real-image, isolated database integration test (requires Docker).

Run: python3 test/integration_databases.py
Only UUID-named resources created here are removed; no host ports or bind mounts.
"""
import subprocess
import threading
import uuid
import sys

IMAGE = sys.argv[1] if len(sys.argv) > 1 else 'restic-backupclient:1.0.0-test'


def docker(*args, input=None, check=True, timeout=120):
    return subprocess.run(['docker', *args], input=input, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=check, timeout=timeout)


def ready(container, command):
    last = None
    for attempt in range(90):
        last = docker('exec', container, *command, check=False, timeout=10)
        if last.returncode == 0:
            return
        # Bounded readiness-probe backoff, not a startup delay or busy poll.
        if attempt < 89:
            threading.Event().wait(1)
    raise RuntimeError(f'{container} not ready: {last.stderr}')


TEST = r'''
import gzip
import os
import pathlib
import subprocess
import sys
import tempfile
sys.path.insert(0, '/scripts')
import mysqldump as mysql
import pgdump as pg

def run(args, sql=None, env=None):
    return subprocess.run(args, input=sql, text=True, check=True,
                          stdout=subprocess.PIPE, env=env).stdout.strip()

m = ['mysql', '--host=mysql', '--user=root', '--batch', '--skip-column-names']
p = ['/usr/bin/psql', '--host=postgres', '--username=postgres', '--no-password',
     '--no-psqlrc', '--tuples-only', '--no-align', '--set=ON_ERROR_STOP=1']
me = dict(os.environ, MYSQL_PWD='integration-secret')
pe = dict(os.environ, PGPASSWORD='integration-secret', PGCONNECT_TIMEOUT='3')
sql = "CREATE TABLE proof (id INTEGER PRIMARY KEY, value VARCHAR(100)); INSERT INTO proof VALUES (1, 'restored content'), (2, 'quotes '' and unicode café');"
with tempfile.TemporaryDirectory() as root:
    root = pathlib.Path(root)
    md = root / 'mysql'; md.mkdir()
    pd = root / 'pg'; pd.mkdir()
    run(m, 'CREATE DATABASE integration;', me)
    run(m + ['integration'], sql, me)
    run(p + ['--dbname=postgres'], 'CREATE DATABASE integration;', pe)
    run(p + ['--dbname=integration'], sql, pe)
    expected_m = run(m + ['integration'], 'SELECT * FROM proof ORDER BY id;', me)
    expected_p = run(p + ['--dbname=integration'], 'SELECT * FROM proof ORDER BY id;', pe)
    assert mysql.mysql_dump(str(md), 'mysql', 3306, 'root', 'integration-secret', ['^integration$'], None, []) is True
    assert pg.pg_dump(str(pd), 'postgres', 5432, 'postgres', 'integration-secret', ['^integration$'], None) is True
    all_pg = root / 'pg-all'; all_pg.mkdir()
    assert pg.pg_dump(str(all_pg), 'postgres', 5432, 'postgres', 'integration-secret', None, None) is True
    assert sorted(x.name for x in all_pg.iterdir()) == ['PGSQL_integration.sql.gz', 'PGSQL_postgres.sql.gz']
    assert sorted(x.name for x in md.iterdir()) == ['MYSQL_integration_DATA.sql.gz', 'MYSQL_integration_DROP_CREATE.sql.gz']
    assert [x.name for x in pd.iterdir()] == ['PGSQL_integration.sql.gz']
    run(m, 'DROP DATABASE integration;', me)
    for name in ['MYSQL_integration_DROP_CREATE.sql.gz', 'MYSQL_integration_DATA.sql.gz']:
        with gzip.open(md / name, 'rt') as f:
            run(m + (['integration'] if '_DATA' in name else []), f.read(), me)
    run(p + ['--dbname=postgres'], 'DROP DATABASE integration;', pe)
    run(p + ['--dbname=postgres'], 'CREATE DATABASE integration;', pe)
    with gzip.open(pd / 'PGSQL_integration.sql.gz', 'rt') as f:
        run(p + ['--dbname=integration'], f.read(), pe)
    assert run(m + ['integration'], 'SELECT * FROM proof ORDER BY id;', me) == expected_m
    assert run(p + ['--dbname=integration'], 'SELECT * FROM proof ORDER BY id;', pe) == expected_p
    print('PASS: MariaDB and PostgreSQL real dump/drop/restore content verification', flush=True)
    bad = root / 'bad'; bad.mkdir()
    assert mysql.mysql_dump(str(bad), 'mysql', 1, 'root', 'wrong', None, None, []) is False
    assert pg.pg_dump(str(bad), 'postgres', 1, 'postgres', 'wrong', None, None) is False
    assert not list(bad.iterdir())
    print('PASS: both connection failures return False without artifacts', flush=True)
    # Actual native dump process failures, after successful database discovery.
    assert mysql.mysql_dump(str(bad), 'mysql', 3306, 'root', 'integration-secret', ['^integration$'], None, ['--invalid-integration-option']) is False
    run(p + ['--dbname=postgres'], "CREATE ROLE dump_denied LOGIN PASSWORD 'denied';", pe)
    assert pg.pg_dump(str(bad), 'postgres', 5432, 'dump_denied', 'denied', ['^integration$'], None) is False
    assert not list(bad.iterdir())
    print('PASS: native dump tool errors return False without artifacts', flush=True)
    # Exercise producer and compressor exit status independently for both modules.
    shim = root / 'bin'; shim.mkdir()
    (shim / 'gzip').write_text('#!/bin/sh\nexit 29\n')
    (shim / 'gzip').chmod(0o755)
    for module in (mysql, pg):
        for compressor_failure in (False, True):
            destination = bad / 'existing.gz'
            destination.write_bytes(b'previous backup')
            old_path = os.environ['PATH']
            try:
                if compressor_failure:
                    os.environ['PATH'] = str(shim) + ':' + old_path
                command = [sys.executable, '-c', 'import sys; sys.exit(0)' if compressor_failure else 'import sys; sys.stdout.write("partial"); sys.exit(23)']
                try:
                    module._dump_gzip(command, str(destination), dict(os.environ))
                except subprocess.CalledProcessError as exc:
                    assert exc.returncode == (29 if compressor_failure else 23)
                else:
                    raise AssertionError('failed subprocess incorrectly accepted')
                assert destination.read_bytes() == b'previous backup'
                assert sorted(x.name for x in bad.iterdir()) == ['existing.gz']
            finally:
                os.environ['PATH'] = old_path
                destination.unlink()
    print('PASS: producer/compressor errors preserve previous files and clean temporary files', flush=True)
'''


def main():
    prefix = 'restic-db-it-' + uuid.uuid4().hex
    network = prefix + '-net'
    containers = []
    network_created = False
    try:
        docker('image', 'inspect', IMAGE)
        docker('network', 'create', network)
        network_created = True
        for suffix, image, environment in [
                ('mysql', 'mariadb:11.8', 'MARIADB_ROOT_PASSWORD=integration-secret'),
                ('postgres', 'postgres:18', 'POSTGRES_PASSWORD=integration-secret'),
                ('client', IMAGE, None)]:
            name = prefix + '-' + suffix
            args = ['run', '-d', '--name', name, '--network', network,
                    '--network-alias', suffix]
            if environment:
                args += ['-e', environment]
            else:
                args += ['--entrypoint', 'python3']
            args += [image]
            if not environment:
                args += ['-c', 'import threading; threading.Event().wait()']
            # Register before creation so even timeout/interruption is cleaned up.
            containers.append(name)
            docker(*args, timeout=300)
        ready(prefix + '-mysql', ['mariadb', '--protocol=tcp', '--host=127.0.0.1',
              '--user=root', '--password=integration-secret', '--execute=SELECT 1'])
        ready(prefix + '-postgres', ['env', 'PGPASSWORD=integration-secret', 'psql', '--no-password', '--host=127.0.0.1', '--username=postgres',
              '--dbname=postgres', '--command=SELECT 1'])
        result = docker('exec', '-i', prefix + '-client', 'python3', '-', input=TEST)
        print(result.stdout, end='')
        print('PASS: all real-image database integration checks')
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or '')
        print(exc.stderr or '')
        raise
    finally:
        errors = []
        for name in reversed(containers):
            result = docker('rm', '-f', '-v', name, check=False)
            if result.returncode and 'No such container' not in result.stderr:
                errors.append(result.stderr)
        if network_created:
            result = docker('network', 'rm', network, check=False)
            if result.returncode:
                errors.append(result.stderr)
        if errors:
            raise RuntimeError('Owned-resource cleanup failed: ' + '\n'.join(errors))


if __name__ == '__main__':
    main()
