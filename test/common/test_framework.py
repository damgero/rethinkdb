# This test framework is responsible for running the test suite

from argparse import ArgumentParser
from os.path import abspath, join, dirname, pardir, getmtime, relpath
import curses
import Queue
import fcntl
import fnmatch
import math
import multiprocessing
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
import traceback

from utils import guess_is_text_file
import test_report

default_test_results_dir = join(dirname(__file__), pardir, 'results')

argparser = ArgumentParser(description='Run RethinkDB tests', add_help=False)
argparser.add_argument('-j', '--jobs', type=int, default=1,
                       help='The number of tests to run simultaneously (Default: 1)')
argparser.add_argument('-l', '--list', action='store_true',
                       help='List all matching tests')
argparser.add_argument('-o', '--output-dir',
                       help='Directory where the tests results and logs will be written (Default: %s/*)' % default_test_results_dir)
argparser.add_argument('-d', '--run-dir',
                       help="Directory where the tests will be run. Use this option to run the tests on another partition or on /dev/shm (Default: same as -o)")
argparser.add_argument('-r', '--repeat', type=int, default=1,
                       help='The number of times to repeat each test (Default: 1)')
argparser.add_argument('-k', '--continue', action='store_true', dest='kontinue',
                       help='Continue repeating even if a test fails (Default: no)')
argparser.add_argument('-a', '--abort-fast', action='store_true', dest='abort_fast',
                       help='Abort the tests when a test fails (Default: no)')
argparser.add_argument('-v', '--verbose', action='store_true',
                       help='Be more verbose when running tests. Also works with -l and -L (Default: no)')
argparser.add_argument('-t', '--timeout', type=int, default=1200,
                       help='Timeout in seconds for each test (Default: 600)')
argparser.add_argument('-L', '--load', nargs='?', const=True, default=False, metavar='DIR',
                       help='Load logs from a previous test (Default: no)')
argparser.add_argument('-F', '--only-failed', action='store_true', dest='only_failed',
                       help='Only load failed tests')
argparser.add_argument('-E', '--examine', action='append', metavar='GLOB',
                       help='Examine log files from a previous run')
argparser.add_argument('-T', '--tree', action='store_true',
                       help='List the files generated by a previous tests')
argparser.add_argument('filter', nargs='*',
                       help='The name of the tests to run, or a group of tests, or their negation with ! (Default: run all tests)')
argparser.add_argument('-g', '--groups', action='store_true',
                       help='List all groups')
argparser.add_argument('-H', '--html-report', action='store_true',
                       help='Generate an HTML report')
argparser.add_argument('-C', '--print-config', action='store_true',
                       help='Show the detected configuration')
argparser.add_argument('-n', '--dry-run', action='store_true',
                       help='Do not run any tests')


def run(all_tests, all_groups, configure, args):
    """ The main entry point
    all_tests: A tree of all the tests
    all_groups: A dict of named groups
    configure: a function that takes a list of requirements and returns a configuration
    args: arguments parsed using argparser
    """
    if args.groups and not args.list:
        list_groups_mode(all_groups, args.filter, args.verbose)
        return
    filter = TestFilter.parse(args.filter, all_groups)
    if args.load or args.tree or args.examine:
        old_tests_mode(all_tests, args.load, filter, args.verbose, args.list, args.only_failed, args.tree, args.examine, args.html_report)
        return
    tests = all_tests.filter(filter)
    reqs = tests.requirements()
    conf = configure(reqs)
    if args.print_config:
        for k in conf:
            print k, '=', conf[k]
    tests = tests.configure(conf)
    filter.check_use()
    if args.list:
        list_tests_mode(tests, args.verbose, args.groups and all_groups)
        return
    if not args.dry_run:
        testrunner = TestRunner(
            tests, conf,
            tasks=args.jobs,
            timeout=args.timeout,
            output_dir=args.output_dir,
            run_dir=args.run_dir,
            verbose=args.verbose,
            repeat=args.repeat,
            kontinue=args.kontinue,
            abort_fast=args.abort_fast)
        testrunner.run()
        if args.html_report:
            test_report.gen_report(testrunner.dir, load_test_results_as_tests(testrunner.dir))
        if testrunner.failed():
            return 'FAILED'

# This mode just lists the tests
def list_tests_mode(tests, verbose, all_groups):
    if all_groups:
        groups = { name: TestFilter.parse(patterns, all_groups) for name, patterns in all_groups.items() }
    else:
        groups = False
    for name, test in tests:
        if groups:
            group_list = ', '.join(group for group in groups if groups[group].at(name.split('.')).match())
            if group_list:
                group_list = ' (' + group_list + ')'
        else:
            group_list = ''
        if verbose:
            print name + group_list + ':'
            for line in str(test).split('\n'):
                print "  " + line
        else:
            print name + group_list

# This mode lists the groups
def list_groups_mode(groups, filters, verbose):
    if filters:
        raise Exception('Cannot combine --groups with positional arguments')
    for name, patterns in groups.items():
        if not verbose:
            print name
        else:
            print name + ':'
            for pattern in patterns:
                print ' ', pattern

# This mode loads previously run tests instead of running any tests
def old_tests_mode(all_tests, load, filter, verbose, list_tests, only_failed, tree, examine, html_report):
    if isinstance(load, "".__class__):
        load_path = load
    else:
        all_dirs = [join(default_test_results_dir, d) for d in os.listdir(default_test_results_dir)]
        load_path = max([d for d in all_dirs if os.path.isdir(d)], key=getmtime)
        print "Loading tests from", load_path
    tests = load_test_results_as_tests(load_path).filter(filter)
    filter.check_use()
    if only_failed:
        tests = tests.filter(PredicateFilter(lambda test: not test.passed()))
    if html_report:
        test_report.gen_report(load_path, tests)
        return
    if list_tests:
        list_tests_mode(tests, verbose, False)
        return
    view = TextView()
    for name, test in tests:
        if not test.passed():
            status = 'FAILED'
        elif test.killed():
            status = 'KILLED'
        else:
            status = 'SUCCESS'
        if verbose:
            test.dump_log()
        view.tell(status, name)
        if tree:
            for name in test.list_files():
                if tree:
                    print "  " + name
        if examine:
            for glob in examine:
                for name in test.list_files(glob):
                    print
                    print '===', name, '==='
                    test.dump_file(name)

def redirect_fd_to_file(fd, file, tee=False):
    if not tee:
        f = open(file, 'w')
    else:
        tee = subprocess.Popen(["tee", file], stdin=subprocess.PIPE)
        f = tee.stdin
    os.dup2(f.fileno(), fd)

# The main logic for running the tests
class TestRunner(object):
    SUCCESS   = 'SUCCESS'
    FAILED    = 'FAILED'
    TIMED_OUT = 'TIMED_OUT'
    STARTED   = 'STARTED'
    KILLED    = 'KILLED'

    def __init__(self, tests, conf, tasks=1, timeout=600, output_dir=None, verbose=False, repeat=1, kontinue=False, abort_fast = False, run_dir=None):
        self.tests = tests
        self.semaphore = multiprocessing.Semaphore(tasks)
        self.processes = []
        self.timeout = timeout
        self.conf = conf
        self.verbose = verbose
        self.repeat = repeat
        self.kontinue = kontinue
        self.failed_set = set()
        self.aborting = False
        self.abort_fast = abort_fast
        self.all_passed = False

        timestamp = time.strftime('%Y-%m-%dT%H:%M:%S.')

        if output_dir:
            self.dir = output_dir
            try:
                os.mkdir(output_dir)
            except OSError as e:
                print >> sys.stderr, "Could not create output directory (" + output_dir + "):", e
                sys.exit(1)
        else:
            tr_dir = default_test_results_dir
            try:
                os.makedirs(tr_dir)
            except OSError:
                pass
            self.dir = tempfile.mkdtemp('', timestamp, tr_dir)

        if run_dir:
            self.run_dir = tempfile.mkdtemp('', timestamp, run_dir)
        else:
            self.run_dir = None


        self.running = Locked({})
        if sys.stdout.isatty() and not verbose:
            self.view = TermView(total = len(self.tests) * self.repeat)
        else:
            self.view = TextView()

    def run(self):
        tests_count = len(self.tests)
        tests_launched = set()
        tests_killed = set()
        try:
            print "Running %d tests (output_dir: %s)" % (tests_count, self.dir)

            for i in range(0, self.repeat):
                if len(self.failed_set) == tests_count:
                    break
                for name, test in self.tests:
                    if self.aborting:
                        break
                    self.semaphore.acquire()
                    if self.aborting:
                        self.semaphore.release()
                        break
                    if self.kontinue or name not in self.failed_set:
                        id = (name, i)
                        subdir = name if self.repeat == 1 else name + '.' + str(i+1)
                        dir = join(self.dir, subdir)
                        run_dir = join(self.run_dir, subdir) if self.run_dir else None
                        process = TestProcess(self, id, test, dir, run_dir)
                        with self.running as running:
                            running[id] = process
                        tests_launched.add(name)
                        process.start()
                    else:
                        self.semaphore.release()

            self.wait_for_running_tests()

        except KeyboardInterrupt:
            self.aborting = True
        except:
            self.aborting = True
            (exc_type, exc_value, exc_trace) = sys.exc_info()
            print
            print '\n'.join(traceback.format_exception(exc_type, exc_value, exc_trace))
            print >>sys.stderr, "\nWaiting for tests to finish..."
            self.wait_for_running_tests()
        running = self.running.copy()
        if running:
            print "\nKilling remaining tasks..."
            for id, process in running.iteritems():
                tests_killed.add(id)
                process.terminate(gracefull_kill=True)
            for id, process in running.iteritems():
                process.join()

        self.view.close()
        if len(tests_launched) != tests_count or tests_killed:
            if len(self.failed_set):
                print "%d tests failed" % (len(self.failed_set),)
            if tests_killed:
                print "%d tests killed" % (len(tests_killed),)
            print "%d tests skipped" % (tests_count - len(tests_launched),)
        elif len(self.failed_set):
            print "%d of %d tests failed" % (len(self.failed_set), tests_count)
        else:
            self.all_passed = True
            print "All tests passed successfully"
        print "Saved test results to %s" % (self.dir,)

    def wait_for_running_tests(self):
        # loop through the remaining TestProcesses and wait for them to finish
        while True:
            with self.running as running:
                if not running:
                    break
                id, process = running.iteritems().next()
            process.join()
            with self.running as running:
                try:
                    del(running[id])
                except KeyError:
                    pass
                else:
                    process.write_fail_message("Test failed to report success or"
                                               " failure status")
                    self.tell(self.FAILED, id)

    def tell(self, status, id, testprocess):
        name = id[0]
        args = {}
        if status == 'FAILED':
            if not self.aborting and not self.verbose:
                args = dict(error = testprocess.tail_error())
            if self.abort_fast:
                self.aborting = True
        if status != 'STARTED':
            with self.running as running:
                del(running[id])
            if status not in ['SUCCESS', 'KILLED']:
                self.view.tell('CANCEL', self.repeat - id[1] - 1)
                self.failed_set.add(name)
            self.semaphore.release()
        self.view.tell(status, name, **args)

    def count_running(self):
        with self.running as running:
            return len(running)

    def failed(self):
        return not self.all_passed

# For printing the status of TestRunner to stdout
class TextView(object):
    def __init__(self):
        self.use_color = sys.stdout.isatty()
        if self.use_color:
            curses.setupterm()
            setf = curses.tigetstr('setaf')
            bold = curses.tigetstr('bold')
            self.red = curses.tparm(setf, 1) + bold
            self.green = curses.tparm(setf, 2) + bold
            self.yellow = curses.tparm(setf, 3) + bold
            self.nocolor = curses.tigetstr('sgr0')
        else:
            self.red = ''
            self.green = ''
            self.yellow = ''
            self.nocolor = ''

    def tell(self, event, name, **args):
        if event not in ['STARTED', 'CANCEL']:
            print self.format_event(event, name, **args)

    def format_event(self, str, name, error=None):
        if str == 'LOG':
            return name
        short = dict(
            FAILED    = (self.red    , "FAIL"),
            SUCCESS   = (self.green  , "OK  "),
            TIMED_OUT = (self.red    , "TIME"),
            KILLED    = (self.yellow , "KILL")
        )[str]
        buf = ''
        if self.use_color:
            buf += short[0] + short[1] + " " + name + self.nocolor
        else:
            buf += short[1] + " " + name
        if error:
            buf += '\n' + error
        return buf

    def close(self):
        pass

# For printing the status to a terminal
class TermView(TextView):

    def __init__(self, total):
        TextView.__init__(self)
        self.running_list = []
        self.buffer = ''
        self.passed = 0
        self.failed = 0
        self.total = total
        self.start_time = time.time()
        self.printingQueue = Queue.Queue()
        self.columns = curses.tigetnum('cols')
        self.clear_line = curses.tparm(curses.tigetstr('cub'), self.columns) + curses.tigetstr('dl1')
        signal.signal(signal.SIGWINCH, lambda *args: self.tell('SIGWINCH', args))
        self.thread = threading.Thread(target=self.run, name='TermView')
        self.thread.daemon = True
        self.thread.start()

    def tell(self, *args, **kwargs):
        self.printingQueue.put((args, kwargs))

    def close(self):
        self.printingQueue.put(('EXIT',None))
        self.thread.join()

    def run(self):
        while True:
            try:
                args, kwargs = self.printingQueue.get(timeout=1)
            except Queue.Empty:
                self.update_status()
                self.flush()
            else:
                if args == 'EXIT':
                    break
                self.thread_tell(*args, **kwargs)

    def thread_tell(self, event, name, **kwargs):
        if event == 'SIGWINCH':
            self.columns = struct.unpack('hh', fcntl.ioctl(1, termios.TIOCGWINSZ, '1234'))[1]
            self.update_status()
        elif event == 'CANCEL':
            self.total -= name
        elif event == 'STARTED':
            self.running_list += [name]
            self.update_status()
        else:
            if event == 'SUCCESS':
                self.passed += 1
            else:
                self.failed += 1
            self.running_list.remove(name)
            self.show(self.format_event(event, name, **kwargs))
        self.flush()

    def update_status(self):
        self.clear_status()
        self.show_status()

    def clear_status(self):
        self.buffer += self.clear_line

    def show_status(self):
        if self.running_list:
            running = len(self.running_list)
            passed = self.passed
            failed = self.failed
            remaining = self.total - passed - failed - running
            duration = self.format_duration(time.time() - self.start_time)
            def format(names):
                return '[%s/%s/%d/%d %s %s]' % (passed, failed, running, remaining, duration, names)
            names = ''
            for i in range(running + 1):
                next = self.format_running(i)
                if len(format(next)) > self.columns - 1:
                    break
                names = next
            if self.use_color:
                if passed:
                    passed = self.green + str(passed) + self.nocolor
                if failed:
                    failed = self.red + str(failed) + self.nocolor
            self.buffer += format(names)

    def format_duration(self, elapsed):
        elapsed = math.floor(elapsed)
        seconds = elapsed % 60
        elapsed = math.floor(elapsed / 60)
        minutes = elapsed % 60
        hours = math.floor(elapsed / 60)
        ret = "%ds" % (seconds,)
        if minutes or hours:
            ret = "%dm%s" % (minutes, ret)
        if hours:
            ret = "%dh%s" % (hours, ret)
        return ret

    def format_running(self, max):
        ret = ''
        for i in range(max):
            if i > 0:
                ret += ', '
            ret += self.running_list[i]
        if len(self.running_list) > max:
            ret += ", ..."
        return ret

    def show(self, line):
        self.clear_status()
        self.buffer += line + "\n"
        self.show_status()

    def flush(self):
        sys.stdout.write(self.buffer)
        self.buffer = ''
        sys.stdout.flush()

# Lock access to an object with a lock
class Locked(object):
    def __init__(self, value=None):
        self.value = value
        self.lock = threading.Lock()

    def __enter__(self):
        self.lock.acquire()
        return self.value

    def __exit__(self, e, x, c):
        self.lock.release()

    def copy(self):
        with self as value:
            return value.copy()

# Run a single test in a separate process
class TestProcess(object):
    def __init__(self, runner, id, test, dir, run_dir):
        self.runner = runner
        self.id = id
        self.name = id[0]
        self.test = test
        self.timeout = test.timeout() or runner.timeout
        self.supervisor = None
        self.process = None
        self.dir = abspath(dir)
        self.run_dir = abspath(run_dir) if run_dir else None
        self.gracefull_kill = False
        self.terminate_thread = None

    def start(self):
        try:
            self.runner.tell(TestRunner.STARTED, self.id, self)
            os.mkdir(self.dir)
            if self.run_dir:
                os.mkdir(self.run_dir)
            with open(join(self.dir, "description"), 'w') as file:
                file.write(str(self.test))

            self.supervisor = threading.Thread(target=self.supervise,
                                               name="supervisor:"+self.name)
            self.supervisor.daemon = True
            self.supervisor.start()
        except Exception:
            raise

    def run(self, write_pipe):
        def recordSignal(signum, frame):
            print('Ignored signal SIGINT')
        signal.signal(signal.SIGINT, recordSignal) # avoiding a problem where signal.SIG_IGN would cause the test to never stop
        sys.stdin.close()
        redirect_fd_to_file(1, join(self.dir, "stdout"), tee=self.runner.verbose)
        redirect_fd_to_file(2, join(self.dir, "stderr"), tee=self.runner.verbose)
        os.chdir(self.run_dir or self.dir)
        os.setpgrp()
        with Timeout(self.timeout):
            try:
                self.test.run()
            except TimeoutException:
                write_pipe.send(TestRunner.TIMED_OUT)
            except:
                sys.stdout.write(traceback.format_exc() + '\n')
                sys.stderr.write(str(sys.exc_info()[1]) + '\n')
                write_pipe.send(TestRunner.FAILED)
            else:
                write_pipe.send(TestRunner.SUCCESS)
            finally:
                if self.run_dir:
                    for file in os.listdir(self.run_dir):
                        shutil.move(join(self.run_dir, file), join(self.dir, file))
                    os.rmdir(self.run_dir)

    def write_fail_message(self, message):
        with open(join(self.dir, "stderr"), 'a') as file:
            file.write(message)
        with open(join(self.dir, "fail_message"), 'a') as file:
            file.write(message)

    def tail_error(self):
        with open(join(self.dir, "stderr")) as f:
            lines = f.read().split('\n')[-10:]
        if len(lines) < 10:
            with open(join(self.dir, "stdout")) as f:
                lines = f.read().split('\n')[-len(lines):] + lines
        return '\n'.join(lines)

    def supervise(self):
        read_pipe, write_pipe = multiprocessing.Pipe(False)
        self.process = multiprocessing.Process(target=self.run, args=[write_pipe],
                                               name="subprocess:"+self.name)
        self.process.start()
        self.process.join(self.timeout + 5)
        if self.terminate_thread:
            self.terminate_thread.join()
        if self.gracefull_kill:
            with open(join(self.dir, "killed"), "a") as file:
                file.write("Test killed")
            self.runner.tell(TestRunner.KILLED, self.id, self)
        elif self.process.is_alive():
            self.terminate()
            self.terminate_thread.join()
            self.write_fail_message("Test failed to exit after timeout of %d seconds"
                                        % (self.timeout,))
            self.runner.tell(TestRunner.FAILED, self.id, self)
        elif self.process.exitcode:
            self.write_fail_message("Test exited abnormally with error code %d"
                                        % (self.process.exitcode,))
            self.runner.tell(TestRunner.FAILED, self.id, self)
        else:
            try:
                write_pipe.close()
                status = read_pipe.recv()
            except EOFError:
                self.write_fail_message("Test did not fail, but"
                                        " failed to report its success")
                status = TestRunner.FAILED
            else:
                if status != TestRunner.SUCCESS:
                    with open(join(self.dir, "fail_message"), 'a') as file:
                        file.write('Failed')
            self.runner.tell(status, self.id, self)

    def join(self):
        while self.supervisor.is_alive():
            self.supervisor.join(1)

    def terminate_thorough(self):
        if not self.process:
            return
        pid = self.process.pid
        self.process.terminate()
        self.process.join(5)
        for sig in [signal.SIGTERM, signal.SIGABRT, signal.SIGKILL]:
            try:
                os.killpg(pid, sig)
            except OSError:
                break
            time.sleep(2)

    def terminate(self, gracefull_kill=False):
        if gracefull_kill:
            self.gracefull_kill = True
        if self.terminate_thread:
            return
        self.terminate_thread = threading.Thread(target=self.terminate_thorough,
                                  name='terminate:'+self.name)
        self.terminate_thread.start()

    def pid(self):
        return self.process.pid

class TimeoutException(Exception):
    pass

# A scoped timeout for single-threaded processes
class Timeout(object):
    def __init__(self, seconds):
        self.timeout = seconds

    def __enter__(self):
        signal.signal(signal.SIGALRM, self.alarm)
        signal.alarm(self.timeout)

    def __exit__(self, type, exception, trace):
        signal.alarm(0)

    @staticmethod
    def alarm(*ignored):
        raise TimeoutException()

# A FilterSource describes what group a filter comes from
class FilterSource(object):
    def __init__(self, group=None, weak=True):
        self.group = group
        self.weak = weak

    def copy(self, weak=True):
        return FilterSource(self.group, weak=weak)

    def combined(self, other):
        if self.weak:
            return other
        return self

    def show(self, default='user input'):
        if self.group:
            return 'group ' + self.group
        else:
            return default

# A test filter that discriminates tests by name
class TestFilter(object):
    INCLUDE = 'INCLUDE'
    EXCLUDE = 'EXCLUDE'

    def __init__(self, default=EXCLUDE, group=None):
        self.default = default
        self.tree = {}
        self.was_matched = False
        self.group = group

    @classmethod
    def parse(self, args, groups, group=None):
        if not args:
            return TestFilter(self.INCLUDE, group=FilterSource(group))
        if args[0][0] == '!':
            filter = TestFilter(self.INCLUDE, group=FilterSource(group))
        else:
            filter = TestFilter(self.EXCLUDE, group=FilterSource(group))
        for arg in args:
            if arg[0] == '!':
                arg = arg[1:]
                type = self.EXCLUDE
            else:
                type = self.INCLUDE
            if groups.has_key(arg):
                group = self.parse(groups[arg], groups, group=arg)
                filter.combine(type, group)
            else:
                path = arg.split('.')
                if path[-1] == '*':
                    path = path[:-1]
                it = filter.at(path).reset(type)
        return filter

    def combine(self, type, other):
        for name in set(self.tree.keys() + other.tree.keys()):
            self.zoom(name, create=True).combine(type, other.zoom(name))
        if other.default == self.INCLUDE:
            self.default = type
        self.group = self.group.combined(other.group)

    def at(self, path):
        if not path:
            return self
        else:
            return self.zoom(path[0], create=True).at(path[1:])

    def reset(self, type=EXCLUDE, weak=False):
        self.group.weak = self.group.weak and weak
        self.default = type
        self.tree = {}

    def match(self, test=None):
        self.was_matched = True
        return self.default == self.INCLUDE

    def zoom(self, name, create=False):
        try:
            return self.tree[name]
        except KeyError:
            subfilter = TestFilter(self.default, group=self.group.copy())
            if create:
                self.tree[name] = subfilter
            return subfilter

    def check_use(self, path=[]):
        if not self.was_matched:
            message = 'No such test ' + '.'.join(path) + ' (from ' + self.group.show() + ')'
            if self.group.group:
                print 'Warning: ' + message
            else:
                raise Exception(message)
        for name, filter in self.tree.iteritems():
            filter.check_use(path + [name])

    def __repr__(self):
        return ("TestFilter(" + self.default + ", " + repr(self.was_matched) +
                ", " + repr(self.tree) + ")")

    def all_same(self):
        self.was_matched = True
        return not self.tree

# A test filter that discriminates using a predicate
class PredicateFilter(object):
    def __init__(self, predicate):
        self.predicate = predicate

    def all_same(self):
        return False

    def match(self, test=None):
        if test:
            return self.predicate(test)
        return True

    def zoom(self, name):
        return self

# A base class for tests or groups of tests
class Test(object):
    def __init__(self, timeout=None):
        self._timeout = timeout

    def run(self):
        raise Exception("run is not defined for the %s class" %
                        (type(self).__name__,))

    def filter(self, filter):
        if filter.match(self):
            return self
        else:
            return None

    def __iter__(self):
        yield (None, self)

    def timeout(self):
        return self._timeout

    def requirements(self):
        return []

    def configure(self, conf):
        return self

# A simple test just runs a python function
class SimpleTest(Test):
    def __init__(self, run, **kwargs):
        Test.__init__(self, **kwargs)
        self._run = run

    def run(self):
        self._run()

# A tree of named tests
class TestTree(Test):
    def __init__(self, tests={}):
        self.tests = dict(tests)

    def filter(self, filter):
        if filter.all_same():
            if filter.match():
                return self
            else:
                return TestTree()
        trimmed = TestTree()
        for name, test in self.tests.iteritems():
            subfilter = filter.zoom(name)
            trimmed[name] = test.filter(subfilter)
        return trimmed

    def run(self):
        for test in self.tests.values():
            test.run()

    def __getitem__(self, name):
        return self.tests[name]

    def __setitem__(self, name, test):
        if not test or (isinstance(test, TestTree) and not test.tests):
            try:
                del(self.tests[name])
            except KeyError:
                pass
        else:
            self.tests[name] = test

    def add(self, name, test):
        if name in self.tests:
            raise Exception('Test already exists: %s' % (name,))
        self.tests[name] = test

    def __iter__(self):
        for name in sorted(self.tests.keys()):
            for subname, test in self.tests[name]:
                if subname:
                    yield (name + '.' + subname, test)
                else:
                    yield name, test

    def requirements(self):
        for test in self.tests.values():
            for req in test.requirements():
                yield req

    def configure(self, conf):
        return TestTree((
            (name, test.configure(conf))
            for name, test
            in self.tests.iteritems()
        ))

    def __len__(self):
        count = 0
        for __, ___ in self:
            count += 1
        return count

    def has_test(self, name):
        return self.tests.has_key(name)

# Used with `--load' to load old test results
def load_test_results_as_tests(path):
    tests = TestTree()
    for dir in os.listdir(path):
        full_dir = join(path, dir)
        if not os.path.isdir(full_dir):
            continue
        names = list(reversed(dir.split('.')))
        parent = tests
        while parent.has_test(names[-1]):
            parent = parent[names[-1]]
            names.pop()
        test = OldTest(full_dir)
        for name in names[:-1]:
            test = TestTree({name: test})
        parent[names[-1]] = test
    return tests

# A test that has already run
class OldTest(Test):
    def __init__(self, dir, **kwargs):
        Test.__init__(self, **kwargs)
        self.dir = dir

    def __str__(self):
        return self.read_file('description', 'unknown test')

    def read_file(self, name, default=None):
        try:
            with open(join(self.dir, name)) as file:
                return file.read()
        except Exception as e:
            # TODO: catch the right exception here
            return default

    def passed(self):
        return not os.path.exists(join(self.dir, "fail_message"))

    def killed(self):
        return os.path.exists(join(self.dir, "killed"))

    def dump_file(self, name):
        with file(join(self.dir, name)) as f:
            for line in f:
                print line,

    def dump_log(self):
        self.dump_file("stdout")
        self.dump_file("stderr")
        print

    def list_files(self, glob=None, text_only=True):
        for root, dirs, files in os.walk(self.dir):
            for file in files:
                name = relpath(join(root, file), self.dir)
                if not glob or fnmatch.fnmatch(name, glob):
                    if not text_only or name == glob or guess_is_text_file(join(root, file)):
                        yield name

def group_from_file(path):
    patterns = []
    with file(path) as f:
        for line in f:
            line = line.split('#')[0]
            if line:
                patterns += line.split()
    return patterns
