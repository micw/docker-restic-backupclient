#!/usr/bin/env python3

import logging as log
import requests
from requests.utils import requote_uri
import os.path
import subprocess
import re
import tempfile

def _dump_gzip(command, destination, env):
	"""Publish a dump only after both the producer and gzip succeed."""
	fd, temporary = tempfile.mkstemp(prefix='.dump-', dir=os.path.dirname(destination))
	producer = compressor = None
	try:
		with os.fdopen(fd, 'wb') as output:
			producer = subprocess.Popen(command, stdout=subprocess.PIPE, env=env)
			try:
				compressor = subprocess.Popen(
					['nice', '-n', '19', 'gzip', '--best', '--rsyncable'],
					stdin=producer.stdout, stdout=output)
			finally:
				producer.stdout.close()
			gzip_status = compressor.wait()
			dump_status = producer.wait()
			if dump_status or gzip_status:
				raise subprocess.CalledProcessError(dump_status or gzip_status, command[0])
		os.replace(temporary, destination)
	finally:
		for process in (compressor, producer):
			if process is not None and process.poll() is None:
				process.terminate()
				process.wait()
		if os.path.exists(temporary):
			os.unlink(temporary)

def pg_list_database(host,port,username,password):

	try:
		log.info('Getting list of databases')
		output=subprocess.check_output([
			'/usr/bin/psql', '--host=%s'%host, '--port=%s'%port,
			'--username=%s'%username, '--no-password', '--no-psqlrc',
			'--dbname=postgres', '--tuples-only', '--no-align',
			'--command=SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname'
			], env=dict(os.environ, PGPASSWORD=password)).decode()
	except (subprocess.CalledProcessError, OSError) as e:
		log.error('Listing of databases failed.')
		return None

	result=[]

	for i,v in enumerate(output.split('\n')):
		v=v.strip()
		if v!="":
			result.append(v)

	if len(result)==0:
		print("No databases found!")
		return []

	return result

def pg_dump_with_config(target_dir,config):
	if not 'host' in config:
		log.error('Missing pg config: host')
	if not 'username' in config:
		log.error('Missing pg config: username')
	if not 'password' in config:
		log.error('Missing pg config: password')
	host=config['host']
	username=config['username']
	password=config['password']
	port=config['port'] if 'port' in config else 5432
	include_patterns=config['include'] if 'include' in config else None
	exclude_patterns=config['exclude'] if 'exclude' in config else None
	return pg_dump(target_dir,host,port,username,password,include_patterns,exclude_patterns)

def pg_dump(target_dir,host,port,username,password,include_patterns,exclude_patterns):
	if include_patterns and exclude_patterns:
		log.error("Either inclusion or exclusion of indices is allowed, not both!")
	databases=pg_list_database(host,port,username,password)
	if databases is None:
		return False

	for database in databases:
		if '/' in database or '\x00' in database:
			log.error('Unsafe database name for dump filename.')
			return False
		if include_patterns:
			included=False
			for p in include_patterns:
				if (re.compile(p).match(database)):
					included=True
					break
			if included:
				log.info('Postgresql: database %s is included for this dump.'%database)
			else:
				log.info('Postgresql: database %s is not included for this dump.'%database)
				continue
		if exclude_patterns:
			excluded=False
			for p in exclude_patterns:
				if (re.compile(p).match(database)):
					excluded=True
					break
			if excluded:
				log.info('Postgresql: database %s is excluded for this dump.'%database)
				continue
			else:
				log.info('Postgresql: database %s is not excluded for this dump.'%database)
		try:
			log.info('Postgresql: Dumping %s'%(database))
			_dump_gzip(['nice', '-n', '19', 'ionice', '-c3', '/usr/bin/pg_dump',
				'--no-password', '--host=%s'%host, '--port=%s'%port,
				'--user=%s'%username, '--', database],
				os.path.join(target_dir, 'PGSQL_%s.sql.gz'%database),
				dict(os.environ, PGPASSWORD=password))
		except (subprocess.CalledProcessError, OSError) as e:
			log.error('Pgdump failed.')
			return False
	return True

def main():
	log.basicConfig(level=log.INFO,format='%(asctime)s %(levelname)7s: %(message)s')
	import argparse
	parser = argparse.ArgumentParser(description='Dump postgresql databases.')
	parser.add_argument('target_dir', metavar='target-dir', type=str,
		help='Directory to dump to')
	parser.add_argument('--host', metavar='host', type=str, help='Postgresql host', required=True)
	parser.add_argument('--port', metavar='host', type=int, help='Postgresql port', default=5432)
	parser.add_argument('-u','--username', metavar='username', type=str, help='Postgresql username', required=True)
	parser.add_argument('-p','--password', metavar='password', type=str, help='Postgresql password', required=True)
	parser.add_argument('-i','--include', metavar='expression', type=str, nargs='*',
		help='Databases to include (regular expression)')
	parser.add_argument('-e','--exclude', metavar='expression', type=str, nargs='*',
		help='Databases to exclude (regular expression)')
	args=parser.parse_args()
	if not os.path.isdir(args.target_dir):
		print('No such directory: %s'%args.target_dir)
		quit(1)

	ok=pg_dump(args.target_dir,args.host,args.port,args.username,args.password,args.include,args.exclude)
	if ok:
		print('Dump successfully created.')
	else:
		print('Dump failed.')
		quit(1)



if __name__ == '__main__':
	main()
