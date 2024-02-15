#! /usr/bin/env python
# encoding: utf-8
# Thomas Nagy, 2019 (ita)

"""
Filesystem-based cache system to share and re-use build artifacts

Cache access operations (copy to and from) are delegated to
independent pre-forked worker subprocesses.

The following environment variables may be set:
* WAFCACHE: several possibilities:
  - File cache:
    absolute path of the waf cache (~/.cache/wafcache_user,
    where `user` represents the currently logged-in user)
  - URL to a cache server, for example:
    export WAFCACHE=http://localhost:8080/files/
    in that case, GET/POST requests are made to urls of the form
    http://localhost:8080/files/000000000/0 (cache management is delegated to the server)
  - GCS, S3 or MINIO bucket
    gs://my-bucket/    (uses gsutil command line tool or WAFCACHE_CMD)
    s3://my-bucket/    (uses aws command line tool or WAFCACHE_CMD)
    minio://my-bucket/ (uses mc command line tool or WAFCACHE_CMD)
* WAFCACHE_CMD: bucket upload/download command, for example:
    WAFCACHE_CMD="gsutil cp %{SRC} %{TGT}"
  Note that the WAFCACHE bucket value is used for the source or destination
  depending on the operation (upload or download). For example, with:
    WAFCACHE="gs://mybucket/"
  the following commands may be run:
    gsutil cp build/myprogram  gs://mybucket/aa/aaaaa/1
    gsutil cp gs://mybucket/bb/bbbbb/2 build/somefile
* WAFCACHE_NO_PUSH: if set, disables pushing to the cache
* WAFCACHE_VERBOSITY: if set, displays more detailed cache operations

File cache specific options:
  Files are copied using hard links by default; if the cache is located
  onto another partition, the system switches to file copies instead.
* WAFCACHE_TRIM_MAX_FOLDER: maximum amount of tasks to cache (1M)
* WAFCACHE_EVICT_MAX_BYTES: maximum amount of cache size in bytes (10GB)
* WAFCACHE_EVICT_INTERVAL_MINUTES: minimum time interval to try
                                   and trim the cache (3 minutess)

Usage::

	def build(bld):
		bld.load('wafcache')
		...

To troubleshoot::

	waf clean build --zones=wafcache
"""

import atexit, base64, errno, fcntl, getpass, os, re, shutil, sys, time, traceback, urllib3, shlex
try:
	import subprocess32 as subprocess
except ImportError:
	import subprocess

base_cache = os.path.expanduser('~/.cache/')
if not os.path.isdir(base_cache):
	base_cache = '/tmp/'
default_wafcache_dir = os.path.join(base_cache, 'wafcache_' + getpass.getuser())

CACHE_DIR = os.environ.get('WAFCACHE', default_wafcache_dir)
WAFCACHE_CMD = os.environ.get('WAFCACHE_CMD')
TRIM_MAX_FOLDERS = int(os.environ.get('WAFCACHE_TRIM_MAX_FOLDER', 1000000))
EVICT_INTERVAL_MINUTES = int(os.environ.get('WAFCACHE_EVICT_INTERVAL_MINUTES', 3))
EVICT_MAX_BYTES = int(os.environ.get('WAFCACHE_EVICT_MAX_BYTES', 10**10))
WAFCACHE_NO_PUSH = 1 if os.environ.get('WAFCACHE_NO_PUSH') else 0
WAFCACHE_VERBOSITY = 1 if os.environ.get('WAFCACHE_VERBOSITY') else 0
OK = "ok"

re_waf_cmd = re.compile('(?P<src>%{SRC})|(?P<tgt>%{TGT})')

try:
	import cPickle
except ImportError:
	import pickle as cPickle

if __name__ != '__main__':
	from waflib import Task, Logs, Utils, Build

def can_retrieve_cache(self):
	"""
	New method for waf Task classes
	"""
	if not self.outputs:
		return False

	self.cached = False

	sig = self.signature()
	ssig = Utils.to_hex(self.uid() + sig)

	files_to = [node.abspath() for node in self.outputs]
	err = cache_command(ssig, [], files_to)
	if err.startswith(OK):
		if WAFCACHE_VERBOSITY:
			Logs.pprint('CYAN', '  Fetched %r from cache' % files_to)
		else:
			Logs.debug('wafcache: fetched %r from cache', files_to)
	else:
		if WAFCACHE_VERBOSITY:
			Logs.pprint('YELLOW', '  No cache entry %s' % files_to)
		else:
			Logs.debug('wafcache: No cache entry %s: %s', files_to, err)
		return False

	self.cached = True
	return True

def put_files_cache(self):
	"""
	New method for waf Task classes
	"""
	if WAFCACHE_NO_PUSH or getattr(self, 'cached', None) or not self.outputs:
		return

	bld = self.generator.bld
	sig = self.signature()
	ssig = Utils.to_hex(self.uid() + sig)

	files_from = [node.abspath() for node in self.outputs]
	err = cache_command(ssig, files_from, [])

	if err.startswith(OK):
		if WAFCACHE_VERBOSITY:
			Logs.pprint('CYAN', '  Successfully uploaded %s to cache' % files_from)
		else:
			Logs.debug('wafcache: Successfully uploaded %r to cache', files_from)
	else:
		if WAFCACHE_VERBOSITY:
			Logs.pprint('RED', '  Error caching step results %s: %s' % (files_from, err))
		else:
			Logs.debug('wafcache: Error caching results %s: %s', files_from, err)

	bld.task_sigs[self.uid()] = self.cache_sig

def hash_env_vars(self, env, vars_lst):
	"""
	Reimplement BuildContext.hash_env_vars so that the resulting hash does not depend on local paths
	"""
	if not env.table:
		env = env.parent
		if not env:
			return Utils.SIG_NIL

	idx = str(id(env)) + str(vars_lst)
	try:
		cache = self.cache_env
	except AttributeError:
		cache = self.cache_env = {}
	else:
		try:
			return self.cache_env[idx]
		except KeyError:
			pass

	v = str([env[a] for a in vars_lst])
	v = v.replace(self.srcnode.abspath().__repr__()[:-1], '')
	m = Utils.md5()
	m.update(v.encode())
	ret = m.digest()

	Logs.debug('envhash: %r %r', ret, v)

	cache[idx] = ret

	return ret

def uid(self):
	"""
	Reimplement Task.uid() so that the signature does not depend on local paths
	"""
	try:
		return self.uid_
	except AttributeError:
		m = Utils.md5()
		src = self.generator.bld.srcnode
		up = m.update
		up(self.__class__.__name__.encode())
		for x in self.inputs + self.outputs:
			up(x.path_from(src).encode())
		self.uid_ = m.digest()
		return self.uid_


def make_cached(cls):
	"""
	Enable the waf cache for a given task class
	"""
	if getattr(cls, 'nocache', None) or getattr(cls, 'has_cache', False):
		return

	m1 = getattr(cls, 'run', None)
	def run(self):
		if getattr(self, 'nocache', False):
			return m1(self)
		if self.can_retrieve_cache():
			return 0
		return m1(self)
	cls.run = run

	m2 = getattr(cls, 'post_run', None)
	def post_run(self):
		if getattr(self, 'nocache', False):
			return m2(self)
		ret = m2(self)
		self.put_files_cache()
		if hasattr(self, 'chmod'):
			for node in self.outputs:
				os.chmod(node.abspath(), self.chmod)
		return ret
	cls.post_run = post_run
	cls.has_cache = True

process_pool = []
def get_process():
	"""
	Returns a worker process that can process waf cache commands
	The worker process is assumed to be returned to the process pool when unused
	"""
	try:
		return process_pool.pop()
	except IndexError:
		filepath = os.path.dirname(os.path.abspath(__file__)) + os.sep + 'wafcache.py'
		cmd = [sys.executable, '-c', Utils.readf(filepath)]
		return subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, bufsize=0)

def atexit_pool():
	for k in process_pool:
		try:
			os.kill(k.pid, 9)
		except OSError:
			pass
		else:
			k.wait()
atexit.register(atexit_pool)

def build(bld):
	"""
	Called during the build process to enable file caching
	"""
	if process_pool:
		# already called once
		return

	# pre-allocation
	processes = [get_process() for x in range(bld.jobs)]
	process_pool.extend(processes)

	Task.Task.can_retrieve_cache = can_retrieve_cache
	Task.Task.put_files_cache = put_files_cache
	Task.Task.uid = uid
	Build.BuildContext.hash_env_vars = hash_env_vars
	for x in reversed(list(Task.classes.values())):
		make_cached(x)

def cache_command(sig, files_from, files_to):
	"""
	Create a command for cache worker processes, returns a pickled
	base64-encoded tuple containing the task signature, a list of files to
	cache and a list of files files to get from cache (one of the lists
	is assumed to be empty)
	"""
	proc = get_process()

	obj = base64.b64encode(cPickle.dumps([sig, files_from, files_to]))
	proc.stdin.write(obj)
	proc.stdin.write('\n'.encode())
	proc.stdin.flush()
	obj = proc.stdout.readline()
	if not obj:
		raise OSError('Preforked sub-process %r died' % proc.pid)
	process_pool.append(proc)
	return cPickle.loads(base64.b64decode(obj))

try:
	copyfun = os.link
except NameError:
	copyfun = shutil.copy2

def atomic_copy(orig, dest):
	"""
	Copy files to the cache, the operation is atomic for a given file
	"""
	global copyfun
	tmp = dest + '.tmp'
	up = os.path.dirname(dest)
	try:
		os.makedirs(up)
	except OSError:
		pass

	try:
		copyfun(orig, tmp)
	except OSError as e:
		if e.errno == errno.EXDEV:
			copyfun = shutil.copy2
			copyfun(orig, tmp)
		else:
			raise
	os.rename(tmp, dest)

def lru_trim():
	"""
	the cache folders take the form:
	`CACHE_DIR/0b/0b180f82246d726ece37c8ccd0fb1cde2650d7bfcf122ec1f169079a3bfc0ab9`
	they are listed in order of last access, and then removed
	until the amount of folders is within TRIM_MAX_FOLDERS and the total space
	taken by files is less than EVICT_MAX_BYTES
	"""
	lst = []
	for up in os.listdir(CACHE_DIR):
		if len(up) == 2:
			sub = os.path.join(CACHE_DIR, up)
			for hval in os.listdir(sub):
				path = os.path.join(sub, hval)

				size = 0
				for fname in os.listdir(path):
					size += os.lstat(os.path.join(path, fname)).st_size
				lst.append((os.stat(path).st_mtime, size, path))

	lst.sort(key=lambda x: x[0])
	lst.reverse()

	tot = sum(x[1] for x in lst)
	while tot > EVICT_MAX_BYTES or len(lst) > TRIM_MAX_FOLDERS:
		_, tmp_size, path = lst.pop()
		tot -= tmp_size

		tmp = path + '.tmp'
		try:
			shutil.rmtree(tmp)
		except OSError:
			pass
		try:
			os.rename(path, tmp)
		except OSError:
			sys.stderr.write('Could not rename %r to %r' % (path, tmp))
		else:
			try:
				shutil.rmtree(tmp)
			except OSError:
				sys.stderr.write('Could not remove %r' % tmp)
	sys.stderr.write("Cache trimmed: %r bytes in %r folders left\n" % (tot, len(lst)))


def lru_evict():
	"""
	Reduce the cache size
	"""
	lockfile = os.path.join(CACHE_DIR, 'all.lock')
	try:
		st = os.stat(lockfile)
	except EnvironmentError as e:
		if e.errno == errno.ENOENT:
			with open(lockfile, 'w') as f:
				f.write('')
			return
		else:
			raise

	if st.st_mtime < time.time() - EVICT_INTERVAL_MINUTES * 60:
		# check every EVICT_INTERVAL_MINUTES minutes if the cache is too big
		# OCLOEXEC is unnecessary because no processes are spawned
		fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o755)
		try:
			try:
				fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except EnvironmentError:
				sys.stderr.write('another process is running!\n')
				pass
			else:
				# now dow the actual cleanup
				lru_trim()
				os.utime(lockfile, None)
		finally:
			os.close(fd)

class netcache(object):
	def __init__(self):
		self.http = urllib3.PoolManager()

	def url_of(self, sig, i):
		return "%s/%s/%s" % (CACHE_DIR, sig, i)

	def upload(self, file_path, sig, i):
		url = self.url_of(sig, i)
		with open(file_path, 'rb') as f:
			file_data = f.read()
		r = self.http.request('POST', url, timeout=60,
			fields={ 'file': ('%s/%s' % (sig, i), file_data), })
		if r.status >= 400:
			raise OSError("Invalid status %r %r" % (url, r.status))

	def download(self, file_path, sig, i):
		url = self.url_of(sig, i)
		with self.http.request('GET', url, preload_content=False, timeout=60) as inf:
			if inf.status >= 400:
				raise OSError("Invalid status %r %r" % (url, inf.status))
			with open(file_path, 'wb') as out:
				shutil.copyfileobj(inf, out)

	def copy_to_cache(self, sig, files_from, files_to):
		try:
			for i, x in enumerate(files_from):
				if not os.path.islink(x):
					self.upload(x, sig, i)
		except Exception:
			return traceback.format_exc()
		return OK

	def copy_from_cache(self, sig, files_from, files_to):
		try:
			for i, x in enumerate(files_to):
				self.download(x, sig, i)
		except Exception:
			return traceback.format_exc()
		return OK

class fcache(object):
	def __init__(self):
		if not os.path.exists(CACHE_DIR):
			os.makedirs(CACHE_DIR)
		if not os.path.exists(CACHE_DIR):
			raise ValueError('Could not initialize the cache directory')

	def copy_to_cache(self, sig, files_from, files_to):
		"""
		Copy files to the cache, existing files are overwritten,
		and the copy is atomic only for a given file, not for all files
		that belong to a given task object
		"""
		try:
			for i, x in enumerate(files_from):
				dest = os.path.join(CACHE_DIR, sig[:2], sig, str(i))
				atomic_copy(x, dest)
		except Exception:
			return traceback.format_exc()
		else:
			# attempt trimming if caching was successful:
			# we may have things to trim!
			lru_evict()
		return OK

	def copy_from_cache(self, sig, files_from, files_to):
		"""
		Copy files from the cache
		"""
		try:
			for i, x in enumerate(files_to):
				orig = os.path.join(CACHE_DIR, sig[:2], sig, str(i))
				atomic_copy(orig, x)

			# success! update the cache time
			os.utime(os.path.join(CACHE_DIR, sig[:2], sig), None)
		except Exception:
			return traceback.format_exc()
		return OK

class bucket_cache(object):
	def bucket_copy(self, source, target):
		if WAFCACHE_CMD:
			def replacer(match):
				if match.group('src'):
					return source
				elif match.group('tgt'):
					return target
			cmd = [re_waf_cmd.sub(replacer, x) for x in shlex.split(WAFCACHE_CMD)]
		elif CACHE_DIR.startswith('s3://'):
			cmd = ['aws', 's3', 'cp', source, target]
		elif CACHE_DIR.startswith('gs://'):
			cmd = ['gsutil', 'cp', source, target]
		else:
			cmd = ['mc', 'cp', source, target]

		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		out, err = proc.communicate()
		if proc.returncode:
			raise OSError('Error copy %r to %r using: %r (exit %r):\n  out:%s\n  err:%s' % (
				source, target, cmd, proc.returncode, out.decode(), err.decode()))

	def copy_to_cache(self, sig, files_from, files_to):
		try:
			for i, x in enumerate(files_from):
				dest = os.path.join(CACHE_DIR, sig[:2], sig, str(i))
				self.bucket_copy(x, dest)
		except Exception:
			return traceback.format_exc()
		return OK

	def copy_from_cache(self, sig, files_from, files_to):
		try:
			for i, x in enumerate(files_to):
				orig = os.path.join(CACHE_DIR, sig[:2], sig, str(i))
				self.bucket_copy(orig, x)
		except EnvironmentError:
			return traceback.format_exc()
		return OK

def loop(service):
	"""
	This function is run when this file is run as a standalone python script,
	it assumes a parent process that will communicate the commands to it
	as pickled-encoded tuples (one line per command)

	The commands are to copy files to the cache or copy files from the
	cache to a target destination
	"""
	# one operation is performed at a single time by a single process
	# therefore stdin never has more than one line
	txt = sys.stdin.readline().strip()
	if not txt:
		# parent process probably ended
		sys.exit(1)
	ret = OK

	[sig, files_from, files_to] = cPickle.loads(base64.b64decode(txt))
	if files_from:
		# TODO return early when pushing files upstream
		ret = service.copy_to_cache(sig, files_from, files_to)
	elif files_to:
		# the build process waits for workers to (possibly) obtain files from the cache
		ret = service.copy_from_cache(sig, files_from, files_to)
	else:
		ret = "Invalid command"

	obj = base64.b64encode(cPickle.dumps(ret))
	sys.stdout.write(obj.decode())
	sys.stdout.write('\n')
	sys.stdout.flush()

if __name__ == '__main__':
	if CACHE_DIR.startswith('s3://') or CACHE_DIR.startswith('gs://') or CACHE_DIR.startswith('minio://'):
		if CACHE_DIR.startswith('minio://'):
			CACHE_DIR = CACHE_DIR[8:]   # minio doesn't need the protocol part, uses config aliases
		service = bucket_cache()
	elif CACHE_DIR.startswith('http'):
		service = netcache()
	else:
		service = fcache()
	while 1:
		try:
			loop(service)
		except KeyboardInterrupt:
			break

