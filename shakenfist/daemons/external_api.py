import os

from shakenfist import config
from shakenfist.daemons import daemon
from shakenfist import logutil
from shakenfist import util


class Monitor(daemon.Daemon):
    def run(self):
        logutil.info(None, 'Starting')
        util.execute(None,
                     (config.parsed.get('API_COMMAND_LINE')
                      % {
                         'port': config.parsed.get('API_PORT'),
                          'timeout': config.parsed.get('API_TIMEOUT'),
                          'name': daemon.process_name('api')
                     }),
                     env_variables=os.environ)
