import multiprocessing
import os
import re
import setproctitle
import signal
import time

from shakenfist import config
from shakenfist.daemons import daemon
from shakenfist import db
from shakenfist import logutil
from shakenfist import virt


def observe(path, instance_uuid):
    setproctitle.setproctitle(
        '%s-%s' % (daemon.process_name('triggers'), instance_uuid))
    regexps = {
        'login prompt': ['^.* login: .*', re.compile('.* login: .*')]
    }

    while not os.path.exists(path):
        time.sleep(1)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    logutil.info([virt.ThinInstance(instance_uuid)],
                 'Monitoring %s for triggers' % path)
    db.add_event('instance', instance_uuid, 'trigger monitor',
                 'detected console log', None, None)
    os.lseek(fd, 0, os.SEEK_END)

    buffer = ''
    while True:
        d = os.read(fd, 1024).decode('utf-8')
        if d:
            buffer += d
            lines = buffer.split('\n')
            buffer = lines[-1]

            for line in lines:
                if line:
                    for trigger in regexps:
                        m = regexps[trigger][1].match(line)
                        if m:
                            logutil.info([virt.ThinInstance(instance_uuid)],
                                         'Trigger %s matched' % trigger)
                            db.add_event('instance', instance_uuid, 'trigger',
                                         None, None, trigger)

        time.sleep(1)


class Monitor(daemon.Daemon):
    def run(self):
        logutil.info(None, 'Starting')
        observers = {}

        while True:
            # Cleanup terminated observers
            all_observers = list(observers.keys())
            for instance_uuid in all_observers:
                if not observers[instance_uuid].is_alive():
                    # Reap process
                    observers[instance_uuid].join(1)
                    logutil.info([virt.ThinInstance(instance_uuid)],
                                 'Trigger observer has terminated')
                    db.add_event(
                        'instance', instance_uuid, 'trigger monitor', 'crashed', None, None)
                    del observers[instance_uuid]

            # Start missing observers
            extra_instances = list(observers.keys())

            for inst in db.get_instances(only_node=config.parsed.get('NODE_NAME')):
                if inst['uuid'] in extra_instances:
                    extra_instances.remove(inst['uuid'])

                if inst['state'] != 'created':
                    continue

                if inst['uuid'] not in observers:
                    console_path = os.path.join(
                        config.parsed.get('STORAGE_PATH'), 'instances', inst['uuid'], 'console.log')
                    p = multiprocessing.Process(
                        target=observe, args=(console_path, inst['uuid']),
                        name='%s-%s' % (daemon.process_name('triggers'),
                                        inst['uuid']))
                    p.start()

                    observers[inst['uuid']] = p
                    logutil.info([virt.ThinInstance(inst['uuid'])],
                                 'Started trigger observer')
                    db.add_event(
                        'instance', inst['uuid'], 'trigger monitor', 'started', None, None)

            # Cleanup extra observers
            for instance_uuid in extra_instances:
                p = observers[instance_uuid]
                try:
                    os.kill(p.pid, signal.SIGKILL)
                except Exception:
                    pass

                del observers[instance_uuid]
                logutil.info([virt.ThinInstance(instance_uuid)],
                             'Finished trigger observer')
                db.add_event(
                    'instance', instance_uuid, 'trigger monitor', 'finished', None, None)

            time.sleep(1)
