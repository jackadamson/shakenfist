# Copyright 2020 Michael Still

import importlib
import json
import multiprocessing
from pbr.version import VersionInfo
import random
import re
import requests
import string
import sys
import time
import traceback

from oslo_concurrency import processutils

from shakenfist import db
from shakenfist import config
from shakenfist import logutil


class RecordedOperation():
    def __init__(self, operation, relatedobject):
        self.operation = operation
        self.object = relatedobject

    def __enter__(self):
        self.start_time = time.time()
        object_type, object_uuid = self.get_describing_tuple()
        if object_type and object_uuid:
            db.add_event(object_type, object_uuid,
                         self.operation, 'start', None, None)
        return self

    def __exit__(self, *args):
        duration = time.time() - self.start_time
        logutil.info([self.object], 'Finish %s, duration %.02f seconds'
                     % (self.operation, duration))

        object_type, object_uuid = self.get_describing_tuple()
        if object_type and object_uuid:
            db.add_event(object_type, object_uuid,
                         self.operation, 'finish', duration, None)

    def get_describing_tuple(self):
        if self.object:
            if isinstance(self.object, str):
                object_type = None
                object_uuid = self.object
            else:
                object_type, object_uuid = self.object.get_describing_tuple()
        else:
            object_type = None
            object_uuid = None

        return object_type, object_uuid


def is_network_node():
    """Test if this node is the network node"""
    return config.parsed.get('NODE_IP') == config.parsed.get('NETWORK_NODE_IP')


def check_for_interface(name, up=False):
    stdout, stderr = execute(None,
                             'ip link show %s' % name, check_exit_code=[0, 1])

    if stderr.rstrip('\n').endswith(' does not exist.'):
        return False

    if up:
        return bool(re.match(r'.*[<,]UP[,>].*', stdout))

    return True


def get_interface_addresses(namespace, name):
    in_namespace = ''
    if namespace:
        in_namespace = 'ip netns exec %s ' % namespace

    stdout, _ = execute(None,
                        '%(in_namespace)sip addr show %(name)s'
                        % {
                            'in_namespace': in_namespace,
                            'name': name
                        },
                        check_exit_code=[0, 1])
    if not stdout:
        return

    inet_re = re.compile(r' +inet (.*)/[0-9]+.*')
    for line in stdout.split('\n'):
        m = inet_re.match(line)
        if m:
            yield m.group(1)

    return


def nat_rules_for_ipblock(ipblock):
    out, _ = execute(None, 'iptables -t nat -L POSTROUTING -n -v')
    # Output looks like this:
    # Chain POSTROUTING (policy ACCEPT 199 packets, 18189 bytes)
    # pkts bytes target     prot opt in     out     source               destination
    #   23  1736 MASQUERADE  all  --  *      ens4    192.168.242.0/24     0.0.0.0/0

    for line in out.split('\n'):
        if line.find(str(ipblock)) != -1:
            return True

    return False


LIBVIRT = None


def get_libvirt():
    global LIBVIRT

    if not LIBVIRT:
        LIBVIRT = importlib.import_module('libvirt')

    return LIBVIRT


def extract_power_state(libvirt, domain):
    state, _ = domain.state()
    if state == libvirt.VIR_DOMAIN_SHUTOFF:
        return 'off'

    if state == libvirt.VIR_DOMAIN_CRASHED:
        return 'crashed'

    if state in [libvirt.VIR_DOMAIN_PAUSED,
                 libvirt.VIR_DOMAIN_PMSUSPENDED]:
        return 'paused'

    # Covers all "runnning states": BLOCKED, NOSTATE,
    # RUNNING, SHUTDOWN
    return 'on'


def get_api_token(base_url, namespace='system'):
    with db.get_lock('namespace', None, namespace):
        auth_url = base_url + '/auth'
        logutil.info(None, 'Fetching %s auth token from %s'
                     % (namespace, auth_url))
        ns = db.get_namespace(namespace)
        if 'service_key' in ns:
            key = ns['service_key']
        else:
            key = ''.join(random.choice(string.ascii_lowercase)
                          for i in range(50))
            ns['service_key'] = key
            db.persist_namespace(namespace, ns)

    r = requests.request('POST', auth_url,
                         data=json.dumps({
                             'namespace': namespace,
                             'key': key
                         }),
                         headers={'Content-Type': 'application/json',
                                  'User-Agent': get_user_agent()})
    if r.status_code != 200:
        raise Exception('Unauthorized')
    return 'Bearer %s' % r.json()['access_token']


CACHED_VERSION = None


def get_version():
    global CACHED_VERSION

    if not CACHED_VERSION:
        CACHED_VERSION = VersionInfo('shakenfist').version_string()
    return CACHED_VERSION


def get_user_agent():
    return 'Mozilla/5.0 (Ubuntu; Linux x86_64) Shaken Fist/%s' % get_version()


def discover_interfaces():
    mac_to_iface = {
        '00:00:00:00:00:00': 'broadcast'
    }
    iface_to_mac = {}
    vxid_to_mac = {}

    iface_name = None
    iface_name_re = re.compile('^[0-9]+: ([^:]+): <')

    link_ether = None
    link_ether_re = re.compile('^    link/ether (.*) brd .*')

    stdout, _ = execute(None, 'ip addr list')
    for line in stdout.split('\n'):
        line = line.rstrip()

        m = iface_name_re.match(line)
        if m:
            iface_name = m.group(1)
            continue

        m = link_ether_re.match(line)
        if m:
            link_ether = m.group(1)
            mac_to_iface[link_ether] = iface_name
            iface_to_mac[iface_name] = link_ether

            if iface_name.startswith('vxlan-'):
                vxid = int(iface_name.split('-')[1])
                vxid_to_mac[vxid] = link_ether

    return mac_to_iface, iface_to_mac, vxid_to_mac


def ignore_exception(processname, e):
    msg = '[Exception] Ignored error in %s: %s' % (processname, e)
    _, _, tb = sys.exc_info()
    if tb:
        msg += '\n%s' % traceback.format_exc()

    logutil.error(None, msg)


def _lock_refresher(locks):
    while True:
        db.refresh_locks(locks)
        time.sleep(10)


def execute(locks, command, check_exit_code=[0], env_variables=None):
    if not locks:
        return processutils.execute(
            command, check_exit_code=check_exit_code,
            env_variables=env_variables, shell=True)

    else:
        p = multiprocessing.Process(
            target=_lock_refresher, args=(locks,))
        p.start()

        try:
            return processutils.execute(
                command, check_exit_code=check_exit_code,
                env_variables=env_variables, shell=True)
        finally:
            p.terminate()
            p.join()
