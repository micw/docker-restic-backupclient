#!/usr/bin/env python3

import logging as log
import requests
from requests.utils import requote_uri
import os.path
import subprocess

def mongodump_with_config(target_dir,config):
	if not 'host' in config:
		log.error('Missing mongodump config: host')
	if not 'username' in config:
		log.error('Missing mongodump config: username')
	if not 'password' in config:
		log.error('Missing mongodump config: password')
	host=config['host']
	username=config['username']
	password=config['password']
	port=config['port'] if 'port' in config else 27017
	dump_version=config['dump_version'] if 'dump_version' in config else 3
	return mongodump(target_dir,host,port,username,password,dump_version)

def mongodump(target_dir,host,port,username,password,dump_version):
	log.info('Setting binary.')
	if dump_version in (3, 4):
		binary = "mongodump"
		log.warning('dump_version is deprecated; using the installed MongoDB Database Tools.')
	else:
		log.error('Couldnt set binary.')
		return False

	try:
		log.info('Dumping mongodb at %s'%host)
		subprocess.check_call([
			'nice', '-n', '19', 'ionice', '-c3', binary,
			'--host=%s'%host, '--port=%s'%port, '--username=%s'%username,
			'--password=%s'%password, '--forceTableScan', '-o', target_dir])
	except (subprocess.CalledProcessError, OSError) as e:
		log.error('Mongodump failed.')
		return False
	return True

def main():
	log.basicConfig(level=log.INFO,format='%(asctime)s %(levelname)7s: %(message)s')
	import argparse
	parser = argparse.ArgumentParser(description='Dump mongo databases.')
	parser.add_argument('target_dir', metavar='target-dir', type=str,
		help='Directory to dump to')
	parser.add_argument('--host', metavar='host', type=str, help='Mongodb host', required=True)
	parser.add_argument('--port', metavar='host', type=int, help='Mongodb port', default=27017)
	parser.add_argument('-u','--username', metavar='username', type=str, help='Mongodb username', required=True)
	parser.add_argument('-p','--password', metavar='password', type=str, help='Mongodb password', required=True)
	parser.add_argument('--dump_version', metavar='3 or 4', type=int, help='Deprecated compatibility selector (3 or 4); both use the installed MongoDB Database Tools.', default=3)
	args=parser.parse_args()
	if not os.path.isdir(args.target_dir):
		print('No such directory: %s'%args.target_dir)
		quit(1)

	ok=mongodump(args.target_dir,args.host,args.port,args.username,args.password,args.dump_version)
	if ok:
		print('Dump successfully created.')
	else:
		print('Dump failed.')
		quit(1)



if __name__ == '__main__':
	main()
