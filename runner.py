#!/usr/bin/env python

import atexit
import os
import signal
import logging
import sys
import re
import socket
import time
import traceback
import ConfigParser
import imp
import json
import urllib2
import random
import base64
from logging.handlers import RotatingFileHandler
from optparse import OptionParser

# global variables.
COLLECTORS = {}
GENERATION = 0
DEFAULT_LOG = '/var/log/tcollector.log'
LOG = logging.getLogger('tcollector')
DEFAULT_PORT = 4242
ALLOWED_INACTIVITY_TIME = 600  # seconds

# config constants
SECTION_BASE = 'base'
CONFIG_ENABLED = 'enabled'
CONFIG_COLLECTOR_CLASS = 'collectorclass'
CONFIG_INTERVAL = 'interval'

# metric entry constant
METRIC_NAME = 'metric'
METRIC_TIMESTAMP = 'timestamp'
METRIC_VALUE = 'value'
METRIC_TAGS = 'tags'


def main(argv):
    try:
        options, args = parse_cmdline(argv)
    except:
        sys.stderr.write("Unexpected error: %s" % sys.exc_info()[0])
        return 1

    if options.daemonize:
        daemonize()

    setup_logging(options.logfile, options.max_bytes or None,
                  options.backup_count or None)

    if options.verbose:
        LOG.setLevel(logging.DEBUG)  # up our level

    if options.pidfile:
        write_pid(options.pidfile)

    # validate everything
    tags = {}
    for tag in options.tags:
        if re.match('^[-_.a-z0-9]+=\S+$', tag, re.IGNORECASE) is None:
            assert False, 'Tag string "%s" is invalid.' % tag
        k, v = tag.split('=', 1)
        if k in tags:
            assert False, 'Tag "%s" already declared.' % k
        tags[k] = v

    if 'host' not in tags and not options.stdin:
        tags['host'] = socket.gethostname()
        LOG.warning('Tag "host" not specified, defaulting to %s.', tags['host'])

    options.cdir = os.path.realpath(options.cdir)
    if not os.path.isdir(options.cdir):
        LOG.fatal('No such directory: %s', options.cdir)
        return 1

    setup_python_path(options.cdir)

    # gracefully handle death for normal termination paths and abnormal
    atexit.register(shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown_signal)

    # prepare list of (host, port) of TSDs given on CLI
    if not options.hosts:
        options.hosts = [(options.host, options.port)]
    else:
        def splithost(hostport):
            if ":" in hostport:
                # Check if we have an IPv6 address.
                if hostport[0] == "[" and "]:" in hostport:
                    host, port = hostport.split("]:")
                    host = host[1:]
                else:
                    host, port = hostport.split(":")
                return host, int(port)
            return hostport, DEFAULT_PORT

        options.hosts = [splithost(host_str) for host_str in options.hosts.split(",")]
        if options.host != "localhost" or options.port != DEFAULT_PORT:
            options.hosts.append((options.host, options.port))

    sender = Sender(options, tags)
    main_loop(options, sender, {}, {})


def main_loop(options, sender, configs, collectors):
    while True:
        start = time.time()

        changed_configs = reload_collector_confs(configs, options)
        load_collectors(options.cdir, changed_configs, collectors)
        data = []
        for name, collector in collectors.iteritems():
            try:
                data.extend(collector.collector_instance())
            except:
                LOG.error('failed to execute collector %s. skip. %s', name, traceback.format_exc())
        sender.send_data_via_http(data)

        end = time.time()
        sleepsec = 2 - (end - start) if 2 > (end - start) else 0
        time.sleep(sleepsec)


def load_collectors(coldir, configs, collectors):
    collector_dir = '%s/builtin' % coldir
    for config_filename, (path, conf, timestamp) in configs.iteritems():
        try:
            name = os.path.splitext(config_filename)[0]
            collector_path_name = '%s/%s.py' % (collector_dir, name)
            if conf.getboolean(SECTION_BASE, CONFIG_ENABLED):
                if os.path.isfile(collector_path_name) and os.access(collector_path_name, os.X_OK):
                    mtime = os.path.getmtime(collector_path_name)
                    if (name not in collectors) or (collectors[name].mtime < mtime):
                        collector_class_name = conf.get(SECTION_BASE, CONFIG_COLLECTOR_CLASS)
                        collector_class = load_collector_module(name, collector_dir, collector_class_name)
                        collector_instance = collector_class(conf)
                        interval = conf.getint(SECTION_BASE, CONFIG_INTERVAL)
                        collectors[name] = Collector(collector_instance, interval)
                        LOG.info('loaded collector %s from %s', name, collector_path_name)
                else:
                    LOG.warn('failed to access collector file: %s', collector_path_name)
            elif name in collectors:
                LOG.info("%s is disabled", collector_path_name)
                del collectors[name]
        except:
            LOG.error('failed to load collector %s, skipped. %s',
                      collector_path_name if collector_path_name else config_filename, traceback.format_exc())


# caller to handle exception
def load_collector_module(module_name, module_path, collector_class_name=None):
    (file_obj, filename, description) = imp.find_module(module_name, [module_path])
    mod = imp.load_module(module_name, file_obj, filename, description)
    if collector_class_name is None:
        collector_class_name = module_name.title().replace('_', '').replace('-', '')
    return getattr(mod, collector_class_name)


def setup_logging(logfile=DEFAULT_LOG, max_bytes=None, backup_count=None):
    """Sets up logging and associated handlers."""

    LOG.setLevel(logging.INFO)
    if backup_count is not None and max_bytes is not None:
        assert backup_count > 0
        assert max_bytes > 0
        ch = RotatingFileHandler(logfile, 'a', max_bytes, backup_count)
    else:  # Setup stream handler.
        ch = logging.StreamHandler(sys.stdout)

    ch.setFormatter(logging.Formatter('%(asctime)s %(name)s[%(process)d] '
                                      '%(levelname)s: %(message)s'))
    LOG.addHandler(ch)


def parse_cmdline(argv):

    try:
        from collectors.etc import config
        defaults = config.get_defaults()
    except ImportError:
        sys.stderr.write("ImportError: Could not load defaults from configuration. Using hardcoded values")
        default_cdir = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), 'collectors')
        defaults = {
            'verbose': False,
            'no_tcollector_stats': False,
            'evictinterval': 6000,
            'dedupinterval': 300,
            'allowed_inactivity_time': 600,
            'dryrun': False,
            'maxtags': 8,
            'max_bytes': 64 * 1024 * 1024,
            'http_password': False,
            'reconnectinterval': 0,
            'http_username': False,
            'port': 4242,
            'pidfile': '/var/run/tcollector.pid',
            'http': False,
            'tags': [],
            'remove_inactive_collectors': False,
            'host': 'localhost',
            'backup_count': 1,
            'logfile': '/var/log/tcollector.log',
            'cdir': default_cdir,
            'ssl': False,
            'stdin': False,
            'daemonize': False,
            'hosts': False
        }
    except:
        sys.stderr.write("Unexpected error: %s" % sys.exc_info()[0])
        raise

    # get arguments
    parser = OptionParser(description='Manages collectors which gather '
                                      'data and report back.')
    parser.add_option('-c', '--collector-dir', dest='cdir', metavar='DIR',
                      default=defaults['cdir'],
                      help='Directory where the collectors are located.')
    parser.add_option('-d', '--dry-run', dest='dryrun', action='store_true',
                      default=defaults['dryrun'],
                      help='Don\'t actually send anything to the TSD, '
                           'just print the datapoints.')
    parser.add_option('-D', '--daemonize', dest='daemonize', action='store_true',
                      default=defaults['daemonize'],
                      help='Run as a background daemon.')
    parser.add_option('-H', '--host', dest='host',
                      metavar='HOST',
                      default=defaults['host'],
                      help='Hostname to use to connect to the TSD.')
    parser.add_option('-L', '--hosts-list', dest='hosts',
                      metavar='HOSTS',
                      default=defaults['hosts'],
                      help='List of host:port to connect to tsd\'s (comma separated).')
    parser.add_option('--no-tcollector-stats', dest='no_tcollector_stats',
                      action='store_true',
                      default=defaults['no_tcollector_stats'],
                      help='Prevent tcollector from reporting its own stats to TSD')
    parser.add_option('-s', '--stdin', dest='stdin', action='store_true',
                      default=defaults['stdin'],
                      help='Run once, read and dedup data points from stdin.')
    parser.add_option('-p', '--port', dest='port', type='int',
                      default=defaults['port'], metavar='PORT',
                      help='Port to connect to the TSD instance on. '
                           'default=%default')
    parser.add_option('-v', dest='verbose', action='store_true',
                      default=defaults['verbose'],
                      help='Verbose mode (log debug messages).')
    parser.add_option('-t', '--tag', dest='tags', action='append',
                      default=defaults['tags'], metavar='TAG',
                      help='Tags to append to all timeseries we send, '
                           'e.g.: -t TAG=VALUE -t TAG2=VALUE')
    parser.add_option('-P', '--pidfile', dest='pidfile',
                      default=defaults['pidfile'],
                      metavar='FILE', help='Write our pidfile')
    parser.add_option('--dedup-interval', dest='dedupinterval', type='int',
                      default=defaults['dedupinterval'], metavar='DEDUPINTERVAL',
                      help='Number of seconds in which successive duplicate '
                           'datapoints are suppressed before sending to the TSD. '
                           'Use zero to disable. '
                           'default=%default')
    parser.add_option('--evict-interval', dest='evictinterval', type='int',
                      default=defaults['evictinterval'], metavar='EVICTINTERVAL',
                      help='Number of seconds after which to remove cached '
                           'values of old data points to save memory. '
                           'default=%default')
    parser.add_option('--allowed-inactivity-time', dest='allowed_inactivity_time', type='int',
                      default=ALLOWED_INACTIVITY_TIME, metavar='ALLOWEDINACTIVITYTIME',
                      help='How long to wait for datapoints before assuming '
                           'a collector is dead and restart it. '
                           'default=%default')
    parser.add_option('--remove-inactive-collectors', dest='remove_inactive_collectors', action='store_true',
                      default=defaults['remove_inactive_collectors'], help='Remove collectors not sending data '
                                                                           'in the max allowed inactivity interval')
    parser.add_option('--max-bytes', dest='max_bytes', type='int',
                      default=defaults['max_bytes'],
                      help='Maximum bytes per a logfile.')
    parser.add_option('--backup-count', dest='backup_count', type='int',
                      default=defaults['backup_count'], help='Maximum number of logfiles to backup.')
    parser.add_option('--logfile', dest='logfile', type='str',
                      default=DEFAULT_LOG,
                      help='Filename where logs are written to.')
    parser.add_option('--reconnect-interval', dest='reconnectinterval', type='int',
                      default=defaults['reconnectinterval'], metavar='RECONNECTINTERVAL',
                      help='Number of seconds after which the connection to'
                           'the TSD hostname reconnects itself. This is useful'
                           'when the hostname is a multiple A record (RRDNS).')
    parser.add_option('--max-tags', dest='maxtags', type=int, default=defaults['maxtags'],
                      help='The maximum number of tags to send to our TSD Instances')
    parser.add_option('--http', dest='http', action='store_true', default=defaults['http'],
                      help='Send the data via the http interface')
    parser.add_option('--http-username', dest='http_username', default=defaults['http_username'],
                      help='Username to use for HTTP Basic Auth when sending the data via HTTP')
    parser.add_option('--http-password', dest='http_password', default=defaults['http_password'],
                      help='Password to use for HTTP Basic Auth when sending the data via HTTP')
    parser.add_option('--ssl', dest='ssl', action='store_true', default=defaults['ssl'],
                      help='Enable SSL - used in conjunction with http')
    (options, args) = parser.parse_args(args=argv[1:])
    if options.dedupinterval < 0:
        parser.error('--dedup-interval must be at least 0 seconds')
    if options.evictinterval <= options.dedupinterval:
        parser.error('--evict-interval must be strictly greater than '
                     '--dedup-interval')
    if options.reconnectinterval < 0:
        parser.error('--reconnect-interval must be at least 0 seconds')
    # We cannot write to stdout when we're a daemon.
    if (options.daemonize or options.max_bytes) and not options.backup_count:
        options.backup_count = 1
    return (options, args)


def daemonize():
    """Performs the necessary dance to become a background daemon."""
    if os.fork():
        os._exit(0)
    os.chdir("/")
    os.umask(022)
    os.setsid()
    os.umask(0)
    if os.fork():
        os._exit(0)
    stdin = open(os.devnull)
    stdout = open(os.devnull, 'w')
    os.dup2(stdin.fileno(), 0)
    os.dup2(stdout.fileno(), 1)
    os.dup2(stdout.fileno(), 2)
    stdin.close()
    stdout.close()
    os.umask(022)
    for fd in xrange(3, 1024):
        try:
            os.close(fd)
        except OSError:  # This FD wasn't opened...
            pass  # ... ignore the exception.


# To be removed
def list_config_modules(etcdir):
    """Returns an iterator that yields the name of all the config modules."""
    if not os.path.isdir(etcdir):
        return iter(())  # Empty iterator.
    return (name for name in os.listdir(etcdir)
            if (name.endswith('.py') and os.path.isfile(os.path.join(etcdir, name))))


def load_etc_dir(options, tags):
    """Loads any Python module from tcollector's own 'etc' directory.

    Returns: A dict of path -> (module, timestamp).
    """

    etcdir = os.path.join(options.cdir, 'etc')
    sys.path.append(etcdir)  # So we can import modules from the etc dir.
    modules = {}  # path -> (module, timestamp)
    for name in list_config_modules(etcdir):
        path = os.path.join(etcdir, name)
        module = load_config_module(name, options, tags)
        modules[path] = (module, os.path.getmtime(path))
    return modules


def load_config_module(name, options, tags):
    """Imports the config module of the given name

    The 'name' argument can be a string, in which case the module will be
    loaded by name, or it can be a module object, in which case the module
    will get reloaded.

    If the module has an 'onload' function, calls it.
    Returns: the reference to the module loaded.
    """

    if isinstance(name, str):
        LOG.info('Loading %s', name)
        d = {}
        # Strip the trailing .py
        module = __import__(name[:-3], d, d)
    else:
        module = reload(name)
    onload = module.__dict__.get('onload')
    if callable(onload):
        try:
            onload(options, tags)
        except:
            LOG.fatal('Exception while loading %s', name)
            raise
    return module


def reload_changed_config_modules(modules, options, tags):

    etcdir = os.path.join(options.cdir, 'etc')
    current_modules = set(list_config_modules(etcdir))
    current_paths = set(os.path.join(etcdir, name)
                        for name in current_modules)
    changed = False

    # Reload any module that has changed.
    for path, (module, timestamp) in modules.iteritems():
        if path not in current_paths:  # Module was removed.
            continue
        mtime = os.path.getmtime(path)
        if mtime > timestamp:
            LOG.info('Reloading %s, file has changed', path)
            module = load_config_module(module, options, tags)
            modules[path] = (module, mtime)
            changed = True

    # Remove any module that has been removed.
    for path in set(modules).difference(current_paths):
        LOG.info('%s has been removed, tcollector should be restarted', path)
        del modules[path]
        changed = True

    # Check for any modules that may have been added.
    for name in current_modules:
        path = os.path.join(etcdir, name)
        if path not in modules:
            module = load_config_module(name, options, tags)
            modules[path] = (module, os.path.getmtime(path))
            changed = True

    return changed


# end: To be removed


def list_collector_confs(confdir):
    if not os.path.isdir(confdir):
        LOG.warn('collector conf directory %s is not a directory', confdir)
        return iter(())  # Empty iterator.
    return (name for name in os.listdir(confdir)
            if (name.endswith('.conf') and os.path.isfile(os.path.join(confdir, name))))


def reload_collector_confs(collector_confs, options):
    confdir = os.path.join(options.cdir, 'conf')
    current_collector_confs = set(list_collector_confs(confdir))
    current_paths = set(os.path.join(confdir, name)
                        for name in current_collector_confs)
    changed_collector_confs = {}

    # Reload any module that has changed.
    for filename, (path, conf, timestamp) in collector_confs.iteritems():
        if filename not in current_collector_confs:  # Module was removed.
            continue
        mtime = os.path.getmtime(path)
        if mtime > timestamp:
            LOG.info('reloading %s, file has changed', path)
            config = ConfigParser.SafeConfigParser(default_config())
            config.read(path)
            collector_confs[filename] = (path, config, mtime)
            changed_collector_confs[filename] = (path, config, mtime)

    # Remove any module that has been removed.
    for filename in set(collector_confs).difference(current_paths):
        LOG.info('%s has been removed, tcollector should be restarted', filename)
        del collector_confs[filename]

    # Check for any modules that may have been added.
    for name in current_collector_confs:
        path = os.path.join(confdir, name)
        if name not in collector_confs:
            LOG.info('adding conf %s', path)
            config = ConfigParser.SafeConfigParser(default_config())
            config.read(path)
            collector_confs[name] = (path, config, os.path.getmtime(path))
            changed_collector_confs[name] = (path, config, os.path.getmtime(path))
    return changed_collector_confs


def default_config():
    return {
        CONFIG_ENABLED: False,
        CONFIG_INTERVAL: '15',
        CONFIG_COLLECTOR_CLASS: None
    }


def write_pid(pidfile):
    """Write our pid to a pidfile."""
    f = open(pidfile, "w")
    try:
        f.write(str(os.getpid()))
    finally:
        f.close()


def setup_python_path(collector_dir):
    """Sets up PYTHONPATH so that collectors can easily import common code."""
    mydir = os.path.dirname(collector_dir)
    libdir = os.path.join(mydir, 'collectors', 'lib')
    if not os.path.isdir(libdir):
        return
    pythonpath = os.environ.get('PYTHONPATH', '')
    if pythonpath:
        pythonpath += ':'
    pythonpath += mydir
    os.environ['PYTHONPATH'] = pythonpath
    LOG.debug('Set PYTHONPATH to %r', pythonpath)


def shutdown():
    LOG.info('exiting')
    sys.exit(1)


# noinspection PyUnusedLocal
def shutdown_signal(signum, frame):
    LOG.warning("shutting down, got signal %d", signum)
    shutdown()


class Collector(object):
    def __init__(self, collector_instance, interval):
        self.collector_instance = collector_instance
        self.interval = interval


# noinspection PyDictCreation
class Sender(object):
    def __init__(self, options, tags):
        self.hosts = options.hosts
        self.http_username = options.http_username
        self.http_password = options.http_password
        self.ssl = options.ssl
        self.tags = tags
        self.maxtags = options.maxtags
        self.dryrun = options.dryrun
        self.current_tsd = -1
        self.blacklisted_hosts = set()
        random.shuffle(self.hosts)

    def send_data_via_http(self, data):
        metrics = []
        for line in data:
            # print " %s" % line
            parts = line.split(None, 3)
            # not all metrics have metric-specific tags
            if len(parts) == 4:
                (metric, timestamp, value, raw_tags) = parts
            else:
                (metric, timestamp, value) = parts
                raw_tags = ""
            # process the tags
            metric_tags = {}
            for tag in raw_tags.strip().split():
                (tag_key, tag_value) = tag.split("=", 1)
                metric_tags[tag_key] = tag_value
            metric_entry = {}
            metric_entry["metric"] = metric
            metric_entry["timestamp"] = long(timestamp)
            metric_entry["value"] = float(value)
            metric_entry["tags"] = dict(self.tags).copy()
            if len(metric_tags) + len(metric_entry["tags"]) > self.maxtags:
                metric_tags_orig = set(metric_tags)
                subset_metric_keys = frozenset(
                        metric_tags[:len(metric_tags[:self.maxtags - len(metric_entry["tags"])])])
                metric_tags = dict((k, v) for k, v in metric_tags.iteritems() if k in subset_metric_keys)
                LOG.error("Exceeding maximum permitted metric tags - removing %s for metric %s",
                          str(metric_tags_orig - set(metric_tags)), metric)
            metric_entry["tags"].update(metric_tags)
            metrics.append(metric_entry)

        if self.dryrun:
            print "Would have sent:\n%s" % json.dumps(metrics,
                                                      sort_keys=True,
                                                      indent=4)
            return

        if (self.current_tsd == -1) or (len(self.hosts) > 1):
            self.pick_connection()
        # print "Using server: %s:%s" % (self.host, self.port)
        # url = "http://%s:%s/api/put?details" % (self.host, self.port)
        # print "Url is %s" % url
        LOG.debug("Sending metrics to http://%s:%s/api/put?details",
                  self.host, self.port)
        if self.ssl:
            protocol = "https"
        else:
            protocol = "http"
        req = urllib2.Request("%s://%s:%s/api/put?details" % (
            protocol, self.host, self.port))
        if self.http_username and self.http_password:
            req.add_header("Authorization", "Basic %s"
                           % base64.b64encode("%s:%s" % (self.http_username, self.http_password)))
        req.add_header("Content-Type", "application/json")
        try:
            response = urllib2.urlopen(req, json.dumps(metrics))
            LOG.debug("Received response %s", response.getcode())
            # print "Got response code: %s" % response.getcode()
            # print "Content:"
            # for line in response:
            #     print line,
            #     print
        except urllib2.HTTPError, e:
            LOG.error("Got error %s", e)
            # for line in http_error:
            #   print line,

    def pick_connection(self):
        """Picks up a random host/port connection."""
        # Try to get the next host from the list, until we find a host that
        # isn't in the blacklist, or until we run out of hosts (i.e. they
        # are all blacklisted, which typically happens when we lost our
        # connectivity to the outside world).
        for self.current_tsd in xrange(self.current_tsd + 1, len(self.hosts)):
            hostport = self.hosts[self.current_tsd]
            if hostport not in self.blacklisted_hosts:
                break
        else:
            LOG.info('No more healthy hosts, retry with previously blacklisted')
            random.shuffle(self.hosts)
            self.blacklisted_hosts.clear()
            self.current_tsd = 0
            hostport = self.hosts[self.current_tsd]
        # noinspection PyAttributeOutsideInit
        self.host, self.port = hostport
        LOG.info('Selected connection: %s:%d', self.host, self.port)

    def blacklist_connection(self):
        """Marks the current TSD host we're trying to use as blacklisted.

           Blacklisted hosts will get another chance to be elected once there
           will be no more healthy hosts."""
        LOG.info('Blacklisting %s:%s for a while', self.host, self.port)
        self.blacklisted_hosts.add((self.host, self.port))


if __name__ == '__main__':
    sys.exit(main(sys.argv))
