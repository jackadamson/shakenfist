import json
import os
import psutil
import time

from etcd3gw.client import Etcd3Client
from etcd3gw.lock import Lock

from shakenfist import config
from shakenfist import db
from shakenfist import exceptions
from shakenfist import logutil
from shakenfist import util

####################################################################
# Please do not call this file directly, but instead call it via   #
# the db.py abstraction.                                           #
####################################################################


class ActualLock(Lock):
    def __init__(self, objecttype, subtype, name, ttl=120, client=None, relatedobjects=None, timeout=None):
        self.path = _construct_key(objecttype, subtype, name)
        super(ActualLock, self).__init__(self.path, ttl=ttl, client=client)

        self.objecttype = objecttype
        self.objectname = name
        self.relatedobjects = relatedobjects
        self.timeout = min(timeout, 1000000000)

        # We override the UUID of the lock with something more helpful to debugging
        self._uuid = json.dumps(
            {
                'node': config.parsed.get('NODE_NAME'),
                'pid': os.getpid()
            },
            indent=4, sort_keys=True)

        # We also override the location of the lock so that we're in our own spot
        self.key = '/sflocks%s' % self.path

    def get_holder(self):
        value = Etcd3Client().get(self.key, metadata=True)
        if value is None or len(value) == 0:
            return None, NotImplementedError

        if not value[0][0]:
            return None, None

        d = json.loads(value[0][0])
        return d['node'], d['pid']

    def __enter__(self):
        start_time = time.time()
        slow_warned = False
        threshold = int(config.parsed.get('SLOW_LOCK_THRESHOLD'))

        try:
            while time.time() - start_time < self.timeout:
                res = self.acquire()
                if res:
                    return self

                duration = time.time() - start_time
                if (duration > threshold and not slow_warned):
                    db.add_event(self.objecttype, self.objectname,
                                 'lock', 'acquire', None,
                                 'Waiting for lock more than threshold')

                    node, pid = self.get_holder()
                    logutil.info(self.relatedobjects,
                                 'Waiting for lock on %s: %.02f seconds, threshold '
                                 '%d seconds. Holder is pid %s on %s.'
                                 % (self.path, duration, threshold, pid, node))
                    slow_warned = True

                time.sleep(1)

            duration = time.time() - start_time
            db.add_event(self.objecttype, self.objectname,
                         'lock', 'failed', None,
                         'Failed to acquire lock after %.02f seconds' % duration)

            node, pid = self.get_holder()
            logutil.info(self.relatedobjects,
                         'Failed to acquire lock %s after %.02f seconds. Holder is pid %s on %s.'
                         % (self.path, duration, pid, node))
            raise exceptions.LockException(
                'Cannot acquire lock %s, timed out after %.02f seconds'
                % (self.name, duration))

        finally:
            duration = time.time() - start_time
            if duration > threshold:
                db.add_event(self.objecttype, self.objectname,
                             'lock', 'acquired', None,
                             'Waited %d seconds for lock' % duration)
                logutil.info(self.relatedobjects,
                             'Acquiring a lock on %s was slow: %.02f seconds'
                             % (self.path, duration))

    def __exit__(self, _exception_type, _exception_value, _traceback):
        if not self.release():
            raise exceptions.LockException(
                'Cannot release lock: %s' % self.name)
        return self


def get_lock(objecttype, subtype, name, ttl=60, timeout=10, relatedobjects=None):
    """Retrieves an etcd lock object. It is not locked, to lock use acquire().

    The returned lock can be used as a context manager, with the lock being
    acquired on entry and released on exit. Note that the lock acquire process
    will have no timeout.
    """
    return ActualLock(objecttype, subtype, name, ttl=ttl, client=Etcd3Client(),
                      relatedobjects=relatedobjects, timeout=timeout)


def refresh_lock(lock, relatedobjects=None):
    if not lock.is_acquired():
        raise exceptions.LockException(
            'The lock on %s has expired.' % lock.path)

    lock.refresh()
    logutil.info(relatedobjects, 'Refreshed lock %s' % lock.name)


def clear_stale_locks():
    # Remove all locks held by former processes on this node. This is required
    # after an unclean restart, otherwise we need to wait for these locks to
    # timeout and that can take a long time.
    client = Etcd3Client()

    for data, metadata in client.get_prefix('/sflocks/', sort_order='ascend', sort_target='key'):
        lockname = str(metadata['key']).replace('/sflocks/', '')
        holder = json.loads(data)
        node = holder['node']
        pid = int(holder['pid'])

        if node == config.parsed.get('NODE_NAME') and not psutil.pid_exists(pid):
            client.delete(metadata['key'])
            logutil.warning(None, 'Removed stale lock for %s, previously held by pid %s on %s'
                            % (lockname, pid, node))


def _construct_key(objecttype, subtype, name):
    if subtype and name:
        return '/sf/%s/%s/%s' % (objecttype, subtype, name)
    if name:
        return '/sf/%s/%s' % (objecttype, name)
    if subtype:
        return '/sf/%s/%s/' % (objecttype, subtype)
    return '/sf/%s/' % objecttype


def put(objecttype, subtype, name, data, ttl=None):
    path = _construct_key(objecttype, subtype, name)
    encoded = json.dumps(data, indent=4, sort_keys=True)
    Etcd3Client().put(path, encoded, lease=None)


def get(objecttype, subtype, name):
    path = _construct_key(objecttype, subtype, name)
    value = Etcd3Client().get(path, metadata=True)
    if value is None or len(value) == 0:
        return None
    return json.loads(value[0][0])


def get_all(objecttype, subtype, sort_order=None):
    path = _construct_key(objecttype, subtype, None)
    for value in Etcd3Client().get_prefix(path, sort_order=sort_order):
        yield json.loads(value[0])


def delete(objecttype, subtype, name):
    path = _construct_key(objecttype, subtype, name)
    Etcd3Client().delete(path)


def delete_all(objecttype, subtype, sort_order=None):
    path = _construct_key(objecttype, subtype, None)
    Etcd3Client().delete_prefix(path)


def enqueue(queuename, workitem):
    with get_lock('queue', None, queuename):
        i = 0
        entry_time = time.time()
        jobname = '%s-%03d' % (entry_time, i)

        while get('queue', queuename, jobname):
            i += 1
            jobname = '%s-%03d' % (entry_time, i)

        put('queue', queuename, jobname, workitem)
        logutil.info(None, 'Enqueued workitem %s for queue %s with work %s'
                     % (jobname, queuename, workitem))


def dequeue(queuename):
    queue_path = _construct_key('queue', queuename, None)
    client = Etcd3Client()

    with get_lock('queue', None, queuename):
        for data, metadata in client.get_prefix(queue_path, sort_order='ascend', sort_target='key'):
            jobname = str(metadata['key']).split('/')[-1].rstrip("'")
            workitem = json.loads(data)

            put('processing', queuename, jobname, workitem)
            client.delete(metadata['key'])
            logutil.info(None, 'Moved workitem %s from queue to processing for %s with work %s'
                         % (jobname, queuename, workitem))

            return jobname, workitem

    return None, None


def resolve(queuename, jobname):
    with get_lock('queue', None, queuename):
        delete('processing', queuename, jobname)
        logutil.info(None, 'Resolved workitem %s for queue %s'
                     % (jobname, queuename))


def get_queue_length(queuename):
    with get_lock('queue', None, queuename):
        queued = len(list(get_all('queue', queuename)))
        processing = len(list(get_all('processing', queuename)))
        return processing, queued


def _restart_queue(queuename):
    queue_path = _construct_key('processing', queuename, None)
    with get_lock('queue', None, queuename):
        for data, metadata in Etcd3Client().get_prefix(queue_path, sort_order='ascend'):
            jobname = str(metadata.key).split('/')[-1].rstrip("'")
            workitem = json.loads(data)
            put('queue', queuename, jobname, workitem)
            delete('processing', queuename, jobname)
            logutil.warning(None, 'Reset %s workitem %s' %
                            (queuename, jobname))


def restart_queues():
    # Move things which were in processing back to the queue because
    # we didn't complete them before crashing.
    if util.is_network_node():
        _restart_queue('networknode')
    _restart_queue(config.parsed.get('NODE_NAME'))
